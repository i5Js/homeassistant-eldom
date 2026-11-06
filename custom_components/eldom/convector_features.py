"""Decode explicit configuration values; never infer heater activity."""

import re

FEATURE_REQUESTS = (
    "GetWiFiStatus", "GetParentControl", "GetOpenWindowMode", "GetAntifreezeMode",
    "GetSleepMode", "GetBrightness", "GetLanguages", "GetRates", "GetTimers",
)
WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


def integer(value, minimum=0, maximum=None):
    """Strict integer protocol fields; bools and malformed values stay unknown."""
    if isinstance(value, bool) or len(str(value)) > 12 or not re.fullmatch(r"-?\d+", str(value), re.ASCII):
        return None
    parsed = int(value)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        return None
    return parsed


def enabled(value):
    return {"0": False, "1": True}.get(value) if isinstance(value, str) else None


def clock(value):
    return value if isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value, re.ASCII) else None


def version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,31}", value, re.ASCII) else None


def window_mode(value):
    return {"0": "off", "1": "on", "2": "smart"}.get(value) if isinstance(value, str) else None


def parse_features(reads):
    """Expose only reviewed fields, never raw IDs, CRCs, credentials or replies."""
    def get(request):
        value = reads.get(request, {})
        return value if isinstance(value, dict) else {}
    wifi, brightness = get("GetWiFiStatus"), get("GetBrightness")
    languages, rates, params = get("GetLanguages"), get("GetRates"), get("GetParams")
    result = {
        "child_lock": enabled(get("GetParentControl").get("Enable")),
        "antifreeze": enabled(get("GetAntifreezeMode").get("Enable")),
        "sleep": enabled(get("GetSleepMode").get("Enable")),
        "power_regulation": enabled(get("GetPowerMode").get("Mode")),
        "open_window_mode": window_mode(get("GetOpenWindowMode").get("Mode")),
        "adaptive_display": enabled(brightness.get("Adaptive")),
        "display_brightness": integer(brightness.get("Level"), minimum=-1, maximum=100),
        "wifi_enabled": enabled(wifi.get("WiFiEnable")),
        "ap_enabled": enabled(wifi.get("APEnable")),
        "firmware_version": version(wifi.get("SWVersion")),
        "hardware_version": version(wifi.get("HWVersion")),
        "automatic_clock": enabled(params.get("AutoTimeSet")),
        "day_tariff_start": clock(rates.get("Rate1")),
        "night_tariff_start": clock(rates.get("Rate2")),
    }
    supported = {}
    for key, value in languages.items():
        if re.fullmatch(r"LngD\d+", key) and isinstance(value, str) and re.fullmatch(r"[A-Z]{2,3}", value):
            index = integer(languages.get("LngI" + key[4:], key[4:]), minimum=1, maximum=64)
            if index is not None:
                supported[index] = value
    result["language"] = supported.get(integer(languages.get("Active"), minimum=1, maximum=64))
    result["supported_languages"] = list(dict.fromkeys(supported.values()))
    settings = params.get("SystemSettings")
    scheduler = settings.split(",")[0].strip() if isinstance(settings, str) else None
    result["scheduler_type"] = {"0": "timer", "1": "weekly_program"}.get(scheduler)
    timers = get("GetTimers")
    slots = []
    for index in range(1, 8):
        mask = integer(timers.get(f"Days{index}"), maximum=127)
        target = integer(timers.get(f"TSet{index}"), minimum=1, maximum=1000)
        slots.append({
            "slot": index,
            "enabled": enabled(timers.get(f"OnOff{index}")),
            "days_mask": mask,
            "days": [day for bit, day in enumerate(WEEKDAYS) if mask & (1 << bit)] if mask is not None else None,
            "start": clock(timers.get(f"ON{index}")),
            "end": clock(timers.get(f"OFF{index}")),
            "temperature": target / 10 if target is not None else None,
        })
    known_flags = all(row["enabled"] is not None for row in slots)
    complete = known_flags and all(
        not row["enabled"] or all(v is not None for v in row.values()) for row in slots
    )
    result["weekly_schedule"] = slots
    result["schedule_enabled_intervals"] = sum(row["enabled"] for row in slots) if known_flags else None
    result["schedule_complete"] = complete
    return result
