"""The Eldom Coordinator."""

from datetime import timedelta
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .connection import EldomConnection
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class EldomCoordinator(DataUpdateCoordinator):
    """Eldom coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        eldom_wrapper_client: EldomConnection,
        entry: ConfigEntry,
    ) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )
        self.eldom_wrapper_client = eldom_wrapper_client
        self._failures = 0

    async def _async_update_data(self) -> dict:
        """Fetch data from Eldom."""

        try:
            data = await self.eldom_wrapper_client.get_devices()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self._failures = min(self._failures + 1, 5)
            delay = min(30 * 2 ** (self._failures - 1), 300)
            if isinstance(err, aiohttp.ClientResponseError):
                # Respect a server's numeric Retry-After without logging payloads.
                try:
                    delay = max(delay, float((err.headers or {}).get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
            self.update_interval = timedelta(seconds=delay)
            raise UpdateFailed(
                f"Eldom read failed ({type(err).__name__}); retry in {delay:g}s"
            ) from err

        self._failures = 0
        self.update_interval = timedelta(seconds=30)
        return data

    async def async_shutdown(self) -> None:
        """Stop polling and release only this entry's network resources."""
        await super().async_shutdown()
        await self.eldom_wrapper_client.async_close()
