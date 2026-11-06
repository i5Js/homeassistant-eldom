"""Common availability and identity for read-only convector configuration."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN


class EldomConvectorFeatureEntity(CoordinatorEntity):
    """One reviewed field on the existing heater, without optimistic values."""

    _attr_has_entity_name = True

    def __init__(self, heater, coordinator, description):
        super().__init__(coordinator)
        self._device_id = heater.device_id
        self.entity_description = description
        self._attr_unique_id = f"{heater.device_id}-{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, heater.device_id)})

    @property
    def snapshot(self):
        return (self.coordinator.data or {}).get(self._device_id, {})

    @property
    def features(self):
        return self.snapshot.get("features", {})

    @property
    def feature_value(self):
        return self.features.get(self.entity_description.key)

    @property
    def available(self):
        stamp = dt_util.parse_datetime(self.snapshot.get("retrieved_at", ""))
        return bool(
            super().available and stamp and stamp.tzinfo
            and 0 <= (dt_util.utcnow() - stamp).total_seconds() < 1800
            and self.feature_value is not None
        )

    @property
    def extra_state_attributes(self):
        key = self.entity_description.key
        if key == "schedule_enabled_intervals":
            return {"schedule": self.features.get("weekly_schedule", []),
                    "scheduler_type": self.features.get("scheduler_type"),
                    "complete": self.features.get("schedule_complete", False)}
        if key == "language":
            return {"supported_languages": self.features.get("supported_languages", [])}
        if key == "estimate_reference_power":
            return {"power_class": self.snapshot.get("power_class"),
                    "estimate_factor": self.snapshot.get("estimate_factor")}
        return None
