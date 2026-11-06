"""Vendor estimates, missingness, day boundaries and isolated read-only polling."""
import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.eldom import energy
from custom_components.eldom.sensor import EldomConvectorEstimatedEnergySensor

NOW = datetime(2026, 11, 6, 12, 20, tzinfo=ZoneInfo("Europe/Amsterdam"))
RAW_ENERGY = {"Res":"GetEnergyStatistic", "Date":"1793923200", "P9":"10", "P10":"10", "P11":"10", "TotalPower":"30"}


class ParsingTests(unittest.TestCase):
    def test_real_app_conversion_without_premature_rounding(self):
        self.assertEqual(energy.power_class("1, 2, 2, 1"), 2)
        self.assertAlmostEqual(energy.estimated_kwh("30", 2), 1.9998)
        self.assertEqual(f"{energy.estimated_kwh('30', 2):.2f}", "2.00")
        self.assertEqual([energy.estimated_kwh("10", i) for i in range(5)], [.3333,.5,.6666,.8333,1.0])

    def test_unknown_configuration_is_never_assumed(self):
        for value in [None, "", "1", "1, 5", "1, -1", "1, 2.0", "1, invalid"]:
            self.assertIsNone(energy.power_class(value))
        for scale in [None, -1, 5, True, "2"]:
            self.assertIsNone(energy.estimated_kwh("34", scale))

    def test_zero_is_valid_but_missing_invalid_and_negative_energy_are_not(self):
        self.assertEqual(energy.estimated_kwh("0", 2), 0)
        for value in [None, "", " ", "NaN", "Infinity", "-1", True, "1e309"]:
            self.assertIsNone(energy.estimated_kwh(value, 2))
        self.assertEqual(energy.number("-5"), -5)

    def test_hour_order_gaps_and_incomplete_hour(self):
        raw = {**RAW_ENERGY, "P23":"999", "P12":"0", "P0":"0"}
        result = energy.parse_history([raw], "energy", 2, NOW)[0]
        self.assertEqual(result["date"], "2026-11-06")
        self.assertEqual(result["hours"][0], 0)
        self.assertIsNone(result["hours"][1])
        self.assertAlmostEqual(result["hours"][9], 0.6666)
        self.assertTrue(all(v is None for v in result["hours"][12:]))
        self.assertAlmostEqual(result["total"], 1.9998)

    def test_temperature_is_already_degrees_not_tenths(self):
        raw = {"Res":"GetTemperatureStatistic", "Date":"1793923200", "T0":"10", "T9":"20", "T11":"0", "T12":"0"}
        hours = energy.parse_history([raw], "temperature", None, NOW)[0]["hours"]
        self.assertEqual(hours[9],20)
        self.assertEqual(hours[11],0)
        self.assertIsNone(hours[12])

    def test_dates_deduplicate_without_filling_missing_days(self):
        yesterday = {**RAW_ENERGY, "Date":"1793836800", "P23":"0"}
        rows = energy.parse_history([yesterday, RAW_ENERGY, RAW_ENERGY, ValueError("test"), {}], "energy", 2, NOW)
        self.assertEqual([r["date"] for r in rows], ["2026-11-05","2026-11-06"])
        self.assertEqual(rows[0]["hours"][23],0)

    def test_invalid_dates_and_vendor_errors_are_not_zero_days(self):
        for value in [None, "0", "-1", "NaN", "1e309", "1793923201", "99999999999999"]:
            self.assertIsNone(energy.vendor_date(value))
        rows = energy.parse_history([{**RAW_ENERGY,"Date":"1794009600"},{**RAW_ENERGY,"Res":"ERROR"}],"energy",2,NOW)
        self.assertEqual(rows,[])

    def test_dst_is_a_vendor_calendar_not_24_invented_utc_instants(self):
        for day in [(2026,3,29),(2026,10,25)]:
            now = datetime(*day,23,30,tzinfo=ZoneInfo("Europe/Amsterdam"))
            date = int(datetime(*day,tzinfo=timezone.utc).timestamp())
            raw = {**RAW_ENERGY,"Date":str(date)}
            rows = energy.parse_history([raw],"energy",2,now)
            self.assertEqual(rows[0]["date"],now.date().isoformat())
            self.assertEqual(len(rows[0]["hours"]),24)
            self.assertIsNone(rows[0]["hours"][23])


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = Mock(config_entry=None)
        self.coordinator.last_update_success = True
        self.coordinator.data = {"test-0128": {"energy": [{"date":"2026-11-06","total":1.9998}], "retrieved_at":NOW.isoformat(),"source":"Eldom (estimated)"}}
        self.sensor = EldomConvectorEstimatedEnergySensor(SimpleNamespace(device_id="test-0128",name="Convector Heater (0128)"), self.coordinator)

    def test_daily_estimate_not_a_lifetime_meter(self):
        with patch("custom_components.eldom.sensor.dt_util.now",return_value=NOW):
            self.assertEqual(self.sensor.native_value,1.9998)
        self.assertIsNone(self.sensor.state_class)
        self.assertEqual(self.sensor.native_unit_of_measurement,"kWh")
        self.assertEqual(self.sensor.extra_state_attributes["source"],"Eldom (estimated)")
        self.assertEqual(self.sensor.unique_id,"test-0128-estimated-energy-today")

    def test_yesterday_never_becomes_todays_zero(self):
        with patch("custom_components.eldom.sensor.dt_util.now",return_value=NOW+timedelta(days=1)):
            self.assertIsNone(self.sensor.native_value)

    def test_stale_missing_and_failed_reads_are_unavailable(self):
        with patch("custom_components.eldom.sensor.dt_util.utcnow",return_value=NOW.astimezone(timezone.utc)):
            self.assertTrue(self.sensor.available)
            self.coordinator.last_update_success = False
            self.assertFalse(self.sensor.available)
            self.coordinator.last_update_success = True
        with patch("custom_components.eldom.sensor.dt_util.utcnow",return_value=(NOW+timedelta(minutes=31)).astimezone(timezone.utc)):
            self.assertFalse(self.sensor.available)
        with patch("custom_components.eldom.sensor.dt_util.utcnow",return_value=(NOW-timedelta(minutes=1)).astimezone(timezone.utc)):
            self.assertFalse(self.sensor.available)
        self.coordinator.data = {}
        self.assertFalse(self.sensor.available)
        self.assertIsNone(self.sensor.native_value)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.entry = Mock(spec=ConfigEntry)
        self.entry.data = {"username":"test@example.invalid","password":"secret-test"}
        self.entry.pref_disable_polling = False
        self.coordinator = energy.EldomEnergyCoordinator(self.hass,self.entry,[])

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        self.temp.cleanup()

    async def test_write_requests_are_rejected_before_network_access(self):
        client=Mock()
        for req in ["SetParams","SetPowerMode","On","Off","ResetEnergy"]:
            with self.assertRaises(ValueError):
                await self.coordinator._read(client,Mock(),req)
        self.assertEqual(client.mock_calls,[])

    async def test_batch_requests_keep_energy_and_temperature_separate(self):
        now=datetime.now(self.coordinator._zone)
        dates=[int(datetime.combine(now.date()-timedelta(days=i),datetime.min.time(),tzinfo=timezone.utc).timestamp()) for i in range(7)]
        calls=[]
        async def read(client,device,req):
            calls.append(req)
            n=calls.count(req)-1
            await asyncio.sleep(0)
            if req=="GetStatus":return {"TimeZone":"1"}
            if req=="GetParams":return {"SystemSettings":"1, 2, 2, 1"}
            if req=="GetPowerMode":return {"Mode":"1"}
            return {**RAW_ENERGY,"Res":req,"Date":str(dates[n]),"T0":"16"}
        self.coordinator._read=read
        result=await self.coordinator._device_snapshot(Mock(),Mock())
        self.assertEqual(len(result["energy"]),7)
        self.assertEqual(len(result["temperature"]),7)
        self.assertEqual(calls.count("GetEnergyStatistic"),7)
        self.assertEqual(calls.count("GetTemperatureStatistic"),7)
        self.assertEqual(result["reference_power_w"],2000)
        self.assertTrue(result["power_regulation"])
        self.assertEqual(set(calls),energy.READ_REQUESTS)

    async def test_failure_message_contains_no_credentials_or_response_body(self):
        with patch("custom_components.eldom.energy.Client") as client:
            client.return_value.token_provider.provide=AsyncMock(side_effect=ValueError("secret-test response-body"))
            with self.assertRaises(UpdateFailed) as caught:
                await self.coordinator._async_update_data()
        self.assertNotIn("secret",str(caught.exception))
        self.assertNotIn("response-body",str(caught.exception))
        self.assertIsNone(self.coordinator._running_task)

    async def test_shutdown_cancels_initial_background_work(self):
        self.coordinator._task=asyncio.create_task(asyncio.Event().wait())
        await self.coordinator.async_shutdown()
        self.assertTrue(self.coordinator._task.cancelled())


if __name__ == "__main__":
    unittest.main()
