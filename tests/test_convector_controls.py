"""Confirmed writes, no replay, display coupling and HA state publication."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from homeassistant.exceptions import HomeAssistantError
from ioteldom.crc import crc32

from custom_components.eldom.convector_control import async_set_control, async_set_temperature_preserving_params, ControlNotConfirmed, ControlNotAllowed
from custom_components.eldom.connection import EldomConnection
from custom_components.eldom.const import IOT_ELDOM_API
from custom_components.eldom.number import BRIGHTNESS, EldomBrightnessNumber
from custom_components.eldom.select import WINDOW_MODE, EldomWindowSelect
from custom_components.eldom.switch import CONTROL_SWITCHES, EldomConvectorConfigSwitch
from custom_components.eldom.eldom_convector import IoTEldomConvectorHeater


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setup_client(self, write):
        self.device = SimpleNamespace(uuid="synthetic-device", pairTok="synthetic-pair")
        self.response = SimpleNamespace(raise_for_status=Mock(), json=AsyncMock(return_value={
            "Res": write, "Code": "0", "Type": "OK", "Reason": "SUCCESS"}))
        cm = AsyncMock()
        cm.__aenter__.return_value = self.response
        self.client = SimpleNamespace(session=SimpleNamespace(post=Mock(return_value=cm)),
            token_provider=SimpleNamespace(provide=AsyncMock(return_value="synthetic-token")))

    async def test_invalid_controls_and_values_never_touch_client(self):
        client = Mock()
        for key, value in [("Pair", True), ("SetTime", True), ("sleep", "1"),
                           ("child_lock", "1"), ("child_lock", 1),
                           ("display_brightness", True), ("display_brightness", 1.5),
                           ("display_brightness", 0), ("display_brightness", 11),
                           ("open_window_mode", 1), ("open_window_mode", "unknown")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                await async_set_control(client, Mock(), key, value)
        self.assertEqual(client.mock_calls, [])

    async def test_boolean_controls_encode_the_actual_vendor_fields(self):
        for key, read, write, field, out in [
            ("child_lock", "GetParentControl", "SetParentControl", "Enable", "Enable"),
            ("antifreeze", "GetAntifreezeMode", "SetAntifreezeMode", "Enable", "Enable"),
            ("power_regulation", "GetPowerMode", "SetPowerMode", "Mode", "Regulation")]:
            with self.subTest(key=key):
                self.setup_client(write)
                reads = AsyncMock(side_effect=[{"Res": read, field: "0"}, {"Res": read, field: "1"}])
                with patch("custom_components.eldom.convector_control.async_read", reads):
                    self.assertEqual(await async_set_control(self.client, self.device, key, True), {key: True})
                payload = self.client.session.post.call_args.kwargs["json"].copy()
                checksum = payload.pop("CRC")
                self.assertEqual(payload, {"ID": "synthetic-pair", "Req": write, out: "1", "CID": "1"})
                self.assertEqual(checksum, crc32(payload))
                self.assertEqual(reads.await_count, 2)
                self.client.session.post.assert_called_once()

    async def test_already_confirmed_value_needs_no_write(self):
        self.setup_client("SetParentControl")
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(return_value={"Enable": "1"})):
            self.assertEqual(await async_set_control(self.client, self.device, "child_lock", True), {"child_lock": True})
        self.client.session.post.assert_not_called()

    async def test_display_writes_preserve_the_other_setting(self):
        for key, value, after in [
            ("display_brightness", 6, {"Level": "6", "Adaptive": "1"}),
            ("adaptive_display", False, {"Level": "5", "Adaptive": "0"})]:
            with self.subTest(key=key):
                self.setup_client("SetBrightness")
                with patch("custom_components.eldom.convector_control.async_read", AsyncMock(side_effect=[{"Level": "5", "Adaptive": "1"}, after])):
                    result = await async_set_control(self.client, self.device, key, value)
                self.assertEqual(result[key], value)
                payload = self.client.session.post.call_args.kwargs["json"]
                self.assertEqual({k: payload[k] for k in after}, after)

    async def test_missing_paired_display_setting_prevents_write(self):
        self.setup_client("SetBrightness")
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(return_value={"Level": "5"})):
            with self.assertRaises(ControlNotConfirmed):
                await async_set_control(self.client, self.device, "display_brightness", 6)
        self.client.session.post.assert_not_called()

    async def test_all_window_options_are_verified(self):
        for option, code in [("off", "0"), ("on", "1"), ("smart", "2")]:
            self.setup_client("SetOpenWindowMode")
            with patch("custom_components.eldom.convector_control.async_read", AsyncMock(side_effect=[{"Mode": "0" if code == "1" else "1"}, {"Mode": code}])):
                self.assertEqual(await async_set_control(self.client, self.device, "open_window_mode", option), {"open_window_mode": option})

    async def test_rejected_ack_is_not_replayed_or_logged(self):
        self.setup_client("SetParentControl")
        self.response.json.return_value = {"Res": "SetParentControl", "Code": "1", "Type": "ERROR", "Reason": "private-body"}
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(return_value={"Enable": "0"})):
            with self.assertRaises(ControlNotConfirmed) as caught:
                await async_set_control(self.client, self.device, "child_lock", True)
        self.assertNotIn("private-body", str(caught.exception))
        self.client.session.post.assert_called_once()

    async def test_success_ack_with_wrong_readback_is_not_success(self):
        self.setup_client("SetParentControl")
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(return_value={"Enable": "0"})):
            with self.assertRaises(ControlNotConfirmed):
                await async_set_control(self.client, self.device, "child_lock", True)
        self.client.session.post.assert_called_once()

    async def test_uncertain_network_write_is_never_replayed(self):
        self.setup_client("SetParentControl")
        self.client.session.post.side_effect = TimeoutError()
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(return_value={"Enable": "0"})):
            with self.assertRaises(TimeoutError):
                await async_set_control(self.client, self.device, "child_lock", True)
        self.client.session.post.assert_called_once()

    async def test_sleep_changes_only_in_manual_without_mode_commands(self):
        for operation in ("16", "18"):
            self.setup_client("SetSleepMode")
            reads = AsyncMock(side_effect=[{"Enable": "0"}, {"Operation": operation}, {"Enable": "1"}])
            with patch("custom_components.eldom.convector_control.async_read", reads):
                self.assertEqual(await async_set_control(self.client, self.device, "sleep", True), {"sleep": True})
            self.client.session.post.assert_called_once()
            self.assertEqual(self.client.session.post.call_args.kwargs['json']['Req'], 'SetSleepMode')

    async def test_sleep_rejects_weekly_timer_and_unknown_modes_before_write(self):
        for operation in ("0", "32", "48", "64", "bad", None):
            self.setup_client("SetSleepMode")
            with patch("custom_components.eldom.convector_control.async_read", AsyncMock(side_effect=[{"Enable": "0"}, {"Operation": operation}])):
                with self.assertRaises(ControlNotAllowed):
                    await async_set_control(self.client, self.device, "sleep", True)
            self.client.session.post.assert_not_called()

    async def test_temperature_preserves_live_params_and_writes_degrees(self):
        self.setup_client("SetParams")
        before = {"Res": "GetParams", "TSet": "150", "AutoTimeSet": "0", "Lock": "1",
                  "Rate1": "05:30", "Rate2": "21:15", "SystemSettings": "1, 3, 1, 1", "Antifrost": "1"}
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(side_effect=[before, {**before, "TSet": "160", "CRC": "new"}])):
            self.assertEqual(await async_set_temperature_preserving_params(self.client, self.device, 16), "160")
        payload = self.client.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["TSet"], "16")
        for key in ("AutoTimeSet", "Lock", "Rate1", "Rate2", "SystemSettings"):
            self.assertEqual(payload[key], before[key])
        self.client.session.post.assert_called_once()

    async def test_temperature_rejects_incomplete_params_and_detects_side_effects(self):
        self.setup_client("SetParams")
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(return_value={"TSet": "150"})):
            with self.assertRaises(ControlNotConfirmed):
                await async_set_temperature_preserving_params(self.client, self.device, 16)
        self.client.session.post.assert_not_called()
        before = {"TSet": "150", "AutoTimeSet": "1", "Lock": "0", "Rate1": "06:00", "Rate2": "22:00", "SystemSettings": "1, 3, 1, 1"}
        with patch("custom_components.eldom.convector_control.async_read", AsyncMock(side_effect=[before, {**before, "TSet": "160", "SystemSettings": "1, 2, 2, 0"}])):
            with self.assertRaises(ControlNotConfirmed):
                await async_set_temperature_preserving_params(self.client, self.device, 16)

    async def test_climate_temperature_never_uses_hardcoded_library_parameters(self):
        client = Mock()
        device = SimpleNamespace(uuid="synthetic", model="HTRCNV")
        details = SimpleNamespace(Operation="32", T="155", TSet="150")
        heater = IoTEldomConvectorHeater(device, details, client)
        with patch("custom_components.eldom.eldom_convector.async_set_temperature_preserving_params", AsyncMock(return_value="160")) as write:
            await heater.set_temperature(16)
        write.assert_awaited_once_with(client, device, 16)
        self.assertEqual(details.TSet, "160")
        client.convector_heater.set_convector_heater_temperature.assert_not_called()
        with patch("custom_components.eldom.eldom_convector.async_set_temperature_preserving_params", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await heater.set_temperature(15)
        self.assertEqual(details.TSet, "160")


class EntityAndLockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stamp = datetime.now(timezone.utc).isoformat()
        self.coordinator = SimpleNamespace(data={"synthetic-device": {
            "retrieved_at": self.stamp, "features": {"child_lock": False, "display_brightness": 5,
            "adaptive_display": True, "open_window_mode": "on"}, "energy": [{"total": 1.2}]}},
            last_update_success=True, async_update_listeners=Mock())
        self.connection = SimpleNamespace(async_set_convector_control=AsyncMock(return_value={"child_lock": True}))
        self.heater = SimpleNamespace(device_id="synthetic-device", _convector_heater_device=Mock())
        self.entity = EldomConvectorConfigSwitch(self.heater, self.coordinator, self.connection, CONTROL_SWITCHES[0])

    async def test_only_confirmed_values_publish_without_refreshing_history_age(self):
        self.connection.async_set_convector_control = AsyncMock(side_effect=TimeoutError("private-error"))
        with self.assertRaises(HomeAssistantError) as caught:
            await self.entity.async_turn_on()
        self.assertNotIn("private-error", str(caught.exception))
        self.assertIsNone(self.entity.is_on)
        self.assertFalse(self.entity.available)
        snapshot = self.coordinator.data["synthetic-device"]
        self.assertEqual(snapshot["retrieved_at"], self.stamp)
        self.assertEqual(snapshot["features"]["display_brightness"], 5)
        self.assertEqual(snapshot["energy"], [{"total": 1.2}])
        self.assertTrue(self.coordinator.last_update_success)

    async def test_validated_readback_updates_both_readers_and_control(self):
        await self.entity.async_turn_on()
        self.assertTrue(self.entity.is_on)
        self.assertTrue(self.entity.available)
        self.assertEqual(self.entity.unique_id, "synthetic-device-control-child_lock")
        self.coordinator.async_update_listeners.assert_called_once()

    async def test_number_and_select_keep_seven_distinct_control_ids(self):
        entities = [EldomConvectorConfigSwitch(self.heater, self.coordinator, self.connection, d) for d in CONTROL_SWITCHES]
        entities += [EldomBrightnessNumber(self.heater, self.coordinator, self.connection, BRIGHTNESS),
                     EldomWindowSelect(self.heater, self.coordinator, self.connection, WINDOW_MODE)]
        self.assertEqual(len({e.unique_id for e in entities}), 7)
        self.assertEqual(entities[-2].native_value, 5)
        self.assertEqual(entities[-1].current_option, "on")

    async def test_new_controls_wait_for_history_lock_and_send_once(self):
        connection = EldomConnection("synthetic", "synthetic", IOT_ELDOM_API)
        connection._async_client = AsyncMock(return_value=SimpleNamespace(iot_eldom_client=Mock()))
        with patch("custom_components.eldom.connection.async_set_control", AsyncMock(return_value={"child_lock": True})) as write:
            async with connection.operation_lock:
                task = asyncio.create_task(connection.async_set_convector_control(Mock(), "child_lock", True))
                await asyncio.sleep(0)
                write.assert_not_awaited()
            await task
            write.assert_awaited_once()
        await connection.async_close()

    async def test_mode_rejection_keeps_the_confirmed_control_value(self):
        self.connection.async_set_convector_control.side_effect = ControlNotAllowed("Manual required")
        with self.assertRaises(HomeAssistantError) as caught:
            await self.entity.async_turn_on()
        self.assertEqual(str(caught.exception), "Manual required")
        self.assertIs(self.entity.is_on, False)

    async def test_invalid_number_does_not_invalidate_known_state(self):
        number = EldomBrightnessNumber(self.heater, self.coordinator, self.connection, BRIGHTNESS)
        with self.assertRaises(ValueError):
            await number.async_set_native_value(1.5)
        self.assertEqual(number.native_value, 5)
        self.connection.async_set_convector_control.assert_not_awaited()

    async def test_cancelled_write_does_not_keep_a_certain_control_state(self):
        started = asyncio.Event()
        async def pending(*args):
            started.set()
            await asyncio.Event().wait()
        self.connection.async_set_convector_control = pending
        task = asyncio.create_task(self.entity.async_turn_on())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.entity.is_on)
        self.assertFalse(self.entity.available)

    async def test_rejected_auth_discards_token_without_replaying_control(self):
        connection = EldomConnection("synthetic", "synthetic", IOT_ELDOM_API)
        connection._token = "rejected-token"
        connection._async_client = AsyncMock(return_value=SimpleNamespace(iot_eldom_client=Mock()))
        with patch("custom_components.eldom.connection.async_set_control", AsyncMock(side_effect=aiohttp.ClientResponseError(None, (), status=401))) as write:
            with self.assertRaises(aiohttp.ClientResponseError):
                await connection.async_set_convector_control(Mock(), "child_lock", True)
            write.assert_awaited_once()
        self.assertIsNone(connection._token)
        await connection.async_close()

    async def test_mode_guard_keeps_the_existing_climate_transport_open(self):
        connection = EldomConnection("synthetic", "synthetic", IOT_ELDOM_API)
        client = SimpleNamespace(iot_eldom_client=Mock())
        connection._client = client
        connection._async_client = AsyncMock(return_value=client)
        with patch("custom_components.eldom.connection.async_set_control", AsyncMock(side_effect=ControlNotAllowed("Manual required"))):
            with self.assertRaises(ControlNotAllowed):
                await connection.async_set_convector_control(Mock(), "sleep", True)
        self.assertIs(connection._client, client)
        await connection.async_close()


if __name__ == "__main__":
    unittest.main()
