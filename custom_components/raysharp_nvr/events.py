"""Event log for the panel: alarms, detections with pictures, doorbell.

Listens to the integration's own bus events, so every source (Event Check
loop, EventPush webhook, AI database poll and its startup backfill) ends up in
one list ordered by the time things happened. An alarm and the detection
picture that comes with it (same snapshot id, or the same channel and type a
few seconds apart) become one entry. Detections replayed from the NVR's
database keep their own time and are marked as history, so they fill the feed
without lighting up a camera.

The log survives restarts: entries go to a HA Store, pictures to files next
to it (the NVR may refuse to hand them out again later).
"""

from __future__ import annotations

import bisect
import datetime as dt
import time
from collections.abc import Callable
from typing import Any

from .const import EVENT_ALARM, EVENT_DOORBELL, EVENT_SNAPSHOT

MAX_EVENTS = 3000  # about a week on a busy site; metadata only
MAX_PICTURES = 200
MERGE_WINDOW_S = 15
RECENT_S = 300
KEY = "raysharp_nvr_event_log"
STORE_KEY = "raysharp_nvr.event_log"
PICTURE_DIR = "raysharp_nvr_pictures"


def parse_ts(value: Any) -> float | None:
    """NVR StartTime -> unix time: seconds, milliseconds or local 'Y-m-d H:M:S'."""
    if value in (None, ""):
        return None
    try:
        ts = float(value)
        return ts / 1000 if ts > 1e11 else ts
    except (TypeError, ValueError):
        pass
    text = str(value).replace("T", " ").replace("/", "-").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m-%d-%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            continue
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.timestamp()


# RaySharp keeps detection times (AI StartTime/EndTime) as the NVR's wall
# clock written as if it were UTC: 17:23 local in UTC+5 arrives as the epoch
# of 17:23 UTC. Seen on NVR-167R V8.2.7: every history event sat 5 hours late
# and its clip opened at the wrong hour. The coordinator learns the NVR's UTC
# offset from the clock in Event Check; until then HA's own zone is assumed.
_NVR_OFFSET: list[float | None] = [None]


def set_nvr_offset(offset_s: float | None) -> None:
    _NVR_OFFSET[0] = offset_s


def nvr_offset() -> float:
    if _NVR_OFFSET[0] is not None:
        return _NVR_OFFSET[0]
    return float(time.localtime().tm_gmtoff)


def wall_to_epoch(date_s: str, time_s: str) -> float:
    """NVR wall clock ('11/06/2026', '16:03:16') -> unix time, by the NVR's own
    UTC offset (learned from its clock), whatever zone HA or the NVR is in."""
    wall = dt.datetime.strptime(f"{date_s} {time_s}", "%m/%d/%Y %H:%M:%S")
    return wall.replace(tzinfo=dt.UTC).timestamp() - nvr_offset()


def epoch_to_wall(ts: float) -> tuple[str, str]:
    """unix time -> the NVR's wall clock as its API wants it (date, time)."""
    wall = dt.datetime.fromtimestamp(ts + nvr_offset(), dt.UTC)
    return wall.strftime("%m/%d/%Y"), wall.strftime("%H:%M:%S")


def search_window(since: float, until: float) -> dict[str, str]:
    """start_/end_ date and time for Playback/SearchRecord, in the NVR's clock."""
    d0, t0 = epoch_to_wall(since)
    d1, t1 = epoch_to_wall(until)
    return {"start_date": d0, "start_time": t0, "end_date": d1, "end_time": t1}


def offset_from_zone(zone: str) -> float | None:
    """'GMT+5:00', 'GMT-3:30', 'UTC+8' (SystemConfig/DateTime time_zone) -> seconds."""
    import re as _re  # noqa: PLC0415

    m = _re.match(r"^\s*(?:GMT|UTC)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$", str(zone or ""))
    if not m:
        return 0.0 if str(zone or "").strip().upper() in ("GMT", "UTC") else None
    sign = -1 if m.group(1) == "-" else 1
    return sign * (int(m.group(2)) * 3600 + int(m.group(3) or 0) * 60)


def offset_from_date_header(value: str, now: float | None = None) -> float | None:
    """RTSP/HTTP 'Date: Fri, 06 Nov 2026 16:14:34 GMT' from a RaySharp NVR is its
    WALL clock labelled GMT: minus the real time it gives the NVR's UTC offset."""
    from email.utils import parsedate_to_datetime  # noqa: PLC0415

    try:
        wall = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if wall is None:
        return None
    if wall.tzinfo is None:
        wall = wall.replace(tzinfo=dt.UTC)
    return round((wall.timestamp() - (now if now is not None else time.time())) / 900) * 900


def learn_offset(nvr_clock: str, now: float | None = None) -> float | None:
    """'11/05/2026 18:59:25' (Event Check alarm_list time) -> UTC offset, s."""
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            wall = dt.datetime.strptime(str(nvr_clock).strip()[:19], fmt)
        except ValueError:
            continue
        as_utc = wall.replace(tzinfo=dt.UTC).timestamp()
        return round((as_utc - (now if now is not None else time.time())) / 900) * 900
    return None


def nvr_time(value: Any, offset: float | None = None) -> float | None:
    """A detection time from the NVR -> real unix time."""
    if value in (None, ""):
        return None
    off = nvr_offset() if offset is None else offset
    try:
        ts = float(value)
    except (TypeError, ValueError):
        text = str(value).replace("T", " ").replace("/", "-").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%m-%d-%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                wall = dt.datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
            return wall.replace(tzinfo=dt.UTC).timestamp() - off
        return parse_ts(value)
    ts = ts / 1000 if ts > 1e11 else ts
    return ts - off


def is_recent(start_time: Any, max_age_s: int = RECENT_S) -> bool:
    """Was a detection taken within the last few minutes? Unknown = yes."""
    ts = nvr_time(start_time)
    return ts is None or time.time() - ts <= max_age_s


def record_event_type(rec: dict[str, Any]) -> str | None:
    """Recording segment -> journal event type; None for plain recording.

    new_record_type letters seen: N normal, SN smart (AI), M motion,
    A/I alarm input, P PIR. record_type is a bit mask (1 normal, 128 smart).
    """
    code = str(rec.get("new_record_type") or "").upper()
    mask = rec.get("record_type")
    if code in ("", "N") and mask in (None, 1, "1"):
        return None
    if "S" in code or (isinstance(mask, int) and mask & 128):
        return "smart"
    if "M" in code or (isinstance(mask, int) and mask & 2):
        return "motion"
    if "P" in code:
        return "pir"
    if "A" in code or "I" in code or (isinstance(mask, int) and mask & 4):
        return "io"
    return "motion" if code not in ("", "N") else None


def timeline_segments(rows: Any, channel: int) -> list[dict[str, Any]]:
    """SearchRecord rows -> [{s, e, kind}] for one channel, sorted; touching
    pieces of the same kind merged. kind: normal / motion / smart / pir / io.
    Times come in the NVR's wall clock and are converted by its own offset."""

    out: list[dict[str, Any]] = []
    for rec in rows:
        digits = "".join(c for c in str(rec.get("channel", "")) if c.isdigit())
        if digits and int(digits) != channel:
            continue
        try:
            s = wall_to_epoch(rec["start_date"], rec["start_time"])
            e = wall_to_epoch(rec["end_date"], rec["end_time"])
        except (KeyError, ValueError):
            continue
        kind = record_event_type(rec) or "normal"
        out.append({"s": s, "e": e, "kind": kind})
    out.sort(key=lambda r: (r["s"], r["e"]))
    merged: list[dict[str, Any]] = []
    for r in out:
        last = merged[-1] if merged else None
        if last and last["kind"] == r["kind"] and r["s"] <= last["e"] + 2:
            last["e"] = max(last["e"], r["e"])
        else:
            merged.append(dict(r))
    return merged


class EventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []  # oldest first
        self.pictures: dict[int, str] = {}
        # Limits (options); the NVR's archive start per channel: events older
        # than it have no recording any more.
        self.max_events = MAX_EVENTS
        self.max_pictures = MAX_PICTURES
        self.archive_start: dict[int, float] = {}
        self._next_id = 1
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        # Persistence hooks, set by the HA wrapper.
        self.on_change: Callable[[], None] | None = None
        self.on_picture: Callable[[int, str | None], None] | None = None

    # ── plumbing ──────────────────────────────────────────────────────────
    def listen(self, cb: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def _notify(self, entry: dict[str, Any]) -> None:
        for cb in list(self._listeners):
            cb(entry)
        if self.on_change:
            self.on_change()

    def _new(self, ts: float, channel: Any, kind: str, **extra: Any) -> dict[str, Any]:
        ev = {
            "id": self._next_id,
            "ts": ts,
            "channel": channel,
            "type": kind,
            "snap_id": None,
            "source": "",
            "picture": False,
            "detail": "",
            **extra,
        }
        self._next_id += 1
        keys = [e["ts"] for e in self.events]
        self.events.insert(bisect.bisect_right(keys, ts), ev)
        while len(self.events) > self.max_events:
            self._drop_picture(self.events.pop(0))
        return ev

    def _drop_picture(self, ev: dict[str, Any]) -> None:
        if self.pictures.pop(ev["id"], None) is not None and self.on_picture:
            self.on_picture(ev["id"], None)
        ev["picture"] = False

    def _keep_picture(self, ev: dict[str, Any], image: str) -> None:
        self.pictures[ev["id"]] = image
        ev["picture"] = True
        if self.on_picture:
            self.on_picture(ev["id"], image)
        if len(self.pictures) > self.max_pictures:
            # Drop pictures of the oldest events first.
            for old in self.events:
                if len(self.pictures) <= self.max_pictures:
                    break
                if old["id"] in self.pictures and old is not ev:
                    self._drop_picture(old)

    def _find(self, channel: Any, kind: str, snap_id: Any) -> dict | None:
        """A live entry to merge into: same snapshot, or same camera and type just now."""
        now = time.time()
        for ev in reversed(self.events):
            if now - ev.get("last", ev["ts"]) > MERGE_WINDOW_S:
                continue
            if snap_id and ev.get("snap_id") == snap_id:
                return ev
            if ev["channel"] == channel and ev["type"] == kind and not ev.get("history"):
                return ev
        return None

    def _near_record(self, channel: Any, when: float) -> dict | None:
        """A recording segment of this camera that this detection falls into."""
        for ev in reversed(self.events):
            if ev["ts"] < when - 600:
                break
            if ev["channel"] != channel or ev.get("source") != "record" or ev.get("snap_id"):
                continue
            if ev.get("rec_start", ev["ts"]) - 5 <= when <= ev.get("rec_end", ev["ts"]) + 5:
                return ev
        return None

    def _by_snap(self, channel: Any, snap_id: Any) -> dict | None:
        if not snap_id:
            return None
        for ev in reversed(self.events):
            if ev.get("snap_id") == snap_id and ev["channel"] == channel:
                return ev
        return None

    # ── inputs ────────────────────────────────────────────────────────────
    def add_alarm(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Record an alarm; returns the entry and whether it is a new one."""
        channel = data.get("channel", 0)
        kind = str(data.get("alarm_type") or "motion")
        snap_id = data.get("snap_id")
        ev = self._find(channel, kind, snap_id)
        if ev is not None:
            ev["last"] = time.time()  # still going on
            self._notify(ev)
            return ev, False
        ev = self._new(time.time(), channel, kind, snap_id=snap_id,
                       source=data.get("source", ""))  # fmt: skip
        self._notify(ev)
        return ev, True

    def attach_frame(self, ev: dict[str, Any], image: str) -> None:
        """A camera frame taken at alarm time, unless a detection crop came first."""
        if ev.get("picture") and ev["id"] in self.pictures:
            return
        ev["frame"] = True
        self._keep_picture(ev, image)
        self._notify(ev)

    def add_snapshot(self, data: dict[str, Any]) -> None:
        kind = str(data.get("alarm_type") or "")
        if kind in ("", "manual"):
            return
        channel = data.get("channel", 0)
        snap_id = data.get("snap_id")
        when = nvr_time(data.get("start_time"))
        now = time.time()
        history = when is not None and now - when > RECENT_S

        ev = self._by_snap(channel, snap_id)  # already known: backfill repeats
        if ev is not None and history and ev.get("source") == "history" and abs(
            ev.get("ts", 0) - when
        ) > 60:
            ev["ts"] = when  # stored before the NVR clock was understood
            self.events.sort(key=lambda e: e.get("ts", 0))
        if ev is None and not history:
            ev = self._find(channel, kind, snap_id)
        if ev is None and history:
            ev = self._near_record(channel, when)
            if ev is not None:
                ev["type"] = kind  # "smart" segment -> what was actually seen
                ev["snap_id"] = snap_id
        if ev is None:
            ev = self._new(when if history else now, channel, kind, snap_id=snap_id,
                           source="history" if history else "snapshot",
                           history=history)  # fmt: skip
        elif snap_id and not ev.get("snap_id"):
            ev["snap_id"] = snap_id
        detail = data.get("plate_number") or data.get("face_name") or ""
        if detail:
            ev["detail"] = str(detail)
        if data.get("image") and not (ev.get("picture") and not ev.get("frame")):
            ev["frame"] = False  # the NVR's own detection picture wins
            self._keep_picture(ev, str(data["image"]))
        self._notify(ev)

    def add_record(self, channel: int, kind: str, start: float, end: float) -> bool:
        """An event-triggered recording segment found on the NVR's disk.

        Skipped when the same camera already has an entry for it (the live
        alarm that caused it, or this segment from an earlier search).
        """
        for ev in reversed(self.events):
            if ev["ts"] < start - 120:
                break
            if ev["channel"] != channel:
                continue
            if ev.get("rec_start") == start or abs(ev["ts"] - start) <= 90 or (
                ev.get("history") and start - 5 <= ev["ts"] <= end + 5
            ):
                ev.setdefault("rec_start", start)
                ev["rec_end"] = end
                return False
        ev = self._new(start, channel, kind, source="record", history=time.time() - start > RECENT_S,
                       rec_start=start, rec_end=end)  # fmt: skip
        self._notify(ev)
        return True

    def add_doorbell(self, data: dict[str, Any]) -> None:
        if not data.get("ringing"):
            return
        ev = self._new(time.time(), data.get("channel", 0), "doorbell", source="doorbell")
        self._notify(ev)

    # ── limits, archive, clearing ─────────────────────────────────────────
    def configure(self, max_events: int, max_pictures: int) -> bool:
        """Apply new limits; trims at once. True if anything was dropped."""
        self.max_events = max(50, int(max_events))
        self.max_pictures = max(0, int(max_pictures))
        changed = False
        while len(self.events) > self.max_events:
            self._drop_picture(self.events.pop(0))
            changed = True
        if len(self.pictures) > self.max_pictures:
            for old in self.events:
                if len(self.pictures) <= self.max_pictures:
                    break
                if old["id"] in self.pictures:
                    self._drop_picture(old)
                    changed = True
        return changed

    def gone(self, ev: dict[str, Any]) -> bool:
        """The NVR no longer has a recording of this event (archive overwritten
        or the disk formatted). Unknown archive = not gone."""
        start = self.archive_start.get(ev.get("channel"))
        if start is None:
            return False
        return (ev.get("rec_end") or ev.get("last") or ev["ts"]) < start

    def remove(self, keep: Callable[[dict[str, Any]], bool]) -> int:
        before = len(self.events)
        kept = []
        for ev in self.events:
            if keep(ev):
                kept.append(ev)
            else:
                self._drop_picture(ev)
        self.events = kept
        removed = before - len(kept)
        if removed and self.on_change:
            self.on_change()
        return removed

    def clear(self, mode: str = "all") -> int:
        if mode == "gone":
            return self.remove(lambda ev: not self.gone(ev))
        return self.remove(lambda ev: False)

    # ── outputs ───────────────────────────────────────────────────────────
    def recent(self, limit: int = 100, since: float | None = None) -> list[dict[str, Any]]:
        """Newest first; `since` cuts by event time. `gone`: no recording left."""
        start = 0
        if since is not None:
            start = bisect.bisect_left([e["ts"] for e in self.events], since)
        rows = [e for e in self.events[start:] if e["channel"] or e["type"] == "doorbell"]
        return [{**e, "gone": self.gone(e)} for e in rows[-limit:][::-1]]

    def dump(self) -> dict[str, Any]:
        return {"next_id": self._next_id, "events": self.events, "clock": 2}

    def load(self, data: dict[str, Any] | None, pictures: dict[int, str]) -> None:
        if not data:
            return
        events = [e for e in data.get("events", []) if isinstance(e, dict) and "id" in e]
        if data.get("clock", 1) < 2:
            # Logs from before v1.4.8 took AI times at face value (NVR wall
            # clock as UTC): move those back by the zone offset.
            off = nvr_offset()
            for e in events:
                if e.get("source") == "history":
                    e["ts"] = e.get("ts", 0) - off
        events.sort(key=lambda e: e.get("ts", 0))
        self.events = events[-self.max_events:]
        ids = {e["id"] for e in self.events}
        self.pictures = {k: v for k, v in pictures.items() if k in ids}
        for e in self.events:
            e["picture"] = e["id"] in self.pictures
        self._next_id = max([data.get("next_id", 1), *(e["id"] + 1 for e in self.events)])


def get_event_log(hass) -> EventLog:
    """The log set up by async_setup_event_log (an empty one before that)."""
    log = hass.data.get(KEY)
    if log is None:
        log = hass.data[KEY] = EventLog()
    return log


async def async_setup_event_log(hass) -> EventLog:
    """Load the saved log once per HA run and wire it to the bus."""
    from pathlib import Path

    from homeassistant.core import Event, callback
    from homeassistant.helpers.storage import Store

    flag = f"{KEY}_ready"
    if hass.data.get(flag):
        return get_event_log(hass)
    hass.data[flag] = True
    log = get_event_log(hass)
    store: Store = Store(hass, 1, STORE_KEY)
    folder = Path(hass.config.path(".storage", PICTURE_DIR))

    def _read_pictures() -> dict[int, str]:
        out: dict[int, str] = {}
        if folder.is_dir():
            for f in folder.glob("*.b64"):
                try:
                    out[int(f.stem)] = f.read_text()
                except (OSError, ValueError):
                    continue
        return out

    saved = await store.async_load()
    pictures = await hass.async_add_executor_job(_read_pictures)
    log.load(saved, pictures)

    def _write(pid: int, image: str | None) -> None:
        path = folder / f"{pid}.b64"
        try:
            if image is None:
                path.unlink(missing_ok=True)
            else:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(image)
        except OSError:
            pass

    def _orphans() -> None:
        keep = set(log.pictures)
        if folder.is_dir():
            for f in folder.glob("*.b64"):
                if not f.stem.isdigit() or int(f.stem) not in keep:
                    f.unlink(missing_ok=True)

    await hass.async_add_executor_job(_orphans)
    log.on_change = lambda: store.async_delay_save(log.dump, 15)
    log.on_picture = lambda pid, img: hass.async_add_executor_job(_write, pid, img)

    last_frame: dict[Any, float] = {}

    async def _grab_frame(ev: dict[str, Any]) -> None:
        """Most NVR alarms carry no picture: take one from the camera now."""
        from .const import API_SNAPSHOT, DOMAIN

        coords = [c for c in hass.data.get(DOMAIN, {}).values() if hasattr(c, "client")]
        if len(coords) != 1:
            return  # cannot tell which NVR the channel belongs to
        key = ev["channel"]
        now = time.monotonic()
        if now - last_frame.get(key, 0.0) < 4:
            return
        last_frame[key] = now
        try:
            resp = await coords[0].client.async_api_call(
                API_SNAPSHOT,
                {"channel": f"CH{ev['channel']}", "snapshot_resolution": "1280 x 720",
                 "reset_session_timeout": False},
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - a missing frame is not an error
            return
        data = resp.get("data", resp) if isinstance(resp, dict) else {}
        img = data.get("img_data") or data.get("ima_data")
        if img:
            log.attach_frame(ev, str(img))

    @callback
    def _alarm(event: Event) -> None:
        if not event.data.get("channel"):
            return  # NVR-wide flag, not a camera: noise in the journal
        ev, new = log.add_alarm(dict(event.data))
        if new:
            hass.async_create_background_task(_grab_frame(ev), "raysharp_nvr alarm frame")

    @callback
    def _snapshot(event: Event) -> None:
        log.add_snapshot(dict(event.data))

    @callback
    def _doorbell(event: Event) -> None:
        log.add_doorbell(dict(event.data))

    hass.bus.async_listen(EVENT_ALARM, _alarm)
    hass.bus.async_listen(EVENT_SNAPSHOT, _snapshot)
    hass.bus.async_listen(EVENT_DOORBELL, _doorbell)

    async def _final_save(_event: Event) -> None:
        await store.async_save(log.dump())

    from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, _final_save)
    return log
