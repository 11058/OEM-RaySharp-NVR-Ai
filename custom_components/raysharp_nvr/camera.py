"""Camera platform for RaySharp NVR."""

from __future__ import annotations

from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_CHANNEL_INFO, DATA_DEVICE_INFO, DATA_RTSP_URLS, DOMAIN
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, channel_num_from_str


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


def _get_rtsp_urls(data: dict[str, Any]) -> dict[int, str]:
    """Extract RTSP URLs mapped by channel index (0-based)."""
    rtsp_data = data.get(DATA_RTSP_URLS)
    if not rtsp_data:
        return {}

    urls: dict[int, str] = {}

    # New endpoint: /API/Preview/StreamUrl/Get returns {"channel_info": [...]}
    if isinstance(rtsp_data, dict):
        channel_info = rtsp_data.get("channel_info")
        if isinstance(channel_info, list):
            for item in channel_info:
                if not isinstance(item, dict):
                    continue
                ch_str = str(item.get("channel", ""))
                url = item.get("mainstream_url", item.get("substream_url", ""))
                # Convert "CH1" → index 0, "CH2" → index 1, etc.
                if ch_str.upper().startswith("CH"):
                    try:
                        idx = int(ch_str[2:]) - 1
                        urls[idx] = url
                        continue
                    except (ValueError, IndexError):
                        pass
                # Fallback: try numeric channel
                try:
                    urls[int(ch_str) - 1] = url
                except (ValueError, TypeError):
                    pass
            return urls

        # Legacy fallback: urls/url list
        url_list = rtsp_data.get("urls", rtsp_data.get("url", []))
        if isinstance(url_list, list):
            for i, item in enumerate(url_list):
                if isinstance(item, dict):
                    url = item.get("url", item.get("rtsp_url", ""))
                    ch = item.get("channel", i)
                    urls[ch] = url
                elif isinstance(item, str):
                    urls[i] = item
        elif isinstance(url_list, str):
            urls[0] = url_list
    elif isinstance(rtsp_data, list):
        for i, item in enumerate(rtsp_data):
            if isinstance(item, dict):
                url = item.get("url", item.get("rtsp_url", ""))
                ch = item.get("channel", i)
                urls[ch] = url
            elif isinstance(item, str):
                urls[i] = item

    return urls


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR cameras."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]

    channels = _get_channel_list(coordinator.data)
    rtsp_urls = _get_rtsp_urls(coordinator.data)

    entities: list[RaySharpCamera] = []
    for i, channel in enumerate(channels):
        status = str(channel.get("connect_status", "")).lower()
        if status != "online":
            continue

        channel_num = channel_num_from_str(channel.get("channel", ""), i + 1)
        channel_name = channel.get("channel_name", f"Channel {channel_num}")
        # Use channel_num - 1 as the index because _get_rtsp_urls maps
        # "CH{N}" → index N-1. Using the enumerate index would break when
        # channels don't start at CH1 (e.g. first online channel is CH2).
        rtsp_url = rtsp_urls.get(channel_num - 1, "")
        entities.append(
            RaySharpCamera(
                coordinator,
                channel_num=channel_num,
                channel_name=channel_name,
                rtsp_url=rtsp_url,
            )
        )

    async_add_entities(entities)


class RaySharpCamera(RaySharpChannelEntity, Camera):
    """Camera entity for a RaySharp NVR channel."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
        rtsp_url: str,
    ) -> None:
        """Initialize the camera."""
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        Camera.__init__(self)
        self._rtsp_url = rtsp_url
        # _attr_name = None → entity is the "main feature" of the channel device;
        # entity_id becomes camera.ch17_cam03 (just the device name slug)
        self._attr_name = None

        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_ch{channel_num}_camera"

    @property
    def is_streaming(self) -> bool:
        """Return whether the camera is streaming."""
        for channel in _get_channel_list(self.coordinator.data):
            ch_num = channel_num_from_str(channel.get("channel", ""), 0)
            if ch_num == self._channel_num:
                return str(channel.get("connect_status", "")).lower() == "online"
        return False

    @property
    def available(self) -> bool:
        """Return whether the camera is available."""
        return self.coordinator.last_update_success and self.is_streaming

    async def stream_source(self) -> str | None:
        """Return the RTSP stream source."""
        # Refresh RTSP URL from coordinator data using channel_num - 1 index
        rtsp_urls = _get_rtsp_urls(self.coordinator.data)
        url = rtsp_urls.get(self._channel_num - 1, self._rtsp_url)
        return url if url else None
