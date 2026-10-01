from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import make_event, make_session

from reinvent_planner.catalog import Catalog
from reinvent_planner.models import BulkFailure, PersonalTime, Session, clean_text

LA = ZoneInfo("America/Los_Angeles")


def test_interval_uses_session_timezone():
    start, end = make_session("a", time="16:00", length="90").interval(None)
    assert start == datetime(2026, 12, 1, 16, 0, tzinfo=LA)
    assert (end - start).total_seconds() == 90 * 60


def test_interval_falls_back_to_event_timezone():
    s = make_session("a", sessionTime={"date": "2026-12-01", "time": "09:30", "length": "60"})
    assert s.interval(None) is None
    start, _ = s.interval(LA)
    assert start.tzinfo == LA and start.hour == 9


def test_interval_ignores_unknown_timezone_names():
    s = make_session(
        "a",
        sessionTime={
            "date": "2026-12-01",
            "time": "09:30",
            "length": "60",
            "timezone": "Nowhere/Land",
        },
    )
    start, _ = s.interval(LA)
    assert start.tzinfo == LA


def test_interval_all_day():
    s = make_session(
        "a",
        isAllDaySession=True,
        sessionTime={"date": "2026-12-01", "timezone": "America/Los_Angeles"},
    )
    start, end = s.interval(None)
    assert start.hour == 0 and (end - start).days == 1


def test_interval_accepts_12_hour_times():
    start, _ = make_session("a", time="4:00 PM").interval(None)
    assert start.hour == 16


def test_interval_is_none_for_bad_data():
    assert make_session("a", time="soon").interval(None) is None
    assert make_session("a", length="abc").interval(None) is None
    assert make_session("a", date="not-a-date").interval(None) is None
    assert make_session("a", sessionTime=None).interval(LA) is None


def test_minimal_session_and_unknown_fields():
    s = Session.model_validate({"sessionId": "x1", "title": "Only the basics", "brandNewField": 1})
    assert s.code == "x1"
    assert s.topics == [] and s.speaker_names == []
    assert s.interval(LA) is None


def test_personal_time_is_utc():
    pt = PersonalTime.model_validate(
        {
            "personalTimeId": "p1",
            "startDateTime": "2026-12-01T20:00:00",
            "endDateTime": "2026-12-01T21:00:00",
            "title": "Lunch",
            "description": "Team lunch",
        }
    )
    start, _ = pt.interval()
    assert start.astimezone(LA).hour == 12


def test_unknown_bulk_failure_code_is_a_generic_refusal():
    failure = BulkFailure.model_validate({"sessionId": "a", "code": "somethingNew"})
    assert failure.reason == "refused"
    assert (
        BulkFailure.model_validate({"sessionId": "a", "code": "sessionFull"}).reason
        == "session is full"
    )


def test_walk_up_only_from_seat_availability():
    assert make_session("a", seatAvailability="walkUp").is_walk_up
    assert not make_session("a", isReservable=False, seatAvailability=None).is_walk_up


def test_place_falls_back_to_room():
    assert make_session("a", venue=None, room="Caesars Palace | Level 2").place == "Caesars Palace"
    assert make_session("a", venue="MGM Grand", room="Level 3 | X").place == "MGM Grand"
    assert make_session("a", venue=None, room=None).place is None


def test_interval_is_none_for_absurd_lengths_and_dates():
    assert make_session("a", length="999999999999999999").interval(None) is None
    assert make_session("a", length=str(7 * 1440 + 1)).interval(None) is None
    assert make_session("a", length=str(7 * 1440)).interval(None) is not None
    assert make_session("a", date="9999-12-31", time="23:00").interval(None) is None
    assert make_session("a", date="0001-01-01", time="00:00").interval(None) is None
    all_day = make_session("a", isAllDaySession=True, date="9999-12-31")
    assert all_day.interval(None) is None


def test_absurd_session_times_dont_break_a_sync(tmp_path):
    sessions = [
        make_session("a", length="999999999999999999"),
        make_session("b", date="9999-12-31", time="23:00"),
    ]
    with Catalog(tmp_path / "c.sqlite3") as cat:
        assert cat.replace_sessions(make_event(), sessions).total == 2


def test_personal_time_out_of_range_has_no_interval():
    pt = PersonalTime.model_validate(
        {
            "personalTimeId": "p1",
            "startDateTime": "9999-12-31T20:00:00",
            "endDateTime": "9999-12-31T21:00:00",
            "title": "Far future",
            "description": "x",
        }
    )
    assert pt.interval() is None


def test_clean_text_strips_terminal_and_bidi_controls():
    osc8 = "\x1b]8;;https://evil.example\x1b\\Click\x1b]8;;\x1b\\"
    assert clean_text(osc8) == "]8;;https://evil.example\\Click]8;;\\"
    assert clean_text("a\x1b[2Jb\x07\x7f\x85\r") == "a[2Jb"
    assert clean_text("abc\u202edef\u2066\u200e") == "abcdef"
    assert clean_text("line 1\n\tline 2") == "line 1\n\tline 2"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # ZWJ emoji sequence
    assert clean_text(family) == family
    assert clean_text("\u0915\u094d\u200c\u0937") == "\u0915\u094d\u200c\u0937"  # ZWNJ


def test_api_text_is_cleaned_at_the_model_boundary():
    s = make_session(
        "a",
        title="\x1b]8;;https://evil.example\x07Deep dive\x1b]8;;\x07 \U0001f469\u200d\U0001f4bb",
        abstract="First line\nSecond\x1b[31m line",
        room="Room\x9b1",
        speakers=[{"name": "Ada\u202e Example"}],
        topics=["AI\x1b[0m"],
        sessionTime={"date": "2026-12-01", "time": "10:00\x00", "length": "60"},
    )
    assert s.title == "]8;;https://evil.exampleDeep dive]8;; \U0001f469\u200d\U0001f4bb"
    assert s.abstract == "First line\nSecond[31m line"
    assert s.room == "Room1"
    assert s.speaker_names == ["Ada Example"]
    assert s.topics == ["AI[0m"]
    assert s.session_time.time == "10:00"
    assert make_event(name="Conf\x1b[5m").name == "Conf[5m"
    pt = PersonalTime.model_validate(
        {
            "personalTimeId": "p1",
            "startDateTime": "2026-12-01T20:00:00",
            "endDateTime": "2026-12-01T21:00:00",
            "title": "Lunch\x1b[2J",
            "description": "Team\x07 lunch",
            "location": "Caf\u00e9\u2067",
        }
    )
    assert (pt.title, pt.description, pt.location) == ("Lunch[2J", "Team lunch", "Caf\u00e9")


def test_clean_text_also_drops_line_separators_and_invisible_marks():
    from reinvent_planner.models import clean_text

    assert clean_text("a b c﻿d​e؜f") == "abcdef"
    assert clean_text("👩‍💻 line\nnext\ttab") == "👩‍💻 line\nnext\ttab"


def test_line_breaks_survive_only_in_free_text():
    """Round 2 of the review: "Foo\\nSeats: Available" in a title faked a line in `rip show`."""
    from conftest import make_session

    s = make_session("x1", title="Foo\nSeats: Available", abstract="Para one.\n\nPara two.")
    assert s.title == "Foo Seats: Available"
    assert s.abstract == "Para one.\n\nPara two."
