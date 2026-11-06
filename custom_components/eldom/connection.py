"""An entry-owned transport that can recover without restarting Home Assistant."""

import asyncio
from contextlib import AsyncExitStack

import aiohttp
from eldom.client import InvalidCredentialsError as EldomInvalidCredentialsError
from homeassistant.exceptions import ConfigEntryAuthFailed
from ioteldom.client import InvalidCredentialsError as IoTInvalidCredentialsError

from .const import IOT_ELDOM_API
from .eldom_client import EldomClientWrapper

FETCH_TIMEOUT = 25
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class EldomConnection:
    """Own a private connector; retry authentication only for read requests."""

    def __init__(self, username: str, password: str, api: str) -> None:
        self._username = username
        self._password = password
        self._api = api
        self._session: aiohttp.ClientSession | None = None
        self._transport: AsyncExitStack | None = None
        self._client: EldomClientWrapper | None = None
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def operation_lock(self) -> asyncio.Lock:
        """Serialize status polls, history transactions and existing commands."""
        return self._lock

    async def _async_client(self) -> EldomClientWrapper:
        if self._client is None:
            # Explicit options give AsyncResolver its own aiodns channel.
            # A default AsyncResolver shares a channel; ThreadedResolver still
            # depends on the event loop's executor. Neither isolates DNS here.
            self._transport = AsyncExitStack()
            resolver = aiohttp.AsyncResolver(timeout=2, tries=2)
            self._transport.push_async_callback(resolver.close)
            self._session = await self._transport.enter_async_context(aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=resolver,
                    limit=10,
                    limit_per_host=4,
                ),
                timeout=REQUEST_TIMEOUT,
            ))
            self._client = EldomClientWrapper(
                self._session, self._username, self._password, self._api, self._lock
            )
            if self._api == IOT_ELDOM_API:
                self._client.iot_eldom_client.token_provider.token = self._token
            await self._client.login()
        return self._client

    async def _async_reset(self, *, reset_auth: bool = False) -> None:
        if self._client is not None and self._api == IOT_ELDOM_API:
            self._token = self._client.iot_eldom_client.token_provider.token
        if reset_auth:
            self._token = None
        transport, self._transport = self._transport, None
        self._session = None
        self._client = None
        if transport is not None:
            await transport.aclose()

    async def get_devices(self) -> dict:
        """Bound the whole read, rotating a failed transport for the next poll.

        A rejected cached token permits one fresh-auth read. Network failures
        are left to the coordinator's backoff; device commands are never replayed.
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("Eldom connection is closed")
            try:
                async with asyncio.timeout(FETCH_TIMEOUT):
                    for attempt in range(2):
                        try:
                            client = await self._async_client()
                            return await client.get_devices()
                        except aiohttp.ClientResponseError as err:
                            if err.status not in (401, 403):
                                raise
                            await self._async_reset(reset_auth=True)
                            if attempt:
                                raise ConfigEntryAuthFailed(
                                    "Eldom rejected authentication; sign in again"
                                ) from err
            except (EldomInvalidCredentialsError, IoTInvalidCredentialsError) as err:
                await self._async_reset(reset_auth=True)
                raise ConfigEntryAuthFailed("Eldom rejected authentication") from err
            except BaseException:
                # Cancellation also releases the private session, then propagates.
                await self._async_reset()
                raise
        raise RuntimeError("Eldom read did not complete")

    async def async_close(self) -> None:
        """Prevent new reads and close the entry-owned transport on unload."""
        self._closed = True
        async with self._lock:
            await self._async_reset(reset_auth=True)
