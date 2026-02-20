"""Image platform for RaySharp NVR.

Provides one ImageEntity per channel that shows the latest AI detection
snapshot received via the NVR EventPush webhook (ai_snap_picture payload).
The image updates automatically whenever the NVR sends a new snapshot.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .const import DATA_CHANNEL_INFO, DATA_DEVICE_INFO, DOMAIN, EVENT_SNAPSHOT
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, channel_num_from_str


def _get_channel_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    channel_data = data.get(DATA_CHANNEL_INFO)
    if not channel_data:
        return []
    if isinstance(channel_data, dict):
        channels = channel_data.get("channel_param", {}).get("items", [])
        if not channels:
            channels = channel_data.get("channels", channel_data.get("channel", []))
    elif isinstance(channel_data, list):
        channels = channel_data
    else:
        return []
    return channels if isinstance(channels, list) else [channels]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR snapshot image entities."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]

    channels = _get_channel_list(coordinator.data)
    entities: list[RaySharpSnapshotImage] = []
    for i, channel in enumerate(channels):
        channel_num = channel_num_from_str(channel.get("channel", ""), i + 1)
        channel_name = channel.get("channel_name", f"Channel {channel_num}")
        entities.append(
            RaySharpSnapshotImage(coordinator, channel_num, channel_name)
        )

    async_add_entities(entities)


class RaySharpSnapshotImage(RaySharpChannelEntity, ImageEntity):
    """Image entity showing the latest AI detection snapshot for a channel.

    Listens for raysharp_nvr_snapshot events and updates when a new
    snapshot arrives for this channel.
    """

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
    ) -> None:
        """Initialize the snapshot image entity."""
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        ImageEntity.__init__(self, coordinator.hass)

        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_ch{channel_num}_snapshot"
        self._attr_translation_key = "last_detection"
        self._image_bytes: bytes | None = None
        self._attr_image_last_updated: datetime | None = None
        # Extra attributes visible in HA UI
        self._extra: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Register snapshot event listener."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SNAPSHOT, self._handle_snapshot)
        )

    @callback
    def _handle_snapshot(self, event: Any) -> None:
        """Handle incoming snapshot event from NVR webhook."""
        data = event.data
        if data.get("channel") != self._channel_num:
            return

        img_b64 = data.get("image", "")
        if img_b64:
            try:
                self._image_bytes = base64.b64decode(img_b64)
            except Exception:
                self._image_bytes = None

        self._attr_image_last_updated = dt_util.utcnow()
        self._extra = {
            k: v for k, v in data.items()
            if k not in ("image",) and v is not None
        }
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return the latest snapshot image bytes."""
        return self._image_bytes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes (alarm_type, snap_id, plate_number…)."""
        return self._extra
