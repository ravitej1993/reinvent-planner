import io
import os
import stat
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import EVENT_ID, make_session
from icalendar import Calendar

from reinvent_planner import export_ics
from reinvent_planner.export_ics import build_calendar, slugify, uid_for, write_calendar
from reinvent_planner.planner import item_from_session, merge_items

LA = ZoneInfo("America/Los_Angeles")


def build(entries):
    return Calendar.from_ical(build_calendar(entries, event_id=EVENT_ID, name="Test", zone=LA))


def events(cal):
    return {str(e["uid"]): e for e in cal.walk("VEVENT")}


def test_uids_are_stable_across_exports():
    s = make_session("aim301")
    item = item_from_session(s, LA, "favorite")
    first = build([(item, s)])
    second = build([(item, s)])
    assert set(events(first)) == set(events(second)) == {uid_for(item, EVENT_ID)}


def test_times_carry_the_event_timezone():
    s = make_session("aim301", time="16:00")
    cal = build([(item_from_session(s, LA, "favorite"), s)])
    (event,) = events(cal).values()
    assert event["dtstart"].dt.tzinfo is not None
    assert event["dtstart"].dt.hour == 16
    assert event["dtstart"].params["TZID"] == "America/Los_Angeles"
    assert cal.walk("VTIMEZONE")


def test_reserved_is_confirmed_favorite_is_tentative():
    r, f = make_session("r1"), make_session("f1", time="13:00")
    items = merge_items(
        [item_from_session(r, LA, "reserved"), item_from_session(f, LA, "favorite")]
    )
    evs = events(build([(i, {"r1": r, "f1": f}[i.key]) for i in items]))
    reserved, favorite = (
        evs[f"r1@{EVENT_ID}.reinvent-planner"],
        evs[f"f1@{EVENT_ID}.reinvent-planner"],
    )
    assert (reserved["status"], reserved["transp"]) == ("CONFIRMED", "OPAQUE")
    assert (favorite["status"], favorite["transp"]) == ("TENTATIVE", "TRANSPARENT")


def test_description_and_location():
    s = make_session("aim301", venue="Venetian", room="Level 3, Room A")
    (event,) = events(build([(item_from_session(s, LA, "favorite"), s)])).values()
    assert str(event["location"]) == "Venetian | Level 3, Room A"
    assert str(event["summary"]) == "AIM301 – Session aim301"
    assert "Speakers: Ada Example" in str(event["description"])


def test_untimed_sessions_are_skipped():
    s = make_session("tba", time="TBA")
    assert events(build([(item_from_session(s, LA, "favorite"), s)])) == {}


def test_slugify():
    assert slugify("Chalk talk") == "chalk-talk"
    assert slugify("Builders' Session / Lab") == "builders-session-lab"
    assert slugify("!!!") == "sessions"


def test_events_carry_sequence_and_last_modified():
    s = make_session("aim301")
    (event,) = events(build([(item_from_session(s, LA, "favorite"), s)])).values()
    assert int(event["sequence"]) > 0 and event["last-modified"]


def test_all_day_uses_the_sessions_own_date():
    s = make_session(
        "ad", isAllDaySession=True, sessionTime={"date": "2026-12-03", "timezone": "Asia/Tokyo"}
    )
    (event,) = events(build([(item_from_session(s, None, "favorite"), s)])).values()
    assert str(event["dtstart"].dt) == "2026-12-03"  # not shifted to Dec 2 in Las Vegas


def test_calendar_name_cant_inject_lines():
    item = item_from_session(make_session("a"), LA, "reserved")
    data = build_calendar(
        [(item, None)], event_id=EVENT_ID, name="Mine\r\nBEGIN:VEVENT\x1b[2J\n", zone=LA
    )
    lines = data.decode().split("\r\n")
    assert "X-WR-CALNAME:Mine BEGIN:VEVENT[2J" in lines
    assert lines.count("BEGIN:VEVENT") == 1
    assert len(Calendar.from_ical(data).walk("VEVENT")) == 1


def _zone_file() -> bytes:
    import zoneinfo

    for root in zoneinfo.TZPATH:
        candidate = Path(root) / "America" / "Los_Angeles"
        if candidate.is_file():
            return candidate.read_bytes()
    pytest.skip("no system timezone database")


@pytest.mark.parametrize("key", ["Bad\r\nX-INJECTED:1", None])
def test_timezone_without_a_valid_key_is_left_out(key):
    zone = ZoneInfo.from_file(io.BytesIO(_zone_file()), key=key)
    item = item_from_session(make_session("a"), LA, "reserved")
    data = build_calendar([(item, None)], event_id=EVENT_ID, name="n", zone=zone)
    assert b"X-WR-TIMEZONE" not in data
    assert not any(line.startswith(b"X-INJECTED") for line in data.split(b"\r\n"))
    assert len(Calendar.from_ical(data).walk("VEVENT")) == 1


def test_write_calendar_is_atomic_and_replaces_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "out" / "plan.ics"
    path.parent.mkdir()
    write_calendar(path, b"first")
    assert path.read_bytes() == b"first"

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(export_ics.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_calendar(path, b"second")
    assert path.read_bytes() == b"first"  # untouched, and no temp file left behind
    assert os.listdir(path.parent) == ["plan.ics"]
    monkeypatch.undo()
    write_calendar(path, b"second")
    assert path.read_bytes() == b"second" and os.listdir(path.parent) == ["plan.ics"]


def test_write_calendar_replaces_a_symlink_instead_of_following_it(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    link = tmp_path / "plan.ics"
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("can't create symlinks here")
    write_calendar(link, b"BEGIN:VCALENDAR")
    assert victim.read_bytes() == b"precious"
    assert not link.is_symlink() and link.read_bytes() == b"BEGIN:VCALENDAR"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_a_new_calendar_file_is_owner_only_and_an_existing_one_keeps_its_mode(tmp_path):
    new = tmp_path / "new.ics"
    write_calendar(new, b"x")
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    shared = tmp_path / "shared.ics"
    shared.write_bytes(b"old")
    shared.chmod(0o644)  # you chose to share it: rewriting it mustn't take that away
    write_calendar(shared, b"new")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o644 and shared.read_bytes() == b"new"


def test_a_missing_folder_is_an_error_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_calendar(tmp_path / "typo" / "plan.ics", b"x")
    assert not (tmp_path / "typo").exists()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX, not root")
def test_a_read_only_file_is_not_overwritten(tmp_path):
    path = tmp_path / "plan.ics"
    path.write_bytes(b"keep")
    path.chmod(0o444)
    with pytest.raises(PermissionError):
        write_calendar(path, b"new")
    assert path.read_bytes() == b"keep"


def test_a_long_file_name_still_writes(tmp_path):
    path = tmp_path / ("x" * 240 + ".ics")
    write_calendar(path, b"ok")
    assert path.read_bytes() == b"ok"
