"""The Eldom integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .connection import EldomConnection
from .const import CONF_API, DOMAIN, DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM
from .coordinator import EldomCoordinator
from .energy import EldomEnergyCoordinator
from .models import EldomData
from .weekly_manager import WeeklyProgramManager

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Eldom from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    api = entry.data[CONF_API]

    client = EldomConnection(username, password, api)
    coordinator = EldomCoordinator(hass, client, entry)

    eldom_data = EldomData(coordinator)

    try:
        await coordinator.async_config_entry_first_refresh()
        devices = [heater._convector_heater_device for heater in coordinator.data.get(DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, {}).values()]
        if devices:
            eldom_data.energy_coordinator = EldomEnergyCoordinator(hass, entry, devices, client.operation_lock)
            eldom_data.weekly_manager = WeeklyProgramManager(hass, entry, client, coordinator, eldom_data.energy_coordinator)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = eldom_data
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if eldom_data.energy_coordinator:
            eldom_data.energy_coordinator.start(hass, entry)
    except BaseException:
        if eldom_data.energy_coordinator:
            await eldom_data.energy_coordinator.async_shutdown()
        await coordinator.async_shutdown()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    data = hass.data[DOMAIN].pop(entry.entry_id)
    if data.energy_coordinator:
        await data.energy_coordinator.async_shutdown()
    await data.coordinator.async_shutdown()
    return True
