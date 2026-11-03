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


def is_recent(start_time: Any, max_age_s: int = RECENT_S) -> bool:
    """Was a detection taken within the last few minutes? Unknown = yes."""
    ts = parse_ts(start_time)
    return ts is None or time.time() - ts <= max_age_s


class EventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []  # oldest first
        self.pictures: dict[int, str] = {}
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
        while len(self.events) > MAX_EVENTS:
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
        if len(self.pictures) > MAX_PICTURES:
            # Drop pictures of the oldest events first.
            for old in self.events:
                if len(self.pictures) <= MAX_PICTURES:
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
        when = parse_ts(data.get("start_time"))
        now = time.time()
        history = when is not None and now - when > RECENT_S

        ev = self._by_snap(channel, snap_id)  # already known: backfill repeats
        if ev is None and not history:
            ev = self._find(channel, kind, snap_id)
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

    def add_doorbell(self, data: dict[str, Any]) -> None:
        if not data.get("ringing"):
            return
        ev = self._new(time.time(), data.get("channel", 0), "doorbell", source="doorbell")
        self._notify(ev)

    # ── outputs ───────────────────────────────────────────────────────────
    def recent(self, limit: int = 100, since: float | None = None) -> list[dict[str, Any]]:
        """Newest first; `since` cuts by event time."""
        if since is None:
            return self.events[-limit:][::-1]
        keys = [e["ts"] for e in self.events]
        return self.events[bisect.bisect_left(keys, since):][-limit:][::-1]

    def dump(self) -> dict[str, Any]:
        return {"next_id": self._next_id, "events": self.events}

    def load(self, data: dict[str, Any] | None, pictures: dict[int, str]) -> None:
        if not data:
            return
        events = [e for e in data.get("events", []) if isinstance(e, dict) and "id" in e]
        events.sort(key=lambda e: e.get("ts", 0))
        self.events = events[-MAX_EVENTS:]
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
        ev, new = log.add_alarm(dict(event.data))
        if new and ev["channel"]:
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
