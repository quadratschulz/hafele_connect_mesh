"""Platform for light integration."""
from __future__ import annotations

import logging
from typing import Any
from datetime import timedelta
import asyncio

import aiohttp

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)

MIN_KELVIN = 2700
MAX_KELVIN = 5000

# Static color mode per device type as determined by the config flow.
# HomeKit builds its accessories from supported_color_modes at startup, so
# these must not depend on whether the first status poll has succeeded yet.
DEVICE_TYPE_COLOR_MODES = {
    "temperature": ColorMode.COLOR_TEMP,
    "rgb": ColorMode.HS,
    "brightness": ColorMode.BRIGHTNESS,
    "switch": ColorMode.ONOFF,
}

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Connect Mesh light platform."""
    api_client = hass.data[DOMAIN][entry.entry_id]["api_client"]
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    
    async def async_update_data():
        """Fetch data from API."""
        data = {}
        last_error = None
        for device in devices:
            try:
                data[device["uniqueId"]] = await api_client.get_device_status(device["uniqueId"])
            except (HomeAssistantError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                # Mark only this device as unavailable; others may still respond.
                data[device["uniqueId"]] = None
                last_error = err
        if devices and last_error is not None and all(v is None for v in data.values()):
            raise UpdateFailed(f"Connect Mesh API unreachable: {last_error}")
        return data

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="light",
        update_method=async_update_data,
        update_interval=timedelta(seconds=30),
    )

    await coordinator.async_refresh()

    entities = []
    for device in devices:
        entities.append(ConnectMeshLight(coordinator, api_client, device))
    
    async_add_entities(entities)

class ConnectMeshLight(CoordinatorEntity, LightEntity):
    """Representation of a Connect Mesh Light."""

    def __init__(self, coordinator, api_client, device):
        """Initialize a Connect Mesh Light."""
        super().__init__(coordinator)
        self._api_client = api_client
        self._device = device
        self._attr_unique_id = device["uniqueId"]
        self._attr_name = device["name"]
        color_mode = DEVICE_TYPE_COLOR_MODES.get(device.get("type"), ColorMode.BRIGHTNESS)
        self._attr_supported_color_modes = {color_mode}
        self._attr_color_mode = color_mode
        self._attr_min_color_temp_kelvin = MIN_KELVIN
        self._attr_max_color_temp_kelvin = MAX_KELVIN
        self._state = None
        self._brightness = None
        self._color_temp = None
        self._hs_color = None
        self._last_known_brightness = None
        self._pending_update = False

    async def async_added_to_hass(self) -> None:
        """Apply the initial coordinator data when the entity is added."""
        await super().async_added_to_hass()
        self._update_attributes()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not self._pending_update:
            self._update_attributes()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return False if the last poll failed for this device."""
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.get(self._attr_unique_id) is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    @property
    def brightness(self) -> int | None:
        """Return the brightness of this light between 0..255."""
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the CT color value in Kelvin."""
        return self._color_temp

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the hs color value."""
        return self._hs_color

    def _snapshot(self) -> tuple:
        return (self._state, self._brightness, self._last_known_brightness, self._color_temp, self._hs_color)

    def _restore(self, snapshot: tuple) -> None:
        (self._state, self._brightness, self._last_known_brightness,
         self._color_temp, self._hs_color) = snapshot

    async def _apply_optimistically(self, update, call) -> None:
        """Apply state optimistically, rolling back if the API call fails."""
        snapshot = self._snapshot()
        self._pending_update = True
        update()
        try:
            await call()
        except (HomeAssistantError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            self._restore(snapshot)
            self._pending_update = False
            self.async_write_ha_state()
            raise HomeAssistantError(f"Could not control {self.name}: {err}") from err
        self.async_write_ha_state()
        await self._async_update_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        ha_brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness or self._last_known_brightness or 255)
        lightness = self._ha_to_api_brightness(ha_brightness)  # 0.0..1.0

        kelvin = None
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = max(MIN_KELVIN, min(MAX_KELVIN, int(kwargs[ATTR_COLOR_TEMP_KELVIN])))

        def update() -> None:
            self._state = True
            self._brightness = ha_brightness
            self._last_known_brightness = ha_brightness
            if kelvin is not None:
                self._color_temp = kelvin
            if ATTR_HS_COLOR in kwargs:
                self._hs_color = kwargs[ATTR_HS_COLOR]

        async def call() -> None:
            if kelvin is not None:
                await self._api_client.set_ctl(self._attr_unique_id, lightness, kelvin)
            elif ATTR_BRIGHTNESS in kwargs:
                await self._api_client.set_lightness(self._attr_unique_id, lightness)
            else:
                await self._api_client.set_power(self._attr_unique_id, True)

            if ATTR_HS_COLOR in kwargs:
                hue, saturation = kwargs[ATTR_HS_COLOR]
                await self._api_client.set_hue_saturation(
                    self._attr_unique_id, hue / 360 * 65535, saturation / 100
                )

        await self._apply_optimistically(update, call)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        def update() -> None:
            self._state = False

        await self._apply_optimistically(
            update, lambda: self._api_client.set_power(self._attr_unique_id, False)
        )

    @staticmethod
    def _api_to_ha_brightness(value: int) -> int:
        """Convert API brightness (0-65535) to HA brightness (0-255)."""
        return round((value / 65535) * 255)

    @staticmethod
    def _ha_to_api_brightness(value: int) -> float:
        """Convert HA brightness (0-255) to API brightness (0-1)."""
        return value / 255

    def _update_attributes(self):
        """Update attributes based on the latest data from the coordinator."""
        if self.coordinator.data is None:
            return
        device_data = self.coordinator.data.get(self._attr_unique_id) or {}
        state = device_data.get("state", {})
        if not state:
            return
        self._state = state.get("power", False)
        
        if "lightness" in state:
            self._brightness = self._api_to_ha_brightness(state["lightness"])
            if self._state:
                self._last_known_brightness = self._brightness

        if "temperature" in state:
            self._color_temp = state["temperature"]
        if "hue" in state and "saturation" in state:
            hue = state["hue"] / 65535 * 360
            saturation = state["saturation"] / 65535 * 100
            self._hs_color = (hue, saturation)

    async def _async_update_ha_state(self):
        """Update Home Assistant state and clear pending update flag."""
        await asyncio.sleep(1)  # Short delay to allow API to process the change
        self._pending_update = False
        await self.coordinator.async_request_refresh()
