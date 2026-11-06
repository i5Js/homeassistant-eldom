"""Reviewed, read-only requests shared by convector telemetry."""

from ioteldom.constants import BASE_URL
from ioteldom.crc import crc32

READ_REQUESTS = frozenset({
    "GetStatus", "GetParams", "GetWiFiStatus", "GetTimers", "GetRates",
    "GetPowerMode", "GetParentControl", "GetOpenWindowMode", "GetAntifreezeMode",
    "GetSleepMode", "GetBrightness", "GetLanguages", "GetGraph",
    "GetEnergyStatistic", "GetTemperatureStatistic",
})


class UnsupportedRead(ValueError):
    """Firmware did not return the requested read response."""


async def async_read(client, device, request):
    """Never accept controls, discovery, pairing or arbitrary parameters."""
    if request not in READ_REQUESTS:
        raise ValueError("Only reviewed read requests are allowed")
    payload = {"ID": device.pairTok, "Req": request, "CID": "1"}
    payload["CRC"] = crc32(payload)
    headers = {
        "ionic-idd": device.uuid,
        "Authorization": "Bearer " + await client.token_provider.provide(),
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    async with client.session.post(BASE_URL + "/api/direct-req", json=payload, headers=headers) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)
    if not isinstance(data, dict) or data.get("Res") != request:
        raise UnsupportedRead("Invalid Eldom read response")
    return data
