"""Verified window detection mode on IoT convectors."""

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory

from .const import DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, DOMAIN
from .control_entity import EldomConvectorControlEntity

WINDOW_MODE = SelectEntityDescription(
    key="open_window_mode", translation_key="open_window_mode", name="Window detection",
    options=["off", "on", "smart"], entity_category=EntityCategory.CONFIG,
    icon="mdi:window-open-variant")


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    if data.energy_coordinator is not None:
        async_add_entities([EldomWindowSelect(heater, data.energy_coordinator,
                            data.coordinator.eldom_wrapper_client, WINDOW_MODE)
                            for heater in data.coordinator.data.get(DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, {}).values()])


class EldomWindowSelect(EldomConvectorControlEntity, SelectEntity):
    @property
    def current_option(self):
        return self.feature_value

    async def async_select_option(self, option):
        await self.async_set_control_value(option)
