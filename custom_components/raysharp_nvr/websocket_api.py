"""Websocket commands for the video panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EVENT_TIMEOUT,
    DATA_DEVICE_INFO,
    DATA_SYSTEM_INFO,
    DEFAULT_EVENT_TIMEOUT,
    DOMAIN,
)
from .events import async_get_event_log

# Per-channel detection binary sensors (unique_id suffix -> panel label key).
DETECTIONS = [
    "motion_detected", "person_detected", "vehicle_detected", "line_crossing",
    "intrusion", "face_detected", "plate_detected", "region_entrance",
    "region_exiting", "wander_detected", "stationary_object", "sound_detected",
    "occlusion_detected", "pir_detected", "crowd_detected",
]  # fmt: skip


def _entries(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        coord = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coord is not None and not isinstance(coord, dict):
            yield entry, coord


def _ent(reg, domain: str, unique_id: str) -> str | None:
    return reg.async_get_entity_id(domain, DOMAIN, unique_id)


def _nvr_view(hass: HomeAssistant, entry, coord) -> dict[str, Any]:
    from .sensor import (  # noqa: PLC0415 - entity module, imported lazily
        _get_channel_list_for_sensors,
        _get_disk_list,
        _get_event_push_status,
        _get_network_ip,
        _get_vhd_count,
    )

    data = coord.data or {}
    reg = er.async_get(hass)
    dev = data.get(DATA_DEVICE_INFO) or {}
    sysi = data.get(DATA_SYSTEM_INFO) or {}
    mac = dev.get("mac_addr", "unknown")

    def state(eid: str | None) -> str | None:
        st = hass.states.get(eid) if eid else None
        return st.state if st else None

    disks = []
    for disk in _get_disk_list(data):
        if not isinstance(disk, dict):
            continue
        total = disk.get("total_size")
        free = disk.get("free_size")
        disks.append(
            {
                "total_gb": round(total / 1024, 1) if isinstance(total, (int, float)) else None,
                "free_gb": round(free / 1024, 1) if isinstance(free, (int, float)) else None,
                "status": disk.get("status"),
            }
        )

    channels = []
    for i, ch in enumerate(_get_channel_list_for_sensors(data)):
        digits = "".join(c for c in str(ch.get("channel", "")) if c.isdigit())
        num = int(digits) if digits else i + 1
        cam = _ent(reg, "camera", f"{mac}_ch{num}_camera")
        cam_state = hass.states.get(cam) if cam else None
        active = []
        for suffix in DETECTIONS:
            eid = _ent(reg, "binary_sensor", f"{mac}_ch{num}_{suffix}")
            if eid and state(eid) == "on":
                active.append(suffix)
        channels.append(
            {
                "num": num,
                "name": ch.get("channel_name") or f"CH{num}",
                "online": str(ch.get("connect_status", "")).lower() == "online",
                "videoloss": bool(ch.get("videoloss")),
                "camera": cam,
                "picture": cam_state.attributes.get("entity_picture") if cam_state else None,
                "recording": coord.record_flags.get(num),
                "active": active,
                "motion_switch": _ent(reg, "switch", f"{mac}_ch{num}_motion_alarm"),
                "ptz": any(
                    "ptz" in a.lower() for a in getattr(coord, "channel_abilities", {}).get(num, [])
                ),
            }
        )

    warnings = []
    snap_err = getattr(coord, "snapshot_errors", {})
    if snap_err:
        chans = ", ".join(f"CH{n}" for n in sorted(snap_err))
        first = next(iter(snap_err.values()))
        warnings.append(f"NVR не отдаёт кадры ({chans}): {first}")
    ai_err = str(getattr(coord, "_ai_last_error", "") or "")
    if "permission" in ai_err.lower():
        warnings.append("NVR отказал в поиске AI-событий: нет прав")
    if warnings and any("permission" in w.lower() or "прав" in w for w in warnings + list(snap_err.values())):
        warnings.append(
            "Похоже, у пользователя NVR, указанного в HA, нет прав на просмотр, "
            "поиск или настройки — выдайте их в NVR (Пользователи → права) или укажите admin."
        )

    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "online": coord.last_update_success,
        "model": dev.get("device_type"),
        "firmware": sysi.get("software_version"),
        "serial": sysi.get("serialNum"),
        "ip": _get_network_ip(data),
        "event_push": _get_event_push_status(data),
        "armed": state(_ent(reg, "binary_sensor", f"{mac}_nvr_armed")),
        "disarm_switch": _ent(reg, "switch", f"{mac}_alarm_disarming"),
        "reboot_button": _ent(reg, "button", f"{mac}_reboot"),
        "disks": disks,
        "ai_today": {
            "faces": _get_vhd_count(data, 0),
            "persons": _get_vhd_count(data, 1),
            "vehicles": _get_vhd_count(data, 2),
            "plates": _get_vhd_count(data, 3),
        },
        "event_timeout": entry.options.get(CONF_EVENT_TIMEOUT, DEFAULT_EVENT_TIMEOUT),
        "channels": channels,
        "warnings": warnings,
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@callback
def ws_overview(hass: HomeAssistant, connection, msg: dict) -> None:
    log = async_get_event_log(hass)
    connection.send_result(
        msg["id"],
        {
            "nvrs": [_nvr_view(hass, e, c) for e, c in _entries(hass)],
            "events": log.recent(120),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/event_picture", vol.Required("event_id"): int}
)
@callback
def ws_event_picture(hass: HomeAssistant, connection, msg: dict) -> None:
    pic = async_get_event_log(hass).pictures.get(msg["event_id"])
    connection.send_result(msg["id"], {"image": pic})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg: dict) -> None:
    """Push new events at once and a bare 'changed' after every poll."""
    unsubs = []

    @callback
    def _event(ev: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"event": ev}))

    @callback
    def _changed() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"changed": True}))

    unsubs.append(async_get_event_log(hass).listen(_event))
    for _entry, coord in _entries(hass):
        unsubs.append(coord.async_add_listener(_changed))

    @callback
    def _unsub() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = _unsub
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/action",
        vol.Required("entry_id"): str,
        vol.Required("action"): vol.In(["snapshot", "ptz", "arm", "disarm", "reboot"]),
        vol.Optional("channel"): int,
        vol.Optional("command"): str,
        vol.Optional("state", default="Start"): vol.In(["Start", "Stop"]),
    }
)
@websocket_api.async_response
async def ws_action(hass: HomeAssistant, connection, msg: dict) -> None:
    entry_id, act = msg["entry_id"], msg["action"]
    try:
        if act == "snapshot":
            await hass.services.async_call(
                DOMAIN, "get_snapshot",
                {"config_entry_id": entry_id, "channel": msg["channel"]}, blocking=True,
            )  # fmt: skip
        elif act == "ptz":
            await hass.services.async_call(
                DOMAIN, "ptz_control",
                {"config_entry_id": entry_id, "channel": msg["channel"],
                 "command": msg["command"], "state": msg["state"], "speed": 50},
                blocking=True,
            )  # fmt: skip
        else:
            view = next(
                (_nvr_view(hass, e, c) for e, c in _entries(hass) if e.entry_id == entry_id),
                None,
            )
            if view is None:
                raise ValueError("NVR not loaded")
            if act == "reboot":
                await hass.services.async_call(
                    "button", "press", {"entity_id": view["reboot_button"]}, blocking=True
                )
            else:
                # The NVR's switch is "disarm": on = alarms silenced.
                await hass.services.async_call(
                    "switch", "turn_off" if act == "arm" else "turn_on",
                    {"entity_id": view["disarm_switch"]}, blocking=True,
                )  # fmt: skip
    except Exception as err:  # noqa: BLE001 - shown to the user verbatim
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


CLIP_BEFORE_S = 10
CLIP_AFTER_S = 30
CLIP_LIFETIME_S = 240
_CLIPS = f"{DOMAIN}_clips"


def playback_url(live: str, start: float, end: float) -> str | None:
    """Live RTSP URL of a channel -> the NVR's playback URL for a time window.

    RaySharp firmware serves recordings at /rtsp/playback with the same query
    as /rtsp/streaming plus starttime/endtime in the NVR's local time, written
    with a literal Z that the NVR ignores.
    """
    import datetime as dt  # noqa: PLC0415

    if "/rtsp/streaming" not in live:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    t0 = dt.datetime.fromtimestamp(start).strftime(fmt)
    t1 = dt.datetime.fromtimestamp(end).strftime(fmt)
    url = live.replace("/rtsp/streaming", "/rtsp/playback", 1)
    return f"{url}{'&' if '?' in url else '?'}starttime={t0}&endtime={t1}"


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/clip",
        vol.Required("camera"): str,
        vol.Required("ts"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_clip(hass: HomeAssistant, connection, msg: dict) -> None:
    """Start an HLS stream of the recording around an event; returns its URL."""
    from homeassistant.components.camera import DynamicStreamSettings  # noqa: PLC0415
    from homeassistant.components.stream import create_stream  # noqa: PLC0415

    component = hass.data.get("camera")
    cam = component.get_entity(msg["camera"]) if component else None
    if cam is None or cam.platform.platform_name != DOMAIN:
        connection.send_error(msg["id"], "not_found", "Камера не найдена")
        return
    live = await cam.stream_source()
    url = playback_url(live or "", msg["ts"] - CLIP_BEFORE_S, msg["ts"] + CLIP_AFTER_S)
    if not url:
        connection.send_error(msg["id"], "unsupported", "NVR не отдаёт запись по RTSP")
        return

    clips: dict = hass.data.setdefault(_CLIPS, {})
    key = (msg["camera"], int(msg["ts"]))
    stream = clips.get(key)
    if stream is None:
        try:
            stream = create_stream(
                hass, url, dict(getattr(cam, "stream_options", None) or {}),
                DynamicStreamSettings(), stream_label=f"{msg['camera']} clip",
            )  # fmt: skip
            stream.add_provider("hls")
            await stream.start()
        except Exception as err:  # noqa: BLE001 - shown in the panel
            connection.send_error(msg["id"], "stream", f"Не удалось открыть запись: {err}")
            return
        clips[key] = stream

        async def _stop(_now=None) -> None:
            clips.pop(key, None)
            await stream.stop()

        from homeassistant.helpers.event import async_call_later  # noqa: PLC0415

        async_call_later(hass, CLIP_LIFETIME_S, _stop)
    connection.send_result(
        msg["id"],
        {"url": stream.endpoint_url("hls"), "before": CLIP_BEFORE_S, "after": CLIP_AFTER_S},
    )


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    for handler in (ws_overview, ws_event_picture, ws_subscribe, ws_action, ws_clip):
        websocket_api.async_register_command(hass, handler)
