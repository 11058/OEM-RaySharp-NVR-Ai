"""Switch platform for RaySharp NVR.

Provides switches for:
- Global alarm disarming (arm/disarm all NVR alerts)
- Per-channel motion alarm recording enable
- Per-channel intelligent alarm enable (face, line crossing, intrusion)
- PIR alarm enable
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_client import RaySharpNVRConnectionError
from .const import (
    API_ALARM_FD_SET,
    API_ALARM_LCD_SET,
    API_ALARM_PID_SET,
    API_DISARMING_SET,
    API_MOTION_ALARM_SET,
    DATA_ALARM_FD,
    DATA_ALARM_LCD,
    DATA_ALARM_PID,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DATA_DISARMING,
    DATA_MOTION_ALARM,
    DOMAIN,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, RaySharpEntity, channel_num_from_str

_LOGGER = logging.getLogger(__name__)


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


def _get_channel_alarm_value(
    alarm_data: Any, channel_num: int, field: str
) -> bool | None:
    """Get a boolean alarm field value for a specific channel.

    Alarm configs use "CH{n}" keys in channel_info dict.
    """
    if not isinstance(alarm_data, dict):
        return None
    channel_info = alarm_data.get("channel_info", {})
    if not isinstance(channel_info, dict):
        return None
    ch_key = f"CH{channel_num}"
    ch_data = channel_info.get(ch_key, {})
    if not isinstance(ch_data, dict):
        return None
    value = ch_data.get(field)
    if value is None:
        return None
    return bool(value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR switches."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []

    # ── Global alarm disarming switch ────────────────────────────────────────
    if coordinator.data.get(DATA_DISARMING) is not None:
        entities.append(RaySharpDisarmingSwitch(coordinator))

    # ── Per-channel switches ─────────────────────────────────────────────────
    channels = _get_channel_list(coordinator.data)
    for i, channel in enumerate(channels):
        channel_num = channel_num_from_str(channel.get("channel", ""), i + 1)
        channel_name = channel.get("channel_name", f"Channel {channel_num}")

        # Motion alarm recording enable
        if coordinator.data.get(DATA_MOTION_ALARM) is not None:
            entities.append(
                RaySharpMotionAlarmSwitch(
                    coordinator,
                    channel_num=channel_num,
                    channel_name=channel_name,
                )
            )

        # Face detection alarm enable
        if coordinator.data.get(DATA_ALARM_FD) is not None:
            entities.append(
                RaySharpIntelligentAlarmSwitch(
                    coordinator,
                    channel_num=channel_num,
                    channel_name=channel_name,
                    alarm_data_key=DATA_ALARM_FD,
                    alarm_set_endpoint=API_ALARM_FD_SET,
                    key_suffix="fd_alarm",
                    translation_key="fd_alarm",
                    icon="mdi:face-recognition",
                )
            )

        # Line crossing detection alarm enable
        if coordinator.data.get(DATA_ALARM_LCD) is not None:
            entities.append(
                RaySharpIntelligentAlarmSwitch(
                    coordinator,
                    channel_num=channel_num,
                    channel_name=channel_name,
                    alarm_data_key=DATA_ALARM_LCD,
                    alarm_set_endpoint=API_ALARM_LCD_SET,
                    key_suffix="lcd_alarm",
                    translation_key="lcd_alarm",
                    icon="mdi:motion-sensor",
                )
            )

        # Perimeter intrusion detection alarm enable
        if coordinator.data.get(DATA_ALARM_PID) is not None:
            entities.append(
                RaySharpIntelligentAlarmSwitch(
                    coordinator,
                    channel_num=channel_num,
                    channel_name=channel_name,
                    alarm_data_key=DATA_ALARM_PID,
                    alarm_set_endpoint=API_ALARM_PID_SET,
                    key_suffix="pid_alarm",
                    translation_key="pid_alarm",
                    icon="mdi:shield-alert",
                )
            )

    async_add_entities(entities)


class RaySharpDisarmingSwitch(RaySharpEntity, SwitchEntity):
    """Switch to enable/disable global NVR alarm disarming.

    When ON: alarms are disarmed (notifications suppressed).
    When OFF: alarms are active (default).
    """

    _attr_translation_key = "alarm_disarming"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bell-off"

    def __init__(self, coordinator: RaySharpNVRCoordinator) -> None:
        """Initialize the disarming switch."""
        super().__init__(coordinator)
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_alarm_disarming"

    @property
    def is_on(self) -> bool | None:
        """Return True if alarms are currently disarmed."""
        disarming_data = self.coordinator.data.get(DATA_DISARMING)
        if disarming_data is None:
            return None
        if isinstance(disarming_data, dict):
            return bool(disarming_data.get("disarming", False))
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Disarm all alarms (suppress notifications)."""
        await self._set_disarming(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Arm all alarms (enable notifications)."""
        await self._set_disarming(False)

    async def _set_disarming(self, disarmed: bool) -> None:
        """Send disarming state to NVR, preserving all other settings."""
        current = self.coordinator.data.get(DATA_DISARMING)
        if isinstance(current, dict):
            # Preserve existing action/channel settings
            payload = {**current, "disarming": disarmed}
        else:
            payload = {"disarming": disarmed}

        try:
            await self.coordinator.client.async_api_call(API_DISARMING_SET, payload)
        except RaySharpNVRConnectionError as err:
            _LOGGER.error("Failed to set alarm disarming: %s", err)
            return
        await self.coordinator.async_request_refresh()


class RaySharpMotionAlarmSwitch(RaySharpChannelEntity, SwitchEntity):
    """Switch to enable/disable motion alarm recording for a channel."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:motion-sensor"
    _attr_translation_key = "motion_alarm_recording"

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
    ) -> None:
        """Initialize the motion alarm switch."""
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_ch{channel_num}_motion_alarm"

    @property
    def is_on(self) -> bool | None:
        """Return True if motion alarm recording is enabled for this channel."""
        motion_data = self.coordinator.data.get(DATA_MOTION_ALARM)
        return _get_channel_alarm_value(motion_data, self._channel_num, "record_enable")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable motion alarm recording."""
        await self._set_channel_field("record_enable", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable motion alarm recording."""
        await self._set_channel_field("record_enable", False)

    async def _set_channel_field(self, field: str, value: Any) -> None:
        """Update a single field in this channel's motion alarm config."""
        motion_data = self.coordinator.data.get(DATA_MOTION_ALARM)
        if not isinstance(motion_data, dict):
            _LOGGER.error("No motion alarm data available for channel %d", self._channel_num)
            return

        channel_info = motion_data.get("channel_info", {})
        ch_key = f"CH{self._channel_num}"
        ch_data = channel_info.get(ch_key, {})

        if not ch_data:
            _LOGGER.warning("Channel %s not found in motion alarm config", ch_key)
            return

        # Build payload with just this channel's config updated
        updated_ch = {**ch_data, field: value}
        payload = {"channel_info": {ch_key: updated_ch}}

        try:
            await self.coordinator.client.async_api_call(API_MOTION_ALARM_SET, payload)
        except RaySharpNVRConnectionError as err:
            _LOGGER.error("Failed to set motion alarm for channel %d: %s", self._channel_num, err)
            return
        await self.coordinator.async_request_refresh()


class RaySharpIntelligentAlarmSwitch(RaySharpChannelEntity, SwitchEntity):
    """Generic switch for intelligent alarm recording enable/disable per channel."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
        alarm_data_key: str,
        alarm_set_endpoint: str,
        key_suffix: str,
        translation_key: str,
        icon: str = "mdi:bell",
    ) -> None:
        """Initialize the intelligent alarm switch."""
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        self._alarm_data_key = alarm_data_key
        self._alarm_set_endpoint = alarm_set_endpoint
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_ch{channel_num}_{key_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon

    @property
    def is_on(self) -> bool | None:
        """Return True if alarm recording is enabled for this channel."""
        alarm_data = self.coordinator.data.get(self._alarm_data_key)
        return _get_channel_alarm_value(alarm_data, self._channel_num, "record_enable")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable alarm recording."""
        await self._set_channel_field("record_enable", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable alarm recording."""
        await self._set_channel_field("record_enable", False)

    async def _set_channel_field(self, field: str, value: Any) -> None:
        """Update a single field in this channel's alarm config."""
        alarm_data = self.coordinator.data.get(self._alarm_data_key)
        if not isinstance(alarm_data, dict):
            _LOGGER.error(
                "No alarm data available for %s channel %d",
                self._alarm_data_key,
                self._channel_num,
            )
            return

        channel_info = alarm_data.get("channel_info", {})
        ch_key = f"CH{self._channel_num}"
        ch_data = channel_info.get(ch_key, {})

        if not ch_data:
            _LOGGER.warning(
                "Channel %s not found in %s config", ch_key, self._alarm_data_key
            )
            return

        updated_ch = {**ch_data, field: value}
        payload = {"channel_info": {ch_key: updated_ch}}

        try:
            await self.coordinator.client.async_api_call(
                self._alarm_set_endpoint, payload
            )
        except RaySharpNVRConnectionError as err:
            _LOGGER.error(
                "Failed to set %s for channel %d: %s",
                self._alarm_data_key,
                self._channel_num,
                err,
            )
            return
        await self.coordinator.async_request_refresh()
