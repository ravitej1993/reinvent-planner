"""The interactive app, driven headless with Textual's test pilot (no terminal, no browser)."""

import inspect
import json

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session
from textual.widgets import Checkbox, DataTable, Input, Select, Static, TabbedContent

from reinvent_planner import cli, services
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import FileTokenStore
from reinvent_planner.catalog import Catalog
from reinvent_planner.models import Schedule
from reinvent_planner.tui.app import ConfirmScreen, PlannerApp, SessionScreen


@pytest.fixture
def seeded():
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("aim301", title="Agents workshop", type="Workshop", time="10:00"),
                make_session("aim302", title="Agents repeat", type="Workshop", time="10:30"),
                make_session("cmp201", title="Graviton talk", type="Chalk talk", time="13:00"),
                make_session("res100", title="Already reserved", time="08:00"),
            ],
        )
        cat.save_schedule(EVENT_ID, Schedule(reserved=["res100"], favorites=["cmp201"]))


@pytest.fixture
def api(monkeypatch):
    """A fake AWS Events API for the favorite flow, and a signed-in user."""
    FileTokenStore().save(fresh_tokens())
    schedule = {"reserved": ["res100"], "favorites": ["cmp201"], "personalTime": []}

    def handler(request):
        if request.url.path.endswith("/schedule"):
            return httpx.Response(200, json={"schedule": schedule})
        if request.url.path.endswith("/favorites"):
            ids = json.loads(request.content)["sessionIds"]
            schedule["favorites"] += ids
            return httpx.Response(200, json={"result": {"successful": ids, "failed": []}})
        if "/favorites/" in request.url.path:
            schedule["favorites"].remove(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(404)

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(handler), sleep=lambda s: None
        ),
    )
    return schedule


def codes(app) -> list[str]:
    table = app.query_one("#results", DataTable)
    return [str(table.get_row_at(i)[1]) for i in range(table.row_count)]


async def test_starts_with_the_whole_catalog(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)):
        assert sorted(codes(app)) == ["AIM301", "AIM302", "CMP201", "RES100"]
        status = str(app.query_one("#status", Static).render())
        assert "4 sessions" in status and "not signed in" in status


async def test_search_and_filters_narrow_the_list(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.query_one("#query", Input).value = "agents"
        await pilot.pause()
        assert sorted(codes(app)) == ["AIM301", "AIM302"]
        app.query_one("#query", Input).value = ""
        app.query_one("#type", Select).value = "Chalk talk"
        await pilot.pause()
        assert codes(app) == ["CMP201"]
        app.query_one("#type", Select).clear()
        app.query_one("#favorites", Checkbox).value = True
        await pilot.pause()
        assert codes(app) == ["CMP201"]


async def test_open_a_session_and_rank_it(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.query_one("#query", Input).value = "AIM301"
        await pilot.pause()
        table = app.query_one("#results", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SessionScreen)
        assert "Agents workshop" in str(app.screen.query_one("#body", Static).render())
        app.screen.query_one("#rank", Input).value = "1"
        await pilot.click("#save-rank")
        await app.workers.wait_for_complete()
        await pilot.pause()
    with Catalog() as cat:
        assert [(p.session_id, p.rank) for p in cat.plan_items(EVENT_ID)] == [("aim301", 1)]


async def test_plan_and_checklist_tabs_show_the_cli_reports(seeded):
    services.use_event(EVENT_ID)
    services.set_rank("aim301", 1)
    services.set_rank("aim302", 2)  # overlaps aim301
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("p")
        await pilot.pause()
        plan = str(app.query_one("#plan-body", Static).render())
        assert "AIM301 and AIM302 overlap" in plan
        await pilot.press("c")
        await pilot.pause()
        checklist = str(app.query_one("#checklist-body", Static).render())
        assert "RESERVE" in checklist and "AIM301" in checklist


async def test_favoriting_asks_first_and_updates_the_real_schedule(seeded, api):
    services.use_event(EVENT_ID)
    services.set_rank("aim301", 1)  # overlaps aim302, so the dialog has a warning to show
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("aim302"))
        await pilot.pause()
        await pilot.click("#favorite")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)  # nothing sent yet
        details = " ".join(str(w.render()) for w in app.screen.query(Static))
        assert "⚠" in details and "AIM302" in details and "[yellow]" not in details
        assert "aim302" not in api["favorites"]
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert "aim302" in api["favorites"]
    with Catalog() as cat:
        assert "aim302" in cat.load_schedule(EVENT_ID)[0].favorites


async def test_declining_changes_nothing(seeded, api):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("aim302"))
        await pilot.pause()
        await pilot.click("#favorite")
        await pilot.pause()
        await pilot.click("#no")
        await pilot.pause()
    assert "aim302" not in api["favorites"]


async def test_favoriting_when_signed_out_explains_instead_of_crashing(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("aim302"))
        await pilot.pause()
        await pilot.click("#favorite")
        await pilot.pause()
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionScreen)  # still open, with an error shown
        shown = " ".join(str(n.message) for n in app._notifications)
        assert "not signed in" in shown.lower()


def test_captured_output_never_leaks_to_the_terminal(seeded, capsys):
    services.use_event(EVENT_ID)
    outcome = services.show("aim301", width=100)
    assert outcome.ok and "Agents workshop" in outcome.output
    assert capsys.readouterr().out == ""
    # The recorder was routed for the call only: afterwards the consoles are the real ones.
    assert cli.console._target.get() is None and cli.err._target.get() is None
    assert not cli.NON_INTERACTIVE.get()


def test_errors_become_outcomes(seeded):
    services.use_event(EVENT_ID)
    outcome = services.show("nope999", width=100)
    assert not outcome.ok and "No session" in outcome.error


def test_no_method_shadows_a_textual_internal():
    """PlannerApp once defined `_filters`, silently replacing an attribute Textual's App uses."""
    from textual.app import App
    from textual.screen import ModalScreen

    for cls, base in (
        (PlannerApp, App),
        (SessionScreen, ModalScreen),
        (ConfirmScreen, ModalScreen),
    ):
        own = {  # methods we wrote (not attributes Textual generates on every subclass)
            n
            for n, v in vars(cls).items()
            if n.startswith("_") and not n.startswith("__") and inspect.isfunction(v)
        }
        base_names = {n for klass in base.__mro__ for n in vars(klass)}
        instance_attrs = set(base.__init__.__code__.co_names)
        # Deliberately overridden: crash reports without locals; quits that wait for a run.
        own -= {"_fatal_error", "_on_exit_app"}
        assert not own & (base_names | instance_attrs), (
            cls.__name__,
            own & (base_names | instance_attrs),
        )


@pytest.fixture
def markup_catalog():
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event, [make_session("mk100", title="[bold red]Not markup[/] [link=x]y[/link] [")]
        )


async def test_catalog_text_is_shown_literally_never_as_markup(markup_catalog):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        table = app.query_one("#results", DataTable)
        title = table.get_row_at(0)[2]
        assert str(title) == "[bold red]Not markup[/] [link=x]y[/link] ["
        app.push_screen(SessionScreen("mk100"))
        await pilot.pause()
        assert "[bold red]Not markup[/]" in str(app.screen.query_one("#body", Static).render())


async def test_error_text_with_brackets_does_not_crash_the_panel(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("[/nope]"))
        await pilot.pause()
        body = str(app.screen.query_one("#body", Static).render())
        assert "[/nope]" in body


async def test_sign_in_can_be_cancelled(seeded, monkeypatch):
    seen = {}

    def fake_sign_in(announce, cancel):
        announce("https://oauth.awsevents.com/authorize?x=[y]")
        seen["cancel"] = cancel
        assert cancel.wait(5)  # blocks like a browser sign-in until Cancel is pressed
        return services.Outcome(error="Sign-in cancelled.")

    monkeypatch.setattr(services, "sign_in", fake_sign_in)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.click("#sign-in")
        await pilot.pause()
        assert app.query_one("#cancel-sign-in").display
        assert not app.query_one("#sign-in").display
        await pilot.click("#cancel-sign-in")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert seen["cancel"].is_set()
        assert app.query_one("#sign-in").display
        assert not app.query_one("#cancel-sign-in").display
        assert app.is_running  # the error was shown, not raised


async def test_quitting_mid_sign_in_releases_the_wait(seeded, monkeypatch):
    seen = {}

    def fake_sign_in(announce, cancel):
        seen["cancel"] = cancel
        cancel.wait(5)
        return services.Outcome(error="Sign-in cancelled.")

    monkeypatch.setattr(services, "sign_in", fake_sign_in)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.click("#sign-in")
        await pilot.pause()
    assert seen["cancel"].is_set()


def test_rip_ui_explains_a_bad_port_or_a_busy_one():
    import socket

    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(cli.app, ["ui", "--no-browser", "--port", "70000"])
    assert result.exit_code != 0 and "Invalid port" in result.output
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        port = busy.getsockname()[1]
        result = runner.invoke(cli.app, ["ui", "--no-browser", "--port", str(port)])
        assert result.exit_code != 0 and "Couldn't start the local server" in result.output
    finally:
        busy.close()


def test_unknown_seat_bands_from_the_api_are_not_markup():
    assert cli._seats("[red]odd[/]") == "\\[red]odd\\[/]"


def test_cached_schedule_time_is_readable():
    shown = cli._fetched("2026-09-30T05:55:05+00:00")
    assert "+00:00" not in shown and ":05" not in shown and "2026-" not in shown
    assert cli._fetched("not a time [x]") == "not a time \\[x]"


async def test_a_crash_report_never_shows_local_variables(seeded, capsys):
    class Boom(PlannerApp):
        def on_mount(self) -> None:
            super().on_mount()
            secret_token = "-".join(["sekrit", "access", "token"])  # noqa: F841
            raise RuntimeError("boom")

    app = Boom(EVENT_ID)
    with pytest.raises(RuntimeError):
        async with app.run_test(size=(120, 40)):
            pass
    captured = capsys.readouterr()
    report = captured.out + captured.err  # Textual prints the crash report as the app exits
    assert "boom" in report and "sekrit-access-token" not in report


async def test_plan_tab_shows_the_timeline_and_strip_map_for_a_day(seeded):
    services.use_event(EVENT_ID)
    services.set_rank("aim301", 1)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("p")
        await pilot.pause()
        assert app.theme == "rip-neon"
        assert isinstance(app.query_one("#plan-day", Select).value, str)
        timeline = str(app.query_one("#timeline", Static).render())
        stripmap = str(app.query_one("#stripmap", Static).render())
        assert "AIM301" in timeline and "AIM301" in stripmap


async def test_rip_theme_picks_another_theme(seeded, monkeypatch):
    monkeypatch.setenv("RIP_THEME", "textual-light")
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(120, 40)):
        assert app.theme == "textual-light"
    monkeypatch.setenv("RIP_THEME", "no-such-theme")
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(120, 40)):
        assert app.theme == "rip-neon"


async def test_escape_leaves_the_search_box_so_shortcuts_work(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("slash")
        await pilot.press("a", "g", "e", "n", "t", "s")
        await pilot.press("escape")
        await pilot.press("p")
        await pilot.pause()
        assert app.query_one("#query", Input).value == "agents"
        assert app.query_one(TabbedContent).active == "plan"
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is app.query_one("#target", Input)


async def test_a_small_window_scrolls_inside_the_tab_and_keeps_the_status_bar(seeded):
    services.use_event(EVENT_ID)
    for n, sid in enumerate(["aim301", "aim302", "cmp201"], 1):
        services.set_rank(sid, n)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(100, 24)) as pilot:
        for tab in ("p", "c", "l"):
            await pilot.press(tab)
            await pilot.pause()
            assert app.screen.scroll_offset.y == 0, tab
            assert app.query_one("#status").region.y < 5, tab  # still on screen, at the top
        await pilot.press("p")
        await pilot.pause()
        scroller = app.query_one("#plan VerticalScroll")
        # The plan scrolls inside its own box, which fits on screen above the footer (in a real
        # terminal an unbounded box scrolled the whole screen and hid the status bar).
        assert scroller.region.bottom <= app.size.height - 1
        assert scroller.virtual_size.height > scroller.region.height


async def test_a_demo_catalog_is_labelled_as_made_up():
    event = make_event(name="Demo week (made-up sessions)")
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(event, [make_session("dmo1")])
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)):
        assert " DEMO " in str(app.query_one("#status", Static).render())


async def test_tab_content_scrolls_inside_its_box_never_the_whole_screen(seeded):
    """In a real terminal an unbounded tab made the screen scroll and hid the status bar."""
    services.use_event(EVENT_ID)
    for n, sid in enumerate(["aim301", "aim302", "cmp201", "res100"], 1):
        services.set_rank(sid, n)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("p")
        await pilot.pause()
        status_y = app.query_one("#status").region.y
        app.screen.scroll_end(animate=False)
        await pilot.pause()
        assert app.screen.max_scroll_y == 0
        assert app.query_one("#status").region.y == status_y
        assert app.query_one("#plan VerticalScroll").max_scroll_y > 0


async def test_catalog_text_in_the_filter_dropdowns_is_never_markup():
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event, [make_session("mk1", venue="Venetian [/] x", type="[@click=app.quit]Talk")]
        )
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        venue = app.query_one("#venue", Select)
        venue.focus()
        await pilot.press("enter")  # opening the dropdown used to raise MarkupError
        await pilot.pause()
        assert app.is_running
        venue.value = "Venetian [/] x"
        await pilot.pause()
        assert venue.value == "Venetian [/] x"


async def test_a_second_sync_while_one_runs_is_refused(seeded, monkeypatch):
    import threading

    release = threading.Event()
    calls = []

    def slow_sync():
        calls.append(1)
        release.wait(5)
        return services.Outcome()

    monkeypatch.setattr(services, "sync", slow_sync)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.sync_pressed()
        await pilot.pause()
        app.sync_pressed()  # pressed again while the first is still running
        app.action_refresh()
        release.set()
        await app.workers.wait_for_complete()
    assert calls == [1]


@pytest.fixture
def reservations_api(monkeypatch):
    """A fake API holding res100 as reserved, which accepts a cancellation."""
    FileTokenStore().save(fresh_tokens())
    schedule = {"reserved": ["res100"], "favorites": ["cmp201"], "personalTime": []}
    deleted = []

    def handler(request):
        if request.url.path.endswith("/schedule"):
            return httpx.Response(200, json={"schedule": schedule})
        if request.method == "DELETE" and "/reservations/" in request.url.path:
            sid = request.url.path.rsplit("/", 1)[-1]
            deleted.append(sid)
            schedule["reserved"].remove(sid)
            return httpx.Response(204)
        return httpx.Response(404)

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(handler), sleep=lambda s: None
        ),
    )
    return deleted


async def test_a_reserved_session_can_be_cancelled_after_confirming(seeded, reservations_api):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("res100"))
        await pilot.pause()
        await pilot.click("#cancel-reservation")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and reservations_api == []
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert reservations_api == ["res100"]
    with Catalog() as cat:
        assert "res100" not in cat.load_schedule(EVENT_ID)[0].reserved


async def test_no_cancel_button_for_a_session_you_dont_hold(seeded):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("aim301"))
        await pilot.pause()
        assert not app.screen.query("#cancel-reservation")


async def test_enter_never_confirms_it_takes_a_click_tab_or_y():
    """Review part A, H1: Yes had the focus, so a double Enter skipped every confirmation."""
    answers = []
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(ConfirmScreen("Really?"), answers.append)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.push_screen(ConfirmScreen("Really?"), answers.append)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert answers == [False, True]


async def test_a_double_enter_on_cancel_reservation_sends_nothing(seeded, reservations_api):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("res100"))
        await pilot.pause()
        app.screen.query_one("#cancel-reservation").focus()
        await pilot.press("enter", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert reservations_api == []


async def test_a_stale_cancel_says_so_instead_of_claiming_success(
    seeded, reservations_api, monkeypatch
):
    """Review part A, M2: the seat was already released on the website, but the cached
    schedule (which decides whether the button shows) still had it."""
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        app.push_screen(SessionScreen("res100"))
        await pilot.pause()
        real_get = EventsClient.get_schedule

        def released_elsewhere(self, event_id):
            schedule = real_get(self, event_id)
            schedule.reserved = [s for s in schedule.reserved if s != "res100"]
            return schedule

        monkeypatch.setattr(EventsClient, "get_schedule", released_elsewhere)
        await pilot.click("#cancel-reservation")
        await pilot.pause()
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        shown = " ".join(str(n.message) for n in app._notifications)
    assert reservations_api == []  # nothing sent
    assert "wasn't reserved" in shown and "Reservation cancelled" not in shown


async def test_the_app_passes_export_deliveries_to_the_more_tab(seeded, tmp_path):

    from textual import events

    from reinvent_planner.tui import more_pane

    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 45)) as pilot:
        name = more_pane.EXPORT_PREFIX + "reinvent2026-plan.csv"
        app.post_message(events.DeliveryComplete("k1", tmp_path / "x.csv", name))
        app.post_message(events.DeliveryFailed("k2", RuntimeError("disk full"), name))
        await pilot.pause()
        shown = " ".join(str(n.message) for n in app._notifications)
    assert "reinvent2026-plan.csv" in shown and "disk full" in shown
