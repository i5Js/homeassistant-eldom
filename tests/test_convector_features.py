"""Synthetic protocol, missingness, feature entities and cursor isolation."""

import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.eldom import convector_features as features
from custom_components.eldom.binary_sensor import BINARY_FEATURES, EldomConvectorFeatureBinarySensor
from custom_components.eldom.connection import EldomConnection
from custom_components.eldom.const import IOT_ELDOM_API
from custom_components.eldom.convector_sensor import SENSOR_FEATURES, EldomConvectorFeatureSensor
from custom_components.eldom.eldom_convector import IoTEldomConvectorHeater
from custom_components.eldom.energy import EldomEnergyCoordinator, MAX_HISTORY_READS
from custom_components.eldom.iot_requests import UnsupportedRead, async_read


def replies():
    timers = {"Res": "GetTimers"}
    for i in range(1, 8):
        timers.update({f"OnOff{i}": "1" if i == 1 else "0", f"Days{i}": "62" if i == 1 else "0",
                       f"ON{i}": "09:00", f"OFF{i}": "17:00", f"TSet{i}": "180"})
    return {
        "GetStatus": {"Res": "GetStatus", "Operation": "32", "Status": "0", "TimeZone": "1"},
        "GetParams": {"Res": "GetParams", "SystemSettings": "1, 2, 0, 1", "AutoTimeSet": "1"},
        "GetPowerMode": {"Res": "GetPowerMode", "Mode": "1"},
        "GetParentControl": {"Res": "GetParentControl", "Enable": "0"},
        "GetAntifreezeMode": {"Res": "GetAntifreezeMode", "Enable": "1"},
        "GetSleepMode": {"Res": "GetSleepMode", "Enable": "0"},
        "GetOpenWindowMode": {"Res": "GetOpenWindowMode", "Mode": "2"},
        "GetBrightness": {"Res": "GetBrightness", "Level": "5", "Adaptive": "1"},
        "GetWiFiStatus": {"Res": "GetWiFiStatus", "SWVersion": "2.3.0", "HWVersion": "1.1.0", "WiFiEnable": "1", "APEnable": "0"},
        "GetLanguages": {"Res": "GetLanguages", "Active": "2", "LngI1": "1", "LngD1": "EN", "LngI2": "2", "LngD2": "ES"},
        "GetRates": {"Res": "GetRates", "Rate1": "07:00", "Rate2": "23:00"},
        "GetTimers": timers,
    }


class FeatureParsingTests(unittest.TestCase):
    def test_explicit_zero_and_enabled_are_distinct_from_missing(self):
        parsed = features.parse_features(replies())
        self.assertIs(parsed["child_lock"], False)
        self.assertIs(parsed["antifreeze"], True)
        self.assertEqual(parsed["open_window_mode"], "smart")
        self.assertIsNone(features.parse_features({})["child_lock"])
        self.assertNotIn("heating", parsed)
        self.assertNotIn("Status", parsed)

    def test_invalid_booleans_enums_and_versions_stay_unknown(self):
        data = replies()
        data["GetParentControl"]["Enable"] = True
        data["GetOpenWindowMode"]["Mode"] = {"unexpected": "1"}
        data["GetBrightness"]["Level"] = "NaN"
        data["GetWiFiStatus"]["SWVersion"] = "private arbitrary text"
        parsed = features.parse_features(data)
        for key in ["child_lock", "open_window_mode", "display_brightness", "firmware_version"]:
            self.assertIsNone(parsed[key])
        self.assertIsNone(features.integer("9" * 10000))

    def test_schedule_uses_sunday_first_mask_and_retains_disabled_intervals(self):
        parsed = features.parse_features(replies())
        self.assertEqual(parsed["schedule_enabled_intervals"], 1)
        self.assertTrue(parsed["schedule_complete"])
        self.assertEqual(len(parsed["weekly_schedule"]), 7)
        first = parsed["weekly_schedule"][0]
        self.assertEqual(first["days"], ["monday", "tuesday", "wednesday", "thursday", "friday"])
        self.assertEqual(first["temperature"], 18)
        self.assertFalse(parsed["weekly_schedule"][1]["enabled"])

    def test_incomplete_schedule_is_explicit_and_not_zero(self):
        parsed = features.parse_features({"GetTimers": {"OnOff1": "1"}})
        self.assertIsNone(parsed["schedule_enabled_intervals"])
        self.assertFalse(parsed["schedule_complete"])
        self.assertIsNone(parsed["weekly_schedule"][0]["days"])

    def test_languages_clock_values_and_sensitive_fields(self):
        data = replies()
        data["GetParams"].update({"ID": "synthetic-secret", "Pwd": "synthetic-password"})
        parsed = features.parse_features(data)
        self.assertEqual(parsed["language"], "ES")
        self.assertEqual(parsed["supported_languages"], ["EN", "ES"])
        self.assertEqual(parsed["day_tariff_start"], "07:00")
        self.assertIsNone(features.clock("25:10"))
        self.assertNotIn("synthetic-secret", str(parsed))
        self.assertNotIn("synthetic-password", str(parsed))


class FeatureEntityTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = Mock(config_entry=None, last_update_success=True)
        self.coordinator.data = {"synthetic-0001": {"features": features.parse_features(replies()),
                                                   "retrieved_at": datetime.now(timezone.utc).isoformat()}}
        self.heater = SimpleNamespace(device_id="synthetic-0001")

    def test_configuration_entities_keep_device_identity_and_no_heater_class(self):
        entities = [EldomConvectorFeatureBinarySensor(self.heater, self.coordinator, d) for d in BINARY_FEATURES]
        entities += [EldomConvectorFeatureSensor(self.heater, self.coordinator, d) for d in SENSOR_FEATURES]
        self.assertEqual(len(entities), 18)
        self.assertEqual(len({e.unique_id for e in entities}), 18)
        for entity in entities:
            self.assertEqual(entity.device_info["identifiers"], {("eldom", "synthetic-0001")})
            self.assertNotEqual(entity.device_class, "heat")

    def test_false_available_and_missing_or_stale_unavailable(self):
        description = next(d for d in BINARY_FEATURES if d.key == "child_lock")
        entity = EldomConvectorFeatureBinarySensor(self.heater, self.coordinator, description)
        self.assertFalse(entity.is_on)
        self.assertTrue(entity.available)
        self.coordinator.data["synthetic-0001"]["features"]["child_lock"] = None
        self.assertFalse(entity.available)
        self.coordinator.data["synthetic-0001"]["features"]["child_lock"] = False
        self.coordinator.data["synthetic-0001"]["retrieved_at"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        self.assertFalse(entity.available)


class HistoryTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.entry = Mock(spec=ConfigEntry)
        self.entry.data = {"username": "synthetic@example.invalid", "password": "synthetic-password"}
        self.entry.pref_disable_polling = False
        self.lock = asyncio.Lock()
        self.coordinator = EldomEnergyCoordinator(self.hass, self.entry, [], self.lock)
        now = datetime.now(self.coordinator._zone)
        self.dates = [str(int(datetime.combine(now.date() - timedelta(days=i), datetime.min.time(), tzinfo=timezone.utc).timestamp())) for i in range(7)]
        self.calls = []
        self.sequence = list(self.dates)

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        self.temp.cleanup()

    async def read(self, client, device, req):
        self.calls.append(req)
        await asyncio.sleep(0)
        if req in replies():
            return replies()[req]
        return {"Res": req, "Date": self.sequence.pop(0), "P0": "10", "T0": "17", "TotalPower": "10"}

    async def test_duplicate_dates_recover_without_interleaving_request_types(self):
        self.sequence = [self.dates[0], self.dates[0], *self.dates[1:]]
        self.coordinator._read = self.read
        result = await self.coordinator._device_snapshot(Mock(), Mock())
        self.assertEqual(len(result["energy"]), 7)
        self.assertEqual(len(result["temperature"]), 7)
        self.assertEqual(self.calls[-8:], ["GetGraph"] * 8)
        self.assertEqual(result["history_coverage"]["energy_missing_dates"], [])

    async def test_repeated_single_date_is_bounded_and_missing_dates_stay_missing(self):
        self.sequence = [self.dates[0]] * MAX_HISTORY_READS
        self.coordinator._read = self.read
        result = await self.coordinator._device_snapshot(Mock(), Mock())
        self.assertEqual(self.calls.count("GetGraph"), MAX_HISTORY_READS)
        self.assertEqual(len(result["energy"]), 1)
        self.assertEqual(len(result["history_coverage"]["energy_missing_dates"]), 6)

    async def test_later_history_failure_preserves_confirmed_dates(self):
        graph_calls = 0
        async def read(client, device, req):
            nonlocal graph_calls
            if req == "GetGraph":
                graph_calls += 1
                if graph_calls == 3:
                    raise TimeoutError("synthetic interrupted traversal")
            return await self.read(client, device, req)
        self.coordinator._read = read
        result = await self.coordinator._device_snapshot(Mock(), Mock())
        self.assertEqual(len(result["energy"]), 2)
        self.assertEqual(len(result["history_coverage"]["energy_missing_dates"]), 5)

    async def test_older_firmware_fallback_reads_each_history_contiguously(self):
        self.sequence = self.dates * 2
        async def read(client, device, req):
            if req == "GetGraph":
                self.calls.append(req)
                raise UnsupportedRead("synthetic unsupported graph")
            return await self.read(client, device, req)
        self.coordinator._read = read
        result = await self.coordinator._device_snapshot(Mock(), Mock())
        self.assertEqual(len(result["energy"]), 7)
        self.assertEqual(self.calls[-14:], ["GetEnergyStatistic"] * 7 + ["GetTemperatureStatistic"] * 7)

    async def test_one_bad_feature_and_failed_history_do_not_hide_other_configuration(self):
        async def read(client, device, req):
            if req == "GetBrightness":
                raise ValueError("synthetic malformed feature")
            if req == "GetGraph":
                raise TimeoutError("synthetic history timeout")
            return await self.read(client, device, req)
        self.coordinator._read = read
        result = await self.coordinator._device_snapshot(Mock(), Mock())
        self.assertIsNone(result["features"]["display_brightness"])
        self.assertFalse(result["features"]["child_lock"])
        self.assertEqual(result["energy"], [])
        self.assertEqual(result["history_coverage"]["error_type"], "TimeoutError")

    async def test_existing_status_poll_waits_for_the_whole_history_transaction(self):
        graph_started, release_graph, status_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.coordinator.devices = [SimpleNamespace(uuid="synthetic-0001")]
        async def read(client, device, req):
            if req == "GetGraph" and not graph_started.is_set():
                graph_started.set()
                await release_graph.wait()
            return await self.read(client, device, req)
        self.coordinator._read = read
        connection = EldomConnection("synthetic", "synthetic", IOT_ELDOM_API)
        connection._lock = self.lock
        async def poll():
            status_started.set()
            return {}
        connection._async_client = AsyncMock(return_value=SimpleNamespace(get_devices=poll))
        fake_client = SimpleNamespace(token_provider=SimpleNamespace(provide=AsyncMock(return_value="synthetic")))
        try:
            with patch("custom_components.eldom.energy.Client", return_value=fake_client):
                history = asyncio.create_task(self.coordinator._async_update_data())
                await asyncio.wait_for(graph_started.wait(), 2)
                status = asyncio.create_task(connection.get_devices())
                await asyncio.sleep(0)
                self.assertFalse(status_started.is_set())
                release_graph.set()
                result = await history
                await status
            self.assertEqual(len(result["synthetic-0001"]["energy"]), 7)
            self.assertTrue(status_started.is_set())
        finally:
            release_graph.set()
            await connection.async_close()

    async def test_existing_commands_wait_and_are_never_retried(self):
        client = SimpleNamespace(convector_heater=SimpleNamespace(set_convector_heater_state=AsyncMock(side_effect=TimeoutError())))
        details = SimpleNamespace(Operation="32", T="170", TSet="180")
        device = SimpleNamespace(uuid="synthetic-0001", model="HTRCNV")
        heater = IoTEldomConvectorHeater(device, details, client, self.lock)
        async with self.lock:
            command = asyncio.create_task(heater.set_operation_mode(HVACMode.OFF))
            await asyncio.sleep(0)
            client.convector_heater.set_convector_heater_state.assert_not_awaited()
        with self.assertRaises(TimeoutError):
            await command
        client.convector_heater.set_convector_heater_state.assert_awaited_once()
        self.assertEqual(details.Operation, "32")

    async def test_cancelled_transaction_releases_gate(self):
        self.coordinator.devices = [SimpleNamespace(uuid="synthetic-0001")]
        fake_client = SimpleNamespace(token_provider=SimpleNamespace(provide=AsyncMock(return_value="synthetic")))
        await self.lock.acquire()
        with patch("custom_components.eldom.energy.Client", return_value=fake_client):
            task = asyncio.create_task(self.coordinator._async_update_data())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.lock.release()
        self.assertIsNone(self.coordinator._running_task)

    async def test_read_helper_rejects_controls_before_accessing_client(self):
        client = Mock()
        for req in ["SetParams", "SetTime", "SetBrightness", "Pair", "GetDevice", "Off"]:
            with self.assertRaises(ValueError):
                await async_read(client, Mock(), req)
        self.assertEqual(client.mock_calls, [])
