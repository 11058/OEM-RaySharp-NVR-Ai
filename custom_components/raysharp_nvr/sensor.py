"""Sensor platform for RaySharp NVR."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .api_client import RaySharpNVRConnectionError
from .const import (
    ALARM_TYPE_FACE,
    ALARM_TYPE_PLATE,
    API_AI_ADDED_PLATES_GET,
    API_AI_FD_GROUPS,
    DATA_AI_CC_STATS,
    DATA_AI_CROSS_COUNTING,
    DATA_AI_FACE_STATS,
    DATA_AI_FACES,
    DATA_AI_OBJECT_STATS,
    DATA_AI_PLATES,
    DATA_AI_VHD_COUNT,
    DATA_CHANNEL_INFO,
    DATA_DATE_TIME,
    DATA_DEVICE_INFO,
    DATA_DISK_CONFIG,
    DATA_EVENT_PUSH_CONFIG,
    DATA_EXCEPTION_ALARM,
    DATA_NETWORK_CONFIG,
    DATA_NETWORK_STATE,
    DATA_RECORD_CONFIG,
    DATA_RECORD_INFO,
    DATA_SYSTEM_GENERAL,
    DATA_SYSTEM_INFO,
    DOMAIN,
    DOMAIN_TRACKERS,
    EVENT_SNAPSHOT,
    STORAGE_KEY_FACES,
    STORAGE_KEY_PLATES,
    STORAGE_KEEP_DAYS,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .coordinator import RaySharpNVRCoordinator
from .entity import RaySharpEntity


@dataclass(frozen=True, kw_only=True)
class RaySharpSensorDescription(SensorEntityDescription):
    """Describe a RaySharp sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    exists_fn: Callable[[dict[str, Any]], bool] = lambda data: True


# ─── Device Info Sensors ──────────────────────────────────────────────────────
DEVICE_INFO_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="device_type",
        translation_key="device_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_DEVICE_INFO) or {}).get("device_type"),
    ),
    RaySharpSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_SYSTEM_INFO) or {}).get(
            "software_version"
        ),
    ),
    RaySharpSensorDescription(
        key="mac_address",
        translation_key="mac_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_DEVICE_INFO) or {}).get("mac_addr"),
    ),
    RaySharpSensorDescription(
        key="total_channels",
        translation_key="total_channels",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_DEVICE_INFO) or {}).get("channel_num"),
    ),
    RaySharpSensorDescription(
        key="cloud_state",
        translation_key="cloud_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_SYSTEM_INFO) or {}).get("network_state"),
    ),
    RaySharpSensorDescription(
        key="device_datetime",
        translation_key="device_datetime",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: (data.get(DATA_DATE_TIME) or {}).get("date_time"),
        exists_fn=lambda data: data.get(DATA_DATE_TIME) is not None,
    ),
)

# ─── System Info Sensors ──────────────────────────────────────────────────────
SYSTEM_INFO_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="system_hw_version",
        translation_key="system_hw_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_SYSTEM_INFO) or {}).get("hardware_version"),
        exists_fn=lambda data: data.get(DATA_SYSTEM_INFO) is not None,
    ),
    RaySharpSensorDescription(
        key="system_serial",
        translation_key="system_serial",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_SYSTEM_INFO) or {}).get("serialNum"),
        exists_fn=lambda data: data.get(DATA_SYSTEM_INFO) is not None,
    ),
    RaySharpSensorDescription(
        key="system_model",
        translation_key="system_model",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_SYSTEM_INFO) or {}).get("device_name"),
        exists_fn=lambda data: data.get(DATA_SYSTEM_INFO) is not None,
    ),
    RaySharpSensorDescription(
        key="system_alarm_inputs",
        translation_key="system_alarm_inputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.get(DATA_DEVICE_INFO) or {}).get("local_alarmin_num"),
        exists_fn=lambda data: data.get(DATA_DEVICE_INFO) is not None,
    ),
    RaySharpSensorDescription(
        key="system_alarm_outputs",
        translation_key="system_alarm_outputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.get(DATA_DEVICE_INFO) or {}).get("local_alarmout_num"),
        exists_fn=lambda data: data.get(DATA_DEVICE_INFO) is not None,
    ),
)

# ─── Network State Sensors ────────────────────────────────────────────────────
NETWORK_STATE_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="network_ip",
        translation_key="network_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_network_ip(data),
        exists_fn=lambda data: data.get(DATA_NETWORK_STATE) is not None
        or data.get(DATA_NETWORK_CONFIG) is not None,
    ),
    RaySharpSensorDescription(
        key="network_gateway",
        translation_key="network_gateway",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_network_field(data, "gateway"),
        exists_fn=lambda data: data.get(DATA_NETWORK_STATE) is not None
        or data.get(DATA_NETWORK_CONFIG) is not None,
    ),
)

# ─── Record Info Sensors ──────────────────────────────────────────────────────
RECORD_INFO_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="record_overwrite",
        translation_key="record_overwrite",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (data.get(DATA_RECORD_INFO) or data.get(DATA_RECORD_CONFIG) or {}).get("disk_full_strategy"),
        exists_fn=lambda data: data.get(DATA_RECORD_INFO) is not None
        or data.get(DATA_RECORD_CONFIG) is not None,
    ),
)

# ─── AI Sensors ───────────────────────────────────────────────────────────────
# All counts come from VhdLogCount/Get with today's time range.
# Type order requested: [0=face, 1=person, 2=vehicle, 10=plate]
# → Count array indices:  [0,      1,        2,          3     ]
AI_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="ai_faces_detected",
        translation_key="ai_faces_detected",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _get_vhd_count(data, 0),  # face
        exists_fn=lambda data: data.get(DATA_AI_VHD_COUNT) is not None,
    ),
    RaySharpSensorDescription(
        key="ai_plates_detected",
        translation_key="ai_plates_detected",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _get_vhd_count(data, 3),  # plate (index 3)
        exists_fn=lambda data: data.get(DATA_AI_VHD_COUNT) is not None,
    ),
    RaySharpSensorDescription(
        key="ai_object_stats_person_total",
        translation_key="ai_object_stats_person_total",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _get_vhd_count(data, 1),  # person
        exists_fn=lambda data: data.get(DATA_AI_VHD_COUNT) is not None,
    ),
    RaySharpSensorDescription(
        key="ai_object_stats_vehicle_total",
        translation_key="ai_object_stats_vehicle_total",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _get_vhd_count(data, 2),  # vehicle
        exists_fn=lambda data: data.get(DATA_AI_VHD_COUNT) is not None,
    ),
)

# ─── EventPush Status Sensor ──────────────────────────────────────────────────
EVENT_PUSH_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="event_push_status",
        translation_key="event_push_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_event_push_status(data),
        exists_fn=lambda data: data.get(DATA_EVENT_PUSH_CONFIG) is not None,
    ),
)

# ─── Exception Alarm Status Sensor ───────────────────────────────────────────
EXCEPTION_SENSORS: tuple[RaySharpSensorDescription, ...] = (
    RaySharpSensorDescription(
        key="exception_alarm_status",
        translation_key="exception_alarm_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_exception_status(data),
        exists_fn=lambda data: data.get(DATA_EXCEPTION_ALARM) is not None,
    ),
)


# ─── Helper functions ─────────────────────────────────────────────────────────

def _get_event_push_status(data: dict[str, Any]) -> str | None:
    """Get EventPush configuration status string."""
    config = data.get(DATA_EVENT_PUSH_CONFIG)
    if config is None:
        return None
    # NVR may return a list of named push configs
    if isinstance(config, list):
        for item in config:
            if isinstance(item, dict) and item.get("enable"):
                addr = item.get("addr", "")
                port = item.get("port", "")
                return f"Enabled ({addr}:{port})"
        return "Disabled"
    if isinstance(config, dict):
        # Flat format (new): fields directly in dict
        if "enable" in config:
            enabled = bool(config["enable"])
            addr = config.get("addr", "")
            port = config.get("port", "")
        else:
            # Legacy nested format: params.table
            table = config.get("params", {}).get("table", config)
            enabled = bool(table.get("enable", False))
            addr = table.get("addr", "")
            port = table.get("port", "")
        return f"Enabled ({addr}:{port})" if enabled else "Disabled"
    return str(config)


def _get_exception_status(data: dict[str, Any]) -> str | None:
    """Summarise exception alarm config."""
    exc = data.get(DATA_EXCEPTION_ALARM)
    if not isinstance(exc, dict):
        return None
    info = exc.get("exception_info", {})
    if not isinstance(info, dict):
        return None
    enabled = [k for k, v in info.items() if isinstance(v, dict) and v.get("switch", False)]
    if not enabled:
        return "None active"
    return ", ".join(enabled)


def _get_network_ip(data: dict[str, Any]) -> str | None:
    """Extract IP address from network state or config."""
    for key in (DATA_NETWORK_STATE, DATA_NETWORK_CONFIG):
        net = data.get(key)
        if not isinstance(net, dict):
            continue
        ip = (
            net.get("ip")
            or net.get("ip_addr")
            or net.get("IP")
            or net.get("ip_address")
            or _search_nested(net, "ip")
            or _search_nested(net, "ip_address")
        )
        if ip:
            return str(ip)
    return None


def _get_network_field(data: dict[str, Any], field: str) -> str | None:
    """Extract a field from network state or config."""
    for key in (DATA_NETWORK_STATE, DATA_NETWORK_CONFIG):
        net = data.get(key)
        if not isinstance(net, dict):
            continue
        val = net.get(field) or _search_nested(net, field)
        if val:
            return str(val)
    return None


def _search_nested(obj: Any, field: str) -> Any:
    """Shallow search for a field in nested dicts."""
    if not isinstance(obj, dict):
        return None
    if field in obj:
        return obj[field]
    for v in obj.values():
        if isinstance(v, dict):
            result = v.get(field)
            if result is not None:
                return result
    return None


def _get_channel_list_for_sensors(data: dict[str, Any]) -> list[dict[str, Any]]:
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


def _get_cc_stats_for_channel(
    data: dict[str, Any], channel_index: int, key: str
) -> int | None:
    """Get cross-counting statistics for a specific channel."""
    cc_data = data.get(DATA_AI_CC_STATS)
    if cc_data is None:
        return None
    if isinstance(cc_data, dict):
        channels = cc_data.get("channels", cc_data.get("channel", []))
        if isinstance(channels, list) and channel_index < len(channels):
            return channels[channel_index].get(key, 0)
        ch_key = f"CH{channel_index + 1}"
        if ch_key in cc_data:
            ch = cc_data[ch_key]
            if isinstance(ch, dict):
                return ch.get(key, 0)
        return cc_data.get(key, 0)
    if isinstance(cc_data, list) and channel_index < len(cc_data):
        item = cc_data[channel_index]
        if isinstance(item, dict):
            return item.get(key, 0)
    return None


def _has_ai_capability(channel: dict[str, Any]) -> bool:
    """Check if a channel has AI capabilities."""
    ability = channel.get("intelligent_ability", "")
    return bool(ability and str(ability).strip())


def _build_cc_stats_sensors(
    data: dict[str, Any],
) -> list[RaySharpSensorDescription]:
    """Build per-channel cross-counting sensors for AI-capable channels."""
    if data.get(DATA_AI_CC_STATS) is None:
        return []

    channels = _get_channel_list_for_sensors(data)
    sensors: list[RaySharpSensorDescription] = []

    for i, channel in enumerate(channels):
        if not _has_ai_capability(channel):
            continue
        channel_num = i + 1
        sensors.append(
            RaySharpSensorDescription(
                key=f"ai_cross_count_in_ch{channel_num}",
                translation_key="ai_cross_count_in_channel",
                state_class=SensorStateClass.TOTAL_INCREASING,
                value_fn=lambda data, idx=i: _get_cc_stats_for_channel(
                    data, idx, "in_count"
                ),
            )
        )
        sensors.append(
            RaySharpSensorDescription(
                key=f"ai_cross_count_out_ch{channel_num}",
                translation_key="ai_cross_count_out_channel",
                state_class=SensorStateClass.TOTAL_INCREASING,
                value_fn=lambda data, idx=i: _get_cc_stats_for_channel(
                    data, idx, "out_count"
                ),
            )
        )
    return sensors


def _get_vhd_count(data: dict[str, Any], type_index: int) -> int | None:
    """Return today's AI count for a type from VhdLogCount response.

    The request is made with Type=[0=face, 1=person, 2=vehicle, 10=plate],
    so the Count array indices are: 0→face, 1→person, 2→vehicle, 3→plate.
    """
    vhd = data.get(DATA_AI_VHD_COUNT)
    if not isinstance(vhd, dict):
        return None
    count = vhd.get("Count")
    if isinstance(count, list) and type_index < len(count):
        try:
            return int(count[type_index])
        except (TypeError, ValueError):
            return None
    return None


def _count_items(data: Any) -> int | None:
    """Count items in a list or return count from data."""
    if data is None:
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        items = data.get("items", data.get("list", []))
        if isinstance(items, list):
            return len(items)
        return data.get("count", data.get("total", 0))
    return 0


def _get_cross_count(data: dict[str, Any], key: str) -> int | None:
    """Get global cross counting value."""
    cc = data.get(DATA_AI_CROSS_COUNTING)
    if cc is None:
        return None
    if isinstance(cc, dict):
        return cc.get(key, 0)
    return 0


def _get_stats_total(stats_data: Any) -> int | None:
    """Extract total count from statistics data."""
    if stats_data is None:
        return None
    if isinstance(stats_data, dict):
        return stats_data.get("total", stats_data.get("count", len(
            stats_data.get("items", stats_data.get("list", []))
        )))
    if isinstance(stats_data, list):
        return len(stats_data)
    return None


def _get_object_stats(stats_data: Any, obj_type: str) -> int | None:
    """Get count for a specific object type from statistics."""
    if stats_data is None:
        return None
    if isinstance(stats_data, dict):
        # Try direct key
        val = stats_data.get(obj_type) or stats_data.get(f"{obj_type}_count")
        if val is not None:
            return int(val)
        # Try nested
        items = stats_data.get("items", stats_data.get("list", []))
        if isinstance(items, list):
            return sum(
                1 for i in items
                if isinstance(i, dict) and i.get("type", "") == obj_type
            )
    return None


def _build_disk_sensors(data: dict[str, Any]) -> list[RaySharpSensorDescription]:
    """Build dynamic sensor descriptions for each disk."""
    disk_data = data.get(DATA_DISK_CONFIG)
    if not disk_data:
        return []

    if isinstance(disk_data, dict):
        disk_list = disk_data.get("disk_info", disk_data.get("disks", disk_data.get("disk", [])))
    elif isinstance(disk_data, list):
        disk_list = disk_data
    else:
        return []

    if not isinstance(disk_list, list):
        disk_list = [disk_list]

    sensors: list[RaySharpSensorDescription] = []
    for i, _ in enumerate(disk_list):
        disk_num = i + 1
        sensors.extend(
            [
                RaySharpSensorDescription(
                    key=f"disk_{disk_num}_capacity",
                    translation_key="disk_capacity",
                    native_unit_of_measurement=UnitOfInformation.GIGABYTES,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    value_fn=lambda data, idx=i: _get_disk_value(
                        data, idx, "total_space"
                    ),
                ),
                RaySharpSensorDescription(
                    key=f"disk_{disk_num}_used",
                    translation_key="disk_used",
                    native_unit_of_measurement=UnitOfInformation.GIGABYTES,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    value_fn=lambda data, idx=i: _get_disk_value(
                        data, idx, "used_space"
                    ),
                ),
                RaySharpSensorDescription(
                    key=f"disk_{disk_num}_free",
                    translation_key="disk_free",
                    native_unit_of_measurement=UnitOfInformation.GIGABYTES,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    value_fn=lambda data, idx=i: _get_disk_value(
                        data, idx, "free_space"
                    ),
                ),
                RaySharpSensorDescription(
                    key=f"disk_{disk_num}_status",
                    translation_key="disk_status",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    value_fn=lambda data, idx=i: _get_disk_value(
                        data, idx, "status"
                    ),
                ),
            ]
        )
    return sensors


def _get_disk_list(data: dict[str, Any]) -> list:
    """Extract disk list from data."""
    disk_data = data.get(DATA_DISK_CONFIG)
    if not disk_data:
        return []
    if isinstance(disk_data, dict):
        disk_list = disk_data.get("disk_info", disk_data.get("disks", disk_data.get("disk", [])))
    elif isinstance(disk_data, list):
        disk_list = disk_data
    else:
        return []
    if not isinstance(disk_list, list):
        disk_list = [disk_list]
    return disk_list


def _get_disk_value(data: dict[str, Any], index: int, key: str) -> Any:
    """Get a disk value by index and key, converting MB to GB where needed."""
    disk_list = _get_disk_list(data)
    if index >= len(disk_list):
        return None
    disk = disk_list[index]
    if key == "total_space":
        mb = disk.get("total_size")
        return round(mb / 1024, 1) if mb is not None else None
    if key == "free_space":
        mb = disk.get("free_size")
        return round(mb / 1024, 1) if mb is not None else None
    if key == "used_space":
        total_mb = disk.get("total_size")
        free_mb = disk.get("free_size")
        if total_mb is not None and free_mb is not None:
            return round((total_mb - free_mb) / 1024, 1)
        return None
    return disk.get(key)


# ─── Setup ────────────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RaySharp NVR sensors."""
    coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[RaySharpSensor] = []

    for description in (
        DEVICE_INFO_SENSORS
        + SYSTEM_INFO_SENSORS
        + NETWORK_STATE_SENSORS
        + RECORD_INFO_SENSORS
        + AI_SENSORS
        + EVENT_PUSH_SENSORS
        + EXCEPTION_SENSORS
    ):
        if description.exists_fn(coordinator.data):
            entities.append(RaySharpSensor(coordinator, description))

    # Per-channel cross-counting sensors
    for description in _build_cc_stats_sensors(coordinator.data):
        entities.append(RaySharpSensor(coordinator, description))

    # Dynamic disk sensors
    for description in _build_disk_sensors(coordinator.data):
        entities.append(RaySharpSensor(coordinator, description))

    # Event-accumulator sensors with persistent storage
    mac = (coordinator.data.get(DATA_DEVICE_INFO, {}) or {}).get("mac_addr", "unknown")
    plates_sensor = RaySharpPlatesTrackerSensor(coordinator, mac, entry.entry_id)
    faces_sensor = RaySharpFacesTrackerSensor(coordinator, mac, entry.entry_id)
    entities.append(plates_sensor)  # type: ignore[arg-type]
    entities.append(faces_sensor)  # type: ignore[arg-type]

    # Store tracker references so the clear_detections_history service can find them
    hass.data.setdefault(DOMAIN_TRACKERS, {})[entry.entry_id] = {
        "plates": plates_sensor,
        "faces": faces_sensor,
    }

    async_add_entities(entities)


# ─── EventPush accumulator sensors (persistent storage) ───────────────────────

# Cyrillic plate letters that are visually identical to Latin letters.
# Russian plates officially use only these 12 Cyrillic characters.
# Some NVR firmware or OCR engines return them in Latin; normalise to Latin
# so plate comparison is consistent regardless of the source encoding.
_PLATE_CYR_TO_LAT = str.maketrans(
    "АВЕКМНОРСТУХавекмнорстух",
    "ABEKMHOPCTYXabekmhopctyx",
)

# Maximum gap (seconds) between two events of the *same* plate that are
# treated as a single detection (deduplication window).
_PLATE_DEDUP_SECS = 60


def _normalize_plate(text: str) -> str:
    """Normalise plate text: translate Cyrillic look-alikes → Latin, uppercase."""
    if not text:
        return ""
    return text.translate(_PLATE_CYR_TO_LAT).upper()


def _plates_are_same(p1: str, p2: str, min_common: int = 3) -> bool:
    """Return True if two normalised plate strings look like the same vehicle.

    Uses position-wise character matching: if at least *min_common* characters
    are identical at the same index, the plates are treated as duplicates.
    This tolerates single-character OCR mistakes (e.g. 'X' read as 'K') and
    partial reads (fewer characters), while still rejecting genuinely different
    plates that happen to share a few digits.
    """
    if p1 == p2:
        return True
    # Quick rejection: if length difference is too large, skip position check
    # but still allow substring match for partial reads.
    short, long_ = (p1, p2) if len(p1) <= len(p2) else (p2, p1)
    if len(short) < min_common:
        return False
    if short in long_:
        return True
    matches = sum(c1 == c2 for c1, c2 in zip(p1, p2))
    return matches >= min_common


def _grp_id_to_list_type(grp_id: Any) -> str:
    """Convert NVR face-group policy code to a list-type key.

    Face group Policy codes (0-based, from PlaceGroup/FaceGroup "policy" field):
      0 = Allow list (Разрешённые / Белый список)
      1 = Block list (Запрещённые / Черный список)
      2 = Stranger / Unknown (Незнакомец / Неизвестно)
    """
    try:
        code = int(grp_id)
    except (TypeError, ValueError):
        return "unknown"
    return {0: "allowed", 1: "blocked", 2: "stranger"}.get(code, "known")


def _plate_grp_id_to_list_type(grp_id: Any) -> str:
    """Convert NVR plate GrpId (1-based) to a list-type key.

    Plate GrpId values (NVR API PlateGroup.Id, 1-based):
      1 = Белый список  (Allow list)
      2 = Черный список (Block list)
      3 = Неизвестно    (Stranger / Unknown)
    """
    try:
        code = int(grp_id)
    except (TypeError, ValueError):
        return "unknown"
    return {1: "allowed", 2: "blocked", 3: "stranger"}.get(code, "known")


_LIST_TYPE_LABEL: dict[str, str] = {
    "allowed":    "Разрешённые",
    "blocked":    "Запрещённые",
    "stranger":   "Незнакомец",
    "unknown":    "Неизвестные",
    "known":      "В базе",
    "recognized": "Распознан",
}


class _BaseTrackerSensor(RaySharpEntity, SensorEntity):
    """Base class for EventPush accumulator sensors with persistent storage.

    Data is saved to HA's .storage/ directory so it survives restarts.
    Entries older than STORAGE_KEEP_DAYS days are automatically pruned.
    State = count in the last 24 hours.
    Use the clear_detections_history service to wipe stored data.
    """

    _MAX_ENTRIES = 5000
    _store_key: str  # override in subclass
    _alarm_type: str  # override in subclass

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        mac: str,
        entry_id: str,
    ) -> None:
        """Initialize the tracker sensor."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._entries: list[dict[str, Any]] = []
        self._store: Store | None = None
        self._save_unsub: Any = None
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _cutoff(self, hours: int = 24) -> str:
        return (datetime.now() - timedelta(hours=hours)).isoformat()

    @property
    def native_value(self) -> int:
        """Count of events in the last 24 h."""
        return sum(1 for e in self._entries if e.get("timestamp", "") >= self._cutoff(24))

    async def async_added_to_hass(self) -> None:
        """Load persisted data and subscribe to events."""
        await super().async_added_to_hass()

        self._store = Store(
            self.hass,
            STORAGE_VERSION,
            f"{self._store_key}_{self._entry_id}",
        )
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._entries = stored.get("entries", [])
        # Prune entries older than STORAGE_KEEP_DAYS on load
        cutoff = self._cutoff(STORAGE_KEEP_DAYS * 24)
        self._entries = [e for e in self._entries if e.get("timestamp", "") >= cutoff]
        self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SNAPSHOT, self._handle_snapshot)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending debounced save before removal."""
        if self._save_unsub:
            self._save_unsub()
            self._save_unsub = None

    @callback
    def _schedule_save(self) -> None:
        """Debounce: save to storage at most every STORAGE_SAVE_DELAY seconds."""
        if self._save_unsub:
            self._save_unsub()
        self._save_unsub = async_call_later(
            self.hass, STORAGE_SAVE_DELAY, self._do_save
        )

    @callback
    def _do_save(self, _now: Any) -> None:
        self._save_unsub = None
        if self._store:
            self.hass.async_create_task(
                self._store.async_save({"entries": self._entries})
            )

    def _append_entry(self, entry: dict[str, Any]) -> None:
        """Add entry, prune old data, cap size, schedule save."""
        self._entries.append(entry)
        cutoff = self._cutoff(STORAGE_KEEP_DAYS * 24)
        self._entries = [e for e in self._entries if e.get("timestamp", "") >= cutoff]
        if len(self._entries) > self._MAX_ENTRIES:
            self._entries = self._entries[-self._MAX_ENTRIES:]
        self.async_write_ha_state()
        self._schedule_save()

    async def async_clear(self) -> None:
        """Clear all stored entries and persist the empty state."""
        self._entries = []
        if self._store:
            await self._store.async_save({"entries": []})
        self.async_write_ha_state()

    @callback
    def _handle_snapshot(self, event: Any) -> None:
        raise NotImplementedError


class RaySharpPlatesTrackerSensor(_BaseTrackerSensor):
    """Accumulates license plate numbers received via EventPush.

    plate text is only available from real-time EventPush (not NVR history API).
    Plates are stored for STORAGE_KEEP_DAYS days (default 30).
    Attributes:
      plates        — detections in last 24 h [{plate_number, channel, time}]
      unique_plates — unique plate numbers in last 24 h (ordered by first seen)
      unique_count  — count of unique plates in last 24 h
      total_stored  — total entries across all stored days
    """

    _store_key = STORAGE_KEY_PLATES
    _alarm_type = ALARM_TYPE_PLATE

    def __init__(
        self, coordinator: RaySharpNVRCoordinator, mac: str, entry_id: str
    ) -> None:
        super().__init__(coordinator, mac, entry_id)
        self._attr_unique_id = f"{mac}_plates_tracker"
        self._attr_name = "Plates Detected Today"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cutoff = self._cutoff(24)
        recent = [e for e in self._entries if e.get("timestamp", "") >= cutoff]
        unique_plates = list(dict.fromkeys(
            e["plate_number"] for e in recent if e.get("plate_number")
        ))
        return {
            "plates": recent,
            "unique_plates": unique_plates,
            "unique_count": len(unique_plates),
            "total_stored": len(self._entries),
        }

    @callback
    def _handle_snapshot(self, event: Any) -> None:
        data = event.data
        if data.get("alarm_type") != ALARM_TYPE_PLATE:
            return
        plate = data.get("plate_number", "")
        if not plate:
            return
        now = datetime.now()
        # ── Deduplication: skip if same plate seen within the last window ──────
        plate_norm = _normalize_plate(plate)
        dedup_cutoff = (now - timedelta(seconds=_PLATE_DEDUP_SECS)).isoformat()
        for e in reversed(self._entries):
            if e.get("timestamp", "") < dedup_cutoff:
                break
            if _plates_are_same(plate_norm, _normalize_plate(e.get("plate_number", ""))):
                return  # Duplicate within dedup window, discard
        # ─────────────────────────────────────────────────────────────────────
        entry: dict[str, Any] = {
            "plate_number": plate,
            "channel": data.get("channel", 0),
            "timestamp": now.isoformat(),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # Copy fields that might have arrived directly in the snapshot
        for field in ("car_brand", "car_type", "car_color", "grp_id"):
            if data.get(field) is not None:
                entry[field] = data[field]
        # Set list_type from grp_id if already known; otherwise enrich via API
        if "grp_id" in entry:
            entry["list_type"] = _plate_grp_id_to_list_type(entry["grp_id"])
            entry["list_type_label"] = _LIST_TYPE_LABEL.get(entry["list_type"], entry["list_type"])
        self._append_entry(entry)
        # Async DB enrichment to fill list_type / car_brand if not yet known
        if "list_type" not in entry:
            self.hass.async_create_task(self._enrich_plate_entry(entry))

    async def _enrich_plate_entry(self, entry: dict[str, Any]) -> None:
        """Look up a plate in the NVR database and enrich the entry in-place."""
        plate = entry.get("plate_number", "")
        try:
            resp = await self.coordinator.client.async_api_call(
                API_AI_ADDED_PLATES_GET, {"PlatesId": [plate]}
            )
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            plate_list = data.get("PlateInfo", [])
            if plate_list:
                info = plate_list[0]
                entry.setdefault("car_brand", info.get("CarBrand", ""))
                entry.setdefault("owner", info.get("Owner", ""))
                grp_id = info.get("GrpId")
                entry["grp_id"] = grp_id
                list_type = _plate_grp_id_to_list_type(grp_id)
            else:
                list_type = "unknown"
        except (RaySharpNVRConnectionError, Exception):
            list_type = "unknown"
        entry["list_type"] = list_type
        entry["list_type_label"] = _LIST_TYPE_LABEL.get(list_type, list_type)
        self.async_write_ha_state()
        self._schedule_save()


class RaySharpFacesTrackerSensor(_BaseTrackerSensor):
    """Accumulates face detection events received via EventPush.

    Faces are stored for STORAGE_KEEP_DAYS days (default 30).
    Attributes:
      detections  — detections in last 24 h [{channel, snap_id, face_id?, time}]
      total_count — count of detections in last 24 h
      total_stored — total entries across all stored days
    """

    _store_key = STORAGE_KEY_FACES
    _alarm_type = ALARM_TYPE_FACE

    def __init__(
        self, coordinator: RaySharpNVRCoordinator, mac: str, entry_id: str
    ) -> None:
        super().__init__(coordinator, mac, entry_id)
        self._attr_unique_id = f"{mac}_faces_tracker"
        self._attr_name = "Faces Detected Today"
        self._face_groups: dict[Any, dict] = {}  # group_id → {name, policy}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cutoff = self._cutoff(24)
        recent = [e for e in self._entries if e.get("timestamp", "") >= cutoff]
        return {
            "detections": recent,
            "total_count": len(recent),
            "total_stored": len(self._entries),
        }

    async def async_added_to_hass(self) -> None:
        """Load persisted data, fetch face groups, subscribe to events."""
        await super().async_added_to_hass()
        # Pre-cache face groups so we can resolve group policy instantly
        self.hass.async_create_task(self._load_face_groups())

    async def _load_face_groups(self) -> None:
        """Fetch face group definitions from the NVR and cache them."""
        try:
            resp = await self.coordinator.client.async_api_call(API_AI_FD_GROUPS, {})
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            groups = data.get("group_info", data.get("groups", data.get("items", [])))
            if isinstance(groups, list):
                # Build {group_id: {"name": ..., "policy": 0/1/2}} mapping
                self._face_groups = {
                    g.get("group_id", g.get("id")): g
                    for g in groups
                    if isinstance(g, dict)
                }
        except Exception:
            pass  # Groups stay empty — list_type will be set based on grp_id code

    @callback
    def _handle_snapshot(self, event: Any) -> None:
        data = event.data
        if data.get("alarm_type") != ALARM_TYPE_FACE:
            return
        now = datetime.now()
        entry: dict[str, Any] = {
            "channel": data.get("channel", 0),
            "snap_id": data.get("snap_id"),
            "timestamp": now.isoformat(),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for field in ("face_id", "face_name", "grp_id", "similarity"):
            if data.get(field) is not None:
                entry[field] = data[field]

        # Resolve list type immediately if grp_id is already available
        if "grp_id" in entry:
            grp = self._face_groups.get(entry["grp_id"], {})
            policy = grp.get("policy", entry["grp_id"])
            list_type = _grp_id_to_list_type(policy)
            entry["list_type"] = list_type
            entry["list_type_label"] = _LIST_TYPE_LABEL.get(list_type, list_type)
            entry.setdefault("face_name", grp.get("name", ""))
        elif entry.get("face_id") is not None:
            # Recognised face but no group info yet — enrich async
            entry["list_type"] = "recognized"
            entry["list_type_label"] = _LIST_TYPE_LABEL["recognized"]
        else:
            entry["list_type"] = "stranger"
            entry["list_type_label"] = _LIST_TYPE_LABEL["stranger"]

        self._append_entry(entry)


class RaySharpSensor(RaySharpEntity, SensorEntity):
    """Sensor entity for RaySharp NVR."""

    entity_description: RaySharpSensorDescription

    def __init__(
        self,
        coordinator: RaySharpNVRCoordinator,
        description: RaySharpSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_data = coordinator.data.get(DATA_DEVICE_INFO, {}) or {}
        mac = device_data.get("mac_addr", "unknown")
        self._attr_unique_id = f"{mac}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)
