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
    # AI times are the NVR wall clock written as UTC.
    now = time.time() + events.nvr_offset()
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


def test_history_picture_lands_on_its_recording():
    log = events.EventLog()
    start = time.time() - 7200
    log.add_record(4, "smart", start, start + 60)
    when = dt.datetime.fromtimestamp(start + 20).strftime("%Y-%m-%d %H:%M:%S")
    log.add_snapshot({"channel": 4, "alarm_type": "person", "snap_id": 5,
                      "start_time": when, "image": "IMG"})
    assert len(log.events) == 1
    ev = log.events[0]
    assert ev["type"] == "person" and ev["picture"] and ev["rec_end"] == start + 60
    # and the other order: picture first, then the segment search
    log2 = events.EventLog()
    log2.add_snapshot({"channel": 4, "alarm_type": "vehicle", "snap_id": 6,
                       "start_time": when, "image": "IMG"})
    assert not log2.add_record(4, "smart", start, start + 60)
    assert log2.events[0]["type"] == "vehicle" and log2.events[0]["rec_end"] == start + 60


def test_nvr_wide_events_hidden():
    log = events.EventLog()
    log.add_alarm({"channel": 0, "alarm_type": "motion"})
    log.add_alarm({"channel": 2, "alarm_type": "motion"})
    assert [e["channel"] for e in log.recent()] == [2]


def test_playback_variants_utc_first():
    from pathlib import Path

    src = Path(__file__).parents[1] / "custom_components/raysharp_nvr/websocket_api.py"
    text = src.read_text()
    start = text.index("PLAYBACK_VARIANTS = (")
    order = text[start : text.index(")", start)]
    assert order.split('"')[1] == "iso-utc"


def test_nvr_clock_offset_and_ai_times():
    ev = events
    # Event Check said 18:59:25 when it was 13:59:25 UTC: UTC+5.
    now = 1793887165.0  # 2026-11-05 13:59:25 UTC
    assert ev.learn_offset("11/05/2026 18:59:25", now) == 5 * 3600
    # AI StartTime 1793813011 = "22:23:31 UTC" = 17:23:31 local = 12:23:31 UTC.
    assert ev.nvr_time(1793813011, 5 * 3600) == 1793813011 - 18000
    assert ev.nvr_time("2026-11-04 17:23:31", 5 * 3600) == 1793813011 - 18000


def test_old_history_moved_back_on_load():
    ev = events
    ev.set_nvr_offset(5 * 3600)
    try:
        log = ev.EventLog()
        log.load({"next_id": 3, "events": [
            {"id": 1, "ts": 1793813011.0, "channel": 1, "type": "person", "source": "history"},
            {"id": 2, "ts": 1793813000.0, "channel": 1, "type": "motion", "source": "alarm"},
        ]}, {})  # fmt: skip
        by_id = {e["id"]: e["ts"] for e in log.events}
        assert by_id == {1: 1793813011.0 - 18000, 2: 1793813000.0}
        assert log.dump()["clock"] == 2
    finally:
        ev.set_nvr_offset(None)


def test_timeline_segments_merge_and_kinds():
    from datetime import datetime

    timeline_segments = events.timeline_segments

    def row(ch, a, b, code="N", mask=1):
        return {"channel": ch, "start_date": "11/05/2026", "start_time": a,
                "end_date": "11/05/2026", "end_time": b,
                "new_record_type": code, "record_type": mask}

    rows = [
        row("CH1", "10:00:00", "10:30:00"),
        row("CH1", "10:30:01", "11:00:00"),
        row("CH1", "10:40:00", "10:41:00", "SN", 128),
        row("CH2", "10:00:00", "12:00:00"),
        row("CH1", "12:00:00", "12:00:30", "M", 2),
    ]
    segs = timeline_segments(rows, 1)
    t = lambda h: datetime.strptime(f"11/05/2026 {h}", "%m/%d/%Y %H:%M:%S").timestamp()  # noqa: E731
    assert [(x["kind"], x["s"], x["e"]) for x in segs] == [
        ("normal", t("10:00:00"), t("11:00:00")),
        ("smart", t("10:40:00"), t("10:41:00")),
        ("motion", t("12:00:00"), t("12:00:30")),
    ]
