"""Camera platform for RaySharp NVR."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_PASSWORD,
    CONF_STREAM_TYPE,
    CONF_USERNAME,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DATA_RTSP_URLS,
    DEFAULT_STREAM_TYPE,
    DOMAIN,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, channel_num_from_str


_STREAM_TYPE_KEY = {
    "main": "mainstream_url",
    "sub": "substream_url",
    "mobile": "mobile_stream_url",
}


def _embed_creds(url: str, username: str, password: str) -> str:
    """Inject HTTP Digest creds into an RTSP URL.

    NVR-published URLs are credential-less (rtsp://host:554/...).  HA's stream
    component cannot prompt for auth, so the username and password are
    pre-embedded here; ffmpeg picks Digest automatically when challenged.
    Special chars in the password are URL-escaped.
    """
    if not url or not username:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    user_quoted = quote(username, safe="")
    pass_quoted = quote(password or "", safe="")
    auth = f"{user_quoted}:{pass_quoted}" if password else user_quoted
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{auth}@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


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


def _get_rtsp_urls(data: dict[str, Any], stream_type: str = DEFAULT_STREAM_TYPE) -> dict[int, str]:
    """Extract RTSP URLs mapped by channel index (0-based)."""
    rtsp_data = data.get(DATA_RTSP_URLS)
    if not rtsp_data:
        return {}

    primary_key = _STREAM_TYPE_KEY.get(stream_type, "mainstream_url")
    fallback_keys = [
        k for k in ("mainstream_url", "substream_url", "mobile_stream_url")
        if k != primary_key
    ]

    urls: dict[int, str] = {}

    # New endpoint: /API/Preview/StreamUrl/Get returns {"channel_info": [...]}
    if isinstance(rtsp_data, dict):
        channel_info = rtsp_data.get("channel_info")
        if isinstance(channel_info, list):
            for item in channel_info:
                if not isinstance(item, dict):
                    continue
                ch_str = str(item.get("channel", ""))
                url = item.get(primary_key, "")
                if not url:
                    for fk in fallback_keys:
                        url = item.get(fk, "")
                        if url:
                            break
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

    stream_type = entry.options.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)
    username = entry.data.get(CONF_USERNAME, "")
    password = entry.data.get(CONF_PASSWORD, "")

    channels = _get_channel_list(coordinator.data)
    rtsp_urls = _get_rtsp_urls(coordinator.data, stream_type)

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
        rtsp_url = _embed_creds(rtsp_urls.get(channel_num - 1, ""), username, password)
        entities.append(
            RaySharpCamera(
                coordinator,
                channel_num=channel_num,
                channel_name=channel_name,
                rtsp_url=rtsp_url,
                stream_type=stream_type,
                username=username,
                password=password,
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
        stream_type: str = DEFAULT_STREAM_TYPE,
        username: str = "",
        password: str = "",
    ) -> None:
        """Initialize the camera."""
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        Camera.__init__(self)
        self._rtsp_url = rtsp_url
        self._stream_type = stream_type
        self._username = username
        self._password = password
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
        """Return the RTSP stream source.

        Refreshes the URL from coordinator data and embeds creds — NVR returns
        credential-less URLs but HA's stream component has no way to prompt
        for them.  When the options flow has switched stream_type, this is
        also where the new selection takes effect after a reload.
        """
        rtsp_urls = _get_rtsp_urls(self.coordinator.data, self._stream_type)
        url = rtsp_urls.get(self._channel_num - 1) or self._rtsp_url
        if not url:
            return None
        return _embed_creds(url, self._username, self._password)
