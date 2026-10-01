import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session, signed_in_auth
from typer.testing import CliRunner

from reinvent_planner import cli
from reinvent_planner.api import EventsClient, WriteOutcomeUnknownError
from reinvent_planner.auth import FileTokenStore
from reinvent_planner.catalog import Catalog
from reinvent_planner.models import PersonalTimeInput

LA = ZoneInfo("America/Los_Angeles")


class FakePersonalTimeServer:
    """GetSchedule plus the personal-time operations, with scripted faults."""

    def __init__(self):
        self.entries: list[dict] = []
        self.reserved: list[str] = []
        self.writes: list[tuple[str, str, dict | None]] = []
        self.faults: list[str | None] = []  # "500-applied", "500-dropped"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/schedule"):
            body = {"reserved": self.reserved, "favorites": [], "personalTime": self.entries}
            return httpx.Response(200, json={"schedule": body})
        body = json.loads(request.content) if request.content else None
        self.writes.append((request.method, path, body))
        fault = self.faults.pop(0) if self.faults else None
        if fault == "500-dropped":
            return httpx.Response(500)
        if request.method == "POST" and path.endswith("/personal-time"):
            self.entries.append({"personalTimeId": uuid.uuid4().hex, **body})
            return httpx.Response(500 if fault == "500-applied" else 204)
        entry_id = path.rsplit("/", 1)[-1]
        match = [e for e in self.entries if e["personalTimeId"] == entry_id]
        if not match:
            return httpx.Response(404, json={"message": "no such entry"})
        if request.method == "PUT":
            match[0].clear()
            match[0].update({"personalTimeId": entry_id, **body})
            return httpx.Response(204)
        if request.method == "DELETE":
            self.entries.remove(match[0])
            return httpx.Response(204)
        return httpx.Response(405)


# -- the model ------------------------------------------------------------------


def test_from_local_converts_to_utc_without_an_offset():
    entry = PersonalTimeInput.from_local(
        datetime(2026, 12, 1, 12, 0, tzinfo=LA),
        datetime(2026, 12, 1, 13, 0, tzinfo=LA),
        title=" Team lunch ",
    )
    assert entry.start_date_time == "2026-12-01T20:00:00"
    assert entry.end_date_time == "2026-12-01T21:00:00"
    assert entry.title == "Team lunch" and entry.description == "Team lunch"
    assert entry.location is None


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ((12, 0), (12, 0), "after the start"),
        ((13, 0), (12, 0), "after the start"),
        ((12, 0), (12, 7), "multiple of 5"),
    ],
)
def test_from_local_rejects_what_the_api_would(start, end, message):
    with pytest.raises(ValueError, match=message):
        PersonalTimeInput.from_local(
            datetime(2026, 12, 1, *start, tzinfo=LA),
            datetime(2026, 12, 1, *end, tzinfo=LA),
            title="x",
        )


def test_field_limits():
    with pytest.raises(ValueError):
        PersonalTimeInput.from_local(
            datetime(2026, 12, 1, 12, tzinfo=LA),
            datetime(2026, 12, 1, 13, tzinfo=LA),
            title="x" * 129,
        )


# -- the client -------------------------------------------------------------------


def client_for(server):
    return EventsClient(
        signed_in_auth(), transport=httpx.MockTransport(server), sleep=lambda s: None
    )


def entry():
    return PersonalTimeInput.from_local(
        datetime(2026, 12, 1, 12, tzinfo=LA), datetime(2026, 12, 1, 13, tzinfo=LA), title="Lunch"
    )


def test_create_is_never_resent_after_a_server_error():
    server = FakePersonalTimeServer()
    server.faults = ["500-applied"]
    with pytest.raises(WriteOutcomeUnknownError):
        client_for(server).create_personal_time(EVENT_ID, entry())
    assert len(server.writes) == 1 and len(server.entries) == 1  # no duplicate


def test_delete_of_a_missing_entry_is_already_done():
    client_for(FakePersonalTimeServer()).delete_personal_time(EVENT_ID, "gone")


# -- the CLI ----------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def env(monkeypatch):
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("aim301", time="12:30", venue="Venetian"),  # overlaps a noon lunch
                make_session("cmp201", time="15:00"),
            ],
        )
    FileTokenStore().save(fresh_tokens())
    server = FakePersonalTimeServer()
    server.reserved = ["aim301"]
    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(server), sleep=lambda s: None
        ),
    )
    return server


def rip(*args, input=None):
    return runner.invoke(cli.app, ["--event", EVENT_ID, *args], input=input, env={"COLUMNS": "200"})


def test_add_warns_about_clashes_confirms_and_adds(env):
    result = rip(
        "time",
        "add",
        "Team lunch",
        "--day",
        "tue",
        "--start",
        "12:00",
        "--end",
        "13:00",
        "--where",
        "Wynn buffet",
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "AIM301" in result.output and "overlap" in result.output  # warned before asking
    assert "Added" in result.output
    (added,) = env.entries
    assert added["startDateTime"] == "2026-12-01T20:00:00"
    assert added["location"] == "Wynn buffet" and added["description"] == "Team lunch"


def test_add_accepts_12_hour_times_and_iso_dates(env):
    result = rip(
        "time", "add", "Breakfast", "-d", "2026-12-02", "-s", "7:30am", "--end", "8:15am", "-y"
    )
    assert result.exit_code == 0, result.output
    assert env.entries[0]["startDateTime"] == "2026-12-02T15:30:00"


def test_add_declined_sends_nothing(env):
    result = rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", input="n\n")
    assert result.exit_code == 1 and env.writes == []


def test_add_skips_an_exact_duplicate(env):
    rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    result = rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    assert "already on your schedule" in result.output
    assert len(env.entries) == 1


def test_add_with_unknown_outcome_confirms_by_readback_and_never_resends(env):
    env.faults = ["500-applied"]
    result = rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    assert [w[0] for w in env.writes] == ["POST"] and len(env.entries) == 1


def test_add_that_didnt_happen_says_so_without_resending(env):
    env.faults = ["500-dropped"]
    result = rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    assert result.exit_code == 1
    assert "avoid a duplicate" in result.output
    assert [w[0] for w in env.writes] == ["POST"] and env.entries == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["-d", "sat", "-s", "12:00", "--end", "13:00"], "has no sat"),
        (["-d", "someday", "-s", "12:00", "--end", "13:00"], "isn't a day"),
        (["-d", "tue", "-s", "noon", "--end", "13:00"], "isn't a time"),
        (["-d", "tue", "-s", "12:00", "--end", "12:07"], "multiple of 5"),
        (["-d", "tue", "-s", "13:00", "--end", "13:00"], "after the start"),
    ],
)
def test_add_rejects_bad_input_before_calling_the_api(env, args, message):
    result = rip("time", "add", "Lunch", *args, "-y")
    assert result.exit_code == 1 and message in result.output
    assert "Traceback" not in result.output and env.writes == []


def test_ls_edit_and_rm(env):
    rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    rip("time", "add", "Breakfast", "-d", "tue", "-s", "07:00", "--end", "07:30", "-y")
    listing = rip("time", "ls")
    assert listing.output.index("Breakfast") < listing.output.index("Lunch")  # sorted by time
    edited = rip("time", "edit", "2", "--end", "13:30", "--where", "Encore", input="y\n")
    assert edited.exit_code == 0, edited.output
    assert "Saved:" in edited.output and "12:00–13:30" in edited.output
    lunch = next(e for e in env.entries if e["title"] == "Lunch")
    assert lunch["endDateTime"] == "2026-12-01T21:30:00" and lunch["location"] == "Encore"
    assert lunch["startDateTime"] == "2026-12-01T20:00:00"  # unchanged fields are kept
    removed = rip("time", "rm", lunch["personalTimeId"][:6], "-y")
    assert removed.exit_code == 0, removed.output
    assert "Removed:" in removed.output and "Lunch" in removed.output
    assert [e["title"] for e in env.entries] == ["Breakfast"]


def test_numbers_need_confirmation_because_they_can_shift(env):
    rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    result = rip("time", "rm", "1", "-y")
    assert result.exit_code == 1 and "give the entry ID" in result.output
    assert len(env.entries) == 1
    assert rip("time", "rm", "1", input="y\n").exit_code == 0 and env.entries == []


def test_blocks_can_cross_midnight(env):
    result = rip("time", "add", "Party", "-d", "tue", "-s", "22:30", "--end", "00:30", "-y")
    assert result.exit_code == 0, result.output
    (party,) = env.entries
    assert (party["startDateTime"], party["endDateTime"]) == (
        "2026-12-02T06:30:00",
        "2026-12-02T08:30:00",
    )
    renamed = rip("time", "edit", party["personalTimeId"], "--title", "Dinner party", "-y")
    assert renamed.exit_code == 0, renamed.output  # an overnight block can be renamed as-is
    assert env.entries[0]["endDateTime"] == "2026-12-02T08:30:00"


def test_moving_the_start_keeps_the_length(env):
    rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    entry_id = env.entries[0]["personalTimeId"]
    result = rip("time", "edit", entry_id, "--start", "14:00", "-y")
    assert result.exit_code == 0, result.output
    assert (env.entries[0]["startDateTime"], env.entries[0]["endDateTime"]) == (
        "2026-12-01T22:00:00",
        "2026-12-01T23:00:00",
    )


def test_blocks_longer_than_a_day_are_refused(env):
    rip("time", "add", "Lunch", "-d", "tue", "-s", "12:00", "--end", "13:00", "-y")
    entry_id = env.entries[0]["personalTimeId"]
    result = rip(
        "time", "edit", entry_id, "--day", "wed", "--start", "23:00", "--end", "22:00", "-y"
    )
    assert result.exit_code == 0  # 23:00 -> 22:00 next day is 23 h: fine
    assert (
        rip("time", "add", "x", "-d", "tue", "-s", "12:00", "--end", "12:00", "-y").exit_code == 1
    )


def test_same_title_and_times_but_different_place_is_explained(env):
    rip(
        "time",
        "add",
        "Lunch",
        "-d",
        "tue",
        "-s",
        "12:00",
        "--end",
        "13:00",
        "--where",
        "Cafe A",
        "-y",
    )
    result = rip(
        "time",
        "add",
        "Lunch",
        "-d",
        "tue",
        "-s",
        "12:00",
        "--end",
        "13:00",
        "--where",
        "Cafe B",
        "-y",
    )
    assert "same title and times" in result.output and "rip time edit" in result.output
    assert len(env.entries) == 1


def test_where_can_be_cleared_and_note_defaults_to_the_title(env):
    rip(
        "time",
        "add",
        "Lunch",
        "-d",
        "tue",
        "-s",
        "12:00",
        "--end",
        "13:00",
        "--where",
        "Cafe",
        "-y",
    )
    entry_id = env.entries[0]["personalTimeId"]
    assert env.entries[0]["description"] == "Lunch"
    result = rip("time", "edit", entry_id, "--where", "", "-y")
    assert result.exit_code == 0, result.output
    assert "location" not in env.entries[0]


def test_edit_doesnt_warn_about_the_entry_being_edited(env):
    rip("time", "add", "Focus time", "-d", "tue", "-s", "16:00", "--end", "17:00", "-y")
    result = rip("time", "edit", "1", "--end", "17:30", "-y")
    assert "overlap" not in result.output


def test_unknown_reference_is_a_clean_error(env):
    result = rip("time", "rm", "99")
    assert result.exit_code == 1 and "No personal time" in result.output


def test_an_all_digit_id_prefix_works_with_yes(env, monkeypatch):
    env.entries.append(
        {
            "personalTimeId": "12345678abcdef",
            "startDateTime": "2026-12-01T20:00:00",
            "endDateTime": "2026-12-01T21:00:00",
            "title": "Lunch",
            "description": "Lunch",
        }
    )
    result = rip("time", "rm", "12345678", "-y")
    assert result.exit_code == 0, result.output and env.entries == []
