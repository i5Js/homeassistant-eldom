"""Closed configuration writes confirmed by independent device reads."""

import re

from ioteldom.constants import BASE_URL
from ioteldom.crc import crc32

from .convector_features import clock, enabled, integer, window_mode
from .iot_requests import async_read

# key: read request, write request, read field, write field
CONTROL_FIELDS = {
    "child_lock": ("GetParentControl", "SetParentControl", "Enable", "Enable"),
    "antifreeze": ("GetAntifreezeMode", "SetAntifreezeMode", "Enable", "Enable"),
    "power_regulation": ("GetPowerMode", "SetPowerMode", "Mode", "Regulation"),
    "adaptive_display": ("GetBrightness", "SetBrightness", "Adaptive", "Adaptive"),
    "display_brightness": ("GetBrightness", "SetBrightness", "Level", "Level"),
    "open_window_mode": ("GetOpenWindowMode", "SetOpenWindowMode", "Mode", "Mode"),
    "sleep": ("GetSleepMode", "SetSleepMode", "Enable", "Enable"),
}
WINDOW_VALUES = {"off": "0", "on": "1", "smart": "2"}


class ControlNotConfirmed(ValueError):
    """The acknowledgement or independent reading did not confirm the write."""


class ControlNotAllowed(ValueError):
    """A known device mode prevents this configuration change."""


def validate_value(key, value):
    if key not in CONTROL_FIELDS:
        raise ValueError("Unsupported Eldom control")
    if key == "open_window_mode":
        if not isinstance(value, str) or value not in WINDOW_VALUES:
            raise ValueError("Invalid window detection mode")
        return WINDOW_VALUES[value]
    if key == "display_brightness":
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError("Brightness must be an integer from 1 to 10")
        return str(value)
    if type(value) is not bool:
        raise ValueError("This Eldom control requires a boolean")
    return "1" if value else "0"


def observed_values(key, response):
    if key in ("adaptive_display", "display_brightness"):
        return {"adaptive_display": enabled(response.get("Adaptive")),
                "display_brightness": integer(response.get("Level"), minimum=1, maximum=10)}
    field = CONTROL_FIELDS[key][2]
    value = window_mode(response.get(field)) if key == "open_window_mode" else enabled(response.get(field))
    return {key: value}


async def async_set_control(client, device, key, value):
    """Read, write once, verify; the caller owns the shared operation lock."""
    encoded = validate_value(key, value)
    read, write, _, write_field = CONTROL_FIELDS[key]
    before = await async_read(client, device, read)
    current = observed_values(key, before)
    if any(v is None for v in current.values()):
        raise ControlNotConfirmed("Eldom returned unknown configuration")
    if current[key] == value:
        return current
    if key == "sleep":
        status = await async_read(client, device, "GetStatus")
        operation = integer(status.get("Operation"), maximum=255)
        if operation is None or operation // 16 != 1:
            raise ControlNotAllowed("Switch the radiator to Manual before changing sleep mode")
    fields = {write_field: encoded}
    if write == "SetBrightness":
        fields = {"Level": before["Level"], "Adaptive": before["Adaptive"], **fields}
    await _async_write_once(client, device, write, fields)
    confirmed = observed_values(key, await async_read(client, device, read))
    if confirmed != {**current, key: value}:
        raise ControlNotConfirmed("Eldom did not confirm the requested configuration")
    return confirmed


async def _async_write_once(client, device, write, fields):
    payload = {"ID": device.pairTok, "Req": write, **fields, "CID": "1"}
    payload["CRC"] = crc32(payload)
    headers = {"ionic-idd": device.uuid,
               "Authorization": "Bearer " + await client.token_provider.provide(),
               "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    async with client.session.post(BASE_URL + "/api/direct-req", json=payload, headers=headers) as response:
        response.raise_for_status()
        ack = await response.json(content_type=None)
    if not isinstance(ack, dict) or ack.get("Res") != write or ack.get("Code") != "0" or ack.get("Type") != "OK":
        raise ControlNotConfirmed("Eldom rejected or did not confirm the control")


async def async_set_temperature_preserving_params(client, device, temperature):
    """SetParams writes degrees but reads tenths; preserve all other settings."""
    if type(temperature) is not int or not 5 <= temperature <= 35:
        raise ValueError("Temperature must be an integer from 5 to 35")
    before = await async_read(client, device, "GetParams")
    settings = before.get("SystemSettings")
    if (enabled(before.get("AutoTimeSet")) is None or enabled(before.get("Lock")) is None
            or clock(before.get("Rate1")) is None or clock(before.get("Rate2")) is None
            or not isinstance(settings, str) or len(settings) > 40
            or not re.fullmatch(r"\d+(?:\s*,\s*\d+){3}", settings, re.ASCII)
            or integer(before.get("TSet"), minimum=50, maximum=350) is None):
        raise ControlNotConfirmed("Eldom returned incomplete temperature parameters")
    expected = str(temperature * 10)
    if before["TSet"] == expected:
        return expected
    fields = {"TSet": str(temperature), **{k: before[k] for k in
              ("AutoTimeSet", "Rate1", "Rate2", "SystemSettings", "Lock")}}
    await _async_write_once(client, device, "SetParams", fields)
    after = await async_read(client, device, "GetParams")
    metadata = {"ID", "CID", "CRC", "Res"}
    actual = {k: v for k, v in after.items() if k not in metadata}
    wanted = {k: v for k, v in before.items() if k not in metadata}
    wanted["TSet"] = expected
    if actual != wanted:
        raise ControlNotConfirmed("Eldom did not preserve the temperature parameters")
    return expected
