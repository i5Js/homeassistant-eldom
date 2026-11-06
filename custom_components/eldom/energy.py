"""Read-only hourly telemetry used by the official IoT convector app.

The vendor returns 24 bins per calendar date, not UTC instants. Do not invent
25/23-hour DST samples or turn the estimated kWh into instantaneous watts.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
import math
from zoneinfo import ZoneInfo

import aiohttp
from ioteldom.client import Client
from ioteldom.constants import BASE_URL
from ioteldom.crc import crc32

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)
READ_REQUESTS = frozenset({"GetStatus", "GetParams", "GetPowerMode", "GetEnergyStatistic", "GetTemperatureStatistic"})
# Match PowerChartPage.calculatePower, including the app's truncated factors.
ENERGY_FACTORS = tuple(Decimal(v) / 10 for v in (".3333", ".5", ".6666", ".8333", "1"))
REFERENCE_WATTS = (1000, 1500, 2000, 2500, 3000)


def number(value, *, nonnegative=False):
    """Reject empty, Boolean and nonfinite values rather than inventing zero."""
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (not nonnegative or result >= 0) else None


def power_class(settings):
    """The second SystemSettings component selects the app's energy scale."""
    if not isinstance(settings, str):
        return None
    parts = settings.split(",")
    if len(parts) < 2 or parts[1].strip() not in {"0", "1", "2", "3", "4"}:
        return None
    return int(parts[1])


def estimated_kwh(value, scale):
    if type(scale) is not int or scale not in range(5) or number(value, nonnegative=True) is None:
        return None
    try:
        result = Decimal(str(value)) * ENERGY_FACTORS[scale]
        return float(result) if result.is_finite() else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def vendor_date(value):
    """Date is encoded at UTC midnight, but represents a vendor calendar label."""
    timestamp = number(value, nonnegative=True)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        date = datetime.fromtimestamp(timestamp, timezone.utc)
        if date.time().isoformat() != "00:00:00" or date.year < 2000:
            return None
        return date.date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_history(payloads, kind, scale, now):
    """Index numerically, deduplicate dates, preserve gaps and hide future bins."""
    result = {}
    prefix = "P" if kind == "energy" else "T"
    expected = "GetEnergyStatistic" if kind == "energy" else "GetTemperatureStatistic"
    today = now.date().isoformat()
    oldest = (now.date() - timedelta(days=6)).isoformat()
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("Res") != expected:
            continue
        date = vendor_date(payload.get("Date"))
        if date is None or not oldest <= date <= today:
            continue
        hours = []
        for hour in range(24):
            value = payload.get(f"{prefix}{hour}")
            value = estimated_kwh(value, scale) if kind == "energy" else number(value)
            hours.append(None if date == today and hour >= now.hour else value)
        record = {"date": date, "hours": hours}
        if kind == "energy":
            record["total"] = estimated_kwh(payload.get("TotalPower"), scale)
        result[date] = record
    return sorted(result.values(), key=lambda item: item["date"])


class EldomEnergyCoordinator(DataUpdateCoordinator):
    """Independent slow telemetry; a failed graph never breaks climate polling."""

    def __init__(self, hass, entry, devices):
        super().__init__(hass, _LOGGER, config_entry=entry, name="eldom_energy", update_interval=timedelta(minutes=15))
        self.devices = devices
        self._username = entry.data["username"]
        self._password = entry.data["password"]
        self._zone = ZoneInfo(hass.config.time_zone)
        self._task = None
        self._running_task = None
        self.data = {}

    def start(self, hass, entry):
        self._task = entry.async_create_background_task(hass, self.async_refresh(), "eldom hourly telemetry")

    async def async_shutdown(self):
        await super().async_shutdown()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._running_task and self._running_task is not asyncio.current_task():
            self._running_task.cancel()
            await asyncio.gather(self._running_task, return_exceptions=True)

    async def _read(self, client, device, request):
        if request not in READ_REQUESTS:
            raise ValueError("Only read-only Eldom requests are allowed")
        payload = {"ID": device.pairTok, "Req": request, "CID": "1"}
        payload["CRC"] = crc32(payload)
        headers = {"ionic-idd": device.uuid, "Authorization": "Bearer " + await client.token_provider.provide(), "User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        async with client.session.post(BASE_URL + "/api/direct-req", json=payload, headers=headers) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if not isinstance(data, dict) or data.get("Res") != request:
            raise ValueError("Invalid Eldom telemetry response")
        return data

    async def _device_snapshot(self, client, device):
        # The app requests seven records concurrently. Serial reads are reset by
        # normal GetStatus polling and can repeat dates instead of reaching all 7.
        status = await self._read(client, device, "GetStatus")
        params = await self._read(client, device, "GetParams")
        try:
            regulation = await self._read(client, device, "GetPowerMode")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            regulation = {}
        scale = power_class(params.get("SystemSettings"))
        energy = await asyncio.gather(*(self._read(client, device, "GetEnergyStatistic") for _ in range(7)), return_exceptions=True)
        temperatures = await asyncio.gather(*(self._read(client, device, "GetTemperatureStatistic") for _ in range(7)), return_exceptions=True)
        now = datetime.now(self._zone)
        energy = parse_history(energy, "energy", scale, now)
        temperatures = parse_history(temperatures, "temperature", scale, now)
        if not energy and not temperatures:
            raise ValueError("No valid hourly telemetry")
        return {"energy": energy, "temperature": temperatures, "power_class": scale,
                "reference_power_w": REFERENCE_WATTS[scale] if scale is not None else None,
                "power_regulation": {"0": False, "1": True}.get(regulation.get("Mode")),
                "vendor_timezone": status.get("TimeZone"), "retrieved_at": now.isoformat(),
                "source": "Eldom (estimated)", "today": now.date().isoformat()}

    async def _async_update_data(self):
        self._running_task = asyncio.current_task()
        try:
            async with asyncio.timeout(90):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, connect=5),
                                                  connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), limit_per_host=7)) as session:
                    client = Client(session, self._username, self._password)
                    # Authenticate once before concurrent calls, avoiding multiple logins.
                    await client.token_provider.provide()
                    result = {}
                    for device in self.devices:
                        result[device.uuid] = await self._device_snapshot(client, device)
                    return result
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            # No response body, tokens, pairing IDs or account details in logs.
            raise UpdateFailed(f"Eldom hourly telemetry failed ({type(error).__name__})") from None
        finally:
            self._running_task = None
