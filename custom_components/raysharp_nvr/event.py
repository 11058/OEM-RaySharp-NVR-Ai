"""Event platform for RaySharp NVR.

Provides HA event entities that fire when the NVR pushes alarm events
via its EventPush HTTP mechanism.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ALARM_TYPE_FACE,
    ALARM_TYPE_INTRUSION,
    ALARM_TYPE_IO,
    ALARM_TYPE_LINE_CROSSING,
    ALARM_TYPE_MOTION,
    ALARM_TYPE_PERSON,
    ALARM_TYPE_PLATE,
    ALARM_TYPE_VEHICLE,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DOMAIN,
    EVENT_ALARM,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, channel_num_from_str

_LOGGER = logging.getLogger(__name__)

ALL_EVENT_TYPES = [
    ALARM_TYPE_MOTION,
    ALARM_TYPE_PERSON,
    ALARM_TYPE_VEHICLE,
    ALARM_TYPE_LINE_CROSSING,
    ALARM_TYPE_INTRUSION,
    ALARM_TYPE_FACE,
    ALARM_TYPE_PLATE,
    ALARM_TYPE_IO,
]


def _get_channel_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract channel list from coordinator data."""
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
    if not isinstance(channels, list):
        channels = [channels]
    return channels


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR event entities."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[RaySharpAlarmEvent] = []

    # Create one event entity per channel for alarm events
    channels = _get_channel_list(coordinator.data)
    for i, channel in enumerate(channels):
        channel_num = channel_num_from_str(channel.get("channel", ""), i + 1)
        channel_name = channel.get("channel_name", f"Channel {channel_num}")
        entities.append(
            RaySharpAlarmEvent(
                coordinator,
                channel_num=channel_num,
                channel_name=channel_name,
            )
        )

    # Also create an NVR-level event entity for system-wide alarms
    entities.append(RaySharpAlarmEvent(coordinator, channel_num=0, channel_name="NVR"))

    async_add_entities(entities)


class RaySharpAlarmEvent(RaySharpChannelEntity, EventEntity):
    """Event entity for RaySharp NVR alarm events.

    Fires when the NVR pushes an alarm event via the webhook.
    """

    _attr_device_class = EventDeviceClass.MOTION
    _attr_event_types = ALL_EVENT_TYPES

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, channel_num, channel_name)
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")

        if channel_num == 0:
            self._attr_unique_id = f"{mac}_alarm_event_nvr"
            self._attr_name = "NVR Alarm"
        else:
            self._attr_unique_id = f"{mac}_ch{channel_num}_alarm_event"
            self._attr_name = "Alarm"

    async def async_added_to_hass(self) -> None:
        """Register event listener when entity is added."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_ALARM, self._handle_alarm_event)
        )

    @callback
    def _handle_alarm_event(self, event: Any) -> None:
        """Handle an alarm event from the NVR webhook."""
        data = event.data
        event_channel = data.get("channel", 0)

        # Channel 0 entity receives all events, channel-specific entities
        # only receive events for their channel
        if self._channel_num != 0 and event_channel != self._channel_num:
            return

        alarm_type = data.get("alarm_type", ALARM_TYPE_MOTION)
        event_data = {
            "channel": event_channel,
            "alarm_type": alarm_type,
        }

        # Include any extra data from the event
        for key in ("timestamp", "details", "object_type", "zone", "confidence"):
            if key in data:
                event_data[key] = data[key]

        self._trigger_event(alarm_type, event_data)
        self.async_write_ha_state()
