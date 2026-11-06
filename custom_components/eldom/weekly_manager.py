"""HA persistence and publication for confirmed weekly-program operations."""

from collections.abc import Mapping

from homeassistant.exceptions import HomeAssistantError

from .const import DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM
from .convector_control import ControlNotAllowed, ControlNotConfirmed
from .convector_features import parse_features
from .weekly_program import base_mode, enabled_slots, interval_list, normalize_timers, validate_request

PROFILE_OPTION = "weekly_program_profiles"


class WeeklyProgramManager:
    def __init__(self, hass, entry, connection, main, auxiliary):
        self.hass, self.entry, self.connection = hass, entry, connection
        self.main, self.auxiliary = main, auxiliary

    def profile(self, device_id):
        profiles = self.entry.options.get(PROFILE_OPTION, {})
        try:
            return normalize_timers(profiles.get(device_id)) if isinstance(profiles, Mapping) else None
        except (ValueError, TypeError):
            return None

    def remember(self, device_id, timers):
        timers = normalize_timers(timers)
        options = dict(self.entry.options)
        current = options.get(PROFILE_OPTION, {})
        profiles = dict(current) if isinstance(current, Mapping) else {}
        if profiles.get(device_id) == timers:
            return
        profiles[device_id] = timers
        self.hass.config_entries.async_update_entry(self.entry, options={**options, PROFILE_OPTION: profiles})

    async def execute(self, heater, operation, **kwargs):
        device = heater._convector_heater_device
        try:
            validate_request(operation, kwargs.get("slot"), kwargs.get("changes"), kwargs.get("selected_slots"))
        except ValueError as error:
            raise HomeAssistantError(str(error)) from None
        try:
            result = await self.connection.async_weekly_program(
                device, operation, profile_provider=lambda: self.profile(heater.device_id),
                remember=lambda timers: self.remember(heater.device_id, timers), **kwargs)
        except Exception as error:
            # Read actual state after a potentially partial operation; no retry.
            await self.main.async_refresh()
            if isinstance(error, (ControlNotAllowed, ControlNotConfirmed)):
                raise HomeAssistantError(str(error)) from None
            raise HomeAssistantError(f"Weekly programme was not confirmed ({type(error).__name__})") from None
        current = self.main.data.get(DEVICE_TYPE_CONVECTOR_HEATER_IOT_ELDOM, {}).get(heater.id)
        if current is not None:
            details = current._convector_heater_details
            for key in ("Operation", "TSet", "T"):
                if key in result["status"]:
                    setattr(details, key, result["status"][key])
            self.main.async_update_listeners()
        if self.auxiliary is not None and result["timers"] is not None:
            data = dict(self.auxiliary.data or {})
            snapshot = dict(data.get(heater.device_id, {}))
            decoded = parse_features({"GetTimers": result["timers"], "GetParams": result["params"]})
            keys = ("weekly_schedule", "schedule_complete", "schedule_enabled_intervals", "scheduler_type")
            snapshot["features"] = {**snapshot.get("features", {}), **{key: decoded[key] for key in keys}}
            data[heater.device_id] = snapshot
            self.auxiliary.data = data
            self.auxiliary.async_update_listeners()
        profile = self.profile(heater.device_id)
        code = int(result["status"]["Operation"]) // 16
        return {"mode": {0: "off", 1: "manual", 2: "weekly_program", 3: "timer"}.get(code, "unknown"),
                "intervals": interval_list(result["timers"]) if result["timers"] is not None else [],
                "remembered_enabled_intervals": enabled_slots(profile) if profile else []}
