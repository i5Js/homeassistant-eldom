"""Publish confirmed configuration without resetting history timers."""

import asyncio

from homeassistant.exceptions import HomeAssistantError

from .convector_control import ControlNotAllowed, observed_values, validate_value
from .convector_entity import EldomConvectorFeatureEntity


class EldomConvectorControlEntity(EldomConvectorFeatureEntity):
    def __init__(self, heater, coordinator, connection, description):
        super().__init__(heater, coordinator, description)
        self._attr_unique_id = f"{heater.device_id}-control-{description.key}"
        self._control_device = heater._convector_heater_device
        self._connection = connection

    def _publish_confirmed(self, values):
        data = dict(self.coordinator.data or {})
        snapshot = dict(data.get(self._device_id, {}))
        snapshot["features"] = {**snapshot.get("features", {}), **values}
        data[self._device_id] = snapshot
        # Preserve retrieval age, failure status and the next history refresh.
        self.coordinator.data = data
        self.coordinator.async_update_listeners()

    async def async_set_control_value(self, value):
        key = self.entity_description.key
        validate_value(key, value)
        try:
            confirmed = await self._connection.async_set_convector_control(self._control_device, key, value)
        except ControlNotAllowed as error:
            raise HomeAssistantError(str(error)) from None
        except asyncio.CancelledError:
            self._publish_confirmed(observed_values(key, {}))
            raise
        except Exception as error:
            self._publish_confirmed(observed_values(key, {}))
            raise HomeAssistantError(f"Eldom control was not confirmed ({type(error).__name__})") from None
        self._publish_confirmed(confirmed)
