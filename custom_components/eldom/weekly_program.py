"""Verified weekly-program transactions for IoT convectors."""

from decimal import Decimal, InvalidOperation

from .convector_control import ControlNotAllowed, ControlNotConfirmed, _async_write_once
from .convector_features import WEEKDAYS, clock, integer
from .iot_requests import async_read

FIELDS = tuple(f"{key}{slot}" for slot in range(1, 8) for key in ("TSet", "ON", "OFF", "Days", "OnOff"))
PATCH_KEYS = {"enabled", "weekdays", "start", "end", "temperature"}
OPERATIONS = {"read", "edit", "auto", "heat", "off", "resume"}


def slot_number(value):
    # HA's number selector validates its values as floats, even with step=1.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or str(value) not in {str(i) for i in range(1, 8)}:
        raise ValueError("Interval must be an integer from 1 to 7")
    return int(value)


def minute_time(value):
    if isinstance(value, str) and len(value) == 8 and value.endswith(":00"):
        value = value[:5]
    if clock(value) is None:
        raise ValueError("Use a time with whole minutes, HH:MM")
    return value


def validate_changes(slot, changes):
    slot = slot_number(slot)
    if not isinstance(changes, dict) or not changes or set(changes) - PATCH_KEYS:
        raise ValueError("Provide at least one supported interval field")
    result = dict(changes)
    if "enabled" in result and type(result["enabled"]) is not bool:
        raise ValueError("Enabled must be a boolean")
    if "weekdays" in result:
        days = result["weekdays"]
        if not isinstance(days, list) or any(not isinstance(day, str) or day not in WEEKDAYS for day in days) or len(set(days)) != len(days):
            raise ValueError("Use distinct weekday names")
    for key in ("start", "end"):
        if key in result:
            result[key] = minute_time(result[key])
    if "temperature" in result:
        value = result["temperature"]
        try:
            number = Decimal(str(value))
            valid = not isinstance(value, bool) and number.is_finite() and 5 <= number <= 35 and number * 2 == (number * 2).to_integral_value()
        except (InvalidOperation, ValueError):
            valid = False
        if not valid:
            raise ValueError("Temperature must be 5–35 °C in half-degree steps")
        result["temperature"] = number
    return slot, result


def normalize_timers(data):
    if not isinstance(data, dict) or any(key not in data for key in FIELDS):
        raise ControlNotConfirmed("Eldom returned an incomplete weekly programme")
    result = {key: data[key] for key in FIELDS}
    if any(not isinstance(value, str) for value in result.values()):
        raise ControlNotConfirmed("Eldom returned non-string programme fields")
    for slot in range(1, 8):
        i = str(slot)
        if (result["OnOff" + i] not in ("0", "1")
                or integer(result["Days" + i], maximum=127) is None
                or integer(result["TSet" + i], minimum=50, maximum=350) is None
                or clock(result["ON" + i]) is None or clock(result["OFF" + i]) is None):
            raise ControlNotConfirmed("Eldom returned invalid weekly programme fields")
        if result["OnOff" + i] == "1" and (int(result["Days" + i]) == 0 or result["ON" + i] >= result["OFF" + i]):
            raise ControlNotAllowed("Enabled intervals need weekdays and a start before their end")
    return result


def enabled_slots(timers):
    return [i for i in range(1, 8) if timers["OnOff" + str(i)] == "1"]


def same_content(first, second):
    return all(first[key] == second[key] for key in FIELDS if not key.startswith("OnOff"))


def remembered_profile(profile, current):
    try:
        profile = normalize_timers(profile)
    except (ValueError, TypeError):
        return None
    return profile if same_content(profile, current) else None


def timers_payload(timers):
    result = dict(timers)
    for i in range(1, 8):
        result["TSet" + str(i)] = format(Decimal(timers["TSet" + str(i)]) / 10, "f")
    return result


def interval_list(timers):
    return [{"slot": i, "enabled": timers[f"OnOff{i}"] == "1",
             "weekdays": [day for bit, day in enumerate(WEEKDAYS) if int(timers[f"Days{i}"]) & (1 << bit)],
             "start": timers[f"ON{i}"], "end": timers[f"OFF{i}"],
             "temperature": int(timers[f"TSet{i}"]) / 10} for i in range(1, 8)]


def base_mode(status):
    operation = integer(status.get("Operation"), maximum=255)
    if operation is None or operation // 16 not in (0, 1, 2):
        raise ControlNotAllowed("This operating mode does not support weekly-program control")
    return operation // 16


def validate_request(operation, slot=None, changes=None, selected_slots=None):
    if operation not in OPERATIONS:
        raise ValueError("Unsupported weekly-program operation")
    if operation == "edit":
        slot, changes = validate_changes(slot, changes)
    if selected_slots is not None:
        if operation != "auto" or not isinstance(selected_slots, list) or not selected_slots:
            raise ValueError("Choose at least one interval to activate")
        selected_slots = [slot_number(i) for i in selected_slots]
        if len(set(selected_slots)) != len(selected_slots):
            raise ValueError("Intervals must be distinct")
    return slot, changes, selected_slots


async def async_weekly_transaction(client, device, operation, *, profile=None,
                                   remember=None, slot=None, changes=None, selected_slots=None):
    """Own lock/timeout externally; every write is sent once and read back."""
    slot, changes, selected_slots = validate_request(operation, slot, changes, selected_slots)
    if operation in ("off", "resume"):
        status = await async_read(client, device, "GetStatus")
        code = integer(status.get("Operation"), maximum=255)
        if code is None:
            raise ControlNotConfirmed("Eldom returned an invalid operating mode")
        needs_write = code // 16 != 0 if operation == "off" else code // 16 == 0
        if needs_write:
            await _async_write_once(client, device, "Off" if operation == "off" else "On", {})
            status = await async_read(client, device, "GetStatus")
        code = integer(status.get("Operation"), maximum=255)
        if code is None or (code // 16 == 0) != (operation == "off"):
            raise ControlNotConfirmed("Eldom did not confirm the power state")
        return {"timers": None, "status": status, "params": None}
    params = await async_read(client, device, "GetParams")
    timers = normalize_timers(await async_read(client, device, "GetTimers"))
    status = await async_read(client, device, "GetStatus")
    original_mode = base_mode(status)
    settings = params.get("SystemSettings")
    if not isinstance(settings, str) or settings.split(",")[0].strip() != "1":
        raise ControlNotAllowed("The device is configured as a timer rather than a weekly programme")
    wanted = dict(timers)
    saved = remembered_profile(profile, timers)
    if operation in ("auto", "edit", "heat") and original_mode == 1 and int(status["Operation"]) % 16:
        raise ControlNotAllowed("Disable sleep or other special modes before changing the programme")
    if operation in ("auto", "edit", "heat"):
        sleep = await async_read(client, device, "GetSleepMode")
        if sleep.get("Enable") != "0":
            raise ControlNotAllowed("Disable sleep before changing the programme")

    def save(value):
        if remember is not None:
            remember(dict(value))

    if operation == "auto":
        if selected_slots is not None:
            for i in range(1, 8):
                wanted[f"OnOff{i}"] = "1" if i in selected_slots else "0"
        elif not enabled_slots(timers) and saved is not None:
            for i in range(1, 8):
                wanted[f"OnOff{i}"] = saved[f"OnOff{i}"]
        if not enabled_slots(wanted):
            raise ControlNotAllowed("Enable an interval or choose intervals in Activate weekly programme first")
        normalize_timers(wanted)
        save(wanted)
    elif operation == "heat":
        if enabled_slots(timers):
            save(timers)  # Persist before removing flags, including an uncertain write.
        for i in range(1, 8):
            wanted[f"OnOff{i}"] = "0"
    elif operation == "edit":
        if changes.get("enabled") is True and not enabled_slots(timers) and saved is not None:
            for i in range(1, 8):
                wanted[f"OnOff{i}"] = saved[f"OnOff{i}"]
        suffix = str(slot)
        if "enabled" in changes:
            wanted["OnOff" + suffix] = "1" if changes["enabled"] else "0"
        if "weekdays" in changes:
            wanted["Days" + suffix] = str(sum(1 << WEEKDAYS.index(day) for day in changes["weekdays"]))
        for key, wire in (("start", "ON"), ("end", "OFF")):
            if key in changes:
                wanted[wire + suffix] = changes[key]
        if "temperature" in changes:
            wanted["TSet" + suffix] = str(int(changes["temperature"] * 10))
        normalize_timers(wanted)
        if not enabled_slots(wanted) and saved is not None:
            saved = {**wanted, **{f"OnOff{i}": saved[f"OnOff{i}"] for i in range(1, 8)}}
            if "enabled" in changes:
                saved["OnOff" + suffix] = wanted["OnOff" + suffix]
            normalize_timers(saved)
            save(saved)
        else:
            save(wanted)
    elif operation in ("off", "resume") and enabled_slots(timers):
        save(timers)

    if wanted != timers or (operation == "auto" and original_mode == 1):
        await _async_write_once(client, device, "SetTimers", timers_payload(wanted))
        actual = normalize_timers(await async_read(client, device, "GetTimers"))
        if actual != wanted:
            raise ControlNotConfirmed("Eldom did not confirm all seven intervals")
        status = await async_read(client, device, "GetStatus")
        expected = 0 if original_mode == 0 else 2 if enabled_slots(wanted) else 1
        if base_mode(status) != expected:
            raise ControlNotConfirmed("Eldom did not confirm the programme mode")

    if operation == "off" and base_mode(status) != 0:
        await _async_write_once(client, device, "Off", {})
        status = await async_read(client, device, "GetStatus")
    elif operation in ("auto", "heat", "resume") and base_mode(status) == 0:
        await _async_write_once(client, device, "On", {})
        status = await async_read(client, device, "GetStatus")
    expected = {"auto": 2, "heat": 1, "off": 0}.get(operation)
    if expected is not None and base_mode(status) != expected:
        raise ControlNotConfirmed("Eldom did not confirm the requested operating mode")
    if operation == "resume" and base_mode(status) == 0:
        raise ControlNotConfirmed("Eldom did not confirm power on")
    after = await async_read(client, device, "GetParams")
    for key, value in params.items():
        if key not in {"ID", "CID", "CRC", "Res", "TSet"} and after.get(key) != value:
            raise ControlNotConfirmed("Eldom changed parameters outside the weekly programme")
    return {"timers": wanted, "status": status, "params": after}
