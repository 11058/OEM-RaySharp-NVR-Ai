"""Button platform for RaySharp NVR.

Provides buttons for:
- NVR Reboot
- Per-channel PTZ presets (go to preset)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_client import RaySharpNVRConnectionError
from .const import (
    API_REBOOT,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DOMAIN,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpEntity

_LOGGER = logging.getLogger(__name__)


def _get_channel_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract channel list from coordinator data."""
    channel_data = data.get(DATA_CHANNEL_INFO)
    if not channel_data:
        return []
    if isinstance(channel_data, dict):
        channels = channel_data.get("channels", channel_data.get("channel", []))
    elif isinstance(channel_data, list):
        channels = channel_data
    else:
        return []
    if not isinstance(channels, list):
        channels = [channels]
    return channels


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR buttons."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []

    # NVR reboot button
    entities.append(RaySharpRebootButton(coordinator))
    async_add_entities(entities)

    # One button per saved PTZ preset: "CAM01 На ворота". Names come from the
    # camera (set them where the preset is saved); new presets appear on the fly.
    mac = (coordinator.data.get(DATA_DEVICE_INFO, {}) or {}).get("mac_addr", "unknown")
    known: set[tuple[int, int]] = set()

    def _add(channel: int) -> None:
        new = []
        for p in coordinator.ptz_presets.get(channel, []):
            key = (channel, p["no"])
            if p["set"] and key not in known:
                known.add(key)
                new.append(RaySharpPresetButton(coordinator, mac, channel, p["no"]))
        if new:
            async_add_entities(new)

    coordinator.preset_listeners.append(_add)
    for ch in coordinator.ptz_channels():
        try:
            await coordinator.async_refresh_presets(ch)
        except Exception as err:  # noqa: BLE001 - e.g. no PTZ right for this account yet
            _LOGGER.debug("PTZ presets CH%s not read: %s", ch, err)


class RaySharpPresetButton(ButtonEntity):
    """Turn a PTZ camera to one of its saved presets (pan, tilt and zoom)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:crosshairs-gps"
    _attr_should_poll = False

    def __init__(self, coordinator: RaySharpNVRCoordinator, mac: str, channel: int, no: int) -> None:
        self.coordinator = coordinator
        self._ch = channel
        self._no = no
        self._attr_unique_id = f"{mac}_ch{channel}_preset{no}"
        from homeassistant.helpers.device_registry import DeviceInfo  # noqa: PLC0415

        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, f"{mac}_ch{channel}")})

    def _preset(self) -> dict[str, Any] | None:
        return next((p for p in self.coordinator.ptz_presets.get(self._ch, []) if p["no"] == self._no), None)

    @property
    def name(self) -> str:
        p = self._preset()
        return (p or {}).get("name") or f"Точка {self._no}"

    @property
    def available(self) -> bool:
        p = self._preset()
        return bool(p and p["set"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"preset": self._no, "channel": self._ch}

    async def async_added_to_hass(self) -> None:
        def _changed(channel: int) -> None:
            if channel == self._ch and self.hass:
                self.async_write_ha_state()

        self.coordinator.preset_listeners.append(_changed)
        self.async_on_remove(lambda: self.coordinator.preset_listeners.remove(_changed))

    async def async_press(self) -> None:
        await self.coordinator.async_preset(self._ch, "call", self._no)


class RaySharpRebootButton(RaySharpEntity, ButtonEntity):
    """Button to reboot the NVR device."""

    _attr_translation_key = "reboot"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: RaySharpNVRCoordinator) -> None:
        """Initialize the reboot button."""
        super().__init__(coordinator)
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_reboot"

    async def async_press(self) -> None:
        """Initiate NVR reboot."""
        try:
            await self.coordinator.client.async_api_call(API_REBOOT, {})
            _LOGGER.info(
                "NVR reboot initiated for %s",
                self.coordinator.config_entry.data.get("host", "unknown"),
            )
        except RaySharpNVRConnectionError as err:
            # Connection error is expected after reboot starts
            _LOGGER.info("NVR reboot command sent (connection may drop): %s", err)
        except Exception as err:
            _LOGGER.error("Unexpected error during NVR reboot: %s", err)
