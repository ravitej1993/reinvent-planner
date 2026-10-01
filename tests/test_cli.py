import pytest
from conftest import EVENT_ID, make_event, make_session
from typer.testing import CliRunner

from reinvent_planner.catalog import Catalog, SearchFilters
from reinvent_planner.cli import app
from reinvent_planner.models import Schedule

runner = CliRunner()


def rip(*args):
    return runner.invoke(app, ["--event", EVENT_ID, *args], env={"COLUMNS": "200"})


@pytest.fixture(autouse=True)
def seeded_catalog():
    """A local catalog and cached schedule, so these tests never touch the network."""
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("aim301", title="Agents workshop", type="Workshop"),
                make_session("aim302", title="Agents repeat", type="Workshop", time="10:30"),
                make_session("cmp201", title="Graviton talk", time="13:00", venue="MGM Grand"),
                make_session("res100", title="Already reserved", time="08:00"),
            ],
        )
        cat.save_schedule(EVENT_ID, Schedule(reserved=["res100"], favorites=["cmp201"]))


def test_search_shows_marks_and_hides_nothing_needed():
    result = rip("search", "agents")
    assert result.exit_code == 0, result.output
    assert "AIM301" in result.output and "AIM302" in result.output
    assert "CMP201" not in result.output


def test_rank_checklist_and_markdown(tmp_path):
    assert rip("rank", "set", "aim301", "1", "--backup", "cmp201").exit_code == 0
    assert rip("rank", "set", "aim302", "2").exit_code == 0
    out = tmp_path / "checklist.md"
    result = rip("checklist", "--offline", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "RESERVE" in result.output
    text = out.read_text(encoding="utf-8")
    assert "**#1 AIM301**" in text
    assert "backup CMP201" in text
    assert "clashes with AIM301" in text  # AIM302 overlaps the #1 pick


def test_plan_flags_problems():
    rip("rank", "set", "aim301", "1")
    rip("rank", "set", "aim302", "2")
    result = rip("plan", "--offline")
    assert result.exit_code == 0, result.output
    assert "AIM301 and AIM302 overlap" in result.output


def test_ics_export(tmp_path):
    result = rip("ics", "--offline", "--out-dir", str(tmp_path), "--split-by-type")
    assert result.exit_code == 0, result.output
    files = sorted(p.name for p in tmp_path.iterdir())
    assert f"{EVENT_ID}-plan.ics" in files
    assert f"{EVENT_ID}-reserved.ics" in files
    assert "BEGIN:VEVENT" in (tmp_path / f"{EVENT_ID}-plan.ics").read_text()


def test_show_unknown_session_is_a_clean_error():
    result = rip("show", "nope123")
    assert result.exit_code == 1
    assert "No session" in result.output
    assert "Traceback" not in result.output


def test_writes_require_sign_in_before_asking():
    result = rip("fav", "add", "aim301")
    assert result.exit_code == 1
    assert "rip login" in result.output
    assert "Add 1 favorite" not in result.output


def test_whoami_when_signed_out():
    result = rip("whoami")
    assert result.exit_code == 1 and "Not signed in" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and "reinvent-planner" in result.output


def test_sync_warns_about_venues_missing_from_the_travel_table(monkeypatch):
    import httpx

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient

    event = make_event(eventId="reinvent2026")
    sessions = [
        make_session("v1", venue="Venetian"),
        make_session("v2", venue="Mystery Hall", time="13:00"),
    ]

    def handler(request):
        if request.url.path.endswith("/sessions"):
            return httpx.Response(
                200,
                json={
                    "items": [s.model_dump(by_alias=True, exclude_none=True) for s in sessions],
                    "totalCount": 2,
                },
            )
        return httpx.Response(200, json={"event": event.model_dump(by_alias=True)})

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(auth, transport=httpx.MockTransport(handler)),
    )
    result = runner.invoke(app, ["--event", "reinvent2026", "sync"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "Mystery Hall" in result.output
    assert "Venetian" not in result.output.split("travel-time table")[1]


def test_reservable_search_explains_an_empty_result_before_opening():
    with Catalog() as cat:
        event = make_event()
        cat.replace_sessions(
            event,
            [make_session(f"s{i}", isReservable=False, seatAvailability=None) for i in range(4)],
        )
    result = rip("search", "--reservable")
    assert "0 result(s)" in result.output
    assert "marked reservable yet" in result.output


def test_csv_plan_and_catalog(tmp_path):
    rip("rank", "set", "aim301", "1")
    out = tmp_path / "plan.csv"
    result = rip("csv", "--offline", "--out", str(out))
    assert result.exit_code == 0, result.output
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    text = raw.decode("utf-8-sig")
    assert "AIM301" in text and "CMP201" in text and "RES100" in text  # ranked, fav, reserved
    assert "AIM302" not in text
    everything = tmp_path / "all.csv"
    assert rip("csv", "--all", "--offline", "--out", str(everything)).exit_code == 0
    assert everything.read_text(encoding="utf-8-sig").count("\n") == 5  # header + 4 sessions


def test_csv_to_stdout():
    result = rip("csv", "--all", "--offline", "--out", "-")
    assert result.exit_code == 0
    assert result.stdout.startswith("Day,Date,Start")  # notes go to stderr, not the CSV


def test_sync_flags_your_picks_that_are_filling_up(monkeypatch):
    import httpx

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient

    with Catalog() as cat:
        cat.set_rank(EVENT_ID, "aim301", 1)
    event = make_event()
    now = [
        make_session(
            "aim301", title="Agents workshop", type="Workshop", seatAvailability="veryLimited"
        ),
        make_session(
            "aim302",
            title="Agents repeat",
            type="Workshop",
            time="10:30",
            seatAvailability="unavailable",
        ),
        make_session("cmp201", title="Graviton talk", time="13:00", venue="MGM Grand"),
        make_session(
            "res100", title="Already reserved", time="08:00", seatAvailability="unavailable"
        ),
    ]

    def handler(request):
        if request.url.path.endswith("/sessions"):
            items = [s.model_dump(by_alias=True, exclude_none=True) for s in now]
            return httpx.Response(200, json={"items": items, "totalCount": len(items)})
        return httpx.Response(200, json={"event": event.model_dump(by_alias=True)})

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(auth, transport=httpx.MockTransport(handler)),
    )
    result = rip("sync")
    assert result.exit_code == 0, result.output
    assert "Filling up on your list (1)" in result.output
    assert "AIM301" in result.output and "Available → Very limited" in result.output
    assert "2 other session(s) also filled up" in result.output  # AIM302, and RES100 (reserved)


def test_setup_when_already_signed_in(monkeypatch):
    import httpx
    from conftest import fresh_tokens

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient
    from reinvent_planner.auth import FileTokenStore

    FileTokenStore().save(fresh_tokens())
    event = make_event()

    def handler(request):
        if request.url.path.endswith("/sessions"):
            with Catalog() as cat:
                current = cat.search(EVENT_ID, SearchFilters(limit=None))
            items = [s.model_dump(by_alias=True, exclude_none=True) for s in current]
            return httpx.Response(200, json={"items": items, "totalCount": len(items)})
        if request.url.path.endswith("/schedule"):
            return httpx.Response(
                200, json={"schedule": {"reserved": [], "favorites": [], "personalTime": []}}
            )
        return httpx.Response(200, json={"event": event.model_dump(by_alias=True)})

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(auth, transport=httpx.MockTransport(handler)),
    )
    result = rip("setup")
    assert "Already signed in as attendee@example.com" in result.output
    assert result.exit_code == 0, result.output
    assert "Synced 4 sessions" in result.output and "You're set" in result.output


def test_csv_stdout_is_utf8_even_on_a_legacy_console():
    import os
    import subprocess
    import sys

    with Catalog() as cat:
        cat.replace_sessions(
            make_event(),
            [
                make_session(f"s{i}", title="Café – 日本語 talk", time=f"{8 + i}:00")
                for i in range(4)
            ],
        )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [
            sys.executable,
            "-m",
            "reinvent_planner",
            "--event",
            EVENT_ID,
            "csv",
            "--all",
            "--offline",
            "-o",
            "-",
        ],
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "Café – 日本語 talk" in proc.stdout.decode("utf-8")


def _sync_with(monkeypatch, sessions, schedule_status=200):
    import httpx

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient

    event = make_event()

    def handler(request):
        if request.url.path.endswith("/sessions"):
            items = [s.model_dump(by_alias=True, exclude_none=True) for s in sessions]
            return httpx.Response(200, json={"items": items, "totalCount": len(items)})
        if request.url.path.endswith("/schedule"):
            return httpx.Response(schedule_status)
        return httpx.Response(200, json={"event": event.model_dump(by_alias=True)})

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(handler), sleep=lambda seconds: None
        ),
    )
    return rip("sync")


def test_first_sync_after_opening_flags_picks_that_are_already_full(monkeypatch):
    """Oct 8: sessions go from no seat data at all straight to full."""
    with Catalog() as cat:
        cat.replace_sessions(  # before opening: no seat data anywhere, as in the live catalog
            make_event(),
            [
                make_session(sid, seatAvailability=None, isReservable=False, time=t)
                for sid, t in (
                    ("aim301", "10:00"),
                    ("aim302", "10:30"),
                    ("cmp201", "13:00"),
                    ("res100", "08:00"),
                )
            ],
        )
        cat.set_rank(EVENT_ID, "aim301", 1)
        cat.set_rank(EVENT_ID, "cmp201", 2)
    result = _sync_with(
        monkeypatch,
        [
            make_session(
                "aim301", title="Agents workshop", type="Workshop", seatAvailability="unavailable"
            ),
            make_session(
                "aim302",
                title="Agents repeat",
                type="Workshop",
                time="10:30",
                seatAvailability="available",
            ),
            make_session(
                "cmp201",
                title="Graviton talk",
                time="13:00",
                venue="MGM Grand",
                seatAvailability="limited",
            ),
            make_session("res100", title="Already reserved", time="08:00"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Filling up on your list (1)" in result.output
    assert "AIM301" in result.output and "now Full" in result.output
    assert "CMP201" not in result.output.split("Filling up")[1]  # none → limited isn't alarming


def test_filling_alerts_survive_a_failed_schedule_refresh(monkeypatch):
    from conftest import fresh_tokens

    from reinvent_planner.auth import FileTokenStore

    FileTokenStore().save(fresh_tokens())
    with Catalog() as cat:
        cat.set_rank(EVENT_ID, "aim301", 1)
    result = _sync_with(
        monkeypatch,
        [
            make_session(
                "aim301", title="Agents workshop", type="Workshop", seatAvailability="unavailable"
            ),
            make_session("aim302", title="Agents repeat", type="Workshop", time="10:30"),
            make_session("cmp201", title="Graviton talk", time="13:00", venue="MGM Grand"),
            make_session("res100", title="Already reserved", time="08:00"),
        ],
        schedule_status=503,
    )
    assert result.exit_code == 0, result.output
    assert "AIM301" in result.output and "Available → Full" in result.output
    assert "schedule wasn't refreshed" in result.output


def test_setup_signs_in_again_when_the_saved_sign_in_is_dead(monkeypatch):
    from conftest import fresh_tokens

    from reinvent_planner import cli
    from reinvent_planner.auth import Auth, FileTokenStore, NotSignedInError

    FileTokenStore().save(fresh_tokens(expires_at=0))
    logins = []

    def dead_refresh(self):
        raise NotSignedInError("expired")

    def fake_login(self, **kwargs):
        logins.append(True)
        raise SystemExit(0)  # stop here: we only care that setup chose to sign in again

    monkeypatch.setattr(Auth, "access_token", dead_refresh)
    monkeypatch.setattr(Auth, "login", fake_login)
    result = rip("setup")
    assert logins == [True]
    assert "saved sign-in has expired" in result.output
    assert cli  # imported for the monkeypatch targets


def test_csv_to_a_closed_pipe_exits_quietly():
    import os
    import subprocess
    import sys

    with Catalog() as cat:
        cat.replace_sessions(
            make_event(), [make_session(f"s{i}", time=f"{8 + i % 10}:00") for i in range(4)]
        )
    cmd = [sys.executable, "-m", "reinvent_planner", "--event", EVENT_ID]
    cmd += ["csv", "--all", "--offline", "-o", "-"]
    proc = subprocess.Popen(  # noqa: S603 - fixed arguments, our own interpreter
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ)
    )
    proc.stdout.close()  # the reader goes away before anything is written
    _, stderr = proc.communicate(timeout=60)
    assert b"Traceback" not in stderr, stderr.decode(errors="replace")


def test_search_favorites_and_reserved_use_your_schedule():
    favs = rip("search", "--favorites")
    assert "CMP201" in favs.output and "AIM301" not in favs.output
    both = rip("search", "--favorites", "--reserved")
    assert "CMP201" in both.output and "RES100" in both.output and "AIM301" not in both.output


def test_search_laptop_and_feature():
    result = rip("search", "--laptop")
    assert "AIM301" in result.output and "CMP201" not in result.output  # workshops only here


def test_rank_set_warns_about_an_unreachable_transfer():
    with Catalog() as cat:
        cat.replace_sessions(
            make_event(eventId=EVENT_ID),
            [
                make_session("aim301", time="10:00", venue="Venetian"),
                make_session("aim302", time="10:30"),
                make_session("cmp201", time="13:00"),
                make_session("res100", time="08:00"),
                make_session("new900", time="11:10", venue="MGM Grand"),  # 10 min after aim301
                make_session("alt901", time="10:00", venue="Venetian"),  # same slot as aim301
            ],
        )
    rip("rank", "set", "aim301", "1")
    result = runner.invoke(
        app,
        ["--event", EVENT_ID, "rank", "set", "new900", "2", "--backup", "alt901"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    assert "AIM301 → NEW900" in result.output
    assert "needs ~30" in result.output  # no table for this test event: unknown-venue guess
    assert "ALT901 → NEW900" not in result.output  # never compared with its own primary
    assert "AIM301 and ALT901 overlap" in result.output  # but clashes with other picks show


def test_rank_set_is_quiet_for_sessions_already_on_the_plan():
    rip("rank", "set", "aim301", "1")
    result = rip("rank", "set", "cmp201", "2")  # cmp201 is already a favorite
    assert "⚠" not in result.output


def test_fav_add_warns_before_asking(monkeypatch):
    import httpx
    from conftest import fresh_tokens

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient
    from reinvent_planner.auth import FileTokenStore

    FileTokenStore().save(fresh_tokens())
    # aim301 (10:00-11:00) is already a favorite; aim302 (10:30) overlaps it.
    schedule = {"reserved": ["res100"], "favorites": ["aim301"], "personalTime": []}

    def handler(request):
        if request.url.path.endswith("/schedule"):
            return httpx.Response(200, json={"schedule": schedule})
        return httpx.Response(500)

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(auth, transport=httpx.MockTransport(handler)),
    )
    result = runner.invoke(
        app, ["--event", EVENT_ID, "fav", "add", "aim302"], input="n\n", env={"COLUMNS": "200"}
    )
    assert "AIM301 and AIM302 overlap" in result.output
    assert result.output.index("⚠") < result.output.index("Add 1 favorite")  # warned first
    assert result.exit_code == 1  # declined at the prompt: nothing was sent


def test_venues_command_for_reinvent():
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "Encore" in result.output and "MGM Grand" in result.output
    assert "Wynn/Encore  " not in result.output  # no phantom combined column before a sync
    assert "km" in result.output and "OpenStreetMap" in result.output


def test_venues_command_without_a_table():
    result = rip("venues")
    assert "No travel-time table" in result.output


def test_search_favorites_falls_back_to_the_cache_when_offline(monkeypatch):
    import httpx
    from conftest import fresh_tokens

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient
    from reinvent_planner.auth import FileTokenStore

    FileTokenStore().save(fresh_tokens())

    def handler(request):
        raise httpx.ConnectError("conference wifi")

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(handler), sleep=lambda s: None
        ),
    )
    result = rip("search", "--favorites")
    assert result.exit_code == 0, result.output
    assert "CMP201" in result.output and "using the cache" in result.output
    assert rip("search", "--favorites", "--offline").exit_code == 0


def test_search_favorites_hints_when_there_is_no_schedule():
    with Catalog() as cat:
        cat.db.execute("DELETE FROM schedules")
        cat.db.commit()
    result = rip("search", "--favorites")
    assert "0 result(s)" in result.output and "rip login" in result.output


def test_venues_tells_wynn_and_encore_apart():
    with Catalog() as cat:
        event = make_event(eventId="reinvent2026")
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("w", venue=None, room="Wynn/Encore | Convention Promenade | Latour 5"),
                make_session("e", venue=None, room="Wynn/Encore | Level 1 | Chopin 4"),
                make_session("m", venue="MGM Grand", room="Level 3 | Room"),
            ],
        )
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "200"})
    assert "Wynn " in result.output and "Encore" in result.output and "MGM Grand" in result.output
    assert "Venetian" not in result.output  # not used by these sessions
    assert "never refuses" in result.output


def test_invalid_override_file_is_a_clean_cli_error():
    from reinvent_planner.auth import config_dir

    path = config_dir() / "venues" / "reinvent2026.toml"
    path.parent.mkdir(parents=True)
    path.write_text("not = [valid", encoding="utf-8")
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "invalid" in result.output and "Traceback" not in result.output


def _alternatives_catalog():
    with Catalog() as cat:
        cat.replace_sessions(
            make_event(eventId=EVENT_ID),  # no travel table: different venues need a 30-min guess
            [
                make_session("aim301", time="09:00", venue="MGM Grand"),  # on the plan, ends 10:00
                make_session("aim302", time="12:00"),
                make_session("cmp201", time="13:00"),
                make_session("res100", time="08:00"),
                make_session("prim", time="10:20", venue="Venetian"),  # 20 min after aim301: tight
                make_session("bk1", time="10:10", venue="MGM Grand"),  # same venue: fine
                make_session("bk2", time="10:20", venue="MGM Grand"),  # overlaps bk1
            ],
        )
        cat.set_rank(EVENT_ID, "aim301", 1)


def test_backups_never_warn_about_each_other():
    _alternatives_catalog()
    result = rip("rank", "set", "prim", "2", "--backup", "bk1", "--backup", "bk2")
    assert "BK1 and BK2 overlap" not in result.output


def test_a_backup_cannot_hide_its_primarys_transfer_warning():
    """bk1 (10:10) sits between aim301 and prim on a shared timeline; checked alone, prim's
    tight transfer from aim301 is still reported."""
    _alternatives_catalog()
    result = rip("rank", "set", "prim", "2", "--backup", "bk1", "--backup", "bk2")
    assert "AIM301 → PRIM" in result.output


def test_favorites_added_together_are_checked_against_each_other(monkeypatch):
    import httpx
    from conftest import fresh_tokens

    from reinvent_planner import cli
    from reinvent_planner.api import EventsClient
    from reinvent_planner.auth import FileTokenStore

    FileTokenStore().save(fresh_tokens())
    schedule = {"reserved": [], "favorites": [], "personalTime": []}

    def handler(request):
        return httpx.Response(200, json={"schedule": schedule})

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(auth, transport=httpx.MockTransport(handler)),
    )
    result = runner.invoke(
        app,
        ["--event", EVENT_ID, "fav", "add", "aim301", "aim302"],  # 10:00 and 10:30: overlap
        input="n\n",
        env={"COLUMNS": "200"},
    )
    assert "AIM301 and AIM302 overlap" in result.output


def test_readme_travel_table_matches_the_data_file():
    import pathlib

    from reinvent_planner.planner import TravelTimes

    t = TravelTimes.load("reinvent2026")
    keys = [k for k in t.venue_keys() if k != "wynn_encore"]
    rows = [
        f"| **{t.name(a)}** | "
        + " | ".join("—" if a == b else str(t.planned(a, b)) for b in keys)
        + " |"
        for a in keys
    ]
    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    for row in rows:
        assert row in readme, f"README travel table is out of date: {row}"


def test_warnings_mention_the_monorail_when_it_is_faster():
    from reinvent_planner.cli import _walk_note
    from reinvent_planner.planner import Issue, Item, TravelTimes

    travel = TravelTimes.load("reinvent2026")
    forum = Item("f", "F", "f", None, None, "Caesars Forum", None)
    mgm = Item("m", "M", "m", None, None, "MGM Grand", None)
    venetian = Item("v", "V", "v", None, None, "Venetian", None)
    wynn_room = Item(
        "w", "W", "w", None, None, None, "Wynn/Encore | Convention Promenade | Latour 5"
    )
    note = _walk_note(Issue("tight", forum, mgm), travel)
    assert "27 min on foot" in note and "monorail off-peak, Harrah's/The LINQ → MGM Grand" in note
    back = _walk_note(Issue("tight", mgm, forum), travel)
    assert "MGM Grand → Harrah's/The LINQ" in back  # oriented the way you travel
    assert "monorail" not in _walk_note(Issue("tight", venetian, wynn_room), travel)


def test_route_text_from_an_override_cannot_inject_markup():
    from reinvent_planner.auth import config_dir
    from reinvent_planner.cli import _walk_note
    from reinvent_planner.planner import Issue, Item, TravelTimes

    path = config_dir() / "venues" / "reinvent2026.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[aliases]\na = ["alpha"]\nb = ["beta"]\n'
        '[walking]\n"a|b" = { meters = 900, minutes = 30 }\n'
        '[monorail]\n"a|b" = { from = "[/]evil[link=http://x]", to = "B", minutes = 10 }\n',
        encoding="utf-8",
    )
    travel = TravelTimes.load("reinvent2026")
    one = Item("1", "ONE", "one", None, None, "Alpha", None)
    two = Item("2", "TWO", "two", None, None, "Beta", None)
    import io

    from rich.console import Console

    Console(file=io.StringIO()).print(
        _walk_note(Issue("tight", one, two), travel)
    )  # no MarkupError


def test_venues_shows_monorail_column():
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "220"})
    assert "Monorail" in result.output and "24 min · LINQ → MGM" in result.output
    assert "no help" in result.output


def test_venues_set_and_reset():
    def venues(*args):
        return runner.invoke(
            app, ["--event", "reinvent2026", "venues", *args], env={"COLUMNS": "220"}
        )

    result = venues("set", "Venetian", "Wynn", "25")
    assert result.exit_code == 0, result.output
    assert "now 25 min (was 35)" in result.output
    table = venues()
    assert "25*" in table.output and "your own correction" in table.output
    assert venues("set", "Venetian", "Luxor", "10").exit_code == 1
    assert "Unknown venue" in venues("set", "Venetian", "Luxor", "10").output
    assert venues("set", "Wynn", "wynn", "10").exit_code == 1  # same venue
    assert "back to the table" in venues("reset", "Wynn", "Venetian").output
    assert "25*" not in venues().output
    venues("set", "Venetian", "MGM", "30")
    assert "All your travel-time corrections are removed" in venues("reset", "--yes").output


def test_venues_shows_shuttles_and_notes():
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "220"})
    assert "Shuttle (2025)" in result.output
    assert "pedestrian bridge" in result.output and "Expo hours" in result.output


def test_warnings_carry_connection_notes():
    from reinvent_planner.cli import _walk_note
    from reinvent_planner.planner import Issue, Item, TravelTimes

    travel = TravelTimes.load("reinvent2026")
    venetian = Item("v", "V", "v", None, None, "Venetian", None)
    forum = Item("f", "F", "f", None, None, "Caesars Forum", None)
    assert "Expo hours" in _walk_note(Issue("tight", venetian, forum), travel)


def test_shuttle_column_only_claims_what_the_source_says():
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "240"})
    rows = [line for line in result.output.splitlines() if line.startswith("│") and "↔" in line]
    lines = {line.split("│")[1].strip(): line for line in rows}
    assert "walk (indoors)" in lines["Wynn ↔ Encore"]
    assert "no" in lines["Venetian ↔ Wynn"].split("│")[4]
    assert "?" in lines["Caesars Palace ↔ MGM Grand"].split("│")[4]  # guide doesn't say
    assert "yes" in lines["Caesars Forum ↔ MGM Grand"].split("│")[4]


def test_unknown_or_same_venue_corrections_are_reported():
    from reinvent_planner.auth import config_dir

    path = config_dir() / "venues" / "reinvent2026.local.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[minutes]\n"venetain|wynn" = 20\n"mgm|mgm" = 3\n"venetian|mgm" = 30\n')
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "220"})
    assert "Ignored correction venetain|wynn" in result.output
    assert "Ignored correction mgm|mgm" in result.output
    assert "30*" in result.output  # the valid one still applies


def test_venues_set_rejects_the_combined_wynn_encore_grouping():
    result = runner.invoke(
        app, ["--event", "reinvent2026", "venues", "set", "Wynn/Encore", "Venetian", "30"]
    )
    assert result.exit_code == 1 and "Unknown venue" in result.output


def test_distances_show_kilometres_and_miles():
    from reinvent_planner.cli import _distance, _walk_note
    from reinvent_planner.planner import Issue, Item, TravelTimes

    assert _distance(1609) == "1.6 km (1.0 mi)"
    assert _distance(894) == "0.9 km (0.6 mi)"
    assert _distance(3409) == "3.4 km (2.1 mi)"
    assert _distance(40) == "<0.1 km (<0.1 mi)"
    assert _distance(60) == "0.1 km (<0.1 mi)"  # never "0.0"
    travel = TravelTimes.load("reinvent2026")
    forum = Item("f", "F", "f", None, None, "Caesars Forum", None)
    mgm = Item("m", "M", "m", None, None, "MGM Grand", None)
    assert "2.0 km (1.2 mi), 27 min on foot" in _walk_note(Issue("tight", forum, mgm), travel)
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "240"})
    assert "0.9 km (0.6 mi)" in result.output and "3.4 km (2.1 mi)" in result.output


def test_venues_output_never_truncates_at_80_columns():
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "80"})
    assert result.exit_code == 0
    assert "0.6 km (0.4 mi)" in result.output  # not split across lines
    assert "…" not in result.output  # nothing cut off


def test_narrow_terminals_get_a_list_that_keeps_every_venue_name():
    for width in ("80", "60", "40"):
        result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": width})
        assert result.exit_code == 0
        assert "Wynn ↔ Encore" in result.output, width  # the pair is never squeezed away
        assert "walk 0.6 km (0.4 mi) · 8 min" in result.output, width
        assert "LINQ → MGM" in result.output, width
        assert "…" not in result.output, width  # nothing truncated (no squeezed matrix)


def test_wide_terminals_get_the_table():
    result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": "200"})
    assert "Shuttle (2025)" in result.output and "0.6 km (0.4 mi) · 8 min" in result.output


def test_venue_names_from_an_override_are_not_markup():
    from reinvent_planner.auth import config_dir

    path = config_dir() / "venues" / "reinvent2026.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[aliases]\na = ["alpha"]\nb = ["beta"]\n[names]\na = "[/]Alpha[bold]"\nb = "Beta"\n'
        '[minutes]\n"a|b" = 20\n[walking]\n"a|b" = { meters = 900, minutes = 12 }\n',
        encoding="utf-8",
    )
    for width in ("200", "60"):
        result = runner.invoke(app, ["--event", "reinvent2026", "venues"], env={"COLUMNS": width})
        assert result.exit_code == 0, result.output
        assert "[/]Alpha[bold]" in result.output


@pytest.mark.parametrize("text", ["10", "12", "19", "130", "9", "00000"])
def test_bare_numbers_that_arent_hhmm_are_refused_not_misread(text):
    from reinvent_planner.cli import InputError, _clock

    with pytest.raises(InputError, match="ambiguous"):
        _clock(text)


def test_four_digit_and_colon_times_still_work():
    from reinvent_planner.cli import _clock

    assert _clock("1030") == (10, 30)
    assert _clock("13:30") == (13, 30)
    assert _clock("1:30pm") == (13, 30)


def test_markup_looking_api_text_never_crashes_output(monkeypatch):
    """The review's P2-M2: an event ID like "evil[/]x" crashed `rip events`."""
    from conftest import make_event
    from typer.testing import CliRunner

    from reinvent_planner import cli

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_events(self, include_past=False):
            return [make_event(eventId="evil[/]x", name="[bold]Conf[/]", endDate="[/]")]

    monkeypatch.setattr(cli, "_client", lambda **kw: FakeClient())
    result = CliRunner().invoke(cli.app, ["events"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "evil[/]x" in result.output


@pytest.mark.parametrize("bad", ["../../x", "a/b", "..", ".hidden", "a b", "x" * 129, ""])
def test_an_unsafe_event_id_is_refused_before_anything_runs(bad):
    """The review's P1-L5: `--event ../../x` wrote travel corrections outside the config dir."""
    from rich.text import Text
    from typer.testing import CliRunner

    from reinvent_planner import cli

    result = CliRunner().invoke(cli.app, ["--event", bad, "venues", "reset", "--yes"])
    # Plain text: on CI, Rich colours the error and escape codes split "--event".
    plain = "".join(Text.from_ansi(result.output).plain.split())
    assert result.exit_code != 0 and "--event" in plain


def test_resetting_all_corrections_asks_first():
    from typer.testing import CliRunner

    from reinvent_planner import cli
    from reinvent_planner.planner import corrections_path, save_corrections

    save_corrections(cli.DEFAULT_EVENT_ID, {"mgm|venetian": 40})
    runner = CliRunner()
    result = runner.invoke(cli.app, ["venues", "reset"], input="n\n")
    assert result.exit_code != 0 and corrections_path(cli.DEFAULT_EVENT_ID).exists()
    result = runner.invoke(cli.app, ["venues", "reset", "--yes"])
    assert result.exit_code == 0 and not corrections_path(cli.DEFAULT_EVENT_ID).exists()


def test_the_app_never_waits_on_a_terminal_prompt():
    from reinvent_planner import cli, services

    outcome = services.run(cli._confirm, "Really?", False)
    assert not outcome.ok and "confirmation" in outcome.error


def test_odd_input_gets_a_message_not_a_traceback(tmp_path):
    """The review's P2-L3."""
    from typer.testing import CliRunner

    from reinvent_planner import cli

    runner = CliRunner()
    huge = runner.invoke(cli.app, ["rank", "set", "AIM301", "99999999999999999999"])
    assert huge.exit_code == 2 and "Traceback" not in huge.output
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    with pytest.raises(cli.InputError, match="Couldn't write"):
        cli._write_output(blocker / "out.md", "x", "utf-8")  # its "folder" is a file


def test_output_files_replace_a_symlink_instead_of_writing_through_it(tmp_path):
    from reinvent_planner import cli

    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = tmp_path / "plan.csv"
    link.symlink_to(victim)
    cli._write_output(link, "new", "utf-8")
    assert victim.read_text() == "keep me"
    assert not link.is_symlink() and link.read_text() == "new"


def test_a_malformed_personal_time_is_still_listed():
    from reinvent_planner import cli
    from reinvent_planner.models import PersonalTime

    pt = PersonalTime.model_validate(
        {
            "personalTimeId": "p1",
            "title": "Lunch",
            "description": "",
            "startDateTime": "garbage",
            "endDateTime": "x",
        }
    )
    assert "Lunch" in cli._describe_block(pt, None)


@pytest.mark.parametrize("text", ["monkey", "tuesdayz", "fr"])
def test_words_that_merely_start_like_a_day_are_refused(text):
    from reinvent_planner import cli
    from reinvent_planner.catalog import Catalog

    with Catalog() as cat, pytest.raises(cli.InputError, match="isn't a day"):
        cli._event_day(cat, text)


def test_checklist_file_puts_travel_warnings_under_their_own_pick(tmp_path):
    from conftest import EVENT_ID, make_event, make_session
    from typer.testing import CliRunner

    from reinvent_planner import cli
    from reinvent_planner.catalog import Catalog

    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("aaa1", title="First\nwith a break", time="10:00", venue="MGM Grand"),
                make_session("bbb1", time="11:00", venue="Wynn"),  # tight after aaa1
            ],
        )
        cat.set_rank(EVENT_ID, "aaa1", 1)
        cat.set_rank(EVENT_ID, "bbb1", 2)
    out = tmp_path / "checklist.md"
    result = CliRunner().invoke(
        cli.app, ["--event", EVENT_ID, "checklist", "--offline", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    lines = out.read_text(encoding="utf-8").splitlines()
    first = next(i for i, line in enumerate(lines) if "#1 AAA1" in line)
    second = next(i for i, line in enumerate(lines) if "#2 BBB1" in line)
    assert "First with a break" in lines[first]
    # Each pick's warning sits right under that pick (it used to land under the pick before).
    assert "⚠" in lines[first + 1] and "⚠" in lines[second + 1]
    assert not any("⚠" in line for line in lines[:first])
