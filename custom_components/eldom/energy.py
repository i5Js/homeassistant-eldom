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
from ioteldom.client import Client, InvalidCredentialsError

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .convector_features import FEATURE_REQUESTS, parse_features
from .iot_requests import READ_REQUESTS as READ_REQUESTS
from .iot_requests import UnsupportedRead, async_read

_LOGGER = logging.getLogger(__name__)
MAX_HISTORY_READS = 14
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
        if not isinstance(payload, dict) or payload.get("Res") not in {expected, "GetGraph"}:
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
    """Read-only configuration and history, isolated from climate availability."""

    def __init__(self, hass, entry, devices, operation_lock=None):
        super().__init__(hass, _LOGGER, config_entry=entry, name="eldom_energy", update_interval=timedelta(minutes=15))
        self.devices = devices
        self._username = entry.data["username"]
        self._password = entry.data["password"]
        self._zone = ZoneInfo(hass.config.time_zone)
        self._task = None
        self._running_task = None
        self.data = {}
        self._operation_lock = operation_lock or asyncio.Lock()

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
        return await async_read(client, device, request)

    async def _single_read(self, client, device, request, timeout=5):
        """Let climate and existing controls run between configuration reads."""
        async with self._operation_lock, asyncio.timeout(timeout):
            return await self._read(client, device, request)

    async def _history(self, client, device, request, now):
        """One uninterrupted request type; recover duplicate dates within a cap."""
        expected = {(now.date() - timedelta(days=i)).isoformat() for i in range(7)}
        seen, payloads = set(), []
        # Only the cursor traversal needs the long exclusive transaction.
        try:
            async with self._operation_lock, asyncio.timeout(45):
                for _ in range(MAX_HISTORY_READS):
                    try:
                        data = await self._read(client, device, request)
                    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                        if isinstance(error, aiohttp.ClientResponseError) and error.status in (401, 403):
                            raise
                        if not payloads:
                            raise
                        break  # Keep confirmed dates when a later read fails.
                    date = vendor_date(data.get("Date"))
                    if date in expected:
                        seen.add(date)
                        payloads.append(data)
                    if seen == expected:
                        break
        except TimeoutError:
            if not payloads:
                raise
        return payloads

    async def _device_snapshot(self, client, device):
        # Any other request can reset the firmware's history cursor. Read
        # settings first, then keep each history transaction homogeneous.
        status = await self._single_read(client, device, "GetStatus", timeout=15)
        reads = {}
        for request in ("GetParams", "GetPowerMode", *FEATURE_REQUESTS):
            try:
                reads[request] = await self._single_read(client, device, request)
            except aiohttp.ClientResponseError as error:
                if error.status not in (400, 404):
                    raise
            except (aiohttp.ClientError, TimeoutError, ValueError):
                # Unsupported/malformed optional features do not hide siblings.
                continue
        params = reads.get("GetParams", {})
        scale = power_class(params.get("SystemSettings"))
        now = datetime.now(self._zone)
        energy, temperatures, history_error = [], [], None
        try:
            try:
                graph = await self._history(client, device, "GetGraph", now)
                energy, temperatures = graph, graph
            except (UnsupportedRead, aiohttp.ClientResponseError) as error:
                if isinstance(error, aiohttp.ClientResponseError) and error.status not in (400, 404):
                    raise
                # Older firmware may support only the original two graph requests.
                energy = await self._history(client, device, "GetEnergyStatistic", now)
                temperatures = await self._history(client, device, "GetTemperatureStatistic", now)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            if isinstance(error, aiohttp.ClientResponseError) and error.status in (401, 403):
                raise
            # Fresh configuration remains useful when history alone fails.
            history_error = type(error).__name__
        now = datetime.now(self._zone)
        energy = parse_history(energy, "energy", scale, now)
        temperatures = parse_history(temperatures, "temperature", scale, now)
        features = parse_features(reads)
        reference = REFERENCE_WATTS[scale] if scale is not None else None
        features["estimate_reference_power"] = reference
        expected = {(now.date() - timedelta(days=i)).isoformat() for i in range(7)}
        return {"energy": energy, "temperature": temperatures, "power_class": scale,
                "reference_power_w": reference,
                "estimate_factor": str(ENERGY_FACTORS[scale]) if scale is not None else None,
                "power_regulation": features["power_regulation"], "features": features,
                "history_coverage": {"energy_missing_dates": sorted(expected - {r["date"] for r in energy}),
                                     "temperature_missing_dates": sorted(expected - {r["date"] for r in temperatures}),
                                     "error_type": history_error},
                "vendor_timezone": status.get("TimeZone"), "retrieved_at": now.isoformat(),
                "source": "Eldom (estimated)", "today": now.date().isoformat()}

    async def _async_update_data(self):
        self._running_task = asyncio.current_task()
        try:
            # Bound the background refresh, including optional feature probes.
            async with asyncio.timeout(90):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, connect=5),
                                                  connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), limit_per_host=1)) as session:
                    client = Client(session, self._username, self._password)
                    # Authenticate once before the serial request groups.
                    await client.token_provider.provide()
                    result = {}
                    for device in self.devices:
                        result[device.uuid] = await self._device_snapshot(client, device)
                    return result
        except InvalidCredentialsError:
            raise ConfigEntryAuthFailed("Eldom rejected authentication") from None
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            if isinstance(error, aiohttp.ClientResponseError) and error.status in (401, 403):
                raise ConfigEntryAuthFailed("Eldom rejected authentication") from None
            # No response body, tokens, pairing IDs or account details in logs.
            raise UpdateFailed(f"Eldom hourly telemetry failed ({type(error).__name__})") from None
        finally:
            self._running_task = None
