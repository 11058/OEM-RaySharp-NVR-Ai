"""Image platform for RaySharp NVR.

Provides image entities per channel:
- One ``last_detection`` entity (any alarm type, real-time, existing behaviour)
- History entities: last N snapshots per channel per alarm type
  (plate, face, person, vehicle), metadata persisted between HA restarts.
  Image bytes are held in memory only; on HA restart they are fetched on demand
  from the NVR via GetByIndex APIs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import (
    ALARM_TYPE_FACE,
    ALARM_TYPE_PERSON,
    ALARM_TYPE_PLATE,
    ALARM_TYPE_VEHICLE,
    API_AI_FACES_GET_BY_INDEX,
    API_AI_OBJECTS_GET_BY_INDEX,
    API_AI_VHD_GET,
    CONF_SNAPSHOT_HISTORY_COUNT,
    DATA_CHANNEL_INFO,
    DATA_DEVICE_INFO,
    DEFAULT_SNAPSHOT_HISTORY_COUNT,
    DOMAIN,
    EVENT_SNAPSHOT,
    STORAGE_KEY_SNAPSHOTS_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpChannelEntity, _get_detection_enabled, channel_num_from_str

_LOGGER = logging.getLogger(__name__)

# Alarm types for which history image entities are created
_HISTORY_ALARM_TYPES = (
    ALARM_TYPE_PLATE,
    ALARM_TYPE_FACE,
    ALARM_TYPE_PERSON,
    ALARM_TYPE_VEHICLE,
)

# Debounce delay (seconds) before flushing metadata to HA Store
_SAVE_DEBOUNCE_S = 30


# ─── Helpers ───────────────────────────────────────────────────────────────────

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


def _parse_ts_to_dt(ts: Any) -> datetime | None:
    """Convert a NVR timestamp to an aware UTC datetime.

    NVR timestamps are in local (device) time — the same timezone as HA is
    configured for.  We therefore interpret the naive string as the HA local
    timezone and convert to UTC so HA displays the correct local time.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=dt_util.UTC)
        except (ValueError, OSError):
            return None
    try:
        naive_dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
        # Localise as HA timezone → convert to UTC
        return dt_util.as_utc(naive_dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    except (ValueError, AttributeError):
        pass
    return dt_util.parse_datetime(str(ts))


def _extract_list(resp: Any, key: str) -> list[dict[str, Any]]:
    """Pull a list of dicts from a NVR API response envelope under data.<key>."""
    if not isinstance(resp, dict):
        return []
    data = resp.get("data", resp)
    if not isinstance(data, dict):
        return []
    items = data.get(key, [])
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


# ─── Snapshot Entry ────────────────────────────────────────────────────────────

@dataclass
class SnapshotEntry:
    """One snapshot record: metadata + optional in-memory image bytes."""

    snap_id: int | str | None
    timestamp: str | int | float | None
    channel: int
    alarm_type: str
    image_bytes: bytes | None = field(default=None, compare=False, repr=False)
    plate_number: str | None = None
    grp_id: int | None = None
    car_brand: str | None = None
    car_color: str | None = None
    face_id: int | None = None
    face_name: str | None = None
    similarity: float | int | None = None


# ─── Snapshot History Store ────────────────────────────────────────────────────

class SnapshotHistoryStore:
    """Persistent ring-buffer of last N snapshots for one (channel, alarm_type).

    Metadata is stored in HA Store (.storage/); image bytes stay in memory only.
    On HA restart the metadata is restored; images are fetched from the NVR on
    demand via GetByIndex APIs when ``async_fetch_image`` is called.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        channel_num: int,
        alarm_type: str,
        max_entries: int,
    ) -> None:
        self._hass = hass
        self._channel_num = channel_num
        self._alarm_type = alarm_type
        self._max_entries = max_entries
        self._store_key = f"{STORAGE_KEY_SNAPSHOTS_PREFIX}_{channel_num}_{alarm_type}"
        self._store: Store | None = None
        self._entries: list[SnapshotEntry] = []
        self._notify_callbacks: list[Callable[[], None]] = []
        self._save_unsub: Callable[[], None] | None = None
        self._event_unsub: Callable[[], None] | None = None

    async def async_load(self) -> None:
        """Load persisted metadata from HA Store and start listening for events."""
        self._store = Store(self._hass, STORAGE_VERSION, self._store_key)
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            for e in stored.get("entries", []):
                if not isinstance(e, dict):
                    continue
                self._entries.append(
                    SnapshotEntry(
                        snap_id=e.get("snap_id"),
                        timestamp=e.get("timestamp"),
                        channel=e.get("channel", self._channel_num),
                        alarm_type=e.get("alarm_type", self._alarm_type),
                        image_bytes=None,
                        plate_number=e.get("plate_number"),
                        grp_id=e.get("grp_id"),
                        car_brand=e.get("car_brand"),
                        car_color=e.get("car_color"),
                        face_id=e.get("face_id"),
                        face_name=e.get("face_name"),
                        similarity=e.get("similarity"),
                    )
                )
        self._event_unsub = self._hass.bus.async_listen(
            EVENT_SNAPSHOT, self._handle_snapshot
        )

    @callback
    def async_unload(self) -> None:
        """Cancel event listener and flush any pending save on unload."""
        if self._event_unsub:
            self._event_unsub()
            self._event_unsub = None
        if self._save_unsub:
            self._save_unsub()
            self._save_unsub = None
            # Flush metadata that was pending a debounced write
            self._hass.async_create_task(self.async_save())

    def register_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked when the entry list changes.

        Returns an unregister callable for use with ``async_on_remove``.
        """
        self._notify_callbacks.append(cb)

        def _unregister() -> None:
            try:
                self._notify_callbacks.remove(cb)
            except ValueError:
                pass

        return _unregister

    @callback
    def _handle_snapshot(self, event: Any) -> None:
        data = event.data
        if data.get("channel") != self._channel_num:
            return
        if data.get("alarm_type") != self._alarm_type:
            return

        img_b64 = data.get("image", "")
        image_bytes: bytes | None = None
        if img_b64:
            try:
                image_bytes = base64.b64decode(img_b64)
            except Exception:
                pass

        self._add_entry(data, image_bytes)

    def _add_entry(
        self, snap_data: dict[str, Any], image_bytes: bytes | None
    ) -> None:
        """Prepend new entry, trim to max_entries, schedule save, notify."""
        entry = SnapshotEntry(
            snap_id=snap_data.get("snap_id"),
            timestamp=snap_data.get("start_time") or snap_data.get("timestamp"),
            channel=self._channel_num,
            alarm_type=self._alarm_type,
            image_bytes=image_bytes,
            plate_number=snap_data.get("plate_number"),
            grp_id=snap_data.get("grp_id"),
            car_brand=snap_data.get("car_brand"),
            car_color=snap_data.get("car_color"),
            face_id=snap_data.get("face_id"),
            face_name=snap_data.get("face_name"),
            similarity=snap_data.get("similarity"),
        )
        self._entries.insert(0, entry)
        self._entries = self._entries[: self._max_entries]

        # Debounce: cancel pending save and reschedule
        if self._save_unsub is not None:
            self._save_unsub()
        self._save_unsub = async_call_later(
            self._hass, _SAVE_DEBOUNCE_S, self._trigger_save
        )

        for cb in list(self._notify_callbacks):
            cb()

    @callback
    def _trigger_save(self, _now: Any) -> None:
        self._save_unsub = None
        self._hass.async_create_task(self.async_save())

    async def async_save(self) -> None:
        """Persist entry metadata (without image bytes) to HA Store."""
        if self._store is None:
            return
        serialized: list[dict[str, Any]] = []
        for e in self._entries:
            d: dict[str, Any] = {
                "snap_id": e.snap_id,
                "timestamp": e.timestamp,
                "channel": e.channel,
                "alarm_type": e.alarm_type,
            }
            for attr in (
                "plate_number",
                "grp_id",
                "car_brand",
                "car_color",
                "face_id",
                "face_name",
                "similarity",
            ):
                val = getattr(e, attr)
                if val is not None:
                    d[attr] = val
            serialized.append(d)
        await self._store.async_save({"entries": serialized})

    def get_entry(self, slot: int) -> SnapshotEntry | None:
        """Return entry at 0-based slot (0 = newest), or None if not available."""
        if 0 <= slot < len(self._entries):
            return self._entries[slot]
        return None

    async def async_fetch_image(
        self, slot: int, coordinator: RaySharpNVRCoordinator
    ) -> bytes | None:
        """Return image bytes for a slot, fetching from NVR if not in memory."""
        entry = self.get_entry(slot)
        if entry is None:
            return None
        if entry.image_bytes is not None:
            return entry.image_bytes
        image_bytes = await self._async_fetch_from_nvr(entry, coordinator)
        if image_bytes:
            entry.image_bytes = image_bytes
        return image_bytes

    async def _async_fetch_from_nvr(
        self, entry: SnapshotEntry, coordinator: RaySharpNVRCoordinator
    ) -> bytes | None:
        """Search NVR for image bytes matching entry.snap_id within ±2 min."""
        if entry.snap_id is None or entry.timestamp is None:
            return None

        # Parse as naive local time — NVR API expects local time strings.
        try:
            naive_dt = datetime.strptime(str(entry.timestamp), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

        start_str = (naive_dt - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        end_str = (naive_dt + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        ch_0 = self._channel_num - 1  # NVR GetByIndex uses 0-based channel index
        # Normalise snap_id to string for type-safe comparison (NVR may return int
        # while JSON storage may deserialise it differently).
        snap_id_str = str(entry.snap_id)

        try:
            if self._alarm_type == ALARM_TYPE_PLATE:
                resp = await coordinator.client.async_api_call(
                    API_AI_OBJECTS_GET_BY_INDEX,
                    {"Chn": [ch_0], "StartTime": start_str, "EndTime": end_str},
                )
                for item in _extract_list(resp, "PlateInfo"):
                    if str(item.get("SnapId", "")) == snap_id_str:
                        img = item.get("BgImg") or item.get("PlateImg")
                        if img:
                            return base64.b64decode(img)

            elif self._alarm_type == ALARM_TYPE_FACE:
                resp = await coordinator.client.async_api_call(
                    API_AI_FACES_GET_BY_INDEX,
                    {"Chn": [ch_0], "StartTime": start_str, "EndTime": end_str},
                )
                for item in _extract_list(resp, "SnapedFaceInfo"):
                    if str(item.get("SnapId", "")) == snap_id_str:
                        img = (
                            item.get("FaceImage")
                            or item.get("Image2")
                            or item.get("Image4")
                        )
                        if img:
                            return base64.b64decode(img)

            elif self._alarm_type in (ALARM_TYPE_PERSON, ALARM_TYPE_VEHICLE):
                resp = await coordinator.client.async_api_call(
                    API_AI_VHD_GET,
                    {"Chn": [ch_0], "StartTime": start_str, "EndTime": end_str},
                )
                for item in _extract_list(resp, "SnapedObjInfo"):
                    if str(item.get("SnapId", "")) == snap_id_str:
                        img = item.get("ObjectImage")
                        if img:
                            return base64.b64decode(img)

        except Exception as err:
            _LOGGER.debug(
                "Failed to fetch snapshot from NVR (ch=%s type=%s snap_id=%s): %s",
                self._channel_num,
                self._alarm_type,
                entry.snap_id,
                err,
            )
        return None


# ─── History Image Entity ──────────────────────────────────────────────────────

class RaySharpHistoryImageEntity(RaySharpChannelEntity, ImageEntity):
    """Image entity showing one history slot for a channel + alarm type.

    Slot 1 is the most recent snapshot; slot N is the oldest kept.
    After HA restart, metadata is restored from HA Store; the image is
    fetched from the NVR on demand via GetByIndex APIs.
    """

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        channel_num: int,
        channel_name: str,
        alarm_type: str,
        slot: int,
        history_store: SnapshotHistoryStore,
    ) -> None:
        RaySharpChannelEntity.__init__(self, coordinator, channel_num, channel_name)
        ImageEntity.__init__(self, coordinator.hass)

        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")

        self._alarm_type = alarm_type
        self._slot = slot  # 1-based
        self._history = history_store

        self._attr_unique_id = f"{mac}_ch{channel_num}_{alarm_type}_slot{slot}"
        type_label = alarm_type.replace("_", " ").title()
        self._attr_name = f"{type_label} {slot}"

        # Disable entity by default if the detection type is off on this channel
        enabled_on_nvr = _get_detection_enabled(coordinator.data, channel_num, alarm_type)
        self._attr_entity_registry_enabled_default = enabled_on_nvr is not False

        # Initialise image_last_updated from any already-loaded store entry
        self._attr_image_last_updated = _parse_ts_to_dt(
            history_store.get_entry(slot - 1).timestamp
            if history_store.get_entry(slot - 1) is not None
            else None
        )

    async def async_added_to_hass(self) -> None:
        """Register state-update callback with the history store."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._history.register_callback(self._on_history_update)
        )

    @callback
    def _on_history_update(self) -> None:
        """Called by the store when a new entry is prepended."""
        entry = self._history.get_entry(self._slot - 1)
        self._attr_image_last_updated = (
            _parse_ts_to_dt(entry.timestamp) if entry is not None else None
        )
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return image bytes for this slot, fetching from NVR if needed."""
        return await self._history.async_fetch_image(self._slot - 1, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return metadata attributes for this snapshot slot."""
        entry = self._history.get_entry(self._slot - 1)
        if not entry:
            return {}
        attrs: dict[str, Any] = {}
        for attr_name in (
            "snap_id",
            "timestamp",
            "plate_number",
            "grp_id",
            "car_brand",
            "car_color",
            "face_id",
            "face_name",
            "similarity",
        ):
            val = getattr(entry, attr_name, None)
            if val is not None:
                attrs[attr_name] = val
        return attrs


# ─── Latest Detection Entity (existing behaviour, unchanged) ───────────────────

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


# ─── Platform setup ────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR snapshot image entities."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]
    history_count = entry.options.get(
        CONF_SNAPSHOT_HISTORY_COUNT, DEFAULT_SNAPSHOT_HISTORY_COUNT
    )

    channels = _get_channel_list(coordinator.data)
    entities: list = []

    for i, channel in enumerate(channels):
        channel_num = channel_num_from_str(channel.get("channel", ""), i + 1)
        channel_name = channel.get("channel_name", f"Channel {channel_num}")

        # Existing latest-detection entity (real-time, any alarm type)
        entities.append(
            RaySharpSnapshotImage(coordinator, channel_num, channel_name)
        )

        # History image entities: one store per (channel, alarm_type), N slots each
        for alarm_type in _HISTORY_ALARM_TYPES:
            store = SnapshotHistoryStore(hass, channel_num, alarm_type, history_count)
            await store.async_load()
            entry.async_on_unload(store.async_unload)
            for slot in range(1, history_count + 1):
                entities.append(
                    RaySharpHistoryImageEntity(
                        coordinator,
                        channel_num,
                        channel_name,
                        alarm_type,
                        slot,
                        store,
                    )
                )

    async_add_entities(entities)
