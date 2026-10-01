import json
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session, signed_in_auth
from typer.testing import CliRunner

from reinvent_planner import cli
from reinvent_planner import reserve as reserve_mod
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import FileTokenStore
from reinvent_planner.catalog import Catalog
from reinvent_planner.models import PersonalTime, Schedule
from reinvent_planner.planner import TravelTimes, item_from_personal_time, item_from_session
from reinvent_planner.reserve import Choice, plan_round, run

LA = ZoneInfo("America/Los_Angeles")
TRAVEL = TravelTimes({}, {}, same=5, unknown=30)


class FakeEventsServer:
    """Just enough of the AWS Events API to exercise reservations."""

    def __init__(self, *, full=(), closed=False, reserved=(), personal_time=()):
        self.full = set(full)
        self.closed = closed
        self.reserved: list[str] = list(reserved)
        self.personal_time = list(personal_time)
        self.posts: list[list[str]] = []
        self.deletes: list[str] = []
        # Scripted faults for POSTs: "500-applied", "500-dropped", "timeout-dropped", "429",
        # "403-edge", "200-garbage" (applied, unreadable body).
        self.post_faults: list[str | None] = []
        self.hidden: set[str] = set()  # reserved, but not yet visible in GetSchedule
        self.silent: set[str] = set()  # applied, but never mentioned in the answer
        self.schedule_failures = 0  # how many upcoming GetSchedule calls fail with 500
        self.schedule_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/schedule"):
            self.schedule_reads += 1
            if self.schedule_failures:
                self.schedule_failures -= 1
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "schedule": {
                        "reserved": [r for r in self.reserved if r not in self.hidden],
                        "favorites": [],
                        "personalTime": self.personal_time,
                    }
                },
            )
        if request.method == "POST" and path.endswith("/reservations"):
            ids = json.loads(request.content)["sessionIds"]
            self.posts.append(ids)
            fault = self.post_faults.pop(0) if self.post_faults else None
            if fault == "429":
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"})
            if self.closed:
                return httpx.Response(409, json={"message": "Reservations are not open yet."})
            if fault in ("500-dropped",):
                return httpx.Response(500)
            if fault == "timeout-dropped":
                raise httpx.ReadTimeout("timed out")
            if fault == "403-edge":
                return httpx.Response(403)
            successful, failed = [], []
            for sid in ids:
                if sid in self.reserved:
                    if sid not in self.silent:
                        failed.append({"sessionId": sid, "code": "alreadyScheduled"})
                elif sid in self.full:
                    failed.append({"sessionId": sid, "code": "sessionFull"})
                else:
                    self.reserved.append(sid)
                    if sid not in self.silent:
                        successful.append(sid)
            if fault == "500-applied":
                return httpx.Response(500)
            if fault == "200-garbage":
                return httpx.Response(200, text="<html>ok</html>")
            return httpx.Response(
                200, json={"result": {"successful": successful, "failed": failed}}
            )
        if request.method == "DELETE" and "/reservations/" in path:
            sid = path.rsplit("/", 1)[-1]
            self.deletes.append(sid)
            if sid not in self.reserved:
                return httpx.Response(404, json={"message": "not reserved"})
            self.reserved.remove(sid)
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


SESSIONS = {
    s.session_id: s
    for s in [
        make_session("p1", time="10:00"),
        make_session("b1", time="10:00"),  # backup for p1, same slot
        make_session("p2", time="10:30"),  # overlaps p1
        make_session("p3", time="13:00"),
        make_session("p4", time="15:00"),
        make_session("held", time="08:00"),
    ]
}


def item(sid, kind="ranked"):
    return item_from_session(SESSIONS[sid], LA, kind)


def choices(*specs):
    """specs: ("p1", "b1") means rank 1 = p1 with backup b1."""
    return [Choice(rank, [item(s) for s in spec]) for rank, spec in enumerate(specs, 1)]


def fixed_items(schedule: Schedule):
    items = [item(s, "reserved") for s in schedule.reserved if s in SESSIONS]
    return items + [item_from_personal_time(pt) for pt in schedule.personal_time]


def reserve(server, picks, **kwargs):
    client = EventsClient(
        signed_in_auth(), transport=httpx.MockTransport(server), sleep=lambda s: None
    )
    kwargs.setdefault("sleep", lambda s: None)
    return run(client, EVENT_ID, picks, fixed_items=fixed_items, travel=TRAVEL, **kwargs)


def keys(items):
    return [i.key for i in items]


# -- plan_round -----------------------------------------------------------------


def test_plan_round_takes_first_fitting_option_in_rank_order():
    picks = plan_round(choices(("p1", "b1"), ("p2",), ("p3",)), [], set(), set(), TRAVEL)
    assert [(c.rank, o.key) for c, o in picks] == [(1, "p1"), (3, "p3")]  # p2 overlaps p1


def test_plan_round_skips_excluded_and_uses_backup():
    picks = plan_round(choices(("p1", "b1")), [], set(), {"p1"}, TRAVEL)
    assert [o.key for _, o in picks] == ["b1"]


def test_plan_round_skips_picks_already_held():
    assert plan_round(choices(("p1", "b1")), [item("b1")], {"b1"}, set(), TRAVEL) == []


# -- run ------------------------------------------------------------------------


def test_reserves_everything_that_fits_in_one_round_in_rank_order():
    server = FakeEventsServer()
    report = reserve(server, choices(("p3",), ("p1",), ("p4",)))
    assert server.posts == [["p3", "p1", "p4"]]
    assert keys(report.reserved) == ["p3", "p1", "p4"]
    assert report.unfilled == [] and report.stopped is None


def test_full_pick_falls_back_to_backup():
    server = FakeEventsServer(full={"p1"})
    report = reserve(server, choices(("p1", "b1"), ("p3",)))
    assert server.posts == [["p1", "p3"], ["b1"]]
    assert keys(report.reserved) == ["p3", "b1"]
    assert report.refused == {"p1": "session is full"}


def test_lower_pick_gets_its_chance_when_the_higher_one_fails():
    server = FakeEventsServer(full={"p1"})
    report = reserve(server, choices(("p1",), ("p2",)))  # p2 overlaps p1
    assert server.posts == [["p1"], ["p2"]]
    assert keys(report.reserved) == ["p2"]
    assert [c.rank for c in report.unfilled] == [1]


def test_closed_reservations_stop_immediately():
    server = FakeEventsServer(closed=True)
    report = reserve(server, choices(("p1", "b1"), ("p3",)))
    assert len(server.posts) == 1
    assert report.stopped == "closed" and report.reserved == []
    assert report.refused == {}  # nothing is marked refused: it may work once open


def test_unknown_outcome_that_actually_applied_is_not_resent():
    server = FakeEventsServer()
    server.post_faults = ["500-applied"]
    report = reserve(server, choices(("p1",), ("p3",)))
    assert server.posts == [["p1", "p3"]]
    assert keys(report.reserved) == ["p1", "p3"]


def test_unknown_outcome_resends_only_what_is_missing_then_gives_up():
    server = FakeEventsServer()
    server.post_faults = ["timeout-dropped", "timeout-dropped"]
    report = reserve(server, choices(("p1",)))
    assert server.posts == [["p1"], ["p1"]]
    assert report.refused == {}  # never refused: it may have gone through
    assert keys(report.in_doubt) == ["p1"]


def test_unknown_outcome_retry_can_succeed():
    server = FakeEventsServer()
    server.post_faults = ["500-dropped"]
    report = reserve(server, choices(("p1",)))
    assert server.posts == [["p1"], ["p1"]]
    assert keys(report.reserved) == ["p1"]


def test_existing_reservations_are_respected():
    server = FakeEventsServer(reserved=["b1"])
    report = reserve(server, choices(("p1", "b1"), ("p2",), ("p3",)))
    # Rank 1 is already satisfied by its backup; p2 overlaps b1; only p3 is new.
    assert server.posts == [["p3"]]
    assert keys(report.held) == ["b1"]
    assert keys(report.reserved) == ["p3"]


def test_personal_time_is_never_double_booked():
    lunch = {
        "personalTimeId": "pt1",
        "startDateTime": "2026-12-01T21:00:00",  # 13:00 in Las Vegas
        "endDateTime": "2026-12-01T22:00:00",
        "title": "Lunch",
        "description": "Team lunch",
    }
    PersonalTime.model_validate(lunch)
    server = FakeEventsServer(personal_time=[lunch])
    report = reserve(server, choices(("p3",), ("p4",)))
    assert server.posts == [["p4"]]
    assert [c.rank for c in report.unfilled] == [1]


def test_persistent_throttling_stops_the_run():
    server = FakeEventsServer()
    server.post_faults = ["429"] * 5
    report = reserve(server, choices(("p1",)))
    assert report.stopped == "throttled" and report.reserved == []


def test_round_limit():
    server = FakeEventsServer(full={"p1", "b1"})
    report = reserve(server, choices(("p1", "b1")), max_rounds=1)
    assert report.stopped == "round_limit"


# -- CLI --------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch):
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(event, list(SESSIONS.values()))
        cat.set_rank(EVENT_ID, "p1", 1)
        cat.add_backup(EVENT_ID, "b1", "p1")
        cat.set_rank(EVENT_ID, "p3", 2)
    FileTokenStore().save(fresh_tokens())
    server = FakeEventsServer()
    real_client = EventsClient

    def client_factory(auth=None, **kwargs):
        return real_client(auth, transport=httpx.MockTransport(server), sleep=lambda s: None)

    monkeypatch.setattr(cli, "EventsClient", client_factory)
    monkeypatch.setattr(reserve_mod, "IN_DOUBT_PAUSE_SECONDS", 0)
    return server


def rip(*args):
    return runner.invoke(cli.app, ["--event", EVENT_ID, *args], env={"COLUMNS": "200"})


def test_cli_dry_run_changes_nothing(cli_env):
    result = rip("reserve", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "P1" in result.output and "P3" in result.output
    assert "Dry run" in result.output
    assert cli_env.posts == []


def test_cli_reserve_with_backup(cli_env):
    cli_env.full = {"p1"}
    result = rip("reserve", "--yes")
    assert result.exit_code == 0, result.output
    assert cli_env.posts == [["p1", "p3"], ["b1"]]
    assert "session is full" in result.output
    assert "2 newly reserved" in result.output


def test_cli_reserve_asks_first(cli_env):
    result = runner.invoke(cli.app, ["--event", EVENT_ID, "reserve"], input="n\n")
    assert result.exit_code == 1
    assert cli_env.posts == []


def test_cli_reserve_before_opening(cli_env):
    cli_env.closed = True
    result = rip("reserve", "--yes")
    assert result.exit_code == 1
    assert "aren't open" in result.output


def test_cli_reserve_explicit_codes(cli_env):
    result = rip("reserve", "p4", "--yes")
    assert result.exit_code == 0, result.output
    assert cli_env.posts == [["p4"]]


def test_cli_cancel(cli_env):
    cli_env.reserved = ["p3"]
    result = rip("cancel", "p3", "--yes")
    assert result.exit_code == 0, result.output
    assert cli_env.deletes == ["p3"] and cli_env.reserved == []


def test_cli_cancel_when_not_reserved(cli_env):
    result = rip("cancel", "p3", "--yes")
    assert result.exit_code == 0
    assert cli_env.deletes == []
    assert "isn't reserved" in result.output


def test_partial_batch_failure_keeps_completed_results():
    """Batch 1 (10 picks) answers with one full session; batch 2 times out and is dropped.

    The full session must be refused with its real reason (not re-sent), and only batch 2's
    sessions count as unknown and get re-sent.
    """
    many = {
        f"m{i}": make_session(f"m{i}", date="2026-12-02", time=f"{8 + i}:00", length="30")
        for i in range(12)
    }
    SESSIONS.update(many)
    try:
        server = FakeEventsServer(full={"m3"})
        server.post_faults = [None, "timeout-dropped"]
        report = reserve(server, [Choice(i + 1, [item(f"m{i}")]) for i in range(12)])
        assert server.posts[0] == [f"m{i}" for i in range(10)]
        assert server.posts[1] == ["m10", "m11"]  # the batch that timed out
        assert server.posts[2] == ["m10", "m11"]  # re-sent after the readback, m3 not included
        assert report.refused == {"m3": "session is full"}
        assert sorted(keys(report.reserved)) == sorted(f"m{i}" for i in range(12) if i != 3)
    finally:
        for key in many:
            SESSIONS.pop(key)


# -- regressions from the adversarial review ---------------------------------------

EXTRA = {
    s.session_id: s
    for s in [
        make_session("A", time="09:00"),
        make_session("B", time="09:00"),
        make_session("B2", time="13:00"),
        make_session("AD", isAllDaySession=True, sessionTime={"date": "2026-12-03"}),
        make_session("NT", time="TBA"),
        make_session("Z", time="10:00", venue="Venetian"),
        make_session("Y", time="11:00", venue="Venetian"),
    ]
}


@pytest.fixture
def extra_sessions():
    SESSIONS.update(EXTRA)
    yield
    for key in EXTRA:
        SESSIONS.pop(key, None)


def test_unexpected_api_error_after_a_write_still_returns_the_report():
    server = FakeEventsServer(full={"p1"})
    server.post_faults = [None, "403-edge"]  # round 2 hits an edge refusal
    report = reserve(server, choices(("p1", "b1"), ("p3",)))
    assert report.stopped == "error" and "refusing" in report.error
    assert keys(report.reserved) == ["p3"]  # round 1's success is not lost
    assert report.schedule.reserved == ["p3"]


def test_failed_readback_after_a_write_still_returns_the_report():
    server = FakeEventsServer()
    real_call = server.__call__

    def handler(request):
        if request.method == "POST":
            server.schedule_failures = 5  # every retry of the readback fails
        return real_call(request)

    client = EventsClient(
        signed_in_auth(), transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    report = run(
        client,
        EVENT_ID,
        choices(("p1",)),
        fixed_items=fixed_items,
        travel=TRAVEL,
        sleep=lambda s: None,
    )
    assert report.stopped == "error" and "read your schedule back" in report.error
    assert keys(report.reserved) == ["p1"]  # the API's own "successful" list counts


def test_unreadable_success_body_is_an_unknown_outcome_not_a_crash():
    server = FakeEventsServer()
    server.post_faults = ["200-garbage"]
    report = reserve(server, choices(("p1",)))
    assert keys(report.reserved) == ["p1"]  # applied; confirmed by the readback
    assert server.posts == [["p1"]]  # and not re-sent


def test_pick_waits_instead_of_jumping_to_its_backup(extra_sessions):
    server = FakeEventsServer(full={"A"})
    report = reserve(server, choices(("A",), ("B", "B2")))
    assert server.posts == [["A"], ["B"]]  # B waits for A's outcome, then gets its turn
    assert keys(report.reserved) == ["B"]


def test_backup_used_when_the_higher_pick_succeeds(extra_sessions):
    server = FakeEventsServer()
    report = reserve(server, choices(("A",), ("B", "B2")))
    assert server.posts == [["A"], ["B2"]]
    assert keys(report.reserved) == ["A", "B2"]


def test_success_hidden_by_a_lagging_readback_is_not_resent_or_refused():
    server = FakeEventsServer()
    server.hidden = {"p1"}
    report = reserve(server, choices(("p1", "b1"), ("p3",)))
    assert server.posts == [["p1", "p3"]]
    assert keys(report.reserved) == ["p1", "p3"]
    assert report.refused == {} and report.unfilled == []


def test_already_scheduled_counts_as_held():
    server = FakeEventsServer()
    server.hidden = {"p1"}
    server.reserved = ["p1"]  # held, but the readback doesn't show it
    report = reserve(server, choices(("p1", "b1")))
    assert server.posts == [["p1"]]
    assert report.refused == {} and report.unfilled == []


def test_all_day_and_untimed_picks_are_sent_for_the_api_to_judge(extra_sessions):
    server = FakeEventsServer()
    report = reserve(server, choices(("AD",), ("NT",), ("p1",)))
    assert server.posts == [["AD", "NT", "p1"]]
    assert len(report.reserved) == 3


def test_guessed_travel_time_does_not_block(extra_sessions):
    hotel = {
        "personalTimeId": "pt1",
        "startDateTime": "2026-12-01T17:00:00",  # 09:00-09:45 in Las Vegas
        "endDateTime": "2026-12-01T17:45:00",
        "title": "Breakfast",
        "description": "Breakfast",
        "location": "my hotel room",
    }
    server = FakeEventsServer(personal_time=[hotel])
    report = reserve(server, choices(("Z",)))
    assert keys(report.reserved) == ["Z"]


def test_same_venue_back_to_back_does_not_block(extra_sessions):
    SESSIONS["Z0"] = make_session("Z0", time="10:00", length="60", venue="Venetian")
    SESSIONS["Z1"] = make_session("Z1", time="11:00", venue="Venetian")
    try:
        report = reserve(FakeEventsServer(), choices(("Z0",), ("Z1",)))
        assert keys(report.reserved) == ["Z0", "Z1"]
    finally:
        SESSIONS.pop("Z0")
        SESSIONS.pop("Z1")


def test_pauses_before_re_reading_after_an_unknown_outcome():
    server = FakeEventsServer()
    server.post_faults = ["500-dropped"]
    sleeps = []
    reserve(server, choices(("p1",)), sleep=sleeps.append)
    assert sleeps and sleeps[0] >= 3


def test_cli_walk_up_primary_is_not_replaced_by_its_backup(cli_env):
    with Catalog() as cat:
        event = make_event()
        cat.replace_sessions(
            event,
            [*SESSIONS.values(), make_session("walk", time="10:00", seatAvailability="walkUp")],
        )
        for item in cat.plan_items(EVENT_ID):
            cat.remove_plan_item(EVENT_ID, item.session_id)
        cat.set_rank(EVENT_ID, "walk", 1)
        cat.add_backup(EVENT_ID, "b1", "walk")  # b1 is at 10:00 too
        cat.set_rank(EVENT_ID, "p2", 2)  # 10:30, overlaps the walk-up
        cat.set_rank(EVENT_ID, "p3", 3)
    result = rip("reserve", "--yes")
    assert result.exit_code == 2, result.output  # P2 stays unfilled: it overlaps the walk-up
    assert cli_env.posts == [["p3"]]  # neither b1 (walk-up's backup) nor p2
    assert "walk-up" in result.output


def test_cli_exit_code_2_when_picks_stay_unfilled(cli_env):
    cli_env.full = {"p1", "b1"}
    result = rip("reserve", "--yes")
    assert result.exit_code == 2, result.output
    assert "nothing reserved" in result.output


def test_cli_reports_what_went_through_when_the_api_fails(cli_env):
    cli_env.full = {"p1"}
    cli_env.post_faults = [None, "403-edge"]
    result = rip("reserve", "--yes")
    assert result.exit_code == 1
    assert "P3" in result.output and "Stopped" in result.output


def test_cli_dry_run_lists_alternatives(cli_env):
    result = rip("reserve", "--dry-run")
    assert "may be tried next" in result.output and "B1" in result.output


# -- regressions from the verification pass ------------------------------------------


def test_low_ranked_walk_up_never_blocks_a_higher_pick(extra_sessions):
    walk = item("A", "ranked")  # 09:00, same slot as B
    picks = [Choice(1, [item("B"), item("B2")]), Choice(5, [walk], walk_up=True)]
    assert [o.key for _, o in plan_round(picks, [], set(), set(), TRAVEL)] == ["B"]


def test_high_ranked_walk_up_blocks_lower_picks(extra_sessions):
    walk = item("A", "ranked")
    picks = [Choice(1, [walk], walk_up=True), Choice(2, [item("B"), item("B2")])]
    assert [o.key for _, o in plan_round(picks, [], set(), set(), TRAVEL)] == ["B2"]


def test_walk_up_choices_are_never_sent_or_unfilled(extra_sessions):
    server = FakeEventsServer()
    report = reserve(server, [Choice(1, [item("A")], walk_up=True), Choice(2, [item("p3")])])
    assert server.posts == [["p3"]]
    assert report.unfilled == []


def test_sign_in_failure_mid_run_still_returns_the_report():
    from reinvent_planner.auth import AuthError

    class FlakyAuth(type(signed_in_auth())):
        calls = 0

        def access_token(self):
            FlakyAuth.calls += 1
            if FlakyAuth.calls >= 4:  # round 2's POST and everything after
                raise AuthError("Could not refresh your sign-in (HTTP 503).")
            return super().access_token()

    base = signed_in_auth()
    auth = FlakyAuth(base.store, http=base._http)
    server = FakeEventsServer(full={"p1"})
    client = EventsClient(auth, transport=httpx.MockTransport(server), sleep=lambda s: None)
    report = run(
        client,
        EVENT_ID,
        choices(("p1", "b1"), ("p3",)),
        fixed_items=fixed_items,
        travel=TRAVEL,
        sleep=lambda s: None,
    )
    assert report.stopped == "error" and "refresh" in report.error
    assert keys(report.reserved) == ["p3"]


def test_unknown_outcome_with_failed_readback_is_reported_in_doubt():
    server = FakeEventsServer()
    server.post_faults = ["500-applied"]
    real_call = server.__call__

    def handler(request):
        if request.method == "POST":
            server.schedule_failures = 5
        return real_call(request)

    client = EventsClient(
        signed_in_auth(), transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    report = run(
        client,
        EVENT_ID,
        choices(("p1",)),
        fixed_items=fixed_items,
        travel=TRAVEL,
        sleep=lambda s: None,
    )
    assert report.stopped == "error"
    assert keys(report.in_doubt) == ["p1"]
    assert report.unfilled == []  # not claimed as "nothing reserved"


def test_accepted_but_invisible_is_reported():
    server = FakeEventsServer()
    server.hidden = {"p1"}
    report = reserve(server, choices(("p1",)))
    assert keys(report.not_visible) == ["p1"]


def test_cli_exit_2_when_nothing_fits(cli_env):
    cli_env.personal_time = [
        {
            "personalTimeId": "pt",
            "startDateTime": "2026-12-01T16:00:00",  # 08:00-23:00 in Las Vegas
            "endDateTime": "2026-12-02T07:00:00",
            "title": "Busy",
            "description": "Busy all day",
        }
    ]
    result = rip("reserve", "--yes")
    assert result.exit_code == 2, result.output
    assert "Nothing new to reserve" in result.output
    assert cli_env.posts == []


# -- regressions from the live re:Invent 2026 catalog -------------------------------


def test_sessions_marked_not_reservable_are_still_sent(cli_env):
    """Before reserved seating opens, the live catalog marks every session isReservable=false
    with no seatAvailability. Those must still be tried; only walkUp means walk-up."""
    with Catalog() as cat:
        cat.replace_sessions(
            make_event(),
            [
                make_session(
                    sid, time=s.session_time.time, isReservable=False, seatAvailability=None
                )
                for sid, s in SESSIONS.items()
            ],
        )
    result = rip("reserve", "--yes")
    assert result.exit_code == 0, result.output
    assert cli_env.posts == [["p1", "p3"]]
    assert "walk-up" not in result.output


def test_travel_never_blocks_a_reservation(extra_sessions):
    SESSIONS["far"] = make_session("far", time="11:10", venue="MGM Grand")  # 10 min after Z ends
    try:
        far_table = TravelTimes.from_toml(
            '[aliases]\nven = ["venetian"]\nmgm = ["mgm grand"]\n[minutes]\n"ven|mgm" = 45\n'
        )
        client = EventsClient(
            signed_in_auth(),
            transport=httpx.MockTransport(FakeEventsServer()),
            sleep=lambda s: None,
        )
        report = run(
            client,
            EVENT_ID,
            choices(("Z",), ("far",)),
            fixed_items=fixed_items,
            travel=far_table,
            sleep=lambda s: None,
        )
        assert keys(report.reserved) == ["Z", "far"]
    finally:
        SESSIONS.pop("far")


def test_cli_reserve_preview_warns_about_travel_but_still_reserves(cli_env):
    with Catalog() as cat:
        cat.replace_sessions(
            make_event(),
            [
                *SESSIONS.values(),
                make_session("hop", time="14:05", venue="MGM Grand"),  # 5 min after p3 (Venetian)
            ],
        )
        cat.set_rank(EVENT_ID, "hop", 3)
    result = rip("reserve", "--yes")
    assert result.exit_code == 0, result.output
    assert "will still be reserved" in result.output
    assert cli_env.posts == [["p1", "p3", "hop"]]


# -- live events and cancel (launch control) ------------------------------------------


def test_events_narrate_each_session_including_the_fallback_to_a_backup():
    server = FakeEventsServer(full={"p1"})
    events = []
    report = reserve(server, choices(("p1", "b1"), ("p3",)), on_event=events.append)
    story = [(e.round, e.kind, e.item.key if e.item else None, e.backup) for e in events]
    assert story == [
        (1, "sending", "p1", False),
        (1, "sending", "p3", False),
        (1, "refused", "p1", False),
        (1, "booked", "p3", False),
        (2, "sending", "b1", True),
        (2, "booked", "b1", True),
    ]
    assert events[2].reason  # the API's reason, for the log
    assert keys(report.reserved) == ["p3", "b1"]


def test_a_stop_is_announced_once_with_its_reason():
    events = []
    report = reserve(FakeEventsServer(closed=True), choices(("p1",)), on_event=events.append)
    assert report.stopped == "closed"
    assert [(e.kind, e.reason) for e in events if e.kind == "stopped"] == [("stopped", "closed")]


def test_cancel_takes_effect_between_rounds_never_mid_request():
    server = FakeEventsServer(full={"p1"})
    rounds = []

    def cancelled():
        return len(rounds) >= 1  # cancel pressed during round 1

    report = reserve(
        server,
        choices(("p1", "b1"), ("p3",)),
        on_round=lambda n, picks: rounds.append(n),
        cancelled=cancelled,
    )
    assert report.stopped == "cancelled"
    assert server.posts == [["p1", "p3"]]  # round 1 finished; the backup round never started
    assert keys(report.reserved) == ["p3"] and report.in_doubt == []


def test_cancel_before_anything_is_sent_sends_nothing():
    server = FakeEventsServer()
    report = reserve(server, choices(("p1",)), cancelled=lambda: True)
    assert report.stopped == "cancelled" and server.posts == []


def test_rip_reserve_refuses_while_another_run_holds_the_lock(cli_env):
    from reinvent_planner import launch

    with launch.reservation_lock(EVENT_ID):
        result = rip("reserve", "--yes")
    assert result.exit_code == 1 and "Another reservation run" in result.output


def test_rip_reserve_reports_and_forgets_a_cut_off_run(cli_env):
    from reinvent_planner import launch

    launch.Journal(EVENT_ID).record(1, ["p1", "p3"])
    cli_env.reserved.append("p3")  # p3 landed before the cut-off; p1 didn't
    result = rip("reserve", "--dry-run")
    assert "cut off while sending" in result.output
    assert "shows P3" in result.output and "not reserved: P1" in result.output
    assert launch.Journal(EVENT_ID).load() is None


def test_a_normal_run_leaves_no_journal(cli_env):
    from reinvent_planner import launch

    result = rip("reserve", "--yes")
    assert result.exit_code in (0, 2), result.output
    assert cli_env.posts  # it did send
    assert launch.Journal(EVENT_ID).load() is None


def test_cancel_during_a_later_batchs_wait_keeps_the_earlier_batchs_seats():
    """12 picks go out in two batches. Batch 1 reserves 10; batch 2 is rate limited, Cancel is
    pressed during the wait, and the first readback attempt fails. The 10 must still show as
    reserved: a cancel stops further writes, never the reconciliation of ones already sent."""
    from reinvent_planner.api import Cancelled

    many = {
        f"m{i:02d}": make_session(
            f"m{i:02d}", time=f"{8 + i // 2:02d}:{30 * (i % 2):02d}", length="25"
        )
        for i in range(12)
    }
    SESSIONS.update(many)
    try:
        server = FakeEventsServer()
        server.post_faults = [None] + ["429"] * 10
        phase = ["read"]
        cancel_pressed = []

        def wait(seconds):
            cancel_pressed.append(True)  # the user presses Cancel during the 429 wait
            if phase[0] != "readback":
                raise Cancelled

        real_call = server.__call__
        readbacks = []

        def handler(request):
            if request.method == "GET" and server.posts and not readbacks:
                readbacks.append(1)
                return httpx.Response(503)  # the first readback attempt fails
            return real_call(request)

        client = EventsClient(signed_in_auth(), transport=httpx.MockTransport(handler), sleep=wait)
        report = run(
            client,
            EVENT_ID,
            [Choice(n, [item(sid)]) for n, sid in enumerate(sorted(many), 1)],
            fixed_items=fixed_items,
            travel=TRAVEL,
            sleep=lambda s: None,
            cancelled=lambda: bool(cancel_pressed),
            on_phase=lambda name: phase.__setitem__(0, name),
        )
        assert report.stopped == "cancelled"
        assert len(server.posts) == 2  # batch 2 was never re-sent after the cancel
        assert sorted(keys(report.reserved)) == sorted(many)[:10]
        assert not {c.primary.key for c in report.unfilled} & set(sorted(many)[:10])
    finally:
        for sid in many:
            SESSIONS.pop(sid)


def test_even_if_the_readback_is_cut_short_the_earlier_batchs_seats_are_kept():
    """The same as above with a caller whose wait cancels everything, readback included: the
    first batch's results (carried on the Cancelled error) still count."""
    from reinvent_planner.api import Cancelled

    many = {
        f"n{i:02d}": make_session(
            f"n{i:02d}", time=f"{8 + i // 2:02d}:{30 * (i % 2):02d}", length="25"
        )
        for i in range(12)
    }
    SESSIONS.update(many)
    try:
        server = FakeEventsServer()
        server.post_faults = [None] + ["429"] * 10
        real_call = server.__call__

        def handler(request):
            if request.method == "GET" and server.posts:
                return httpx.Response(503)  # every readback attempt fails
            return real_call(request)

        def wait(seconds):
            raise Cancelled

        client = EventsClient(signed_in_auth(), transport=httpx.MockTransport(handler), sleep=wait)
        report = run(
            client,
            EVENT_ID,
            [Choice(n, [item(sid)]) for n, sid in enumerate(sorted(many), 1)],
            fixed_items=fixed_items,
            travel=TRAVEL,
            sleep=lambda s: None,
        )
        assert report.stopped == "cancelled"
        assert sorted(keys(report.reserved)) == sorted(many)[:10]
        assert report.in_doubt == []  # batch 2 only ever got a 429: nothing was performed
    finally:
        for sid in many:
            SESSIONS.pop(sid)


def test_cancel_during_the_first_batchs_wait_leaves_nothing_in_doubt():
    from reinvent_planner.api import Cancelled

    server = FakeEventsServer()
    server.post_faults = ["429"] * 10

    def wait(seconds):
        raise Cancelled

    client = EventsClient(signed_in_auth(), transport=httpx.MockTransport(server), sleep=wait)
    report = run(
        client,
        EVENT_ID,
        choices(("p1",), ("p3",)),
        fixed_items=fixed_items,
        travel=TRAVEL,
        sleep=lambda s: None,
    )
    assert report.stopped == "cancelled"
    assert report.reserved == [] and report.in_doubt == []
    assert len(server.posts) == 1


def test_a_live_runs_journal_is_left_alone_by_a_dry_run(cli_env):
    """The review's P3-M1: Arm or `--dry-run` during a run elsewhere wiped its journal."""
    from reinvent_planner import launch

    launch.Journal(EVENT_ID).record(1, ["p1", "p3"])
    with launch.reservation_lock(EVENT_ID):  # a run in progress in another tab
        result = rip("reserve", "--dry-run")
    assert "in progress" in result.output and "cut off" not in result.output
    assert launch.Journal(EVENT_ID).load()["sent"] == ["p1", "p3"]


def test_an_unreadable_journal_is_reported_not_silently_dropped(cli_env):
    from reinvent_planner import launch

    launch.Journal(EVENT_ID).path.write_text("{garbage")
    result = rip("reserve", "--dry-run")
    assert "couldn't be read" in result.output
    assert not launch.Journal(EVENT_ID).path.exists()


def test_a_pick_that_may_be_reserved_keeps_its_backup_back():
    """The review's P3-M2: 500s that were applied plus a lagging readback booked p1 AND b1."""
    server = FakeEventsServer()
    server.post_faults = ["500-applied", "500-applied"]
    server.hidden = {"p1"}  # applied, but the readback doesn't show it yet
    report = reserve(server, choices(("p1", "b1")))
    assert ["b1"] not in server.posts
    assert keys(report.in_doubt) == ["p1"] and report.refused == {}
    assert report.unfilled == []  # not "nothing reserved": it may be


def test_after_a_failed_readback_the_stale_schedule_isnt_cached(cli_env, monkeypatch):
    """The review's P3-L5: the pre-write schedule was cached (and published) after a failed
    readback, dropping seats that had just been reserved."""

    def handler(request):
        if request.method == "GET" and cli_env.posts:
            return httpx.Response(500)  # every readback after the write fails
        return cli_env(request)

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(handler), sleep=lambda s: None
        ),
    )
    published = []
    monkeypatch.setattr(cli, "_refresh_feed", lambda *args, **kw: published.append(args))
    rip("reserve", "--yes")
    assert cli_env.posts  # it did send
    assert published == []  # the pre-write schedule is never pushed to the calendar feed


def test_a_pick_given_up_on_that_a_later_readback_shows_counts_as_booked():
    """Round 2 of the review: a presumed session confirmed later vanished from the report.
    p1 is applied but never confirmed (twice), so it's given up on; pick 2 needs three rounds
    (p3 and p4 are full), and round 3's readback finally shows p1."""
    server = FakeEventsServer(full={"p3", "p4"})
    server.silent = server.hidden = {"p1"}
    real_call = server.__call__

    def handler(request):
        if request.method == "POST" and len(server.posts) == 2:
            server.hidden = set()  # the readback catches up in round 3
        return real_call(request)

    events = []
    client = EventsClient(
        signed_in_auth(), transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    report = run(
        client,
        EVENT_ID,
        choices(("p1", "b1"), ("p3", "p4", "held")),
        fixed_items=fixed_items,
        travel=TRAVEL,
        sleep=lambda s: None,
        on_event=events.append,
    )
    assert server.posts == [["p1", "p3"], ["p1", "p4"], ["held"]]
    assert [e.kind for e in events if e.item and e.item.key == "p1"][-2:] == ["in_doubt", "booked"]
    assert sorted(keys(report.reserved)) == ["held", "p1"] and report.in_doubt == []
    assert any(e.kind == "booked" and e.item.key == "p1" for e in events)
