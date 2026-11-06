"""Offline failures against the real HA/aiohttp runtime; no device commands.

Run inside the HA image: python -B -m unittest discover -s tests -v
"""

import asyncio
from collections import deque
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import eldom
from custom_components.eldom.connection import EldomConnection
from custom_components.eldom.const import IOT_ELDOM_API
from custom_components.eldom.coordinator import EldomCoordinator


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.outcomes = deque()
        self.requests = []
        self.clients = []
        self.release = asyncio.Event()

        async def status(request):
            self.requests.append(request.method)
            outcome = self.outcomes.popleft() if self.outcomes else 200
            if outcome == "hang":
                await self.release.wait()
                outcome = 200
            return web.json_response({"temperature": 16.1}, status=outcome)

        app = web.Application()
        app.router.add_get("/status", status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://localhost:{site._server.sockets[0].getsockname()[1]}/status"

        class Client:
            def __init__(client, session, *_):
                client.session = session
                client.iot_eldom_client = SimpleNamespace(
                    token_provider=SimpleNamespace(token=None)
                )
                self.clients.append(client)

            async def login(client):
                pass

            async def get_devices(client):
                client.iot_eldom_client.token_provider.token = (
                    client.iot_eldom_client.token_provider.token or "test-token"
                )
                async with client.session.get(url) as response:
                    response.raise_for_status()
                    return await response.json()

        self.patch = patch(
            "custom_components.eldom.connection.EldomClientWrapper", Client
        )
        self.patch.start()
        self.connection = EldomConnection("test@example.invalid", "test", IOT_ELDOM_API)

    async def asyncTearDown(self):
        self.release.set()
        await self.connection.async_close()
        self.patch.stop()
        await self.runner.cleanup()
        self.assertTrue(all(c.session.closed for c in self.clients))
        self.assertTrue(all(method == "GET" for method in self.requests))

    async def test_private_resolver_and_reuse(self):
        await self.connection.get_devices()
        session = self.clients[0].session
        self.assertIsInstance(session.connector._resolver, aiohttp.ThreadedResolver)
        self.assertEqual(session.timeout.total, 15)
        await self.connection.get_devices()
        self.assertEqual(len(self.clients), 1)
        self.assertFalse(session.closed)

    async def test_gateway_failure_rotates_transport_preserving_token(self):
        await self.connection.get_devices()
        self.outcomes.append(504)
        with self.assertRaises(aiohttp.ClientResponseError):
            await self.connection.get_devices()
        self.assertTrue(self.clients[0].session.closed)
        self.assertEqual(self.connection._token, "test-token")
        self.assertEqual((await self.connection.get_devices())["temperature"], 16.1)
        self.assertEqual(len(self.clients), 2)

    async def test_timeout_is_bounded_and_next_read_recovers(self):
        self.outcomes.append("hang")
        with patch("custom_components.eldom.connection.FETCH_TIMEOUT", 0.05):
            with self.assertRaises(TimeoutError):
                await self.connection.get_devices()
        self.assertTrue(self.clients[0].session.closed)
        self.release.set()
        await self.connection.get_devices()
        self.assertEqual(len(self.clients), 2)

    async def test_rejected_token_reauthenticates_read_once(self):
        self.outcomes.extend([401, 200])
        await self.connection.get_devices()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.clients[0].session.closed)

    async def test_bad_credentials_stop_after_one_retry(self):
        self.outcomes.extend([401, 403])
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.connection.get_devices()
        self.assertEqual(len(self.requests), 2)
        self.assertIsNone(self.connection._token)

    async def test_dns_error_releases_transport_and_next_poll_recovers(self):
        original = self.connection._async_client
        client = await original()
        with patch.object(client.session.connector._resolver, "resolve", AsyncMock(side_effect=OSError("DNS failed"))):
            with self.assertRaises(aiohttp.ClientConnectionError):
                await self.connection.get_devices()
            self.assertTrue(client.session.closed)
            # The old resolver still fails, but the replacement uses a new one.
            await self.connection.get_devices()

    async def test_cancellation_cleans_up_and_propagates(self):
        self.outcomes.append("hang")
        task = asyncio.create_task(self.connection.get_devices())
        while not self.requests:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.clients[0].session.closed)

    async def test_unload_cannot_reopen_connection(self):
        await self.connection.get_devices()
        await self.connection.async_close()
        await self.connection.async_close()
        with self.assertRaises(RuntimeError):
            await self.connection.get_devices()


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = SimpleNamespace(async_unload_platforms=AsyncMock())
        self.entry = Mock(spec=ConfigEntry)
        self.entry.entry_id = "eldom-test"
        self.entry.pref_disable_polling = False
        self.connection = SimpleNamespace(get_devices=AsyncMock(), async_close=AsyncMock())
        self.coordinator = EldomCoordinator(self.hass, self.connection, self.entry)

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        self.temp.cleanup()

    async def test_real_coordinator_marks_unavailable_then_recovers(self):
        self.connection.get_devices.side_effect = [TimeoutError(), {"test": "fresh"}]
        await self.coordinator.async_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        await self.coordinator.async_refresh()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.data, {"test": "fresh"})
        self.assertEqual(self.coordinator.update_interval.total_seconds(), 30)

    async def test_backoff_is_capped_and_resets_after_success(self):
        self.connection.get_devices.side_effect = TimeoutError()
        for delay in [30, 60, 120, 240, 300, 300]:
            with self.assertRaises(UpdateFailed):
                await self.coordinator._async_update_data()
            self.assertEqual(self.coordinator.update_interval.total_seconds(), delay)
        self.connection.get_devices.side_effect = None
        self.connection.get_devices.return_value = {}
        await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator.update_interval.total_seconds(), 30)

    async def test_rate_limit_respects_retry_after(self):
        self.connection.get_devices.side_effect = aiohttp.ClientResponseError(
            None, (), status=429, headers={"Retry-After": "600"}
        )
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator.update_interval.total_seconds(), 600)

    async def test_auth_failure_uses_ha_reauthentication(self):
        self.connection.get_devices.side_effect = ConfigEntryAuthFailed("test")
        await self.coordinator.async_refresh()
        self.entry.async_start_reauth_if_available.assert_called_once_with(self.hass)
        self.assertFalse(self.coordinator.last_update_success)

    async def test_setup_failure_closes_session_and_preserves_retry(self):
        self.entry.data = {"username": "test", "password": "test", "api": IOT_ELDOM_API}
        fake = SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(side_effect=ConfigEntryNotReady("offline")),
            async_shutdown=AsyncMock(),
        )
        with patch.object(eldom, "EldomConnection"), patch.object(eldom, "EldomCoordinator", return_value=fake):
            with self.assertRaises(ConfigEntryNotReady):
                await eldom.async_setup_entry(self.hass, self.entry)
        fake.async_shutdown.assert_awaited_once()
        self.assertNotIn("eldom-test", self.hass.data.get("eldom", {}))

    async def test_failed_unload_keeps_working_entry(self):
        self.hass.data["eldom"] = {"eldom-test": SimpleNamespace(coordinator=self.coordinator, energy_coordinator=None)}
        with patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
            self.assertFalse(await eldom.async_unload_entry(self.hass, self.entry))
        self.assertIn("eldom-test", self.hass.data["eldom"])
        self.connection.async_close.assert_not_awaited()

    async def test_successful_unload_closes_transport(self):
        self.hass.data["eldom"] = {"eldom-test": SimpleNamespace(coordinator=self.coordinator, energy_coordinator=None)}
        with patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
            self.assertTrue(await eldom.async_unload_entry(self.hass, self.entry))
        self.assertNotIn("eldom-test", self.hass.data["eldom"])
        self.connection.async_close.assert_awaited_once()

    async def test_hourly_start_is_background_after_platform_setup(self):
        self.entry.data = {"username": "test", "password": "test", "api": IOT_ELDOM_API}
        device = Mock()
        fake = SimpleNamespace(
            data={"HTRCNV":{"test":SimpleNamespace(_convector_heater_device=device)}},
            async_config_entry_first_refresh=AsyncMock(), async_shutdown=AsyncMock())
        hourly = SimpleNamespace(start=Mock(), async_shutdown=AsyncMock())
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        with patch.object(eldom, "EldomConnection"), patch.object(eldom, "EldomCoordinator", return_value=fake), patch.object(eldom, "EldomEnergyCoordinator", return_value=hourly) as factory:
            self.assertTrue(await eldom.async_setup_entry(self.hass, self.entry))
        factory.assert_called_once_with(self.hass, self.entry, [device])
        hourly.start.assert_called_once_with(self.hass, self.entry)
        self.assertIs(self.hass.data["eldom"]["eldom-test"].energy_coordinator, hourly)

    async def test_successful_unload_closes_both_coordinators(self):
        hourly = SimpleNamespace(async_shutdown=AsyncMock())
        self.hass.data["eldom"] = {"eldom-test": SimpleNamespace(coordinator=self.coordinator, energy_coordinator=hourly)}
        with patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
            self.assertTrue(await eldom.async_unload_entry(self.hass, self.entry))
        hourly.async_shutdown.assert_awaited_once()
        self.connection.async_close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
