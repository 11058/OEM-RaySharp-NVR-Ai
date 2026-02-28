"""The RaySharp NVR integration."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from aiohttp import web

from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.network import get_url

from .api_client import RaySharpNVRClient, RaySharpNVRConnectionError
from .const import (
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
    API_AI_ADDED_PLATES_GET,
    API_AI_FACES,
    API_AI_FACES_GET_BY_INDEX,
    API_AI_FD_GROUPS,
    API_AI_OBJECTS_GET_BY_INDEX,
    API_AI_PLATES,
    API_EVENT_PUSH_SET,
    API_MANUAL_ALARM_SET,
    API_PTZ_CONTROL,
    API_RECORD_SEARCH,
    API_SNAPSHOT,
    CONF_EVENT_PUSH_AUTO_CONFIGURE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    DATA_DEVICE_INFO,
    DATA_EVENT_PUSH_CONFIG,
    DEFAULT_EVENT_PUSH_AUTO_CONFIGURE,
    DOMAIN,
    DOMAIN_TRACKERS,
    EVENT_ALARM,
    EVENT_SNAPSHOT,
    PLATFORMS,
    PTZ_STATE_START,
    SERVICE_CLEAR_DETECTIONS,
    SERVICE_CONFIGURE_EVENT_PUSH,
    SERVICE_GET_PLATE_DATABASE_INFO,
    SERVICE_GET_SNAPSHOT,
    SERVICE_PTZ_CONTROL,
    SERVICE_SEARCH_FACES,
    SERVICE_SEARCH_PLATES,
    SERVICE_SEARCH_RECORDS,
    SERVICE_TRIGGER_ALARM_OUTPUT,
    WEBHOOK_ID_PREFIX,
)
from .coordinator import RaySharpNVRCoordinator

_LOGGER = logging.getLogger(__name__)

# ─── NVR alarm-type normalization map ─────────────────────────────────────────
NVR_ALARM_TYPE_MAP: dict[str, str] = {
    # Motion
    "motion": ALARM_TYPE_MOTION,
    "md": ALARM_TYPE_MOTION,
    "VMD": ALARM_TYPE_MOTION,
    "MotionDetect": ALARM_TYPE_MOTION,
    "VideoMotion": ALARM_TYPE_MOTION,
    # Person / Human
    "person": ALARM_TYPE_PERSON,
    "pd": ALARM_TYPE_PERSON,
    "PD": ALARM_TYPE_PERSON,
    "PVD_person": ALARM_TYPE_PERSON,
    "human": ALARM_TYPE_PERSON,
    "HumanDetect": ALARM_TYPE_PERSON,
    # Vehicle
    "vehicle": ALARM_TYPE_VEHICLE,
    "vd": ALARM_TYPE_VEHICLE,
    "PVD_vehicle": ALARM_TYPE_VEHICLE,
    "car": ALARM_TYPE_VEHICLE,
    "VehicleDetect": ALARM_TYPE_VEHICLE,
    # Line Crossing
    "line_crossing": ALARM_TYPE_LINE_CROSSING,
    "lcd": ALARM_TYPE_LINE_CROSSING,
    "LCD": ALARM_TYPE_LINE_CROSSING,
    "LineCross": ALARM_TYPE_LINE_CROSSING,
    "LineCrossing": ALARM_TYPE_LINE_CROSSING,
    # Intrusion / Perimeter
    "intrusion": ALARM_TYPE_INTRUSION,
    "pid": ALARM_TYPE_INTRUSION,
    "SOD": ALARM_TYPE_INTRUSION,
    "RegionDetect": ALARM_TYPE_INTRUSION,
    "PID": ALARM_TYPE_INTRUSION,
    "PerimeterIntrusion": ALARM_TYPE_INTRUSION,
    # Face
    "face": ALARM_TYPE_FACE,
    "fd": ALARM_TYPE_FACE,
    "FD": ALARM_TYPE_FACE,
    "FaceDetect": ALARM_TYPE_FACE,
    "FaceDetection": ALARM_TYPE_FACE,
    # License Plate
    "plate": ALARM_TYPE_PLATE,
    "lpr": ALARM_TYPE_PLATE,
    "LPR": ALARM_TYPE_PLATE,
    "LicensePlate": ALARM_TYPE_PLATE,
    "LPD": ALARM_TYPE_PLATE,
    "lpd": ALARM_TYPE_PLATE,
    "lp": ALARM_TYPE_PLATE,
    "LP": ALARM_TYPE_PLATE,
    # IO Alarm
    "io": ALARM_TYPE_IO,
    "IO": ALARM_TYPE_IO,
    "AlarmInput": ALARM_TYPE_IO,
    "IOAlarm": ALARM_TYPE_IO,
    # Stationary Object
    "sod": ALARM_TYPE_SOD,
    "stationary_object": ALARM_TYPE_SOD,
    "StationaryObject": ALARM_TYPE_SOD,
    "SODAlarm": ALARM_TYPE_SOD,
    # Sound Detection
    "sound": ALARM_TYPE_SOUND,
    "rsd": ALARM_TYPE_SOUND,
    "SoundDetection": ALARM_TYPE_SOUND,
    "RSD": ALARM_TYPE_SOUND,
    # Crowd Density
    "crowd": "crowd",
    "CrowdDensity": "crowd",
    "CD": "crowd",
    # Wander
    "wander": ALARM_TYPE_WANDER,
    "WanderDetection": ALARM_TYPE_WANDER,
    # Region Entrance / Exiting
    "region_entrance": ALARM_TYPE_REGION_ENTRANCE,
    "RegionEntrance": ALARM_TYPE_REGION_ENTRANCE,
    "region_exiting": ALARM_TYPE_REGION_EXITING,
    "RegionExiting": ALARM_TYPE_REGION_EXITING,
    # Occlusion
    "occlusion": ALARM_TYPE_OCCLUSION,
    "OcclusionDetection": ALARM_TYPE_OCCLUSION,
    # PIR
    "pir": ALARM_TYPE_PIR,
    "PIR": ALARM_TYPE_PIR,
}


def _get_webhook_id(entry: ConfigEntry) -> str:
    """Generate a unique webhook ID from the config entry."""
    return f"{WEBHOOK_ID_PREFIX}{entry.entry_id}"


def _normalize_alarm_type(raw_type: str) -> str:
    """Normalize an NVR alarm type string to a standard type.

    Handles plain types ("pd", "MD"), compound NVR subtypes ("pd_vd",
    "md_vd") where the prefix before the first "_" is the detection category.
    """
    if raw_type in NVR_ALARM_TYPE_MAP:
        return NVR_ALARM_TYPE_MAP[raw_type]
    raw_lower = raw_type.lower()
    for key, value in NVR_ALARM_TYPE_MAP.items():
        if key.lower() == raw_lower:
            return value
    # Compound subtype: "pd_vd" → try prefix "pd"
    if "_" in raw_lower:
        prefix = raw_lower.split("_")[0]
        if prefix in NVR_ALARM_TYPE_MAP:
            return NVR_ALARM_TYPE_MAP[prefix]
        for key, value in NVR_ALARM_TYPE_MAP.items():
            if key.lower() == prefix:
                return value
    _LOGGER.debug("Unknown NVR alarm type '%s', defaulting to motion", raw_type)
    return ALARM_TYPE_MOTION


# ─── Webhook handler ──────────────────────────────────────────────────────────

async def _handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    """Handle incoming webhook from NVR EventPush."""
    try:
        payload = await request.json()
    except Exception:
        try:
            payload = dict(await request.post())
        except Exception:
            text = await request.text()
            _LOGGER.debug("Received non-JSON webhook payload: %s", text[:500])
            payload = {"raw": text}

    _LOGGER.debug("Received NVR EventPush webhook: %s", payload)

    # Log plate/face events at INFO level so they appear without debug mode,
    # and write the raw payload to a debug file for format verification.
    _webhook_log_interesting(payload)

    # Handle AI snapshot events separately
    snapshots = _parse_snapshot_payload(payload)
    for snap in snapshots:
        hass.bus.async_fire(EVENT_SNAPSHOT, snap)
        _LOGGER.debug("Fired snapshot event for channel %s", snap.get("channel"))

    # Handle alarm events
    events = _parse_alarm_payload(payload)
    for event_data in events:
        hass.bus.async_fire(EVENT_ALARM, event_data)

    return web.Response(text="OK", status=200)


def _webhook_log_interesting(payload: Any) -> None:
    """Log plate/face webhook events at INFO level and append to debug file.

    Writes the raw NVR payload to /config/nvr_webhook_debug.json (appending
    up to 50 entries) whenever the webhook contains plate or face data.
    Removed once the format is confirmed working.
    """
    if not isinstance(payload, dict):
        return
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return
    ai_snap = data.get("ai_snap_picture", {})
    if not isinstance(ai_snap, dict):
        ai_snap = {}
    plates = ai_snap.get("PlateInfo", [])
    faces = ai_snap.get("FaceInfo", [])
    alarm_list = data.get("alarm_list", [])

    if plates:
        _LOGGER.info(
            "NVR EventPush: %d plate(s) in ai_snap_picture.PlateInfo — %s",
            len(plates),
            [{"ch": p.get("StrChn"), "plate": p.get("Id") or p.get("SnapId"), "grp": p.get("GrpId")} for p in plates if isinstance(p, dict)],
        )
    if faces:
        _LOGGER.info(
            "NVR EventPush: %d face(s) in ai_snap_picture.FaceInfo",
            len(faces),
        )

    # Log alarm_list entries that look plate/face related
    if isinstance(alarm_list, list):
        for entry in alarm_list:
            for ch_alarm in (entry.get("channel_alarm", []) if isinstance(entry, dict) else []):
                subtype = (ch_alarm.get("int_alarm", {}) or {}).get("int_subtype", "")
                if subtype and any(k in subtype.lower() for k in ("lp", "lpr", "plate", "fd", "face")):
                    _LOGGER.info(
                        "NVR EventPush alarm_list: channel=%s int_subtype=%s",
                        ch_alarm.get("channel"), subtype,
                    )

    # Write raw payload to debug file when it contains something interesting
    if plates or faces or (
        isinstance(alarm_list, list) and any(
            any(
                any(k in str((ca.get("int_alarm") or {}).get("int_subtype", "")).lower()
                    for k in ("lp", "lpr", "plate", "fd", "face"))
                for ca in (e.get("channel_alarm", []) if isinstance(e, dict) else [])
            )
            for e in alarm_list
        )
    ):
        import json as _json
        import os as _os
        debug_path = "/config/nvr_plate_face_debug.json"
        try:
            existing: list = []
            if _os.path.exists(debug_path):
                with open(debug_path) as f:
                    existing = _json.load(f)
            if not isinstance(existing, list):
                existing = []
            # Strip base64 images to keep file small
            stripped = _json.loads(_json.dumps(payload))
            _strip_images(stripped)
            existing.append(stripped)
            # Keep only last 50 entries
            if len(existing) > 50:
                existing = existing[-50:]
            with open(debug_path, "w") as f:
                _json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            _LOGGER.debug("Failed to write plate/face debug file: %s", exc)


def _strip_images(obj: Any) -> None:
    """Recursively remove base64 image fields to keep debug files small."""
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in ("ObjectImage", "BgImg", "PlateImg", "Image2", "Image4", "FaceImage"):
                obj[key] = "<base64_stripped>"
            else:
                _strip_images(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            _strip_images(item)


# AI detection Type codes from NVR ai_snap_picture payload
_AI_SNAP_TYPE_MAP: dict[int, str] = {
    1: ALARM_TYPE_PERSON,
    2: ALARM_TYPE_VEHICLE,
    3: ALARM_TYPE_FACE,
    4: ALARM_TYPE_PLATE,
    5: ALARM_TYPE_INTRUSION,
    6: ALARM_TYPE_LINE_CROSSING,
}


# Extra NVR field → HA snapshot key mapping for SnapedObjInfo entries
_NVR_SNAP_FIELD_MAP: dict[str, str] = {
    "PlateNum": "plate_number",
    "FaceId": "face_id",
    "Name": "face_name",       # matched face name (if NVR sends it)
    "GrpId": "grp_id",         # group ID
    "Similarity": "similarity", # face match confidence 0-100
    "CarBrand": "car_brand",
    "CarType": "car_type",
    "CarColor": "car_color",
    "CarNum": "plate_number",   # alternative plate field on some firmware
}


def _parse_snapshot_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse NVR ai_snap_picture payload into snapshot event dicts.

    Handles three sub-arrays inside ai_snap_picture:
      • SnapedObjInfo — person / vehicle / intrusion / line-crossing detections
      • PlateInfo     — LPD license plate detections (Type=10 / GrpId 1-based)
      • FaceInfo      — FD face detections / recognition results

    NVR push format (EventPush webhook or Event Check response):
      {"data": {"ai_snap_picture": {
        "SnapedObjInfo": [...],
        "PlateInfo": [...],
        "FaceInfo": [...]
      }}}
    """
    snapshots: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return snapshots
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return snapshots
    ai_snap = data.get("ai_snap_picture")
    if not isinstance(ai_snap, dict):
        return snapshots

    # ── SnapedObjInfo: person / vehicle / intrusion / line-crossing ──────────
    for item in ai_snap.get("SnapedObjInfo", []):
        if not isinstance(item, dict):
            continue
        ch_str = str(item.get("StrChn", item.get("Chn", "")))
        channel = _channel_str_to_int(ch_str)
        snap_type = item.get("Type", 0)
        alarm_type = _AI_SNAP_TYPE_MAP.get(snap_type, ALARM_TYPE_MOTION)
        snap: dict[str, Any] = {
            "channel": channel,
            "channel_str": ch_str,
            "alarm_type": alarm_type,
            "snap_id": item.get("SnapId"),
            "start_time": item.get("StartTime"),
            "end_time": item.get("EndTime"),
            "image": item.get("ObjectImage", ""),
        }
        for nvr_key, snap_key in _NVR_SNAP_FIELD_MAP.items():
            if nvr_key in item and snap_key not in snap:
                snap[snap_key] = item[nvr_key]
        snapshots.append(snap)

    # ── PlateInfo: License Plate Detection (LPD) ─────────────────────────────
    # Fields: Id (plate text for DB-registered plates, empty for strangers),
    # SnapId (OCR-read plate text used as snapshot ID — the only text field
    # populated for GrpId=3 stranger plates),
    # GrpId (1=allow/2=block/3=stranger), Chn (0-based index),
    # StrChn (e.g. "CH16"), PlateImg (plate crop), BgImg (background),
    # CarBrand, CarType, CarColor, StartTime, EndTime.
    for item in ai_snap.get("PlateInfo", []):
        if not isinstance(item, dict):
            continue
        ch_str = str(item.get("StrChn", item.get("Chn", "")))
        channel = _channel_str_to_int(ch_str)
        snap: dict[str, Any] = {
            "channel": channel,
            "channel_str": ch_str,
            "alarm_type": ALARM_TYPE_PLATE,
            "snap_id": item.get("SnapId"),
            "start_time": item.get("StartTime"),
            "end_time": item.get("EndTime"),
            # For registered plates (GrpId 1/2) Id = DB plate text.
            # For stranger plates (GrpId 3) Id is empty; SnapId carries the OCR text.
            "plate_number": item.get("Id") or item.get("SnapId", ""),
            "grp_id": item.get("GrpId"),
            "car_brand": item.get("CarBrand", ""),
            "car_type": item.get("CarType", ""),
            "car_color": item.get("CarColor", ""),
            # BgImg shows the vehicle in context; PlateImg is the plate crop.
            # Prefer BgImg for the image entity (more informative).
            "image": item.get("BgImg", item.get("PlateImg", "")),
        }
        _LOGGER.debug(
            "LPD snapshot: channel=%s plate=%s grp=%s",
            channel, snap["plate_number"], snap["grp_id"],
        )
        snapshots.append(snap)

    # ── FaceInfo: Face Detection / Recognition ────────────────────────────────
    # Fields: Id (face id), GrpId (0=allow/1=block/2=stranger for faces),
    # Chn (0-based), StrChn, Score (similarity 0-100), Image2 (captured face),
    # Image4 (background), Name (matched name), Sex, Age, StartTime, EndTime.
    for item in ai_snap.get("FaceInfo", []):
        if not isinstance(item, dict):
            continue
        ch_str = str(item.get("StrChn", item.get("Chn", "")))
        channel = _channel_str_to_int(ch_str)
        snap: dict[str, Any] = {
            "channel": channel,
            "channel_str": ch_str,
            "alarm_type": ALARM_TYPE_FACE,
            "snap_id": item.get("SnapId"),
            "start_time": item.get("StartTime"),
            "end_time": item.get("EndTime"),
            "face_id": item.get("Id"),
            "grp_id": item.get("GrpId"),
            "similarity": item.get("Score"),
            "face_name": item.get("Name", ""),
            # Image2 = captured face crop; Image4 = full background frame.
            "image": item.get("Image2", item.get("Image4", "")),
        }
        _LOGGER.debug(
            "FD snapshot: channel=%s face_id=%s grp=%s score=%s",
            channel, snap["face_id"], snap["grp_id"], snap["similarity"],
        )
        snapshots.append(snap)

    return snapshots


def _parse_alarm_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse NVR alarm payload into a list of event data dicts."""
    events: list[dict[str, Any]] = []

    if not isinstance(payload, dict):
        return [{"alarm_type": ALARM_TYPE_MOTION, "channel": 0, "raw": str(payload)}]

    data = payload.get("data", payload)
    if not isinstance(data, dict):
        data = payload

    # ── Format 1: RaySharp native EventPush alarm_list ─────────────────────
    # {"data": {"alarm_list": [{"time": "...", "channel_alarm": [
    #   {"channel": "CH17", "int_alarm": {"int_subtype": "pd_vd"}}]}]}}
    alarm_list = data.get("alarm_list")
    if isinstance(alarm_list, list):
        for alarm_entry in alarm_list:
            timestamp = alarm_entry.get("time")
            for ch_alarm in alarm_entry.get("channel_alarm", []):
                ch_str = str(ch_alarm.get("channel", ""))
                channel = _channel_str_to_int(ch_str)
                int_alarm = ch_alarm.get("int_alarm", {})
                raw_type = str(int_alarm.get("int_subtype", "motion"))
                alarm_type = _normalize_alarm_type(raw_type)
                event: dict[str, Any] = {"alarm_type": alarm_type, "channel": channel}
                if timestamp:
                    event["timestamp"] = timestamp
                events.append(event)
        if events:
            return events

    # ── Format 2: List-based formats (events / alarms / alarm) ─────────────
    event_list = data.get("events", data.get("alarms", data.get("alarm", [])))
    if isinstance(event_list, list) and event_list:
        for item in event_list:
            if isinstance(item, dict):
                events.append(_parse_single_event(item))
        return events

    # ── Format 3: Flat dict with recognized alarm keys ──────────────────────
    if any(
        k in data
        for k in ("alarm_type", "type", "AlarmType", "event_type", "channel", "Chn")
    ):
        events.append(_parse_single_event(data))
        return events

    events.append({"alarm_type": ALARM_TYPE_MOTION, "channel": 0, "raw": data})
    return events


def _channel_str_to_int(ch: str) -> int:
    """Convert a channel string like 'CH17' or '17' to int."""
    ch = ch.strip()
    if ch.upper().startswith("CH"):
        try:
            return int(ch[2:])
        except ValueError:
            pass
    try:
        return int(ch)
    except (ValueError, TypeError):
        return 0


def _parse_single_event(data: dict[str, Any]) -> dict[str, Any]:
    """Parse a single alarm event dict."""
    raw_type = (
        data.get("alarm_type")
        or data.get("type")
        or data.get("AlarmType")
        or data.get("event_type")
        or "motion"
    )
    alarm_type = _normalize_alarm_type(str(raw_type))

    channel_raw = data.get("channel") or data.get("Chn") or data.get("ch") or 0
    channel = _channel_str_to_int(str(channel_raw))

    event: dict[str, Any] = {
        "alarm_type": alarm_type,
        "channel": channel,
    }
    for key in (
        "timestamp",
        "time",
        "details",
        "object_type",
        "zone",
        "confidence",
        "plate_number",
        "face_id",
    ):
        if key in data:
            event[key] = data[key]

    return event


# ─── Auto-configure EventPush ─────────────────────────────────────────────────

async def _async_configure_event_push(
    coordinator: RaySharpNVRCoordinator, entry: ConfigEntry, hass: HomeAssistant
) -> None:
    """Configure the NVR to push events to the HA webhook.

    The NVR's EventPush Set endpoint accepts a flat configuration dict.
    The webhook URL/ID is logged at INFO level so the user can also configure
    it manually in the NVR UI under Push-events (Push-события).
    """
    try:
        internal_url = get_url(hass, prefer_external=False)
    except Exception:
        _LOGGER.warning(
            "Could not determine Home Assistant internal URL for EventPush config"
        )
        return

    webhook_id = _get_webhook_id(entry)
    webhook_path = f"/api/webhook/{webhook_id}"
    parsed = urlparse(internal_url)
    ha_host = parsed.hostname or "127.0.0.1"
    ha_port = parsed.port or 8123

    _LOGGER.info(
        "EventPush webhook URL (use this in NVR Push-events settings): "
        "http://%s:%s%s",
        ha_host, ha_port, webhook_path,
    )

    # NVR EventPush Set uses the same nested params.table structure as Get.
    # Field "method" (not "push_method") mirrors what the NVR returns on Get.
    push_config = {
        "params": {
            "name": "HA",
            "table": {
                "addr": ha_host,
                "port": ha_port,
                "url": webhook_path,
                "enable": True,
                "method": "POST",
                "auth_enable": False,
                "keep_alive_interval": "0",
                "push_way": "HTTP",
            },
        }
    }

    try:
        await coordinator.client.async_api_call(API_EVENT_PUSH_SET, push_config)
        _LOGGER.info(
            "Configured NVR EventPush → %s:%s%s", ha_host, ha_port, webhook_path
        )
    except RaySharpNVRConnectionError as err:
        _LOGGER.warning("Failed to configure NVR EventPush: %s", err)
    except Exception:
        _LOGGER.warning("Failed to configure NVR EventPush", exc_info=True)


# ─── Service handlers ─────────────────────────────────────────────────────────

def _get_coordinator_for_entry(
    hass: HomeAssistant, config_entry_id: str
) -> RaySharpNVRCoordinator | None:
    """Return the coordinator for the given config entry ID."""
    return hass.data.get(DOMAIN, {}).get(config_entry_id)


async def _async_handle_ptz_control(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the ptz_control service call."""
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    channel_num = call.data["channel"]
    command = call.data["command"]
    state = call.data.get("state", PTZ_STATE_START)
    speed = call.data.get("speed", 50)
    preset_num = call.data.get("preset_num")

    payload: dict[str, Any] = {
        "channel": f"CH{channel_num}",
        "cmd": command,
        "state": state,
        "speed": speed,
    }
    if preset_num is not None:
        payload["preset_num"] = preset_num

    try:
        await coordinator.client.async_api_call(API_PTZ_CONTROL, payload)
        _LOGGER.debug(
            "PTZ control sent: channel=%s cmd=%s speed=%s state=%s",
            channel_num,
            command,
            speed,
            state,
        )
    except RaySharpNVRConnectionError as err:
        _LOGGER.error("PTZ control failed: %s", err)


async def _async_handle_get_snapshot(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the get_snapshot service call."""
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    channel_num = call.data["channel"]
    payload = {
        "channel": f"CH{channel_num}",
        "snapshot_resolution": "1280 x 720",
        "reset_session_timeout": False,
    }

    try:
        response = await coordinator.client.async_api_call(API_SNAPSHOT, payload)
        snap_data = response.get("data", response) if isinstance(response, dict) else {}

        event_data = {
            "channel": channel_num,
            "config_entry_id": entry_id,
            "img_format": snap_data.get("img_format", "image/jpeg"),
            "img_encodes": snap_data.get("img_encodes", "base64"),
            "ima_time": snap_data.get("ima_time"),
            "ima_data": snap_data.get("ima_data", ""),
        }
        hass.bus.async_fire("raysharp_nvr_snapshot", event_data)
        _LOGGER.debug("Snapshot captured for channel %d", channel_num)
    except RaySharpNVRConnectionError as err:
        _LOGGER.error("Failed to get snapshot for channel %d: %s", channel_num, err)


async def _async_handle_trigger_alarm_output(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the trigger_alarm_output service call."""
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    output_id = call.data["output_id"]
    active = call.data.get("active", True)
    payload = {output_id: active}

    try:
        await coordinator.client.async_api_call(API_MANUAL_ALARM_SET, payload)
        _LOGGER.debug("Alarm output %s set to %s", output_id, active)
    except RaySharpNVRConnectionError as err:
        _LOGGER.error("Failed to trigger alarm output %s: %s", output_id, err)


async def _async_handle_search_records(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the search_records service call."""
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    channel_num = call.data["channel"]
    start_time = call.data["start_time"]
    end_time = call.data["end_time"]
    record_type = call.data.get("record_type", "all")

    # Convert datetime objects to strings if needed
    if hasattr(start_time, "isoformat"):
        start_time = start_time.isoformat()
    if hasattr(end_time, "isoformat"):
        end_time = end_time.isoformat()

    payload: dict[str, Any] = {
        "start_time": start_time,
        "end_time": end_time,
        "record_type": record_type,
    }
    if channel_num > 0:
        payload["channel"] = f"CH{channel_num}"

    try:
        response = await coordinator.client.async_api_call(API_RECORD_SEARCH, payload)
        result_data = response.get("data", response) if isinstance(response, dict) else {}
        hass.bus.async_fire(
            "raysharp_nvr_record_search_result",
            {
                "channel": channel_num,
                "config_entry_id": entry_id,
                "records": result_data,
            },
        )
        _LOGGER.debug("Record search completed for channel %d", channel_num)
    except RaySharpNVRConnectionError as err:
        _LOGGER.error("Record search failed: %s", err)


async def _async_handle_search_plates(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the search_plates service call.

    Two-step NVR search:
    1. SearchPlate → returns Count of matching records.
    2. GetByIndex (paginated) → returns SnapedObjInfo records with images and
       vehicle attributes (CarType, CarColor, CarBrand).

    NOTE: The NVR does not include OCR'd plate number text in SnapedObjInfo
    records. Plate numbers are stored in the database and are only available
    via get_plate_database_info. For real-time plate numbers use EventPush
    webhook events (they include PlateNum in the ai_snap_picture payload).
    """
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    start_time = call.data["start_time"]
    end_time = call.data["end_time"]
    channel = call.data.get("channel", 0)
    include_images = call.data.get("include_images", False)
    plate_filter = call.data.get("plate_numbers", [])
    max_results = call.data.get("max_results", 100)

    # NVR SearchPlate uses 0-based channel indexing (CH16 → Chn=15).
    # All-channel search (Chn=[]) may return "no_permission" on some firmware;
    # per-channel search always works.
    search_payload: dict[str, Any] = {
        "MsgId": None,
        "StartTime": start_time,
        "EndTime": end_time,
        "Chn": [channel - 1] if channel > 0 else [],
        "SortType": 1,
        "Engine": 0,
    }
    if plate_filter:
        search_payload["PlatesId"] = plate_filter

    try:
        resp = await coordinator.client.async_api_call(API_AI_PLATES, search_payload)
        search_data = resp.get("data", resp) if isinstance(resp, dict) else {}
        count = search_data.get("Count", 0)
        _LOGGER.debug("Plate search found %d records", count)

        if count == 0:
            hass.bus.async_fire(
                "raysharp_nvr_plates_result",
                {"config_entry_id": entry_id, "count": 0, "plates": []},
            )
            return

        # Retrieve records in pages (max 50 per call)
        fetch_count = min(count, max_results)
        page_size = 50
        all_records: list[dict[str, Any]] = []

        for start_idx in range(0, fetch_count, page_size):
            batch = min(page_size, fetch_count - start_idx)
            get_payload: dict[str, Any] = {
                "MsgId": None,
                "Engine": 0,
                "StartIndex": start_idx,
                "Count": batch,
                "SimpleInfo": 0,
                "WithObjectImage": 1 if include_images else 0,
                "WithBackgroud": 0,
            }
            get_resp = await coordinator.client.async_api_call(
                API_AI_OBJECTS_GET_BY_INDEX, get_payload
            )
            get_data = get_resp.get("data", get_resp) if isinstance(get_resp, dict) else {}
            records = get_data.get("SnapedObjInfo", [])
            all_records.extend(records)

        hass.bus.async_fire(
            "raysharp_nvr_plates_result",
            {
                "config_entry_id": entry_id,
                "count": count,
                "fetched": len(all_records),
                "plates": all_records,
            },
        )
        _LOGGER.debug("Plate search complete: %d records retrieved", len(all_records))

    except RaySharpNVRConnectionError as err:
        _LOGGER.error("Plate search failed: %s", err)


async def _async_handle_search_faces(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the search_faces service call.

    Two-step NVR search:
    1. SnapedFaces/Search → returns Count.
    2. GetByIndex (paginated) → returns SnapedFaceInfo records with face
       attributes (gender, age, glasses, expression, race), match info
       (MatchedFaceId, Similarity), and optional images.
    """
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    start_time = call.data["start_time"]
    end_time = call.data["end_time"]
    channel = call.data.get("channel", 0)
    include_images = call.data.get("include_images", False)
    matched_only = call.data.get("matched_only", False)
    max_results = call.data.get("max_results", 100)

    # NVR SnapedFaces/Search uses 0-based channel indexing (CH17 → Chn=16).
    search_payload: dict[str, Any] = {
        "MsgId": None,
        "StartTime": start_time,
        "EndTime": end_time,
        "Chn": [channel - 1] if channel > 0 else [],
        "Similarity": -1,
        "Engine": 0,
        "Count": 0,
        "FaceInfo": [],
    }

    try:
        resp = await coordinator.client.async_api_call(API_AI_FACES, search_payload)
        search_data = resp.get("data", resp) if isinstance(resp, dict) else {}
        count = search_data.get("Count", 0)
        _LOGGER.debug("Face search found %d records", count)

        if count == 0:
            hass.bus.async_fire(
                "raysharp_nvr_faces_result",
                {"config_entry_id": entry_id, "count": 0, "faces": []},
            )
            return

        fetch_count = min(count, max_results)
        page_size = 50
        all_records: list[dict[str, Any]] = []

        for start_idx in range(0, fetch_count, page_size):
            batch = min(page_size, fetch_count - start_idx)
            get_payload: dict[str, Any] = {
                "Engine": 0,
                "MatchedFaces": 1 if matched_only else 0,
                "StartIndex": start_idx,
                "Count": batch,
                "SimpleInfo": 0,
                "WithFaceImage": 1 if include_images else 0,
                "WithBodyImage": 0,
                "WithBackgroud": 0,
                "WithFeature": 0,
            }
            get_resp = await coordinator.client.async_api_call(
                API_AI_FACES_GET_BY_INDEX, get_payload
            )
            get_data = get_resp.get("data", get_resp) if isinstance(get_resp, dict) else {}
            records = get_data.get("SnapedFaceInfo", [])
            all_records.extend(records)

        hass.bus.async_fire(
            "raysharp_nvr_faces_result",
            {
                "config_entry_id": entry_id,
                "count": count,
                "fetched": len(all_records),
                "faces": all_records,
            },
        )
        _LOGGER.debug("Face search complete: %d records retrieved", len(all_records))

    except RaySharpNVRConnectionError as err:
        _LOGGER.error("Face search failed: %s", err)


async def _async_handle_get_plate_database_info(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the get_plate_database_info service call.

    Looks up one or more license plate numbers in the NVR's plate database and
    returns owner/group/vehicle information. Fires raysharp_nvr_plate_db_result.

    The database also returns FDGroup-style plate groups; GrpId in the response
    indicates which allow/block list (if any) the plate belongs to.
    """
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return

    plate_numbers: list[str] = call.data["plate_numbers"]

    try:
        resp = await coordinator.client.async_api_call(
            API_AI_ADDED_PLATES_GET, {"PlatesId": plate_numbers}
        )
        data = resp.get("data", resp) if isinstance(resp, dict) else {}
        hass.bus.async_fire(
            "raysharp_nvr_plate_db_result",
            {
                "config_entry_id": entry_id,
                "queried_plates": plate_numbers,
                "result": data.get("Result", -1),
                "count": data.get("Count", 0),
                "plate_info": data.get("PlateInfo", []),
            },
        )
        _LOGGER.debug(
            "Plate DB lookup for %s: %d results", plate_numbers, data.get("Count", 0)
        )
    except RaySharpNVRConnectionError as err:
        _LOGGER.error("Plate database lookup failed: %s", err)


async def _async_handle_clear_detections(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the clear_detections_history service call.

    Clears the persistent plates/faces tracker storage for the given entry.
    detection_type: "plates" | "faces" | "all" (default "all")
    """
    entry_id = call.data["config_entry_id"]
    detection_type = call.data.get("detection_type", "all")

    trackers = hass.data.get(DOMAIN_TRACKERS, {}).get(entry_id, {})
    if not trackers:
        _LOGGER.warning("No tracker sensors found for entry %s", entry_id)
        return

    if detection_type in ("plates", "all"):
        sensor = trackers.get("plates")
        if sensor:
            await sensor.async_clear()
            _LOGGER.info("Cleared plates detection history for entry %s", entry_id)

    if detection_type in ("faces", "all"):
        sensor = trackers.get("faces")
        if sensor:
            await sensor.async_clear()
            _LOGGER.info("Cleared faces detection history for entry %s", entry_id)


async def _async_handle_configure_event_push(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the configure_event_push service call (manual trigger)."""
    entry_id = call.data["config_entry_id"]
    coordinator = _get_coordinator_for_entry(hass, entry_id)
    if coordinator is None:
        _LOGGER.error("No coordinator found for config entry %s", entry_id)
        return
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        _LOGGER.error("Config entry %s not found", entry_id)
        return
    await _async_configure_event_push(coordinator, entry, hass)


# ─── Service schemas ──────────────────────────────────────────────────────────

PTZ_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("channel"): vol.All(int, vol.Range(min=1, max=64)),
        vol.Required("command"): cv.string,
        vol.Optional("state", default=PTZ_STATE_START): vol.In(["Start", "Stop"]),
        vol.Optional("speed", default=50): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional("preset_num"): vol.All(int, vol.Range(min=1, max=255)),
    }
)

SNAPSHOT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("channel"): vol.All(int, vol.Range(min=1, max=64)),
    }
)

ALARM_OUTPUT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("output_id"): cv.string,
        vol.Optional("active", default=True): cv.boolean,
    }
)

SEARCH_RECORDS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("channel"): vol.All(int, vol.Range(min=0, max=64)),
        vol.Required("start_time"): cv.string,
        vol.Required("end_time"): cv.string,
        vol.Optional("record_type", default="all"): cv.string,
    }
)

SEARCH_PLATES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("start_time"): cv.string,
        vol.Required("end_time"): cv.string,
        vol.Optional("channel", default=0): vol.All(int, vol.Range(min=0, max=64)),
        vol.Optional("plate_numbers", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("include_images", default=False): cv.boolean,
        vol.Optional("max_results", default=100): vol.All(int, vol.Range(min=1, max=1000)),
    }
)

SEARCH_FACES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("start_time"): cv.string,
        vol.Required("end_time"): cv.string,
        vol.Optional("channel", default=0): vol.All(int, vol.Range(min=0, max=64)),
        vol.Optional("matched_only", default=False): cv.boolean,
        vol.Optional("include_images", default=False): cv.boolean,
        vol.Optional("max_results", default=100): vol.All(int, vol.Range(min=1, max=1000)),
    }
)

GET_PLATE_DB_INFO_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("plate_numbers"): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1)),
    }
)

CONFIGURE_EVENT_PUSH_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
    }
)

CLEAR_DETECTIONS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("detection_type", default="all"): vol.In(["plates", "faces", "all"]),
    }
)


# ─── Integration lifecycle ────────────────────────────────────────────────────

async def _async_migrate_image_entity_ids(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove spurious '_2' suffixes from image snapshot entity_ids.

    When the unique_id format changed in v2.7.1, old image entities still
    occupied the primary entity_ids, causing new entities to register as
    'image.ch1_cam03_last_detection_2' etc.  Once the old entities are gone
    the '_2' suffix is no longer needed; this migration renames them cleanly.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity_entry in list(registry.entities.values()):
        eid = entity_entry.entity_id
        if (
            entity_entry.config_entry_id == entry.entry_id
            and eid.startswith("image.")
            and (entity_entry.unique_id or "").endswith("_snapshot")
            and eid.endswith("_2")
        ):
            target_id = eid[:-2]
            if not registry.async_get(target_id):
                registry.async_update_entity(eid, new_entity_id=target_id)
                _LOGGER.info("Migrated image entity %s → %s", eid, target_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RaySharp NVR from a config entry."""
    await _async_migrate_image_entity_ids(hass, entry)

    client = RaySharpNVRClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    coordinator = RaySharpNVRCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register webhook for NVR EventPush
    webhook_id = _get_webhook_id(entry)
    webhook_register(
        hass,
        DOMAIN,
        "RaySharp NVR EventPush",
        webhook_id,
        _handle_webhook,
    )
    _LOGGER.debug("Registered webhook %s for NVR EventPush", webhook_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Start Event Check long-polling loop for real-time events (plates, faces…)
    coordinator.async_start_event_check_loop()
    entry.async_on_unload(coordinator.async_stop_event_check_loop)

    # Auto-configure NVR EventPush if enabled
    if entry.options.get(CONF_EVENT_PUSH_AUTO_CONFIGURE, DEFAULT_EVENT_PUSH_AUTO_CONFIGURE):
        hass.async_create_task(
            _async_configure_event_push(coordinator, entry, hass)
        )

        # Periodic EventPush watchdog: re-configure whenever coordinator data
        # shows EventPush is disabled (NVR may reset it after reboot).
        # Rate-limited to at most once every 5 minutes.
        _last_ep_configure: list[float] = [0.0]

        def _check_event_push_enabled() -> None:
            ep_data = coordinator.data.get(DATA_EVENT_PUSH_CONFIG) if coordinator.data else None
            if ep_data is None:
                return
            # Determine enabled state — handle flat dict, nested params.table, and list
            if isinstance(ep_data, list):
                enabled = any(
                    item.get("enable", False)
                    for item in ep_data
                    if isinstance(item, dict)
                )
            elif isinstance(ep_data, dict):
                if "enable" in ep_data:
                    enabled = bool(ep_data["enable"])
                else:
                    table = ep_data.get("params", {}).get("table", ep_data)
                    enabled = bool(table.get("enable", True))
            else:
                return
            if not enabled:
                now = time.monotonic()
                if now - _last_ep_configure[0] > 300:
                    _last_ep_configure[0] = now
                    _LOGGER.debug("EventPush detected as disabled — triggering auto-configure")
                    hass.async_create_task(
                        _async_configure_event_push(coordinator, entry, hass)
                    )

        entry.async_on_unload(
            coordinator.async_add_listener(_check_event_push_enabled)
        )

    # Register services (idempotent — only register once across all entries)
    if not hass.services.has_service(DOMAIN, SERVICE_PTZ_CONTROL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PTZ_CONTROL,
            lambda call: hass.async_create_task(
                _async_handle_ptz_control(hass, call)
            ),
            schema=PTZ_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_SNAPSHOT,
            lambda call: hass.async_create_task(
                _async_handle_get_snapshot(hass, call)
            ),
            schema=SNAPSHOT_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_TRIGGER_ALARM_OUTPUT,
            lambda call: hass.async_create_task(
                _async_handle_trigger_alarm_output(hass, call)
            ),
            schema=ALARM_OUTPUT_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_RECORDS,
            lambda call: hass.async_create_task(
                _async_handle_search_records(hass, call)
            ),
            schema=SEARCH_RECORDS_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_PLATES,
            lambda call: hass.async_create_task(
                _async_handle_search_plates(hass, call)
            ),
            schema=SEARCH_PLATES_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH_FACES,
            lambda call: hass.async_create_task(
                _async_handle_search_faces(hass, call)
            ),
            schema=SEARCH_FACES_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_PLATE_DATABASE_INFO,
            lambda call: hass.async_create_task(
                _async_handle_get_plate_database_info(hass, call)
            ),
            schema=GET_PLATE_DB_INFO_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CONFIGURE_EVENT_PUSH,
            lambda call: hass.async_create_task(
                _async_handle_configure_event_push(hass, call)
            ),
            schema=CONFIGURE_EVENT_PUSH_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_DETECTIONS,
            lambda call: hass.async_create_task(
                _async_handle_clear_detections(hass, call)
            ),
            schema=CLEAR_DETECTIONS_SERVICE_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a RaySharp NVR config entry."""
    webhook_id = _get_webhook_id(entry)
    webhook_unregister(hass, webhook_id)
    _LOGGER.debug("Unregistered webhook %s", webhook_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: RaySharpNVRCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_close()

    # Clean up tracker references
    hass.data.get(DOMAIN_TRACKERS, {}).pop(entry.entry_id, None)

    # Remove services only when the last entry is unloaded
    if not hass.data.get(DOMAIN):
        for service_name in (
            SERVICE_PTZ_CONTROL,
            SERVICE_GET_SNAPSHOT,
            SERVICE_TRIGGER_ALARM_OUTPUT,
            SERVICE_SEARCH_RECORDS,
            SERVICE_SEARCH_PLATES,
            SERVICE_SEARCH_FACES,
            SERVICE_GET_PLATE_DATABASE_INFO,
            SERVICE_CONFIGURE_EVENT_PUSH,
            SERVICE_CLEAR_DETECTIONS,
        ):
            if hass.services.has_service(DOMAIN, service_name):
                hass.services.async_remove(DOMAIN, service_name)

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
