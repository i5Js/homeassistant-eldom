"""Vendor-observed mode semantics, preserved intervals and durable pause state."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import selector
import voluptuous as vol
import yaml
from ioteldom.crc import crc32

from custom_components.eldom.climate import IoTEldomConvectorHeaterEntity
from custom_components.eldom import climate, const
from custom_components.eldom.connection import EldomConnection
from custom_components.eldom.const import IOT_ELDOM_API, DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM
from custom_components.eldom.convector_control import ControlNotAllowed, ControlNotConfirmed
from custom_components.eldom.eldom_convector import IoTEldomConvectorHeater
from custom_components.eldom.weekly_manager import WeeklyProgramManager, PROFILE_OPTION
from custom_components.eldom.weekly_program import (
    FIELDS, async_weekly_transaction, enabled_slots, interval_list,
    normalize_timers, validate_changes, validate_request,
)


def timers():
    result = {}
    for i in range(1, 8):
        result.update({f"TSet{i}": "180" if i < 3 else "70", f"ON{i}": "09:00" if i < 3 else "00:00",
                       f"OFF{i}": "17:00" if i < 3 else "00:00", f"Days{i}": "62" if i < 3 else "0",
                       f"OnOff{i}": "1" if i == 1 else "0"})
    return result


class Reply:
    def __init__(self, value, error=None):
        self.value, self.error = value, error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def json(self, **kwargs):
        if self.error:
            raise self.error
        return self.value


class Vendor:
    """Observed firmware: SetTimers selects manual/weekly only while powered."""
    def __init__(self, mode=32):
        self.timers = timers()
        self.params = {"Res": "GetParams", "TSet": "150", "Lock": "1", "AutoTimeSet": "0",
                       "Rate1": "05:30", "Rate2": "21:15", "Antifrost": "1", "SystemSettings": "1, 3, 1, 1"}
        self.mode, self.sleep = mode, "0"
        self.calls = []
        self.reject = False
        self.uncertain = False
        self.mismatch = False
        self.wrote = False
        self.client = SimpleNamespace(session=SimpleNamespace(post=self.post),
            token_provider=SimpleNamespace(provide=AsyncMock(return_value="synthetic-token")))
        self.device = SimpleNamespace(uuid="synthetic-device", pairTok="synthetic-pair", model="HTRCNV")

    @property
    def writes(self):
        return [req for req, _ in self.calls if not req.startswith("Get")]

    def post(self, url, *, json, headers):
        payload = dict(json)
        checksum = payload.pop("CRC")
        assert checksum == crc32(payload)
        req = payload["Req"]
        self.calls.append((req, payload))
        if req == "GetParams":
            return Reply(dict(self.params))
        if req == "GetTimers":
            value = dict(self.timers)
            if self.mismatch and self.wrote:
                value["TSet2"] = "195"
            return Reply({"Res": req, **value})
        if req == "GetStatus":
            return Reply({"Res": req, "Operation": str(self.mode), "T": "159", "TSet": "150"})
        if req == "GetSleepMode":
            return Reply({"Res": req, "Enable": self.sleep})
        assert req in ("SetTimers", "On", "Off"), "No unrelated parameter writes are allowed"
        if self.reject:
            return Reply({"Res": req, "Code": "643", "Type": "Error", "Reason": "private-body"})
        if req == "SetTimers":
            assert set(payload) == {"ID", "Req", "CID", *FIELDS}
            for field in FIELDS:
                self.timers[field] = str(int(Decimal(payload[field]) * 10)) if field.startswith("TSet") else payload[field]
            if self.mode:
                self.mode = 32 if enabled_slots(self.timers) else 16
        elif req == "On":
            self.mode = 32 if enabled_slots(self.timers) else 16
        else:
            self.mode = 0
        self.wrote = True
        return Reply({"Res": req, "Code": "0", "Type": "OK"}, TimeoutError() if self.uncertain else None)

    async def run(self, operation, **kwargs):
        return await async_weekly_transaction(self.client, self.device, operation, **kwargs)


class ValidationTests(unittest.TestCase):
    def test_partial_changes_accept_half_degrees_and_whole_minute_time_selector(self):
        slot, values = validate_changes(2, {"temperature": 15.5, "start": "08:30:00", "weekdays": ["sunday", "monday"]})
        self.assertEqual(slot, 2)
        self.assertEqual(values["start"], "08:30")
        self.assertEqual(values["temperature"], Decimal("15.5"))

    def test_invalid_inputs_are_rejected(self):
        for slot, changes in [(True, {"enabled": True}), (0, {"enabled": True}), (8, {"enabled": True}),
                              (1.5, {"enabled": True}), (float("inf"), {"enabled": True}),
                              (1, {}), (1, {"arbitrary": "value"}), (1, {"temperature": True}),
                              (1, {"temperature": 15.2}), (1, {"temperature": float("nan")}),
                              (1, {"weekdays": ["monday", "monday"]}), (1, {"weekdays": ["unknown"]}),
                              (1, {"start": "24:00"}), (1, {"end": "08:00:01"})]:
            with self.subTest(slot=slot, changes=changes), self.assertRaises(ValueError):
                validate_changes(slot, changes)

    def test_enabled_rows_need_days_and_positive_same_day_intervals(self):
        for field, value in [("Days1", "0"), ("OFF1", "09:00"), ("OFF1", "08:00")]:
            data = timers(); data[field] = value
            with self.assertRaises(ControlNotAllowed):
                normalize_timers(data)

    def test_all_seven_fields_are_required_and_types_are_not_guessed(self):
        data = timers(); del data["ON7"]
        with self.assertRaises(ControlNotConfirmed):
            normalize_timers(data)
        data = timers(); data["Days1"] = 62
        with self.assertRaises(ControlNotConfirmed):
            normalize_timers(data)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_changes_one_row_and_preserves_six_rows_and_params(self):
        vendor = Vendor(); original = deepcopy(vendor.timers); params = dict(vendor.params)
        result = await vendor.run("edit", slot=7, changes={"temperature": 8.5, "start": "10:30", "end": "12:45", "weekdays": ["sunday", "saturday"]})
        for key in FIELDS:
            if not key.endswith("7"):
                self.assertEqual(result["timers"][key], original[key])
        self.assertEqual(vendor.timers["TSet7"], "85")
        self.assertEqual(vendor.timers["Days7"], "65")
        self.assertEqual(vendor.params, params)
        payload = next(p for req,p in vendor.calls if req == "SetTimers")
        self.assertEqual(Decimal(payload["TSet7"]), Decimal("8.5"))
        self.assertEqual(vendor.writes, ["SetTimers"])

    async def test_manual_pause_remembers_before_write_and_auto_restores(self):
        vendor = Vendor(); original = deepcopy(vendor.timers); saved = {}
        def remember(value):
            self.assertEqual(vendor.mode, 32)
            saved.update(value)
        await vendor.run("heat", remember=remember)
        self.assertEqual(vendor.mode, 16)
        self.assertEqual(enabled_slots(vendor.timers), [])
        self.assertEqual(saved, original)
        await vendor.run("auto", profile=saved)
        self.assertEqual(vendor.mode, 32)
        self.assertEqual(vendor.timers, original)

    async def test_editing_while_off_never_sends_on(self):
        vendor = Vendor(0)
        await vendor.run("edit", slot=7, changes={"temperature": 8.5})
        self.assertEqual(vendor.mode, 0)
        self.assertEqual(vendor.writes, ["SetTimers"])

    async def test_auto_from_off_confirms_restored_schedule_before_on(self):
        vendor = Vendor(0); saved = deepcopy(vendor.timers)
        vendor.timers["OnOff1"] = "0"
        await vendor.run("auto", profile=saved)
        self.assertEqual(vendor.writes, ["SetTimers", "On"])
        self.assertEqual(vendor.mode, 32)

    async def test_heat_from_off_clears_flags_before_on(self):
        vendor = Vendor(0); saved = {}
        await vendor.run("heat", remember=lambda value: saved.update(value))
        self.assertEqual(vendor.writes, ["SetTimers", "On"])
        self.assertEqual(vendor.mode, 16)
        self.assertEqual(enabled_slots(saved), [1])

    async def test_existing_active_programme_is_idempotent(self):
        vendor = Vendor()
        await vendor.run("auto")
        self.assertEqual(vendor.writes, [])

    async def test_stale_or_empty_profile_never_invents_enabled_intervals(self):
        vendor = Vendor(16); saved = deepcopy(vendor.timers); vendor.timers["OnOff1"] = "0"
        vendor.timers["TSet1"] = "190"
        for profile in [saved, {}, None]:
            with self.assertRaises(ControlNotAllowed):
                await vendor.run("auto", profile=profile)
        self.assertEqual(vendor.writes, [])
        await vendor.run("auto", profile=saved, selected_slots=[1])
        self.assertEqual(vendor.mode, 32)
        self.assertEqual(vendor.timers["TSet1"], "190")

    async def test_edit_while_paused_updates_saved_content_without_starting(self):
        vendor = Vendor(); saved = {}
        await vendor.run("heat", remember=lambda value: saved.update(value))
        await vendor.run("edit", profile=saved, slot=1, changes={"temperature": 18.5}, remember=lambda value: saved.update(value))
        self.assertEqual(vendor.mode, 16)
        self.assertEqual(saved["OnOff1"], "1")
        self.assertEqual(saved["TSet1"], "185")
        await vendor.run("auto", profile=saved)
        self.assertEqual(vendor.timers["TSet1"], "185")
        self.assertEqual(vendor.mode, 32)

    async def test_disabling_last_row_is_an_intentional_empty_profile(self):
        vendor = Vendor(); saved = {}
        await vendor.run("edit", slot=1, changes={"enabled": False}, remember=lambda value: saved.update(value))
        self.assertEqual(vendor.mode, 16)
        self.assertEqual(enabled_slots(saved), [])
        with self.assertRaises(ControlNotAllowed):
            await vendor.run("auto", profile=saved)

    async def test_read_has_no_writes_or_persistence(self):
        vendor = Vendor(); remember = Mock()
        result = await vendor.run("read", remember=remember)
        self.assertEqual(len(interval_list(result["timers"])), 7)
        self.assertEqual(vendor.writes, [])
        remember.assert_not_called()

    async def test_rejected_and_uncertain_writes_are_not_replayed(self):
        for fault, error in [("reject", ControlNotConfirmed), ("uncertain", TimeoutError)]:
            vendor = Vendor(); setattr(vendor, fault, True); saved = {}
            with self.assertRaises(error):
                await vendor.run("heat", remember=lambda value: saved.update(value))
            self.assertEqual(vendor.writes, ["SetTimers"])
            self.assertEqual(enabled_slots(saved), [1])

    async def test_wrong_readback_aborts_before_power_on(self):
        vendor = Vendor(0); saved = deepcopy(vendor.timers)
        vendor.timers["OnOff1"] = "0"; vendor.mismatch = True
        with self.assertRaises(ControlNotConfirmed):
            await vendor.run("auto", profile=saved)
        self.assertEqual(vendor.mode, 0)
        self.assertEqual(vendor.writes, ["SetTimers"])

    async def test_invalid_patch_or_slots_never_touch_a_client(self):
        client = Mock()
        for kwargs in [{"operation": "edit", "slot": 1, "changes": {}},
                       {"operation": "auto", "selected_slots": [1, 1]},
                       {"operation": "auto", "selected_slots": []}, {"operation": "SetTime"}]:
            with self.assertRaises(ValueError):
                await async_weekly_transaction(client, Mock(), **kwargs)
        self.assertEqual(client.mock_calls, [])

    async def test_sleep_and_timer_configuration_are_rejected_without_writes(self):
        vendor = Vendor(); vendor.sleep = "1"
        with self.assertRaises(ControlNotAllowed):
            await vendor.run("auto")
        vendor.sleep = "0"; vendor.params["SystemSettings"] = "0, 3, 1, 1"
        with self.assertRaises(ControlNotAllowed):
            await vendor.run("edit", slot=7, changes={"temperature": 8.5})
        self.assertEqual(vendor.writes, [])

    async def test_off_works_in_timer_mode_without_reading_or_changing_schedule(self):
        vendor = Vendor(48); vendor.params["SystemSettings"] = "0, 3, 1, 1"
        await vendor.run("off")
        self.assertEqual(vendor.mode, 0)
        self.assertEqual([req for req,_ in vendor.calls], ["GetStatus", "Off", "GetStatus"])


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def build(self, vendor, entry=None):
        entry = entry or SimpleNamespace(options={"unrelated_option": "preserved"})
        def update(target, *, options):
            target.options = options
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock(side_effect=update)))
        connection = EldomConnection("synthetic", "synthetic", IOT_ELDOM_API)
        connection._async_client = AsyncMock(return_value=SimpleNamespace(iot_eldom_client=vendor.client))
        heater = IoTEldomConvectorHeater(vendor.device, SimpleNamespace(Operation=str(vendor.mode), T="159", TSet="150"), vendor.client)
        main = SimpleNamespace(data={DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM: {heater.id: heater}},
                               async_refresh=AsyncMock(), async_update_listeners=Mock())
        auxiliary = SimpleNamespace(data={heater.id: {"retrieved_at": "original-age", "features": {"child_lock": True}, "energy": [1]}},
                                    last_update_success=False, async_update_listeners=Mock())
        return WeeklyProgramManager(hass, entry, connection, main, auxiliary), heater, connection

    async def test_profile_survives_manager_and_transport_recreation(self):
        vendor = Vendor(); manager, heater, connection = self.build(vendor)
        await manager.execute(heater, "heat")
        self.assertEqual(vendor.mode, 16)
        self.assertEqual(manager.entry.options["unrelated_option"], "preserved")
        self.assertIn(PROFILE_OPTION, manager.entry.options)
        await connection.async_close()
        new, heater, connection = self.build(vendor, manager.entry)
        result = await new.execute(heater, "auto")
        self.assertEqual(result["mode"], "weekly_program")
        self.assertEqual(enabled_slots(vendor.timers), [1])
        snapshot = new.auxiliary.data[heater.id]
        self.assertEqual(snapshot["retrieved_at"], "original-age")
        self.assertEqual(snapshot["energy"], [1])
        self.assertTrue(snapshot["features"]["child_lock"])
        self.assertFalse(new.auxiliary.last_update_success)
        await connection.async_close()

    async def test_failed_write_reads_actual_state_without_optimistic_publish(self):
        vendor = Vendor(); manager, heater, connection = self.build(vendor)
        vendor.reject = True
        with self.assertRaises(HomeAssistantError) as caught:
            await manager.execute(heater, "heat")
        self.assertNotIn("private-body", str(caught.exception))
        self.assertEqual(heater.operation_code, "32")
        manager.main.async_refresh.assert_awaited_once()
        manager.main.async_update_listeners.assert_not_called()
        self.assertEqual(vendor.writes, ["SetTimers"])
        await connection.async_close()

    async def test_profile_is_loaded_after_acquiring_the_shared_lock(self):
        vendor = Vendor(); connection = EldomConnection("synthetic", "synthetic", IOT_ELDOM_API)
        connection._async_client = AsyncMock(return_value=SimpleNamespace(iot_eldom_client=vendor.client))
        getter = Mock(return_value=timers())
        with patch("custom_components.eldom.connection.async_weekly_transaction", AsyncMock(return_value={})) as request:
            async with connection.operation_lock:
                task = asyncio.create_task(connection.async_weekly_program(vendor.device, "auto", profile_provider=getter))
                await asyncio.sleep(0)
                getter.assert_not_called()
                request.assert_not_awaited()
            await task
        getter.assert_called_once()
        await connection.async_close()

    async def test_native_auto_and_services_use_manager_without_legacy_commands(self):
        vendor = Vendor(16); manager = SimpleNamespace(execute=AsyncMock(return_value={"mode": "weekly_program"}))
        heater = IoTEldomConvectorHeater(vendor.device, SimpleNamespace(Operation="16", T="159", TSet="150"), Mock())
        entity = IoTEldomConvectorHeaterEntity(heater, Mock(config_entry=None), manager)
        self.assertIn(HVACMode.AUTO, entity.hvac_modes)
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        manager.execute.assert_awaited_with(heater, "auto")
        await entity.async_set_weekly_interval(slot=7, temperature=8.5)
        manager.execute.assert_awaited_with(heater, "edit", slot=7, changes={"temperature": 8.5})
        await entity.async_activate_weekly_program(enabled_intervals=[1])
        manager.execute.assert_awaited_with(heater, "auto", selected_slots=[1])
        self.assertEqual(heater._iot_eldom_client.mock_calls, [])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_services_register_and_normalize_minute_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            coordinator = SimpleNamespace(data={const.DEVICE_TYPE_CONVECTOR_HEATER_ELDOM: {},
                                                const.DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM: {}})
            entry = SimpleNamespace(entry_id="synthetic-entry")
            hass.data[const.DOMAIN] = {entry.entry_id: SimpleNamespace(coordinator=coordinator, weekly_manager=None)}
            await climate.async_setup_entry(hass, entry, lambda entities: list(entities))
            await climate.async_setup_entry(hass, entry, lambda entities: list(entities))
            services = hass.services.async_services()[const.DOMAIN]
            self.assertEqual(set(services), {"get_weekly_schedule", "set_weekly_interval", "activate_weekly_program"})
            self.assertEqual(services["get_weekly_schedule"].supports_response, SupportsResponse.OPTIONAL)
            selected = selector.NumberSelector({"min": 1, "max": 7, "step": 1, "mode": "box"})(7)
            values = services["set_weekly_interval"].schema({"entity_id": "climate.synthetic", "slot": selected, "start": "08:30:00", "temperature": 8.5})
            self.assertEqual(values["slot"], 7)
            self.assertEqual(values["start"], "08:30")
            with self.assertRaises(vol.Invalid):
                services["set_weekly_interval"].schema({"entity_id": "climate.synthetic", "slot": 8})

    async def test_all_action_form_selectors_are_accepted_by_real_ha(self):
        descriptions = yaml.safe_load((Path(climate.__file__).parent / "services.yaml").read_text())
        count = 0
        for service in descriptions.values():
            for field in service.get("fields", {}).values():
                selector.validate_selector(field["selector"])
                count += 1
        self.assertEqual(count, 7)


if __name__ == "__main__":
    unittest.main()
