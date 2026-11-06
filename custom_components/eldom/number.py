"""Verified display brightness on IoT convectors."""

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory

from .const import DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, DOMAIN
from .control_entity import EldomConvectorControlEntity

BRIGHTNESS = NumberEntityDescription(
    key="display_brightness", translation_key="display_brightness", name="Display brightness",
    native_min_value=1, native_max_value=10, native_step=1, mode=NumberMode.SLIDER,
    entity_category=EntityCategory.CONFIG, icon="mdi:brightness-6")


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    if data.energy_coordinator is not None:
        async_add_entities([EldomBrightnessNumber(heater, data.energy_coordinator,
                            data.coordinator.eldom_wrapper_client, BRIGHTNESS)
                            for heater in data.coordinator.data.get(DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, {}).values()])


class EldomBrightnessNumber(EldomConvectorControlEntity, NumberEntity):
    @property
    def native_value(self):
        return self.feature_value

    async def async_set_native_value(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ValueError("Brightness must be an integer from 1 to 10")
        await self.async_set_control_value(int(value))
