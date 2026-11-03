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


def test_history_kept_with_its_own_time():
    log = events.EventLog()
    old = dt.datetime.now() - dt.timedelta(hours=3)
    log.add_alarm({"channel": 1, "alarm_type": "motion"})
    log.add_snapshot({"channel": 2, "alarm_type": "vehicle", "snap_id": 9,
                      "start_time": old.strftime("%Y-%m-%d %H:%M:%S"), "image": "x"})
    log.add_snapshot({"channel": 2, "alarm_type": "vehicle", "snap_id": 9,
                      "start_time": old.strftime("%Y-%m-%d %H:%M:%S"), "image": "x"})
    log.add_snapshot({"channel": 1, "alarm_type": "manual", "image": "x"})
    newest, oldest = log.recent()
    assert newest["type"] == "motion"
    assert oldest["history"] and oldest["picture"] and abs(oldest["ts"] - old.timestamp()) < 2
    assert len(log.events) == 2  # backfill repeat did not duplicate


def test_dump_and_load_round_trip():
    log = events.EventLog()
    log.add_snapshot({"channel": 1, "alarm_type": "person", "snap_id": 1,
                      "image": "AAA", "start_time": time.time()})
    saved, pics = log.dump(), dict(log.pictures)
    again = events.EventLog()
    again.load(saved, pics)
    assert again.recent()[0]["picture"] and again.pictures == pics
    again.add_alarm({"channel": 5, "alarm_type": "motion"})
    assert again.recent()[0]["id"] > saved["events"][0]["id"]


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
    assert log.recent()[0]["picture"]  # the newest keeps its picture


def test_is_recent():
    now = time.time()
    assert events.is_recent(now) and events.is_recent(now * 1000)
    assert not events.is_recent(now - 3600)
    assert events.is_recent("garbage") and events.is_recent(None)


def test_record_flag_forms():
    import sys
    import types

    # coordinator imports Home Assistant; only the pure helper is needed.
    src = (load_module.__globals__["COMPONENT"] / "coordinator.py").read_text()
    body = src[src.index("def _recording("):]
    ns: dict = {"Any": object}
    exec(body, ns)
    rec = ns["_recording"]
    assert rec({"r": "SR"}) and not rec({"r": ""}) and rec(True) and not rec(False)
    assert not rec("") and rec("1")
    del sys, types


def test_record_segments():
    rt = events.record_event_type
    assert rt({"record_type": 1, "new_record_type": "N"}) is None
    assert rt({"record_type": 128, "new_record_type": "SN"}) == "smart"
    assert rt({"record_type": 2, "new_record_type": "M"}) == "motion"
    log = events.EventLog()
    start = time.time() - 3600
    assert log.add_record(1, "smart", start, start + 46)
    assert not log.add_record(1, "smart", start, start + 46)  # next search
    log.add_alarm({"channel": 3, "alarm_type": "person"})
    assert not log.add_record(3, "smart", time.time() - 5, time.time())  # live alarm has it
    ev = log.recent()[-1]
    assert ev["history"] and ev["rec_end"] - ev["rec_start"] == 46
