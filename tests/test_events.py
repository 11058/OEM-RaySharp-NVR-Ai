"""Event log behind the video panel."""

from __future__ import annotations

import datetime as dt
import time

from conftest import load_module

events = load_module("events")


def test_alarm_and_picture_merge():
    log = events.EventLog()
    log.add_alarm({"channel": 3, "alarm_type": "person", "snap_id": 77})
    log.add_snapshot({"channel": 3, "alarm_type": "person", "snap_id": 77,
                      "image": "QUJD", "start_time": int(time.time())})
    assert len(log.events) == 1
    ev = log.recent()[0]
    assert ev["picture"] and log.pictures[ev["id"]] == "QUJD"


def test_repeated_alarm_extends_not_duplicates():
    log = events.EventLog()
    log.add_alarm({"channel": 1, "alarm_type": "motion"})
    log.add_alarm({"channel": 1, "alarm_type": "motion"})
    log.add_alarm({"channel": 2, "alarm_type": "motion"})
    assert len(log.events) == 2


def test_history_and_manual_snapshots_skipped():
    log = events.EventLog()
    old = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    log.add_snapshot({"channel": 1, "alarm_type": "vehicle", "start_time": old, "image": "x"})
    log.add_snapshot({"channel": 1, "alarm_type": "manual", "image": "x"})
    assert not log.events


def test_plate_detail_and_doorbell():
    log = events.EventLog()
    log.add_snapshot({"channel": 2, "alarm_type": "plate", "plate_number": "А123ВС59",
                      "start_time": time.time()})
    log.add_doorbell({"channel": 4, "ringing": True})
    log.add_doorbell({"channel": 4, "ringing": False})
    kinds = [e["type"] for e in log.recent()]
    assert kinds == ["doorbell", "plate"]
    assert log.recent()[1]["detail"] == "А123ВС59"


def test_picture_cap():
    log = events.EventLog()
    for i in range(events.MAX_PICTURES + 5):
        log.add_snapshot({"channel": i, "alarm_type": "person", "image": "x",
                          "start_time": time.time()})
    assert len(log.pictures) == events.MAX_PICTURES
    assert sum(1 for e in log.events if e["picture"]) == events.MAX_PICTURES


def test_is_recent():
    now = time.time()
    assert events.is_recent(now) and events.is_recent(now * 1000)
    assert not events.is_recent(now - 3600)
    assert events.is_recent("garbage") and events.is_recent(None)
