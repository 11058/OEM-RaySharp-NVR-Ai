"""In-memory event log for the panel: alarms, detections with pictures, doorbell.

Listens to the integration's own bus events, so every source (Event Check
loop, EventPush webhook, AI database poll) ends up in one ordered list.
An alarm and the detection picture that comes with it (same snapshot id, or
the same channel and type a few seconds apart) become one entry.

Pictures are kept for the newest entries only: each is a JPEG of tens of kB.
"""

from __future__ import annotations

import itertools
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

from .const import EVENT_ALARM, EVENT_DOORBELL, EVENT_SNAPSHOT

MAX_EVENTS = 300
MAX_PICTURES = 40
MERGE_WINDOW_S = 15
KEY = "raysharp_nvr_event_log"


class EventLog:
    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.pictures: OrderedDict[int, str] = OrderedDict()
        self._ids = itertools.count(1)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def listen(self, cb: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def _notify(self, entry: dict[str, Any]) -> None:
        for cb in list(self._listeners):
            cb(entry)

    def _find(self, channel: Any, kind: str, snap_id: Any) -> dict | None:
        now = time.time()
        for ev in reversed(self.events):
            if now - ev["ts"] > MERGE_WINDOW_S:
                break
            if snap_id and ev.get("snap_id") == snap_id:
                return ev
            if ev["channel"] == channel and ev["type"] == kind:
                return ev
        return None

    def _keep_picture(self, ev: dict[str, Any], image: str) -> None:
        self.pictures[ev["id"]] = image
        ev["picture"] = True
        while len(self.pictures) > MAX_PICTURES:
            old, _ = self.pictures.popitem(last=False)
            for e in self.events:
                if e["id"] == old:
                    e["picture"] = False
                    break

    def add_alarm(self, data: dict[str, Any]) -> None:
        channel = data.get("channel", 0)
        kind = str(data.get("alarm_type") or "motion")
        snap_id = data.get("snap_id")
        ev = self._find(channel, kind, snap_id)
        if ev is not None:
            ev["ts"] = time.time()  # still going on
            self._notify(ev)
            return
        ev = {
            "id": next(self._ids),
            "ts": time.time(),
            "channel": channel,
            "type": kind,
            "snap_id": snap_id,
            "source": data.get("source", ""),
            "picture": False,
            "detail": "",
        }
        self.events.append(ev)
        self._notify(ev)

    def add_snapshot(self, data: dict[str, Any]) -> None:
        kind = str(data.get("alarm_type") or "")
        if kind in ("", "manual"):
            return
        if not is_recent(data.get("start_time")):
            return  # history replayed at startup, not something happening now
        channel = data.get("channel", 0)
        ev = self._find(channel, kind, data.get("snap_id"))
        if ev is None:
            ev = {
                "id": next(self._ids),
                "ts": time.time(),
                "channel": channel,
                "type": kind,
                "snap_id": data.get("snap_id"),
                "source": "snapshot",
                "picture": False,
                "detail": "",
            }
            self.events.append(ev)
        detail = data.get("plate_number") or data.get("face_name") or ""
        if detail:
            ev["detail"] = str(detail)
        if data.get("image"):
            self._keep_picture(ev, str(data["image"]))
        self._notify(ev)

    def add_doorbell(self, data: dict[str, Any]) -> None:
        if not data.get("ringing"):
            return
        ev = {
            "id": next(self._ids),
            "ts": time.time(),
            "channel": data.get("channel", 0),
            "type": "doorbell",
            "snap_id": None,
            "source": "doorbell",
            "picture": False,
            "detail": "",
        }
        self.events.append(ev)
        self._notify(ev)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.events)[-limit:][::-1]


def is_recent(start_time: Any, max_age_s: int = 300) -> bool:
    """Was a detection taken within the last few minutes?

    StartTime comes as unix seconds (or ms) or as local "YYYY-MM-DD HH:MM:SS";
    anything unparsable counts as recent.
    """
    import datetime as _dt

    if start_time in (None, ""):
        return True
    now = _dt.datetime.now()
    try:
        ts = float(start_time)
        when = _dt.datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts)
    except (TypeError, ValueError):
        try:
            when = _dt.datetime.fromisoformat(str(start_time).replace("T", " ")[:19])
        except ValueError:
            return True
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return (now - when).total_seconds() <= max_age_s


def async_get_event_log(hass) -> EventLog:
    """One log per Home Assistant run, wired to the bus on first use."""
    from homeassistant.core import Event, callback

    log: EventLog | None = hass.data.get(KEY)
    if log is not None:
        return log
    log = EventLog()
    hass.data[KEY] = log

    @callback
    def _alarm(event: Event) -> None:
        log.add_alarm(dict(event.data))

    @callback
    def _snapshot(event: Event) -> None:
        log.add_snapshot(dict(event.data))

    @callback
    def _doorbell(event: Event) -> None:
        log.add_doorbell(dict(event.data))

    hass.bus.async_listen(EVENT_ALARM, _alarm)
    hass.bus.async_listen(EVENT_SNAPSHOT, _snapshot)
    hass.bus.async_listen(EVENT_DOORBELL, _doorbell)
    return log
