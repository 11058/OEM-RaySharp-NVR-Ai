"""Base entity for RaySharp NVR."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ALARM_TYPE_FACE,
    ALARM_TYPE_INTRUSION,
    ALARM_TYPE_LINE_CROSSING,
    ALARM_TYPE_MOTION,
    ALARM_TYPE_PERSON,
    ALARM_TYPE_PLATE,
    ALARM_TYPE_VEHICLE,
    DATA_AI_FD_SETUP,
    DATA_AI_INTRUSION_SETUP,
    DATA_AI_LCD_SETUP,
    DATA_AI_LPD_SETUP,
    DATA_AI_PVD_SETUP,
    DATA_DEVICE_INFO,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import RaySharpNVRCoordinator


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
        return _ch_switch(DATA_AI_PVD_SETUP)
    if alarm_type == ALARM_TYPE_MOTION:
        return None
    if alarm_type == ALARM_TYPE_FACE:
        return _ch_switch(DATA_AI_FD_SETUP)
    if alarm_type == ALARM_TYPE_PLATE:
        lpd = _ch_switch(DATA_AI_LPD_SETUP)
        return lpd if lpd is not None else _ch_switch(DATA_AI_FD_SETUP)
    if alarm_type == ALARM_TYPE_LINE_CROSSING:
        return _ch_switch(DATA_AI_LCD_SETUP)
    if alarm_type == ALARM_TYPE_INTRUSION:
        return _ch_switch(DATA_AI_INTRUSION_SETUP)
    return None


def channel_num_from_str(ch_str: str, fallback: int) -> int:
    """Convert 'CH17' → 17, '17' → 17, or return fallback."""
    s = str(ch_str).strip()
    if s.upper().startswith("CH"):
        try:
            return int(s[2:])
        except ValueError:
            pass
    try:
        return int(s)
    except (ValueError, TypeError):
        return fallback


class RaySharpEntity(CoordinatorEntity[RaySharpNVRCoordinator]):
    """Base class for RaySharp NVR-level entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RaySharpNVRCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_info_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}

    @property
    def device_info(self) -> DeviceInfo:
        """Return NVR device info."""
        device_data = self.coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        model = device_data.get("device_type", "NVR")
        fw_version = device_data.get("http_api_version")

        return DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=f"RaySharp {model}",
            manufacturer=MANUFACTURER,
            model=model,
            sw_version=fw_version,
        )


class RaySharpChannelEntity(RaySharpEntity):
    """Base class for channel-specific entities.

    Each channel gets its own HA device, linked to the NVR via via_device.
    Entity names then appear as "CH17 CAM03 – Person Detected" etc.
    """

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
    ) -> None:
        """Initialize the channel entity."""
        super().__init__(coordinator)
        self._channel_num = channel_num
        self._channel_name = channel_name

    @property
    def device_info(self) -> DeviceInfo:
        """Return per-channel device info linked to the NVR.

        For channel_num == 0 (NVR-level entities), returns the NVR device
        itself so those entities appear under the main NVR device, not under
        a phantom "CH0" device.
        """
        nvr_data = self.coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = nvr_data.get("mac_addr", "unknown")
        if self._channel_num == 0:
            model = nvr_data.get("device_type", "NVR")
            fw_version = nvr_data.get("http_api_version")
            return DeviceInfo(
                identifiers={(DOMAIN, mac)},
                name=f"RaySharp {model}",
                manufacturer=MANUFACTURER,
                model=model,
                sw_version=fw_version,
            )
        # Avoid "CH2 CH2" when NVR channel_name equals the channel identifier
        name_part = self._channel_name.strip()
        if name_part.upper() == f"CH{self._channel_num}":
            name_part = ""
        device_name = (
            f"CH{self._channel_num} {name_part}" if name_part else f"CH{self._channel_num}"
        )
        return DeviceInfo(
            identifiers={(DOMAIN, f"{mac}_ch{self._channel_num}")},
            name=device_name,
            manufacturer=MANUFACTURER,
            via_device=(DOMAIN, mac),
        )
