"""Camera platform for RaySharp NVR."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_client import RaySharpNVRAuthError, RaySharpNVRConnectionError
from .const import (
    API_SNAPSHOT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_STREAM_TYPE,
    CONF_USERNAME,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DATA_RTSP_URLS,
    DEFAULT_STREAM_TYPE,
    DOMAIN,
)
from .coordinator import RaySharpNVRCoordinator, get_channel_list as _get_channel_list
from .entity import RaySharpChannelEntity, channel_num_from_str

_LOGGER = logging.getLogger(__name__)

_STREAM_TYPE_KEY = {
    "main": "mainstream_url",
    "sub": "substream_url",
    "mobile": "mobile_stream_url",
}

# Still images are pulled from the NVR one JPEG at a time; the dashboard asks
# for them far more often than the scene changes.
_SNAPSHOT_CACHE_TTL = 10.0
_SNAPSHOT_RESOLUTION = "1280 x 720"


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


def _retarget_host(url: str, host: str) -> str:
    """Point an NVR-published RTSP URL back at the address HA actually uses.

    The firmware builds these URLs from its own "server address" setting, which
    on a multi-homed install is an address Home Assistant cannot reach — seen in
    the wild: an NVR reachable at 10.100.12.10 advertising rtsp://10.40.0.2/…,
    which ffmpeg can only time out on.  The stream has to come from the box we
    are already talking to, so swap the host and keep everything else.

    The port is left alone on purpose: these devices serve RTSP on their web
    port (verified — port 80 answers the RTSP handshake with 401, while 554
    refuses the connection), so second-guessing it would break working setups.
    """
    if not url or not host:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.hostname or parsed.hostname == host:
        return url

    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


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
    host = entry.data.get(CONF_HOST, "")

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
        published = rtsp_urls.get(channel_num - 1, "")
        retargeted = _retarget_host(published, host)
        rtsp_url = _embed_creds(retargeted, username, password)
        if published and retargeted != published:
            _LOGGER.debug(
                "CH%d stream: NVR published an unreachable host, %s → %s",
                channel_num, published, retargeted,
            )
        entities.append(
            RaySharpCamera(
                coordinator,
                channel_num=channel_num,
                channel_name=channel_name,
                rtsp_url=rtsp_url,
                stream_type=stream_type,
                username=username,
                password=password,
                host=host,
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
        host: str = "",
    ) -> None:
        """Initialize the camera."""
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        Camera.__init__(self)
        self._rtsp_url = rtsp_url
        self._stream_type = stream_type
        self._username = username
        self._password = password
        self._host = host
        self._snapshot: bytes | None = None
        self._snapshot_at = 0.0
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
        published = rtsp_urls.get(self._channel_num - 1)
        if not published:
            return self._rtsp_url or None
        url = _retarget_host(published, self._host)
        return _embed_creds(url, self._username, self._password)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still frame, pulled from the NVR's snapshot API.

        Without this the dashboard preview raises NotImplementedError and the
        card stays blank until (and unless) the RTSP stream comes up.  The NVR
        serves one snapshot at a time and is slow about it, so frames are
        cached briefly.
        """
        now = time.monotonic()
        if self._snapshot is not None and now - self._snapshot_at < _SNAPSHOT_CACHE_TTL:
            return self._snapshot

        payload = {
            "channel": f"CH{self._channel_num}",
            "snapshot_resolution": _SNAPSHOT_RESOLUTION,
            "reset_session_timeout": False,
        }
        try:
            response = await self.coordinator.client.async_api_call(
                API_SNAPSHOT, payload
            )
        except (RaySharpNVRAuthError, RaySharpNVRConnectionError) as err:
            _LOGGER.debug("Snapshot CH%d failed: %s", self._channel_num, err)
            return self._snapshot

        snap = response.get("data", response) if isinstance(response, dict) else {}
        # Firmware spells this `img_data`; older docs say `ima_data`.
        img_b64 = snap.get("img_data") or snap.get("ima_data", "")
        if not img_b64:
            _LOGGER.debug("Snapshot CH%d returned no image data", self._channel_num)
            return self._snapshot

        try:
            self._snapshot = base64.b64decode(img_b64)
        except ValueError:
            _LOGGER.debug("Snapshot CH%d base64 decode failed", self._channel_num)
            return None
        self._snapshot_at = now
        return self._snapshot
