"""Platform for Eldom convector heater integration."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.service import async_register_platform_entity_service
from homeassistant.helpers import config_validation as cv

from .const import (
    DEVICE_TYPE_CONVECTOR_HEATER_ELDOM,
    DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM,
    DEVICE_TYPE_MAPPING,
    DOMAIN,
    MANUFACTURER_NAME,
)
from .coordinator import EldomCoordinator
from .eldom_convector import EldomConvectorHeater, IoTEldomConvectorHeater
from .models import EldomData
from .weekly_program import minute_time, slot_number

SUPPORT_FLAGS_CLIMATE = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)

TEMP_UNIT = UnitOfTemperature.CELSIUS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Eldom convector heater setup."""

    eldom_data: EldomData = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        EldomConvectorHeaterEntity(convector_heater, eldom_data.coordinator)
        for convector_heater in eldom_data.coordinator.data.get(
            DEVICE_TYPE_CONVECTOR_HEATER_ELDOM
        ).values()
    )

    async_add_entities(
        IoTEldomConvectorHeaterEntity(convector_heater, eldom_data.coordinator, getattr(eldom_data, "weekly_manager", None))
        for convector_heater in eldom_data.coordinator.data.get(
            DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM
        ).values()
    )

    for name, schema, method in (
        ("get_weekly_schedule", {}, "async_get_weekly_schedule"),
        ("activate_weekly_program", {vol.Optional("enabled_intervals"): vol.All(cv.ensure_list, [slot_number])}, "async_activate_weekly_program"),
        ("set_weekly_interval", {
            vol.Required("slot"): slot_number,
            vol.Optional("enabled"): cv.boolean,
            vol.Optional("weekdays"): [cv.string],
            vol.Optional("start"): minute_time,
            vol.Optional("end"): minute_time,
            vol.Optional("temperature"): vol.Coerce(float),
        }, "async_set_weekly_interval"),
    ):
        if not hass.services.has_service(DOMAIN, name):
            async_register_platform_entity_service(hass, DOMAIN, name, entity_domain="climate",
                schema=schema, func=method, supports_response=SupportsResponse.OPTIONAL)


class WeeklyProgrammeServices:
    async def _weekly_action(self, operation, **kwargs):
        manager = getattr(self, "_weekly_manager", None)
        if manager is None:
            raise HomeAssistantError("Weekly programming is available only for supported IoT convectors")
        return await manager.execute(self._convector_heater, operation, **kwargs)

    async def async_get_weekly_schedule(self):
        return await self._weekly_action("read")

    async def async_activate_weekly_program(self, enabled_intervals=None):
        return await self._weekly_action("auto", selected_slots=enabled_intervals)

    async def async_set_weekly_interval(self, slot, **changes):
        return await self._weekly_action("edit", slot=slot, changes=changes)


class EldomConvectorHeaterEntity(WeeklyProgrammeServices, ClimateEntity, CoordinatorEntity):
    """Representation of an Eldom convector heater.

    The CoordinatorEntity class provides:
        should_poll
        async_update
        async_added_to_hass
        available
    """

    def __init__(
        self, convector_heater: EldomConvectorHeater, coordinator: EldomCoordinator
    ) -> None:
        """Initialize an Eldom convector heater."""
        super().__init__(coordinator)

        self._convector_heater = convector_heater

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this convector heater."""
        return DeviceInfo(
            name=self._convector_heater.name,
            identifiers={(DOMAIN, self._convector_heater.device_id)},
            manufacturer=MANUFACTURER_NAME,
            model=DEVICE_TYPE_MAPPING.get(self._convector_heater.type),
            sw_version=str(self._convector_heater.software_version),
            hw_version=str(self._convector_heater.hardware_version),
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._convector_heater.device_id

    @property
    def name(self) -> str:
        """Return the name of the convector heater."""
        return self._convector_heater.name

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the list of supported features."""
        return SUPPORT_FLAGS_CLIMATE

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return TEMP_UNIT

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return self._convector_heater.max_temperature

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return self._convector_heater.min_temperature

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._convector_heater.current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._convector_heater.target_temperature

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current operation ie. Off or Heating."""
        return self._convector_heater.current_operation

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        return self._convector_heater.operation_modes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the convector heater on."""
        await self._convector_heater.turn_on()
        self.schedule_update_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the convector heater off."""
        await self._convector_heater.turn_off()
        self.schedule_update_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target operation mode."""
        try:
            await self._convector_heater.set_operation_mode(hvac_mode)
            self.schedule_update_ha_state()
            await self.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Error while setting operation mode: %s", e)
            raise HomeAssistantError("Error while setting operation mode") from e

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get("temperature")
        await self._convector_heater.set_temperature(temperature)
        self.schedule_update_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._convector_heater = self.coordinator.data.get(
            self._convector_heater.type
        ).get(self._convector_heater.id)

        self.async_write_ha_state()


class IoTEldomConvectorHeaterEntity(WeeklyProgrammeServices, ClimateEntity, CoordinatorEntity):
    """Representation of an Eldom convector heater.

    The CoordinatorEntity class provides:
        should_poll
        async_update
        async_added_to_hass
        available
    """

    def __init__(
        self, convector_heater: IoTEldomConvectorHeater, coordinator: EldomCoordinator, weekly_manager=None
    ) -> None:
        """Initialize an Eldom convector heater."""
        super().__init__(coordinator)

        self._convector_heater = convector_heater
        self._weekly_manager = weekly_manager

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this convector heater."""
        return DeviceInfo(
            name=self._convector_heater.name,
            identifiers={(DOMAIN, self._convector_heater.device_id)},
            manufacturer=MANUFACTURER_NAME,
            model=DEVICE_TYPE_MAPPING.get(self._convector_heater.type),
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._convector_heater.device_id

    @property
    def name(self) -> str:
        """Return the name of the convector heater."""
        return self._convector_heater.name

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the list of supported features."""
        return SUPPORT_FLAGS_CLIMATE

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return TEMP_UNIT

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return self._convector_heater.max_temperature

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return self._convector_heater.min_temperature

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._convector_heater.current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._convector_heater.target_temperature

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current operation ie. Off or Heating."""
        return self._convector_heater.current_operation

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        if self._weekly_manager is not None:
            return [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
        return self._convector_heater.operation_modes

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Keep the manufacturer's programme and raw code visible."""
        return {
            "eldom_operation_code": self._convector_heater.operation_code,
            "eldom_operation_mode": self._convector_heater.operation_name,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the convector heater on."""
        if self._weekly_manager is not None:
            await self._weekly_action("resume")
            return
        await self._convector_heater.turn_on()
        self.schedule_update_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the convector heater off."""
        if self._weekly_manager is not None:
            await self._weekly_action("off")
            return
        await self._convector_heater.turn_off()
        self.schedule_update_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target operation mode."""
        if self._weekly_manager is not None:
            if hvac_mode not in (HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO):
                raise HomeAssistantError("Unsupported operating mode")
            await self._weekly_action({HVACMode.OFF: "off", HVACMode.HEAT: "heat", HVACMode.AUTO: "auto"}[hvac_mode])
            return
        try:
            await self._convector_heater.set_operation_mode(hvac_mode)
            self.schedule_update_ha_state()
            await self.coordinator.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Error while setting operation mode: %s", e)
            raise HomeAssistantError("Error while setting operation mode") from e

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get("temperature")
        await self._convector_heater.set_temperature(temperature)
        self.schedule_update_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._convector_heater = self.coordinator.data.get(
            self._convector_heater.type
        ).get(self._convector_heater.id)

        self.async_write_ha_state()
