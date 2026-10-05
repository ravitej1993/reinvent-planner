"""Launch control's rules, with a fake clock: nothing fires by itself, one press is one run."""

import multiprocessing
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import make_session

from reinvent_planner import launch
from reinvent_planner.launch import Copilot, Journal, Launch, LaunchError, Mark, Phase
from reinvent_planner.planner import item_from_session
from reinvent_planner.reserve import Choice

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
TARGET = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)  # 09:00 PDT


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def armed(seconds_before: float = 100, skew: float = 0.0, *, preflighted: bool = True):
    clock = Clock(TARGET.timestamp() - seconds_before)
    steady = Clock(1000.0)
    control = Launch(clock=clock, steady=steady)
    control.arm(TARGET, skew, preflighted=preflighted)
    control.steady_clock = steady  # for tests that move it
    return control, clock


# -- target time -----------------------------------------------------------------------


def test_target_is_stored_as_an_aware_utc_instant():
    assert launch.parse_target("2026-10-08 09:00", LA) == TARGET
    assert launch.parse_target("  2026-10-08   09:00 ", LA) == TARGET


def test_target_is_shown_in_event_time_and_your_own():
    assert launch.describe_target(TARGET, LA, LA) == "Thu Oct 8, 09:00 PDT"
    assert launch.describe_target(TARGET, LA, NY) == (
        "Thu Oct 8, 09:00 PDT (your time: Thu Oct 8, 12:00 EDT)"
    )


def test_bad_or_ambiguous_times_are_refused_not_guessed():
    with pytest.raises(LaunchError, match="YYYY-MM-DD HH:MM"):
        launch.parse_target("Oct 8 9am", LA)
    with pytest.raises(LaunchError, match="doesn't exist"):
        launch.parse_target("2026-03-08 02:30", LA)  # skipped by the spring change
    with pytest.raises(LaunchError, match="happens twice"):
        launch.parse_target("2026-11-01 01:30", LA)  # repeated by the fall change


def test_a_target_across_a_dst_change_in_another_zone_is_still_right():
    target = launch.parse_target("2026-11-02 09:00", LA)  # PST: after LA's change on Nov 1
    assert target == datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
    shown = launch.describe_target(target, LA, ZoneInfo("Europe/Berlin"))  # CET since Oct 25
    assert shown == "Mon Nov 2, 09:00 PST (your time: Mon Nov 2, 18:00 CET)"


def test_countdown_text():
    assert launch.countdown(3 * 86400 + 3661) == "T-3d 01:01:01"
    assert launch.countdown(59.2) == "T-00:01:00"  # rounds up: never 0 early
    assert launch.countdown(-5) == "T-00:00:00"


def test_clock_skew_from_a_date_header():
    received = datetime(2026, 10, 8, 15, 59, 50, tzinfo=UTC).timestamp()
    assert launch.clock_skew("Thu, 08 Oct 2026 16:00:00 GMT", received) == 10
    assert launch.clock_skew(None, received) is None
    assert launch.clock_skew("garbage", received) is None


# -- the GO control ----------------------------------------------------------------------


def test_nothing_fires_by_itself_however_long_you_wait():
    control, clock = armed(100)
    assert control.phase() is Phase.ARMED
    clock.t += 10 * 86400  # far past the target
    assert control.phase() is Phase.GO  # lit, but waiting for a person
    assert not control._running


def test_go_only_works_at_go_and_only_once():
    control, clock = armed(100)
    assert control.start_run() is False  # counting down
    clock.t += 100
    assert control.start_run() is True
    assert control.start_run() is False  # a second press while running
    assert control.phase() is Phase.RUNNING


def test_after_a_run_go_stays_dark_for_the_cooldown():
    control, clock = armed(0)
    assert control.start_run()
    control.finish_run(None)
    assert control.phase() is Phase.COOLDOWN and not control.start_run()
    clock.t += launch.RUN_COOLDOWN_SECONDS - 1
    assert not control.start_run()
    clock.t += 1
    assert control.phase() is Phase.GO and control.start_run()


def test_not_open_means_a_cooldown_never_an_automatic_retry():
    control, clock = armed(0)
    control.start_run()
    control.finish_run("closed")
    assert control.phase() is Phase.NOT_OPEN
    clock.t += launch.NOT_OPEN_COOLDOWN_SECONDS
    assert control.phase() is Phase.GO  # a person has to press again
    assert not control._running


def test_skew_moves_the_countdown_to_the_servers_clock():
    control, _ = armed(100, skew=30)  # the server is 30 s ahead of us
    assert control.seconds_left() == pytest.approx(70)


def test_cannot_rearm_or_disarm_mid_run():
    control, _ = armed(0)
    control.start_run()
    with pytest.raises(LaunchError):
        control.arm(TARGET)
    with pytest.raises(LaunchError):
        control.disarm()


# -- website copilot ---------------------------------------------------------------------


def item(sid, time="10:00"):
    return item_from_session(make_session(sid, time=time), LA, "ranked")


def copilot():
    return Copilot(
        [
            Choice(1, [item("p1"), item("b1")]),
            Choice(2, [item("p2", "13:00")]),
            Choice(3, [item("w1", "15:00")], walk_up=True),
        ]
    )


def test_copilot_walks_picks_and_falls_back_to_backups():
    pilot = copilot()
    assert pilot.current()[1].key == "p1"
    pilot.mark(Mark.FULL)
    assert pilot.current()[1].key == "b1"  # its backup next
    pilot.mark(Mark.BOOKED)
    assert pilot.current()[1].key == "p2"
    pilot.mark(Mark.SKIPPED)
    assert pilot.current() is None  # the walk-up pick is never offered
    assert [pilot.status(c) for c in pilot.choices[:2]] == ["booked", "skipped"]


def test_a_schedule_check_overrides_marks_and_flags_disagreements():
    pilot = copilot()
    pilot.mark(Mark.BOOKED)  # p1, by hand
    disagreements = pilot.apply_schedule(["p2"], clock=0)
    assert disagreements == ["P1"]  # you said booked; the schedule doesn't show it
    assert pilot.status(pilot.choices[1]) == "confirmed"


def test_schedule_checks_are_rate_limited():
    pilot = copilot()
    assert pilot.may_read(1000)
    pilot.apply_schedule([], clock=1000)
    assert not pilot.may_read(1000 + launch.READ_INTERVAL_SECONDS - 1)
    assert pilot.may_read(1000 + launch.READ_INTERVAL_SECONDS)


# -- lock and journal --------------------------------------------------------------------


def _try_lock(data_dir, queue):
    import os

    os.environ["RIP_DATA_DIR"] = data_dir
    try:
        with launch.reservation_lock("ev1"):
            queue.put("got it")
    except LaunchError:
        queue.put("refused")


def test_only_one_run_at_a_time_across_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with launch.reservation_lock("ev1"):
        other = ctx.Process(target=_try_lock, args=(str(tmp_path), queue))
        other.start()
        other.join(30)
        assert queue.get(timeout=5) == "refused"
    other = ctx.Process(target=_try_lock, args=(str(tmp_path), queue))
    other.start()
    other.join(30)
    assert queue.get(timeout=5) == "got it"  # released when the run ended


def test_the_lock_is_not_reentrant_within_a_process(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    with launch.reservation_lock("ev1"), pytest.raises(LaunchError):  # noqa: SIM117
        with launch.reservation_lock("ev1"):
            pass


def test_journal_records_before_writes_and_survives_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    journal = Journal("ev1")
    assert journal.load() is None
    journal.record(1, ["p1", "p3"])
    journal.record(2, ["b1", "p1"])
    assert Journal("ev1").load()["sent"] == ["p1", "p3", "b1"]  # a fresh process sees it
    journal.clear()
    assert journal.load() is None
    journal.clear()  # clearing twice is fine


def test_a_corrupt_journal_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    journal = Journal("ev1")
    journal.path.write_text("{not json")
    assert journal.load() is None
    journal.path.write_text('{"sent": [1, "p1", null]}')
    assert journal.load()["sent"] == ["p1"]


# -- round-1 review fixes ----------------------------------------------------------------


def test_go_needs_an_api_preflight_not_just_a_copilot_arming():
    control, _ = armed(0, preflighted=False)
    assert control.phase() is Phase.GO  # the copilot shows "open"…
    assert control.start_run() is False  # …but GO never runs without a preflight


def test_a_wall_clock_step_after_arming_moves_the_skew_back():
    control, clock = armed(100, skew=-30)  # our clock was 30 s fast at arming
    steady = control.steady_clock
    control.observe()
    clock.t += 10
    steady.t += 10
    assert control.observe() == 0.0  # normal time passing
    clock.t -= 30  # NTP fixes our clock
    steady.t += 1
    clock.t += 1
    assert control.observe() == pytest.approx(-30)
    assert control.skew == pytest.approx(0)  # no longer double-corrected
    assert control.seconds_left() == pytest.approx(119)  # 130 true seconds at arming, 11 passed


def test_where_the_steady_clock_may_skip_sleep_any_forward_jump_asks_for_a_rearm():
    """Sleep or a clock step can't be told apart there, so the countdown isn't silently moved
    (the review's M4: a 200 s sleep was taken as a step and lit GO 200 s late)."""
    for asleep in (200, 8 * 3600):
        control, clock = armed(100_000)
        control.steady_counts_sleep = False  # e.g. time.monotonic on Windows
        steady = control.steady_clock
        control.observe()
        left = control.seconds_left()
        clock.t += asleep  # asleep, on a clock that didn't count it
        steady.t += 1
        assert control.observe() == 0.0 and control.skew == 0.0
        assert control.seconds_left() == pytest.approx(left - asleep)
        assert control.big_jump and not control.preflighted


def test_where_the_steady_clock_may_skip_sleep_a_clock_set_back_is_still_corrected():
    control, clock = armed(1000)
    control.steady_counts_sleep = False
    steady = control.steady_clock
    control.observe()
    clock.t -= 30  # can't be sleep: the wall clock went back
    steady.t += 1
    clock.t += 1
    assert control.observe() == pytest.approx(-30)
    assert control.preflighted


def test_where_the_steady_clock_counts_sleep_a_huge_jump_is_real_and_needs_a_rearm():
    control, clock = armed(100_000)
    control.steady_counts_sleep = True  # Linux CLOCK_BOOTTIME, macOS CLOCK_MONOTONIC
    steady = control.steady_clock
    control.observe()
    clock.t += 20 * 60  # someone fixed a clock that was 20 minutes slow
    steady.t += 1
    assert control.observe() == pytest.approx(20 * 60 - 1)
    assert control.big_jump and not control.preflighted
    clock.t = TARGET.timestamp() + 1
    assert control.start_run() is False  # GO needs a fresh Arm


def test_a_run_that_sent_nothing_costs_no_cooldown():
    control, _ = armed(0)
    assert control.start_run()
    control.finish_run("cancelled", sent=False)
    assert control.phase() is Phase.GO and control.start_run()


def test_fingerprint_notices_a_session_that_moved():
    moved = item("p1", time="11:00")
    assert launch.fingerprint([Choice(1, [item("p1")])]) != launch.fingerprint([Choice(1, [moved])])


def test_an_unwritable_lock_file_is_explained_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    import builtins

    real_open = builtins.open

    def refuse(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse)
    with pytest.raises(LaunchError, match="RIP_DATA_DIR"):  # noqa: SIM117
        with launch.reservation_lock("ev1"):
            pass


def test_an_implausible_skew_is_ignored():
    assert launch.clock_skew("Thu, 08 Oct 2026 16:00:00 GMT", TARGET.timestamp() - 3600) is None
    control, _ = armed(100, skew=3600)
    assert control.skew == 0.0


def test_the_cooldown_survives_restarting_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    now = TARGET.timestamp()
    launch.save_cooldown("ev1", launch.Cooldown(*launch.next_cooldown(now, None, 0)))
    reopened = Launch(clock=Clock(now + 10), event_id="ev1")
    reopened.arm(TARGET, preflighted=True)
    assert reopened.phase() is Phase.COOLDOWN and not reopened.start_run()


def test_repeated_not_open_answers_wait_longer():
    waits, strikes = [], 0
    for _ in range(4):
        until, not_open, strikes = launch.next_cooldown(0, "closed", strikes)
        waits.append(until)
        assert not_open
    assert waits == [60, 120, 300, 300]
    assert launch.next_cooldown(0, None, strikes) == (launch.RUN_COOLDOWN_SECONDS, False, 0)


def test_fingerprint_changes_when_the_picks_do():
    one = [Choice(1, [item("p1"), item("b1")])]
    assert launch.fingerprint(one) == launch.fingerprint([Choice(1, [item("p1"), item("b1")])])
    assert launch.fingerprint(one) != launch.fingerprint([Choice(1, [item("b1"), item("p1")])])
    assert launch.fingerprint(one) != launch.fingerprint([Choice(2, [item("p1"), item("b1")])])


def test_a_lock_failure_that_isnt_contention_says_what_went_wrong(tmp_path, monkeypatch):
    import errno

    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))

    def unsupported(handle):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(launch, "_lock", unsupported)
    with pytest.raises(LaunchError, match="RIP_DATA_DIR") as info:  # noqa: SIM117
        with launch.reservation_lock("ev1"):
            pass
    assert "Another reservation run" not in str(info.value)


def test_journal_writes_are_flushed_to_disk(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    Journal("ev1").record(1, ["p1"])
    assert synced  # before the rename into place


def test_not_open_strikes_from_earlier_days_dont_lengthen_the_real_days_wait():
    """The review's H1: practice presses on Oct 6 and 7 must not make Oct 8's wait 300 s."""
    day = 86400
    cooldown = launch.Cooldown(*launch.next_cooldown(0, "closed", 0))
    cooldown = launch.Cooldown(
        *launch.next_cooldown(day, "closed", launch.live_strikes(cooldown, day))
    )
    oct8 = 2 * day
    until, _, strikes = launch.next_cooldown(oct8, "closed", launch.live_strikes(cooldown, oct8))
    assert until - oct8 == 60 and strikes == 1
    # …while presses a moment apart still build a streak.
    quick = launch.Cooldown(*launch.next_cooldown(0, "closed", 0))
    until, _, _ = launch.next_cooldown(70, "closed", launch.live_strikes(quick, 70))
    assert until - 70 == 120


def test_a_clock_set_back_cant_stretch_a_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    control = Launch(clock=Clock(0))
    control.arm(TARGET, preflighted=True)
    control._running = True
    control.finish_run(None)
    control.clock.t -= 3600  # NTP fixed a fast clock after the run
    assert control.cooldown_left() <= launch.MAX_COOLDOWN_SECONDS
    launch.save_cooldown("ev1", launch.Cooldown(float("inf"), True, 1))
    assert launch.load_cooldown("ev1") == launch.Cooldown()


def test_copilot_checks_arent_locked_by_a_clock_set_back():
    pilot = copilot()
    pilot.apply_schedule([], clock=1000)
    assert pilot.may_read(1000 - 3600)


def test_countdown_never_shows_zero_early():
    assert launch.countdown(0.0005) == "T-00:00:01"


def test_a_clock_set_back_after_a_run_ends_the_cooldown_on_time(tmp_path, monkeypatch):
    """Round 2 of the review: the number shown was capped but GO stayed dark ~56 minutes."""
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    control = Launch(clock=Clock(10_000))
    control.arm(TARGET, preflighted=True)
    control._running = True
    control.finish_run(None)  # a 60 s cooldown
    control.clock.t -= 3600  # the clock is set back an hour
    assert control.cooldown_left() <= launch.MAX_COOLDOWN_SECONDS
    control.clock.t += launch.MAX_COOLDOWN_SECONDS + 1
    assert control.cooldown_left() == 0


def test_a_saved_cooldown_is_capped_and_strictly_parsed(tmp_path, monkeypatch):
    import json
    import time

    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    launch.save_cooldown("ev1", launch.Cooldown(1e300, True, 1))
    assert launch.load_cooldown("ev1").until <= time.time() + launch.MAX_COOLDOWN_SECONDS + 1
    path = next(tmp_path.rglob("cooldown-ev1.json"))
    path.write_text(json.dumps({"until": 0, "not_open": "false", "strikes": 1}))
    assert launch.load_cooldown("ev1").not_open is False
    path.write_text(json.dumps({"until": 0, "not_open": True, "strikes": 2.9}))
    assert launch.load_cooldown("ev1") == launch.Cooldown()


def test_go_waits_briefly_for_a_momentary_lock_holder(tmp_path, monkeypatch):
    """Round 2 of the review: GO failed if another tab's Arm held the lock for a moment."""
    import threading
    import time

    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))

    def hold(held: threading.Event, release: threading.Event, seconds: float) -> None:
        with launch.reservation_lock("ev1"):
            held.set()
            release.wait(seconds)

    # A brief holder lets go within the wait: the lock is taken.
    held, release = threading.Event(), threading.Event()
    brief = threading.Thread(target=hold, args=(held, release, 0.2))
    brief.start()
    assert held.wait(5)
    start = time.monotonic()
    with launch.reservation_lock("ev1", wait=2.0):
        assert time.monotonic() - start < 2.0
    brief.join()

    # A holder that keeps it: with no wait, refused at once.
    held, release = threading.Event(), threading.Event()
    keeper = threading.Thread(target=hold, args=(held, release, 10))
    keeper.start()
    assert held.wait(5)
    try:
        with pytest.raises(LaunchError, match="in progress"), launch.reservation_lock("ev1"):
            pass
    finally:
        release.set()
        keeper.join()
