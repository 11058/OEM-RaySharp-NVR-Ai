"""Base entity for RaySharp NVR."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_DEVICE_INFO, DOMAIN, MANUFACTURER
from .coordinator import RaySharpNVRCoordinator


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
