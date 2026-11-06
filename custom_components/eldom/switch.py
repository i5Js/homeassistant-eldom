"""Switch platform for Eldom integration."""

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_TYPE_FLAT_BOILER_ELDOM,
    DEVICE_TYPE_NATURELA_BOILER_ELDOM,
    DEVICE_TYPE_SMART_BOILER_ELDOM,
    DOMAIN,
    DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM,
)
from .control_entity import EldomConvectorControlEntity
from .coordinator import EldomCoordinator
from .eldom_boiler import EldomBoiler
from .models import EldomData

SWITCH_NAME = "Powerful"

CONTROL_SWITCHES = tuple(SwitchEntityDescription(
    key=key, translation_key=key, name=name, icon=icon,
    entity_category=EntityCategory.CONFIG,
) for key, name, icon in (
    ("child_lock", "Child lock", "mdi:lock"),
    ("antifreeze", "Antifreeze protection", "mdi:snowflake"),
    ("power_regulation", "Power regulation", "mdi:tune"),
    ("adaptive_display", "Automatic display dimming", "mdi:brightness-auto"),
    ("sleep", "Sleep mode", "mdi:power-sleep"),
))


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Eldom switch platform."""

    eldom_data: EldomData = hass.data[DOMAIN][config_entry.entry_id]

    if eldom_data.energy_coordinator is not None:
        async_add_entities([
            EldomConvectorConfigSwitch(heater, eldom_data.energy_coordinator,
                eldom_data.coordinator.eldom_wrapper_client, description)
            for heater in eldom_data.coordinator.data.get(DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, {}).values()
            for description in CONTROL_SWITCHES
        ])

    async_add_entities(
        EldomBoilerPowerfulModeSwitch(flat_boiler, eldom_data.coordinator)
        for flat_boiler in eldom_data.coordinator.data.get(
            DEVICE_TYPE_FLAT_BOILER_ELDOM, []
        ).values()
    )

    async_add_entities(
        EldomBoilerPowerfulModeSwitch(smart_boiler, eldom_data.coordinator)
        for smart_boiler in eldom_data.coordinator.data.get(
            DEVICE_TYPE_SMART_BOILER_ELDOM, []
        ).values()
    )

    async_add_entities(
        EldomBoilerPowerfulModeSwitch(naturela_boiler, eldom_data.coordinator)
        for naturela_boiler in eldom_data.coordinator.data.get(
            DEVICE_TYPE_NATURELA_BOILER_ELDOM, []
        ).values()
    )


class EldomConvectorConfigSwitch(EldomConvectorControlEntity, SwitchEntity):
    @property
    def is_on(self):
        return self.feature_value

    async def async_turn_on(self, **kwargs):
        await self.async_set_control_value(True)

    async def async_turn_off(self, **kwargs):
        await self.async_set_control_value(False)


class EldomBoilerPowerfulModeSwitch(SwitchEntity, CoordinatorEntity):
    """Representation of Eldom powerful switch."""

    def __init__(
        self, eldom_boiler: EldomBoiler, coordinator: EldomCoordinator
    ) -> None:
        """Initialize an Eldom powerful control."""
        super().__init__(coordinator)

        self._eldom_boiler = eldom_boiler

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this water heater."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._eldom_boiler.device_id)},
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._eldom_boiler.device_id}-powerful-switch"

    @property
    def name(self) -> str:
        """Return the name of the powerful mode switch."""
        return f"{self._eldom_boiler.name}'s {SWITCH_NAME} Switch"

    @property
    def icon(self) -> str:
        """Return the icon of the powerful mode switch."""
        return "mdi:rocket-launch"

    @property
    def device_class(self) -> SwitchDeviceClass:
        """Return the device class of the powerful mode switch."""
        return SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool:
        """Return the powerful status."""
        return self._eldom_boiler.powerful_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn powerful mode on."""
        await self._eldom_boiler.enable_powerful_mode()
        self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn powerful mode off by cycling off then restoring the previous mode."""
        await self._eldom_boiler.disable_powerful_mode()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._eldom_boiler = self.coordinator.data.get(self._eldom_boiler.type).get(
            self._eldom_boiler.id
        )

        self.async_write_ha_state()
