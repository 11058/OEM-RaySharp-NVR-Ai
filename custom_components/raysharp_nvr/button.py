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
