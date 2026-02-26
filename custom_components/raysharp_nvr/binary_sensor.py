"""Binary sensor platform for RaySharp NVR."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    ALARM_TYPE_CROWD,
    ALARM_TYPE_FACE,
    ALARM_TYPE_INTRUSION,
    ALARM_TYPE_IO,
    ALARM_TYPE_LINE_CROSSING,
    ALARM_TYPE_MOTION,
    ALARM_TYPE_OCCLUSION,
    ALARM_TYPE_PERSON,
    ALARM_TYPE_PIR,
    ALARM_TYPE_PLATE,
    ALARM_TYPE_REGION_ENTRANCE,
    ALARM_TYPE_REGION_EXITING,
    ALARM_TYPE_SOD,
    ALARM_TYPE_SOUND,
    ALARM_TYPE_VEHICLE,
    ALARM_TYPE_WANDER,
    CONF_EVENT_TIMEOUT,
    DATA_AI_FD_SETUP,
    DATA_AI_INTRUSION_SETUP,
    DATA_AI_LCD_SETUP,
    DATA_AI_LPD_SETUP,
    DATA_AI_PVD_SETUP,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DATA_DISARMING,
    DEFAULT_EVENT_TIMEOUT,
    DOMAIN,
    EVENT_ALARM,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, RaySharpEntity, channel_num_from_str


@dataclass(frozen=True, kw_only=True)
class RaySharpBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a RaySharp binary sensor."""

    value_fn: Callable[[dict[str, Any]], bool | None]


NVR_BINARY_SENSORS: tuple[RaySharpBinarySensorDescription, ...] = (
    RaySharpBinarySensorDescription(
        key="nvr_connected",
        translation_key="nvr_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: True,
    ),
    RaySharpBinarySensorDescription(
        key="nvr_armed",
        translation_key="nvr_armed",
        device_class=BinarySensorDeviceClass.SAFETY,
        entity_category=EntityCategory.DIAGNOSTIC,
        # armed = NOT disarmed
        value_fn=lambda data: not bool(
            (data.get(DATA_DISARMING) or {}).get("disarming", False)
        ) if data.get(DATA_DISARMING) is not None else None,
    ),
)


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


def _get_detection_enabled(
    data: dict[str, Any], channel_num: int, alarm_type: str
) -> bool | None:
    """Check if an alarm/detection type is enabled on the NVR for a channel.

    Returns:
        True  – explicitly enabled in NVR config
        False – explicitly disabled (entity will start as disabled in HA)
        None  – config not available, default to enabled

    NVR setup data structure (FD/PVD/LCD/Intrusion):
        {"channel_info": {"CH17": {"switch": true/false, ...}}}
    """
    ch_key = f"CH{channel_num}"

    def _ch_switch(data_key: str) -> bool | None:
        setup = data.get(data_key)
        if not isinstance(setup, dict):
            return None
        ch_info = setup.get("channel_info", {})
        if not isinstance(ch_info, dict):
            return None
        ch = ch_info.get(ch_key, {})
        if not isinstance(ch, dict):
            return None
        switch = ch.get("switch")
        return bool(switch) if switch is not None else None

    if alarm_type in (ALARM_TYPE_PERSON, ALARM_TYPE_VEHICLE):
        # Person and Vehicle detection require AI PVD (Human & Vehicle Detection)
        return _ch_switch(DATA_AI_PVD_SETUP)
    if alarm_type == ALARM_TYPE_MOTION:
        # Basic motion detection works independently of AI PVD — always enabled
        return None
    if alarm_type == ALARM_TYPE_FACE:
        return _ch_switch(DATA_AI_FD_SETUP)
    if alarm_type == ALARM_TYPE_PLATE:
        # Try dedicated LPD setup first; fall back to FD setup if LPD returns 404
        lpd = _ch_switch(DATA_AI_LPD_SETUP)
        return lpd if lpd is not None else _ch_switch(DATA_AI_FD_SETUP)
    if alarm_type == ALARM_TYPE_LINE_CROSSING:
        return _ch_switch(DATA_AI_LCD_SETUP)
    if alarm_type == ALARM_TYPE_INTRUSION:
        return _ch_switch(DATA_AI_INTRUSION_SETUP)
    # SOD, sound, PIR, wander, region, occlusion, crowd, IO —
    # no per-channel config currently fetched → default to enabled
    return None


# (alarm_type, key_suffix, translation_key, device_class)
EVENT_BINARY_SENSOR_TYPES: list[tuple[str, str, str, BinarySensorDeviceClass]] = [
    (ALARM_TYPE_MOTION, "motion_detected", "motion_detected", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_PERSON, "person_detected", "person_detected", BinarySensorDeviceClass.OCCUPANCY),
    (ALARM_TYPE_VEHICLE, "vehicle_detected", "vehicle_detected", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_LINE_CROSSING, "line_crossing", "line_crossing", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_INTRUSION, "intrusion", "intrusion", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_FACE, "face_detected", "face_detected", BinarySensorDeviceClass.OCCUPANCY),
    (ALARM_TYPE_PLATE, "plate_detected", "plate_detected", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_IO, "io_alarm", "io_alarm", BinarySensorDeviceClass.TAMPER),
    (ALARM_TYPE_SOD, "stationary_object", "stationary_object", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_SOUND, "sound_detected", "sound_detected", BinarySensorDeviceClass.SOUND),
    (ALARM_TYPE_WANDER, "wander_detected", "wander_detected", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_REGION_ENTRANCE, "region_entrance", "region_entrance", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_REGION_EXITING, "region_exiting", "region_exiting", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_OCCLUSION, "occlusion_detected", "occlusion_detected", BinarySensorDeviceClass.PROBLEM),
    (ALARM_TYPE_PIR, "pir_detected", "pir_detected", BinarySensorDeviceClass.MOTION),
    (ALARM_TYPE_CROWD, "crowd_detected", "crowd_detected", BinarySensorDeviceClass.OCCUPANCY),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR binary sensors."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]
    event_timeout = entry.options.get(CONF_EVENT_TIMEOUT, DEFAULT_EVENT_TIMEOUT)
    entities: list[BinarySensorEntity] = []

    # NVR-level binary sensors
    for description in NVR_BINARY_SENSORS:
        entities.append(RaySharpBinarySensor(coordinator, description))

    # Channel sensors
    channels = _get_channel_list(coordinator.data)
    for i, channel in enumerate(channels):
        channel_num = channel_num_from_str(channel.get("channel", ""), i + 1)
        channel_name = channel.get("channel_name", f"Channel {channel_num}")

        # Channel connectivity and video loss
        entities.append(
            RaySharpChannelBinarySensor(
                coordinator,
                RaySharpBinarySensorDescription(
                    key=f"channel_{channel_num}_online",
                    translation_key="channel_online",
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                    value_fn=lambda data, idx=i: _is_channel_online(data, idx),
                ),
                channel_num=channel_num,
                channel_name=channel_name,
            )
        )
        entities.append(
            RaySharpChannelBinarySensor(
                coordinator,
                RaySharpBinarySensorDescription(
                    key=f"channel_{channel_num}_videoloss",
                    translation_key="channel_videoloss",
                    device_class=BinarySensorDeviceClass.PROBLEM,
                    value_fn=lambda data, idx=i: _is_channel_videoloss(data, idx),
                ),
                channel_num=channel_num,
                channel_name=channel_name,
            )
        )

        # Event-triggered binary sensors (one per alarm type per channel)
        for alarm_type, key_suffix, translation_key, device_class in EVENT_BINARY_SENSOR_TYPES:
            entities.append(
                RaySharpEventBinarySensor(
                    coordinator,
                    channel_num=channel_num,
                    channel_name=channel_name,
                    alarm_type=alarm_type,
                    key_suffix=key_suffix,
                    translation_key=translation_key,
                    device_class=device_class,
                    event_timeout=event_timeout,
                )
            )

    async_add_entities(entities)


def _is_channel_online(data: dict[str, Any], index: int) -> bool | None:
    """Check if a channel is online."""
    channels = _get_channel_list(data)
    if index < len(channels):
        status = str(channels[index].get("connect_status", "")).lower()
        return status == "online"
    return None


def _is_channel_videoloss(data: dict[str, Any], index: int) -> bool | None:
    """Check if a channel has video loss."""
    channels = _get_channel_list(data)
    if index < len(channels):
        videoloss = channels[index].get("videoloss")
        if videoloss is None:
            return None
        if isinstance(videoloss, bool):
            return videoloss
        return str(videoloss).lower() in ("true", "1", "yes")
    return None


class RaySharpBinarySensor(RaySharpEntity, BinarySensorEntity):
    """Binary sensor entity for RaySharp NVR."""

    entity_description: RaySharpBinarySensorDescription

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        description: RaySharpBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        return self.entity_description.value_fn(self.coordinator.data)


class RaySharpChannelBinarySensor(RaySharpChannelEntity, BinarySensorEntity):
    """Binary sensor for a specific NVR channel (connectivity / video loss)."""

    entity_description: RaySharpBinarySensorDescription

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        description: RaySharpBinarySensorDescription,
        channel_num: int,
        channel_name: str,
    ) -> None:
        """Initialize the channel binary sensor."""
        super().__init__(coordinator, channel_num, channel_name)
        self.entity_description = description
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        # Channel-first unique_id ensures entity_id reflects the channel device
        suffix = description.key.replace(f"channel_{channel_num}_", "")
        self._attr_unique_id = f"{mac}_ch{channel_num}_{suffix}"
        # Short name — device name (CH17 CAM03) provides the channel context
        label = description.translation_key.replace("channel_", "").replace("_", " ").title()
        self._attr_name = label

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        return self.entity_description.value_fn(self.coordinator.data)


class RaySharpEventBinarySensor(RaySharpChannelEntity, BinarySensorEntity):
    """Binary sensor triggered by NVR alarm events.

    Turns on when an alarm event is received and auto-resets after the
    configurable timeout period.
    """

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
        alarm_type: str,
        key_suffix: str,
        translation_key: str,
        device_class: BinarySensorDeviceClass,
        event_timeout: int,
    ) -> None:
        """Initialize the event binary sensor."""
        super().__init__(coordinator, channel_num, channel_name)
        self._alarm_type = alarm_type
        self._event_timeout = event_timeout
        self._is_on = False
        self._reset_unsub: Callable[[], None] | None = None

        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        # Channel-first unique_id ensures entity_id reflects the channel device
        self._attr_unique_id = f"{mac}_ch{channel_num}_{key_suffix}"
        # Short name — device name (CH17 CAM03) provides the channel context
        label = translation_key.replace("_", " ").title()
        self._attr_name = label
        self._attr_translation_key = translation_key
        self._attr_device_class = device_class

        # Disable the entity in HA registry if this detection type is
        # explicitly turned off in the NVR config. The entity still exists
        # and users can manually enable it; it becomes active if the NVR
        # config is changed and the integration is reloaded.
        enabled_on_nvr = _get_detection_enabled(coordinator.data, channel_num, alarm_type)
        self._attr_entity_registry_enabled_default = enabled_on_nvr is not False

    @property
    def is_on(self) -> bool:
        """Return whether the sensor is triggered."""
        return self._is_on

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
        if data.get("channel", 0) != self._channel_num:
            return
        if data.get("alarm_type", "") != self._alarm_type:
            return

        self._is_on = True
        self.async_write_ha_state()

        if self._reset_unsub is not None:
            self._reset_unsub()

        self._reset_unsub = async_call_later(
            self.hass, self._event_timeout, self._async_reset
        )

    @callback
    def _async_reset(self, _now: Any) -> None:
        """Reset the sensor to off after timeout."""
        self._is_on = False
        self._reset_unsub = None
        self.async_write_ha_state()
