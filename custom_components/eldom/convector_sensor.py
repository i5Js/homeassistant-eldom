"""Read-only programme, display and diagnostic descriptions."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory, UnitOfPower

from .convector_entity import EldomConvectorFeatureEntity


SENSOR_FEATURES = (
    SensorEntityDescription(key="schedule_enabled_intervals", translation_key="schedule_enabled_intervals",
                            name="Weekly schedule enabled intervals", icon="mdi:calendar-clock"),
    SensorEntityDescription(key="open_window_mode", translation_key="open_window_mode",
                            name="Open window detection mode", device_class=SensorDeviceClass.ENUM,
                            options=["off", "on", "smart"], icon="mdi:window-open-variant"),
    *(SensorEntityDescription(key=key, translation_key=key, name=name, icon=icon,
                             entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False)
      for key, name, icon in (
          ("display_brightness", "Display brightness level", "mdi:brightness-6"),
          ("language", "Display language", "mdi:translate"),
          ("firmware_version", "Firmware version", "mdi:chip"),
          ("hardware_version", "Hardware version", "mdi:chip"),
          ("day_tariff_start", "Day tariff start", "mdi:clock-outline"),
          ("night_tariff_start", "Night tariff start", "mdi:clock-outline"),
      )),
    SensorEntityDescription(key="scheduler_type", translation_key="scheduler_type", name="Scheduler configuration",
                            device_class=SensorDeviceClass.ENUM, options=["timer", "weekly_program"],
                            entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False),
    SensorEntityDescription(key="estimate_reference_power", translation_key="estimate_reference_power",
                            name="Energy estimate reference power", native_unit_of_measurement=UnitOfPower.WATT,
                            icon="mdi:calculator", entity_category=EntityCategory.DIAGNOSTIC,
                            entity_registry_enabled_default=False),
)


class EldomConvectorFeatureSensor(EldomConvectorFeatureEntity, SensorEntity):
    """A reviewed configuration value; unknown protocol values stay unavailable."""

    @property
    def native_value(self):
        return self.feature_value
