"""Read-only configuration flags; none represents live heating activity."""

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.const import EntityCategory

from .const import DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, DOMAIN
from .convector_entity import EldomConvectorFeatureEntity


BINARY_FEATURES = tuple(BinarySensorEntityDescription(
    key=key, translation_key=key, name=name, icon=icon,
    entity_category=EntityCategory.DIAGNOSTIC if diagnostic else None,
    entity_registry_enabled_default=not diagnostic,
) for key, name, icon, diagnostic in (
    ("child_lock", "Child lock enabled", "mdi:lock", False),
    ("antifreeze", "Antifreeze protection enabled", "mdi:snowflake", False),
    ("sleep", "Sleep mode enabled", "mdi:power-sleep", False),
    ("power_regulation", "Power regulation enabled", "mdi:tune", False),
    ("adaptive_display", "Automatic display dimming enabled", "mdi:brightness-auto", True),
    ("wifi_enabled", "Wi-Fi mode enabled", "mdi:wifi", True),
    ("ap_enabled", "Access point mode enabled", "mdi:access-point", True),
    ("automatic_clock", "Automatic clock synchronization enabled", "mdi:clock-check-outline", True),
))


async def async_setup_entry(hass, config_entry, async_add_entities):
    data = hass.data[DOMAIN][config_entry.entry_id]
    if data.energy_coordinator is None:
        return
    async_add_entities([
        EldomConvectorFeatureBinarySensor(heater, data.energy_coordinator, description)
        for heater in data.coordinator.data.get(DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, {}).values()
        for description in BINARY_FEATURES
    ])


class EldomConvectorFeatureBinarySensor(EldomConvectorFeatureEntity, BinarySensorEntity):
    """A configuration flag from its explicit API field, not a demand estimate."""

    @property
    def is_on(self):
        return self.feature_value
