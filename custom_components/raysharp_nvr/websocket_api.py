"""Websocket commands for the video panel."""

from __future__ import annotations

import re

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
from .events import get_event_log

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


def _profiles(hass: HomeAssistant, entity_id: str | None) -> list[str]:
    cam = _raysharp_camera(hass, entity_id) if entity_id else None
    if cam is None or not hasattr(cam, "profile_urls"):
        return []
    dead = hass.data.get(_DEAD, {})
    return [
        p for p in ("main", "sub", "mobile")
        if p in cam.profile_urls() and not dead.get((entity_id, p))
    ]


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
                "streams": _profiles(hass, cam),
                "talk": bool((hass.data.get(f"{DOMAIN}_talk", {}).get(cam) or {}).get("backchannel")),
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
        # Seen with every right ticked: this firmware keeps its picture database
        # to the admin account. Pictures still arrive with the pushed alarms.
        warnings.append(
            "Поиск по базе AI-снимков NVR недоступен этой учётке (no_permission) — "
            "история кадров после перезапуска HA не подтягивается; новые события с кадрами приходят."
        )
    if snap_err and any("permission" in v.lower() or "401" in v for v in snap_err.values()):
        warnings.append("Проверьте права пользователя NVR на живой просмотр.")

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


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/overview",
        vol.Optional("since"): vol.Coerce(float),
        vol.Optional("limit", default=400): vol.All(int, vol.Range(min=1, max=3000)),
    }
)
@callback
def ws_overview(hass: HomeAssistant, connection, msg: dict) -> None:
    log = get_event_log(hass)
    connection.send_result(
        msg["id"],
        {
            "nvrs": [_nvr_view(hass, e, c) for e, c in _entries(hass)],
            "events": log.recent(msg["limit"], msg.get("since")),
            "last_event": next((e["ts"] for e in log.recent(1)), None),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/event_picture", vol.Required("event_id"): int}
)
@callback
def ws_event_picture(hass: HomeAssistant, connection, msg: dict) -> None:
    pic = get_event_log(hass).pictures.get(msg["event_id"])
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

    unsubs.append(get_event_log(hass).listen(_event))
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
ARCHIVE_CHUNK_S = 600  # the timeline plays the archive in 10-minute pieces
SUBTYPE = {"main": 0, "sub": 1, "mobile": 2}
PROFILE_RU = {"main": "Основной", "sub": "Дополнительный", "mobile": "Мобильный"}
CLIP_LIFETIME_S = 240
_CLIPS = f"{DOMAIN}_clips"


# Ways RaySharp firmware has been seen to want the playback window. UTC with
# a Z comes first: NVR-167R V8.2.7 honours the Z, and sending local time there
# played a recording 5 hours off (Asia/Yekaterinburg) without any error, since
# a continuously recording NVR has video for any hour. The probe only tells
# "there is video", never "it is the right time", so the order matters.
PLAYBACK_VARIANTS = (
    "iso-utc", "compact-utc", "iso-local", "iso-local-noz", "compact-local",
)
_VARIANT = f"{DOMAIN}_playback_variant"


def playback_url(
    live: str, start: float, end: float, variant: str = "iso-utc", subtype: int = 0
) -> str | None:
    """Live RTSP URL of a channel -> the NVR's playback URL for a time window.

    RaySharp firmware serves recordings at /rtsp/playback with the same query
    as /rtsp/streaming plus starttime/endtime.
    """
    import datetime as dt  # noqa: PLC0415

    if "/rtsp/streaming" not in live:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ" if variant.startswith("iso") else "%Y%m%dT%H%M%SZ"
    if variant.endswith("-noz"):
        fmt = fmt.rstrip("Z")
        variant = variant[: -len("-noz")]
    if variant.endswith("utc"):
        t0 = dt.datetime.fromtimestamp(start, dt.UTC).strftime(fmt)
        t1 = dt.datetime.fromtimestamp(end, dt.UTC).strftime(fmt)
    else:
        t0 = dt.datetime.fromtimestamp(start).strftime(fmt)
        t1 = dt.datetime.fromtimestamp(end).strftime(fmt)
    url = live.replace("/rtsp/streaming", "/rtsp/playback", 1)
    # NVRs record the main stream only: asking for the recording of the sub
    # or mobile stream (what the grid shows) returns a session without video.
    url = re.sub(r"([?&])subtype=\d+", rf"\g<1>subtype={int(subtype)}", url)
    return f"{url}{'&' if '?' in url else '?'}starttime={t0}&endtime={t1}"


FLAT_TS: set[str] = set()  # playback URLs whose frames all share one timestamp


def _probe(url: str, timeout: float = 12) -> str:
    """Open the RTSP source and read a few packets; "" if there is picture,
    otherwise why not (for the panel and for picking a playback form)."""
    import av  # noqa: PLC0415

    try:
        with av.open(
            url, options={"rtsp_transport": "tcp", "timeout": str(int(timeout * 1e6))},
            timeout=timeout,
        ) as box:
            if not box.streams.video:
                return "нет видеодорожки"
            seen: set = set()
            frames = 0
            for n, packet in enumerate(box.demux(box.streams.video[0])):
                if packet.size:
                    frames += 1
                    seen.add(packet.pts)
                    if frames >= 12:
                        break
                if n > 80:
                    break
            if not frames:
                return "поток пустой"
            # Some firmware (V8.2.4) sends recordings with every frame stamped
            # the same: players stall on them. Re-time those by arrival.
            if frames >= 12 and len(seen) <= 1:
                FLAT_TS.add(url)
            return ""
    except Exception as err:  # noqa: BLE001 - reported, not raised
        return str(err).replace(url, "<url>")[:120] or type(err).__name__


def _codecs(url: str, timeout: float = 10) -> tuple[str, str]:
    """(video, audio) codec names of the source; "" = no such track, "?" = unknown."""
    import av  # noqa: PLC0415

    try:
        with av.open(
            url, options={"rtsp_transport": "tcp", "timeout": str(int(timeout * 1e6))},
            timeout=timeout,
        ) as box:
            v = box.streams.video[0].codec_context.name if box.streams.video else ""
            a = box.streams.audio[0].codec_context.name if box.streams.audio else ""
            return v, a
    except Exception:  # noqa: BLE001 - unknown is reported as such
        return "?", "?"


async def _codecs_of(hass: HomeAssistant, camera: str, profile: str, url: str) -> tuple[str, str]:
    cache: dict = hass.data.setdefault(f"{DOMAIN}_codecs", {})
    key = (camera, profile)
    if key not in cache or "?" in cache[key]:
        cache[key] = await hass.async_add_executor_job(_codecs, url)
    return cache[key]


def _has_video(url: str) -> bool:
    return _probe(url) == ""


def _with_port(url: str, port: int) -> str:
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

    u = urlparse(url)
    if not u.hostname:
        return url
    auth = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
    return urlunparse(u._replace(netloc=f"{auth}{u.hostname}:{port}"))


def _playback_bases(live: str) -> list[str]:
    """The live URL, then the same on 554 and 80: some firmware publishes
    live video on its web port but serves recordings only on 554 (and the
    other way round), answering the wrong port with an empty session."""
    from urllib.parse import urlparse  # noqa: PLC0415

    out = [live]
    try:
        own = urlparse(live).port or 554
    except ValueError:
        return out
    for port in (554, 80):
        try:
            alt = _with_port(live, port) if own != port else live
        except ValueError:
            continue
        if alt not in out:
            out.append(alt)
    return out


def _port_dead(why: str) -> bool:
    """Errors that no other time format can fix on this port."""
    w = why.lower()
    return any(s in w for s in ("refused", "timed out", "timeout", "unreachable", "no route"))


def _flat_ts(hass: HomeAssistant, camera: str) -> bool:
    return camera in hass.data.get(f"{DOMAIN}_flat_ts", set())


async def _playback_source(
    hass: HomeAssistant, camera: str, live: str, ts: float, end: float | None = None,
    *, before: float = CLIP_BEFORE_S, subtype: int = 0,
) -> str | None:
    """Find (once per camera) which port and time form the NVR answers with video."""
    from urllib.parse import urlparse  # noqa: PLC0415

    known: dict = hass.data.setdefault(_VARIANT, {})
    start, end = ts - before, end or ts + CLIP_AFTER_S
    bases = _playback_bases(live)
    if camera in known:
        port, variant = known[camera]
        order = [(b, variant) for b in bases if (urlparse(b).port or 554) == port][:1]
    else:
        order = [(b, v) for b in bases for v in PLAYBACK_VARIANTS]
    reasons = hass.data.setdefault(f"{DOMAIN}_clip_reasons", {})
    reasons[camera] = []
    skip: set[str] = set()
    for base, variant in order:
        if base in skip:
            continue
        url = playback_url(base, start, end, variant, subtype)
        why = await hass.async_add_executor_job(_probe, url) if url else "нет адреса"
        port = urlparse(base).port or 554
        if not why:
            known[camera] = (port, variant)
            flat = hass.data.setdefault(f"{DOMAIN}_flat_ts", set())
            if url in FLAT_TS:
                flat.add(camera)
            return url
        reasons[camera].append(f"{port}/{variant}: {why}")
        if _port_dead(why):
            skip.add(base)
    if camera in known:  # remembered form stopped working: search again
        known.pop(camera)
        return await _playback_source(
            hass, camera, live, ts, end, before=before, subtype=subtype
        )
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/clip",
        vol.Required("camera"): str,
        vol.Required("ts"): vol.Coerce(float),
        vol.Optional("until"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_clip(hass: HomeAssistant, connection, msg: dict) -> None:
    """Start an HLS stream of the recording around an event; returns its URL."""
    from homeassistant.components.camera import DynamicStreamSettings  # noqa: PLC0415
    from homeassistant.components.stream import create_stream  # noqa: PLC0415

    cam = _raysharp_camera(hass, msg["camera"])
    if cam is None:
        connection.send_error(msg["id"], "not_found", "Камера не найдена")
        return
    live = await cam.stream_source() or ""
    if "/rtsp/streaming" not in live:
        connection.send_error(msg["id"], "unsupported", "NVR не отдаёт запись по RTSP")
        return
    end = None
    if msg.get("until") and msg["until"] > msg["ts"]:
        end = min(msg["until"] + 5, msg["ts"] + 600)  # whole recorded segment
    url = await _playback_source(hass, msg["camera"], live, msg["ts"], end)
    if not url:
        connection.send_error(
            msg["id"], "no_recording",
            "Регистратор не отдал запись за это время: "
            + "; ".join(hass.data.get(f"{DOMAIN}_clip_reasons", {}).get(msg["camera"], [])),
        )  # fmt: skip
        return

    clips: dict = hass.data.setdefault(_CLIPS, {})
    key = (msg["camera"], int(msg["ts"]), int(end or 0))
    stream = clips.get(key)
    if stream is None:
        try:
            options = dict(getattr(cam, "stream_options", None) or {})
            if _flat_ts(hass, msg["camera"]):
                options["use_wallclock_as_timestamps"] = True
            stream = create_stream(
                hass, url, options,
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
        {"url": stream.endpoint_url("hls"), "before": CLIP_BEFORE_S,
         "after": int(end - msg["ts"]) if end else CLIP_AFTER_S},
    )


_LIVE = f"{DOMAIN}_live_streams"
_DEAD = f"{DOMAIN}_dead_profiles"


def _raysharp_camera(hass: HomeAssistant, entity_id: str):
    component = hass.data.get("camera")
    cam = component.get_entity(entity_id) if component else None
    if cam is None or cam.platform.platform_name != DOMAIN:
        return None
    return cam


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/stream",
        vol.Required("camera"): str,
        vol.Required("profile"): vol.In(["main", "sub", "mobile", "lowest"]),
    }
)
@websocket_api.async_response
async def ws_stream(hass: HomeAssistant, connection, msg: dict) -> None:
    """HLS URL for one stream profile of a camera (for the grid and phones).

    "lowest" picks mobile, then sub, then main. Streams are shared between
    viewers and stop by themselves a few minutes after the last one leaves.
    """
    from homeassistant.components.camera import DynamicStreamSettings  # noqa: PLC0415
    from homeassistant.components.stream import create_stream  # noqa: PLC0415

    cam = _raysharp_camera(hass, msg["camera"])
    if cam is None or not hasattr(cam, "profile_urls"):
        connection.send_error(msg["id"], "not_found", "Камера не найдена")
        return
    urls = cam.profile_urls()
    profile = msg["profile"]
    dead: dict = hass.data.setdefault(_DEAD, {})
    if profile == "lowest":
        # The mobile stream is published on some firmware but carries no
        # picture; check once per camera and remember.
        profile = ""
        for cand in ("mobile", "sub", "main"):
            if cand not in urls or dead.get((msg["camera"], cand)):
                continue
            running = hass.data.get(_LIVE, {}).get((msg["camera"], cand))
            if running is not None and not getattr(running, "available", True) and cand != "main":
                dead[(msg["camera"], cand)] = True
                continue
            if cand == "main" or await hass.async_add_executor_job(_has_video, urls[cand]):
                profile = cand
                break
            dead[(msg["camera"], cand)] = True
    elif dead.get((msg["camera"], profile)):
        connection.send_error(msg["id"], "no_video", "В этом потоке у камеры нет видео")
        return
    source = urls.get(profile)
    if not source:
        connection.send_error(msg["id"], "no_stream", "Такого потока у камеры нет")
        return
    live: dict = hass.data.setdefault(_LIVE, {})
    key = (msg["camera"], profile)
    stream = live.get(key)
    if stream is not None and not getattr(stream, "available", True):
        # The worker gave up (NVR refused, codec, session limit): the old HLS
        # URL would never produce a segment. Start over.
        live.pop(key, None)
        await stream.stop()
        stream = None
    if stream is None or "hls" not in stream.outputs():
        try:
            stream = create_stream(
                hass, source, dict(getattr(cam, "stream_options", None) or {}),
                DynamicStreamSettings(), stream_label=f"{msg['camera']} {profile}",
            )  # fmt: skip
            stream.add_provider("hls")
            await stream.start()
        except Exception as err:  # noqa: BLE001 - shown in the panel
            connection.send_error(msg["id"], "stream", f"Поток не открылся: {err}")
            return
        live[key] = stream
    connection.send_result(msg["id"], {"url": stream.endpoint_url("hls"), "profile": profile})


# Read-only NVR calls for diagnosing firmware differences from the panel side.
_RAW_ALLOWED = ("/API/Playback/", "/API/AI/", "/API/Event/", "/API/Log/")


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/api",
        vol.Required("entry_id"): str,
        vol.Required("path"): str,
        vol.Optional("data"): dict,
    }
)
@websocket_api.async_response
async def ws_api(hass: HomeAssistant, connection, msg: dict) -> None:
    path = msg["path"]
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not path.startswith(_RAW_ALLOWED) or not (
        last in ("Get", "Search", "Range") or last.startswith(("Get", "Search"))
    ):
        connection.send_error(msg["id"], "forbidden", "Только чтение: */Get, */Search")
        return
    coord = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if coord is None or not hasattr(coord, "client"):
        connection.send_error(msg["id"], "not_found", "NVR не загружен")
        return
    try:
        resp = await coord.client.async_api_call(path, msg.get("data"))
    except Exception as err:  # noqa: BLE001 - diagnostics
        connection.send_error(msg["id"], "nvr", str(err))
        return
    connection.send_result(msg["id"], resp)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/probe_playback",
        vol.Required("camera"): str,
        vol.Required("query"): str,
        vol.Optional("profile", default="main"): vol.In(["main", "sub", "mobile"]),
    }
)
@websocket_api.async_response
async def ws_probe_playback(hass: HomeAssistant, connection, msg: dict) -> None:
    """Diagnostics: try a raw playback query against the camera's NVR.

    `query` replaces everything after "/rtsp/" in the camera's own URL, e.g.
    "playback?channel=6&subtype=0&starttime=...&endtime=...". Read-only.
    """
    cam = _raysharp_camera(hass, msg["camera"])
    urls = cam.profile_urls() if cam is not None and hasattr(cam, "profile_urls") else {}
    live = urls.get(msg["profile"], "")
    if "/rtsp/" not in live:
        connection.send_error(msg["id"], "no_stream", "Нет RTSP-адреса")
        return
    url = live.split("/rtsp/", 1)[0] + "/rtsp/" + msg["query"].lstrip("/")
    why = await hass.async_add_executor_job(_probe, url)
    connection.send_result(msg["id"], {"ok": not why, "why": why})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/av",
        vol.Required("camera"): str,
        vol.Optional("profile", default="main"): vol.In(["main", "sub", "mobile"]),
        vol.Optional("ts"): vol.Coerce(float),
        vol.Optional("until"): vol.Coerce(float),
        vol.Optional("format", default="mp4"): vol.In(["mp4", "hls"]),
        # Archive (timeline): start exactly at ts, not CLIP_BEFORE_S earlier.
        vol.Optional("exact", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_av(hass: HomeAssistant, connection, msg: dict) -> None:
    """Signed URL of an MP4 with sound (AAC) for the camera window: live when
    no `ts` is given, otherwise the recording around `ts` like raysharp_nvr/clip.
    HLS from HA's stream component drops the cameras' G.711 audio."""
    from .av_view import hls_url, new_token, signed_url  # noqa: PLC0415

    def _url(token: str) -> str:
        if msg["format"] == "hls":
            return hls_url(token)
        return signed_url(hass, token, connection.refresh_token_id)

    cam = _raysharp_camera(hass, msg["camera"])
    if cam is None or not hasattr(cam, "profile_urls"):
        connection.send_error(msg["id"], "not_found", "Камера не найдена")
        return
    urls = cam.profile_urls()
    if msg.get("ts") is None:
        source = urls.get(msg["profile"]) or await cam.stream_source()
        if not source:
            connection.send_error(msg["id"], "no_stream", "Такого потока у камеры нет")
            return
        vcodec, audio = await _codecs_of(hass, msg["camera"], msg["profile"], source)
        token = new_token(hass, source, None, vcodec)
        connection.send_result(msg["id"], {"url": _url(token), "audio": audio, "video": vcodec})
        return
    live = await cam.stream_source() or ""
    if "/rtsp/streaming" not in live:
        connection.send_error(msg["id"], "unsupported", "NVR не отдаёт запись по RTSP")
        return
    ts = msg["ts"]
    before = 0 if msg["exact"] else CLIP_BEFORE_S
    end = None
    if msg.get("until") and msg["until"] > ts:
        end = min(msg["until"] + (0 if msg["exact"] else 5), ts + ARCHIVE_CHUNK_S)
    subtype = SUBTYPE[msg["profile"]]
    url = await _playback_source(
        hass, msg["camera"], live, ts, end, before=before, subtype=subtype
    )
    if not url:
        reasons = hass.data.get(f"{DOMAIN}_clip_reasons", {}).get(msg["camera"], [])
        text = "Регистратор не отдал запись за это время: " + "; ".join(reasons)
        if subtype:
            text = (
                f"Запись потока «{PROFILE_RU[msg['profile']]}» регистратор не отдаёт — "
                "обычно он пишет только основной поток. Выберите «Основной». "
                + text
            )
        connection.send_error(msg["id"], "no_recording", text)
        return
    duration = (end or ts + CLIP_AFTER_S) - (ts - before)
    vcodec, audio = await _codecs_of(hass, msg["camera"], "main", live)
    token = new_token(hass, url, duration, vcodec, wallclock=_flat_ts(hass, msg["camera"]))
    connection.send_result(msg["id"], {
        "audio": audio, "video": vcodec, "url": _url(token),
        "before": before, "after": int(end - ts) if end else CLIP_AFTER_S,
        "start": ts - before, "duration": duration,
    })


def _record_rows(resp_data: dict[str, Any]):
    for group in resp_data.get("record") or []:
        yield from (r for r in (group if isinstance(group, list) else [group]) if isinstance(r, dict))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/timeline",
        vol.Required("camera"): str,
        vol.Required("day"): str,  # YYYY-MM-DD, the NVR's local day
    }
)
@websocket_api.async_response
async def ws_timeline(hass: HomeAssistant, connection, msg: dict) -> None:
    """Recordings of one camera for one day, with motion/AI pieces and the
    journal's events, for the archive timeline. Read-only."""
    from datetime import date, datetime, timedelta  # noqa: PLC0415

    from .const import API_RECORD_SEARCH  # noqa: PLC0415

    cam = _raysharp_camera(hass, msg["camera"])
    if cam is None:
        connection.send_error(msg["id"], "not_found", "Камера не найдена")
        return
    try:
        day = date.fromisoformat(msg["day"])
    except ValueError:
        connection.send_error(msg["id"], "bad_day", "Неверная дата")
        return
    coord = cam.coordinator
    num = cam._channel_num  # noqa: SLF001
    since = datetime.combine(day, datetime.min.time())
    until = since + timedelta(days=1) - timedelta(seconds=1)
    try:
        resp = await coord.client.async_api_call(
            API_RECORD_SEARCH,
            {
                "channel": [f"CH{num}"],
                "start_date": since.strftime("%m/%d/%Y"),
                "start_time": since.strftime("%H:%M:%S"),
                "end_date": until.strftime("%m/%d/%Y"),
                "end_time": until.strftime("%H:%M:%S"),
                "record_type": 4294967295,
                "smart_region": [],
                "enable_smart_search": 0,
            },
        )
    except Exception as err:  # noqa: BLE001 - shown in the panel
        connection.send_error(msg["id"], "search_failed", f"Поиск записей не удался: {err}")
        return
    from .events import timeline_segments  # noqa: PLC0415

    segments = timeline_segments(_record_rows(coord._extract_data(resp)), num)  # noqa: SLF001
    t0, t1 = since.timestamp(), until.timestamp() + 1
    events = [
        {"id": e["id"], "ts": e["ts"], "type": e["type"], "picture": e.get("picture", False)}
        for e in get_event_log(hass).events
        if e.get("channel") == num and t0 <= e["ts"] < t1
    ]
    connection.send_result(
        msg["id"],
        {"day": msg["day"], "start": t0, "end": t1, "segments": segments, "events": events},
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/talk_probe",
        vol.Required("camera"): str,
        vol.Optional("profile", default="main"): vol.In(["main", "sub", "mobile"]),
        vol.Optional("url"): str,  # another RTSP source, e.g. the door panel itself
    }
)
@websocket_api.async_response
async def ws_talk_probe(hass: HomeAssistant, connection, msg: dict) -> None:
    """Diagnostics for two-way audio: does the RTSP source offer an ONVIF audio
    back-channel? With `url`, the camera's own login is used for that host."""
    from urllib.parse import urlparse  # noqa: PLC0415

    from .talk_probe import probe  # noqa: PLC0415

    cam = _raysharp_camera(hass, msg["camera"])
    urls = cam.profile_urls() if cam is not None and hasattr(cam, "profile_urls") else {}
    live = urls.get(msg["profile"], "") or (await cam.stream_source() if cam else "") or ""
    if not live.startswith("rtsp://"):
        connection.send_error(msg["id"], "no_stream", "Нет RTSP-адреса")
        return
    url = live
    if msg.get("url"):
        own = urlparse(live)
        other = urlparse(msg["url"])
        cred = f"{own.username}:{own.password}@" if own.username else ""
        url = f"rtsp://{cred}{other.hostname}:{other.port or 554}{other.path}{'?' + other.query if other.query else ''}"
    result = await probe(url)
    result["source"] = (urlparse(url).hostname or "") + ":" + str(urlparse(url).port or 554)
    connection.send_result(msg["id"], result)


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    from .av_view import RaySharpAvHlsView, RaySharpAvView  # noqa: PLC0415

    for handler in (ws_overview, ws_event_picture, ws_subscribe, ws_action, ws_clip,
                    ws_stream, ws_api, ws_probe_playback, ws_av, ws_timeline, ws_talk_probe):  # fmt: skip
        websocket_api.async_register_command(hass, handler)
    hass.http.register_view(RaySharpAvView(hass))
    hass.http.register_view(RaySharpAvHlsView(hass))
