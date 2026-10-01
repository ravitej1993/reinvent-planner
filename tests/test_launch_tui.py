"""The Launch tab, driven headless against a fake API and a fake clock."""

from datetime import UTC, datetime

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session
from test_reserve import FakeEventsServer
from textual.widgets import Button, Input, RichLog, Select

from reinvent_planner import cli, launch
from reinvent_planner import reserve as reserve_mod
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import FileTokenStore
from reinvent_planner.catalog import Catalog
from reinvent_planner.launch import Phase
from reinvent_planner.tui.app import ConfirmScreen, PlannerApp
from reinvent_planner.tui.launch_pane import LaunchPane

TARGET_TEXT = "2026-10-08 09:00"
TARGET = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def server(monkeypatch):
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("p1", title="[bold]Top pick[/]", time="10:00"),
                make_session("b1", time="10:00"),
                make_session("p3", time="13:00"),
            ],
        )
        cat.set_rank(EVENT_ID, "p1", 1)
        cat.add_backup(EVENT_ID, "b1", "p1")
        cat.set_rank(EVENT_ID, "p3", 2)
    FileTokenStore().save(fresh_tokens())
    fake = FakeEventsServer()
    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(fake), sleep=kw.get("sleep") or (lambda s: None)
        ),
    )
    monkeypatch.setattr(reserve_mod, "IN_DOUBT_PAUSE_SECONDS", 0)
    return fake


async def open_launch(app, pilot, *, mode="api", seconds_before=100):
    await pilot.press("l")
    await pilot.pause()
    pane = app.query_one(LaunchPane)
    clock = Clock(TARGET.timestamp() - seconds_before)
    pane.control.clock = clock
    # The steady clock follows the fake one, so the pane's real ticks never see a "clock step"
    # (a real steady clock racing a frozen fake one made this flaky on a busy machine).
    pane.control.steady = lambda: clock.t
    pane.query_one("#target", Input).value = TARGET_TEXT
    pane.query_one("#mode", Select).value = mode
    await pilot.pause()
    pane.query_one("#arm", Button).press()
    await app.workers.wait_for_complete()
    await pilot.pause()
    if mode == "api":
        why = str(pane.query_one("#preflight").render())
        notes = [str(n.message) for n in app._notifications]
        assert pane.control.preflighted, f"arming failed: {why!r} {notes!r}"
    return pane, clock


async def test_arming_reads_once_and_waiting_never_touches_the_network(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, clock = await open_launch(app, pilot)
        assert pane.control.phase() is Phase.ARMED
        reads = server.schedule_reads
        assert reads == 1  # the preflight's one schedule read
        for _ in range(20):  # the clock runs far past the target
            clock.t += 3600
            pane._tick()
            await pilot.pause()
        assert pane.control.phase() is Phase.GO
        assert not pane.query_one("#go", Button).disabled
        assert server.posts == [] and server.schedule_reads == reads


async def test_go_runs_exactly_once_however_often_it_is_pressed(server):
    server.full = {"p1"}
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        pane._tick()
        go = pane.query_one("#go", Button)
        pane.go_pressed()
        pane.go_pressed()  # a double press
        pane.go_pressed()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert server.posts == [["p1", "p3"], ["b1"]]  # one run: two rounds, backup included
        log = "\n".join(line.text for line in pane.query_one("#log", RichLog).lines)
        assert "✓ #2 P3 booked" in log and "backup for P1" in log and "✓ #1 B1 booked" in log
        assert pane.control.phase() is Phase.COOLDOWN
        pane._tick()
        assert go.disabled
        pane.go_pressed()  # pressed again during the cooldown
        await app.workers.wait_for_complete()
        assert len(server.posts) == 2
        assert launch.Journal(EVENT_ID).load() is None  # a finished run leaves no journal


async def test_not_open_goes_dark_and_never_retries_by_itself(server):
    server.closed = True
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, clock = await open_launch(app, pilot, seconds_before=0)
        pane.go_pressed()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert pane.control.phase() is Phase.NOT_OPEN
        posts = len(server.posts)
        for _ in range(10):
            clock.t += 600
            pane._tick()
            await pilot.pause()
        assert len(server.posts) == posts  # no automatic retry, however long it waits
        assert pane.control.phase() is Phase.GO


async def test_enter_elsewhere_never_fires_go(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        pane._tick()
        await pilot.press("s")  # back to search
        app.query_one("#query").focus()
        await pilot.press("enter")
        await pilot.press("l")
        await pilot.pause()
        pane.query_one("#target").focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert server.posts == []


async def test_quitting_mid_run_asks_first(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()  # as if GO were pressed and the run is still going
        await app.action_quit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#no")
        await pilot.pause()
        assert app.is_running
        pane.control.finish_run(None)


async def test_cancel_stops_before_the_next_round(server, monkeypatch):
    server.full = {"p1"}
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        pane._cancel.set()  # pressed before round 1 starts
        monkeypatch.setattr(pane._cancel, "clear", lambda: None)
        pane.go_pressed()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert server.posts == []
        log = "\n".join(line.text for line in pane.query_one("#log", RichLog).lines)
        assert "Cancelled before the next round" in log


async def test_copilot_walks_picks_writes_nothing_and_rate_limits_checks(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, mode="web")
        assert server.schedule_reads == 0  # arming the copilot makes no call
        pane.query_one("#mark-full", Button).focus()
        await pilot.press("f")  # p1 is full on the website: its backup is next
        now = str(pane.query_one("#now").render())
        assert "B1" in now and "backup for P1" in now
        await pilot.press("b")
        now = str(pane.query_one("#now").render())
        assert "P3" in now
        pane.check_pressed()
        pane.check_pressed()  # too soon
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert server.schedule_reads == 1
        assert server.posts == []


async def test_presentation_mode_hides_the_email(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("l")
        await pilot.pause()
        app.query_one("#presentation").value = True
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "signed in" in status and "@" not in status


def test_log_lines_keep_session_text_literal():
    from reinvent_planner.planner import item_from_session
    from reinvent_planner.reserve import Choice, Event
    from reinvent_planner.tui.launch_pane import event_line

    item = item_from_session(make_session("x1", title="[red]boom[/]"), None, "ranked")
    line = event_line(Event(1, "sending", Choice(1, [item]), item))
    assert "[red]boom[/]" in line.plain


# -- round-1 review fixes ----------------------------------------------------------------


async def test_copilot_arming_then_switching_to_api_never_lights_go(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, mode="web", seconds_before=0)
        pane.query_one("#mode", Select).value = "api"
        await pilot.pause()
        pane._tick()
        assert pane.query_one("#go", Button).disabled
        assert not pane.control.armed  # switching modes disarms
        pane.go_pressed()
        await app.workers.wait_for_complete()
        assert server.posts == [] and server.schedule_reads == 0


async def test_picks_changed_after_arming_are_refused_at_go(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        with Catalog() as cat:
            cat.set_rank(EVENT_ID, "b1", 3)  # edited after the plan was shown
        pane.go_pressed()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert server.posts == []
        log = "\n".join(line.text for line in pane.query_one("#log", RichLog).lines)
        assert "picks changed since you armed" in log


async def test_a_rate_limit_wait_is_shown_and_cancel_ends_it(server, monkeypatch):
    server.post_faults = ["429"] * 10
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        # A real wait (the fake server's Retry-After is 0), cancelled as soon as it starts.
        monkeypatch.setattr("reinvent_planner.api.MAX_RETRY_AFTER_SECONDS", 5)
        from reinvent_planner import api

        real_error = api._error_for

        def slow_429(response, **kw):
            error = real_error(response, **kw)
            if isinstance(error, api.ThrottledError):
                error.retry_after = 5
            return error

        monkeypatch.setattr(api, "_error_for", slow_429)

        def on_waiting(event):
            if event.kind == "waiting":
                pane._cancel.set()

        real_log = pane._log_event

        def log_and_cancel(event):
            real_log(event)
            on_waiting(event)

        monkeypatch.setattr(pane, "_log_event", log_and_cancel)
        monkeypatch.setattr(pane._cancel, "clear", lambda: None)
        pane.go_pressed()
        await app.workers.wait_for_complete()
        await pilot.pause()
        log = "\n".join(line.text for line in pane.query_one("#log", RichLog).lines)
        assert "retrying in 5 s" in log
        assert "Cancelled" in log
        assert len(server.posts) == 1  # the 429'd attempt only; nothing re-sent after cancel
        assert pane.control.phase() is Phase.COOLDOWN


async def test_the_cooldown_is_shared_with_rip_reserve_and_survives_a_restart(server):
    from typer.testing import CliRunner

    server.full = {"p1", "b1"}  # leaves pick #1 unfilled, so `rip reserve` has work to do
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        pane.go_pressed()
        await app.workers.wait_for_complete()
    posts = len(server.posts)
    assert posts  # the app's run did send
    result = CliRunner().invoke(cli.app, ["--event", EVENT_ID, "reserve", "--yes"])
    assert result.exit_code == 1 and "just finished; wait" in result.output
    assert len(server.posts) == posts
    reopened = PlannerApp(EVENT_ID)
    async with reopened.run_test(size=(160, 50)) as pilot:
        await pilot.press("l")
        await pilot.pause()
        assert reopened.query_one(LaunchPane).control.phase() is Phase.COOLDOWN


async def test_a_big_clock_jump_brings_back_the_arm_button(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, clock = await open_launch(app, pilot, seconds_before=600)
        pane.control.steady_counts_sleep = True
        steady = [clock.t]
        pane.control.steady = lambda: steady[0]
        pane.control._last = None
        pane._tick()
        clock.t += 20 * 60  # the wall clock jumps; the steady clock doesn't
        pane._tick()
        await pilot.pause()
        assert pane.query_one("#arm", Button).display
        assert "Press Arm again" in str(pane.query_one("#skew").render())
        assert pane.query_one("#go", Button).disabled


def test_a_readback_wait_says_it_cannot_be_cut_short():
    from reinvent_planner.reserve import Event
    from reinvent_planner.tui.launch_pane import event_line

    assert (
        "can't be cut short" in event_line(Event(0, "waiting", detail="4", reason="readback")).plain
    )
    assert "can't" not in event_line(Event(0, "waiting", detail="4", reason="write")).plain


async def test_a_big_clock_jump_during_a_run_still_brings_back_arm_afterwards(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, clock = await open_launch(app, pilot, seconds_before=0)
        pane.control.steady_counts_sleep = True
        steady = [clock.t]
        pane.control.steady = lambda: steady[0]
        pane.control._last = None
        assert pane.control.start_run()  # a run is going
        pane._tick()
        clock.t += 20 * 60
        pane._tick()  # the jump is seen mid-run: no disarm yet
        assert pane.control.armed
        pane.control.finish_run(None)
        pane._tick()
        await pilot.pause()
        assert not pane.control.armed and pane.query_one("#arm", Button).display


async def test_a_quit_from_the_browser_or_a_signal_waits_for_the_run(server):
    """Round 2 of the review: the tab closing or Ctrl+C in `rip ui` cut a GO run off."""
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()  # a run is going
        await app._on_exit_app()  # what a closed tab or SIGTERM delivers
        await pilot.pause()
        assert app.is_running and app.exit_when_idle
        pane.control.finish_run(None)
        app.run_finished()
        await pilot.pause()
    assert not app.is_running  # it left once the run ended


async def test_quit_anyway_during_a_run_still_quits(server):
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()
        await app.action_quit()
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
    assert not app.is_running


async def test_sync_waits_for_a_launch_run_and_go_waits_for_a_sync(server, monkeypatch):
    """Round 2 of the review: Sync or `r` during GO competed for the catalog and the feed."""
    from reinvent_planner import services

    synced = []
    monkeypatch.setattr(services, "sync", lambda: synced.append(1) or services.Outcome())
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()  # a run is going
        app.sync_pressed()
        app.action_refresh()
        await app.workers.wait_for_complete()
        assert synced == []
        pane.control.finish_run(None, sent=False)
        monkeypatch.setattr(app, "syncing", lambda: True)
        pane.go_pressed()
        assert not pane.running()  # GO waited for the sync


async def test_no_sync_in_the_last_minute_before_go_or_while_it_is_lit(server, monkeypatch):
    """Verification round 2: a sync started at 09:59:58 made GO refuse at 10:00:00."""
    from reinvent_planner import services

    synced = []
    monkeypatch.setattr(services, "sync", lambda: synced.append(1) or services.Outcome())
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, clock = await open_launch(app, pilot, seconds_before=30)
        app.sync_pressed()  # 30 s before GO
        clock.t += 60  # GO is lit
        app.action_refresh()
        await app.workers.wait_for_complete()
        assert synced == []
        pane.disarm_pressed()
        app.sync_pressed()  # disarmed: allowed again
        await app.workers.wait_for_complete()
        assert synced == [1]


async def test_a_launch_worker_that_crashes_still_ends_the_run(server, monkeypatch):
    """Verification round 2: a crashed worker left running() true, so every quit waited."""
    import contextlib

    from textual.worker import WorkerFailed

    from reinvent_planner import services

    def boom(*args, **kwargs):
        raise RuntimeError("worker died")

    monkeypatch.setattr(services, "launch_reserve", boom)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        pane.go_pressed()
        with contextlib.suppress(WorkerFailed):  # the crash itself is reported, not raised
            await app.workers.wait_for_complete()
        await pilot.pause()
        assert not pane.running()


async def test_the_lock_is_held_from_the_go_press_not_just_during_writes(server, monkeypatch):
    """Verification round 2: `rip ui` couldn't see a run before it started sending."""
    from reinvent_planner import cli, launch

    seen = []
    real = cli._reservation_choices

    def spy(cat, codes):
        seen.append(launch.run_in_progress(EVENT_ID))  # before any write
        return real(cat, codes)

    monkeypatch.setattr(cli, "_reservation_choices", spy)
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        seen.clear()  # the preflight also called it
        pane.go_pressed()
        await app.workers.wait_for_complete()
    assert seen and all(seen)
    assert server.posts  # and the run itself still went ahead under that lock


async def test_no_sync_during_the_not_open_cooldown_either(server, monkeypatch):
    from reinvent_planner import services

    synced = []
    monkeypatch.setattr(services, "sync", lambda: synced.append(1) or services.Outcome())
    server.closed = True
    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        pane.go_pressed()
        await app.workers.wait_for_complete()
        assert pane.control.phase() is Phase.NOT_OPEN
        app.sync_pressed()
        await app.workers.wait_for_complete()
        assert synced == []


async def test_the_app_leaves_when_the_rip_ui_server_is_gone(server):
    import asyncio

    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()
        await asyncio.to_thread(app._watch_server, -1)  # "our parent is no longer the server"
        await pilot.pause()
        assert app.is_running and app.exit_when_idle  # a run in progress finishes first
        pane.control.finish_run(None)
        app.run_finished()
        await pilot.pause()
    assert not app.is_running


async def test_a_leftover_journal_at_go_is_reported_not_merged(server):
    from reinvent_planner import launch

    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        launch.Journal(EVENT_ID).record(1, ["zz9"])  # another tab's run died after we armed
        pane.go_pressed()
        await app.workers.wait_for_complete()
        await pilot.pause()
        shown = str(pane.query_one("#preflight").render())
        assert "cut off" in shown
    assert launch.Journal(EVENT_ID).load() is None


async def test_personal_time_changes_wait_for_a_reservation_run(server):
    from reinvent_planner.tui.personal_pane import PersonalPane

    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()
        await pilot.press("t")
        await pilot.pause()
        assert app.query_one(PersonalPane)._personal_busy()  # refused while the run goes
        pane.control.finish_run(None)
        pane.disarm_pressed()  # (armed and cooling down still counts as "GO soon")
        assert not app.query_one(PersonalPane)._personal_busy()


async def test_favorites_and_cancelling_wait_for_a_reservation_run(server):
    """Review part A, M1: a cancel or favorite change mid-run changed what the run planned."""
    from reinvent_planner.tui.app import SessionScreen

    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        pane, _ = await open_launch(app, pilot, seconds_before=0)
        assert pane.control.start_run()
        app.push_screen(SessionScreen("p3"))
        await pilot.pause()
        await pilot.click("#favorite")
        await pilot.pause()
        assert isinstance(app.screen, SessionScreen)  # no confirmation offered mid-run
        pane.control.finish_run(None)


async def test_personal_time_waits_while_go_is_about_to_light(server):
    """New-slice round 2: a personal change seconds before T-0 made GO refuse."""
    from reinvent_planner.tui.personal_pane import PersonalPane

    app = PlannerApp(EVENT_ID)
    async with app.run_test(size=(160, 50)) as pilot:
        await open_launch(app, pilot, seconds_before=30)
        assert app.query_one(PersonalPane)._personal_busy()
