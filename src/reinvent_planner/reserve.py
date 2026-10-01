"""Reserve your ranked picks, falling back to backups, with the schedule as the source of truth.

The loop:

1. Read your schedule (GetSchedule).
2. Walk your picks in rank order and choose one option per pick for this round (see
   `plan_round`): the pick itself, or a backup only if the pick is refused or doesn't fit your
   actual calendar. A pick that only collides with a higher pick being tried this round waits
   for the next round, so it gets its chance if that higher pick fails.
3. Reserve the round in one bulk request (sent in rank order), then read the schedule back.
   The API reports per session; the readback and the API's own "successful" list decide.
4. Refused options are excluded; repeat until nothing new can be tried.

It never blindly re-sends a write whose outcome is unknown. It pauses, reads the schedule, and
sends only what's still missing, at most twice per session. It stops at once if reservations
are closed (409), rate limiting persists, or the API fails in a way we can't recover from, and
in every case returns a report of what did go through. It does nothing in the background: a
run starts when you start it and ends when there's nothing left to try.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .api import (
    ApiError,
    Cancelled,
    EventsClient,
    OperationClosedError,
    ThrottledError,
    WriteOutcomeUnknownError,
)
from .models import BulkFailure, Schedule
from .planner import Item, TravelTimes, blocks

MAX_ROUNDS = 10
MAX_UNKNOWN_ATTEMPTS = 2
# After an unknown outcome (often a 503 at peak load), wait before reading back and re-sending.
IN_DOUBT_PAUSE_SECONDS = 3.0

Stop = Literal["closed", "throttled", "round_limit", "error", "cancelled"]
EventKind = Literal["sending", "booked", "refused", "retrying", "in_doubt", "waiting", "stopped"]


@dataclass(frozen=True)
class Event:
    """One step of a run, for a live display: a session being sent, its outcome, or the end.

    `choice` and `item` are set for per-session events; `backup` means the option isn't the
    pick itself. For "stopped", `reason` is the Stop kind and `detail` any error text."""

    round: int
    kind: EventKind
    choice: Choice | None = None
    item: Item | None = None
    reason: str = ""
    detail: str = ""

    @property
    def backup(self) -> bool:
        return bool(self.choice and self.item and self.item.key != self.choice.primary.key)


@dataclass
class Choice:
    """One ranked pick: its options in order of preference (the pick, then its backups)."""

    rank: int
    options: list[Item]
    # A walk-up pick takes no reservation: you'll attend by walking up. It's never sent, its
    # backups aren't tried, and it keeps its time free, but only from lower-ranked picks.
    walk_up: bool = False

    @property
    def primary(self) -> Item:
        return self.options[0]


@dataclass
class Report:
    reserved: list[Item] = field(default_factory=list)  # newly reserved by this run
    held: list[Item] = field(default_factory=list)  # options you already held before the run
    refused: dict[str, str] = field(default_factory=dict)  # session ID -> reason
    unfilled: list[Choice] = field(default_factory=list)  # picks with no option reserved
    # Sent, but whether they went through is unknown (the run stopped before it could tell).
    in_doubt: list[Item] = field(default_factory=list)
    # Accepted by the API but not (yet) visible in the schedule readback.
    not_visible: list[Item] = field(default_factory=list)
    rounds: int = 0
    stopped: Stop | None = None
    error: str | None = None
    schedule: Schedule | None = None
    # The last read-back failed, so `schedule` predates the last write: show it, don't cache
    # or publish it (it would drop seats that may have just been reserved).
    readback_failed: bool = False


def plan_round(
    choices: Sequence[Choice],
    fixed: Sequence[Item],
    held: set[str],
    excluded: set[str],
    travel: TravelTimes,
) -> list[tuple[Choice, Item]]:
    """The options to try next, at most one per unsatisfied pick, in rank order.

    `fixed` is what's really on the calendar (reservations and personal time); `held` is the
    set of reserved session IDs. A pick is satisfied once any of its options is held.

    An option is skipped for the next one only if it's refused (`excluded`) or blocked by the
    real calendar. If it's blocked only by a higher pick chosen *this round*, the whole pick
    waits: that higher pick may be refused, and then this pick's first choice is free.

    Walk-up picks join the calendar at their rank, so they block lower picks but never a
    higher one.
    """
    committed = list(fixed)
    tentative: list[Item] = []
    picks: list[tuple[Choice, Item]] = []
    for choice in sorted(choices, key=lambda c: c.rank):
        if choice.walk_up:
            committed.append(choice.primary)
            continue
        if any(option.key in held for option in choice.options):
            continue
        for option in choice.options:
            if option.key in excluded:
                continue
            if any(blocks(option, k, travel) for k in committed if k.key != option.key):
                continue
            if any(blocks(option, t, travel) for t in tentative):
                break  # wait for the next round
            tentative.append(option)
            picks.append((choice, option))
            break
    return picks


def run(
    client: EventsClient,
    event_id: str,
    choices: Sequence[Choice],
    *,
    fixed_items: Callable[[Schedule], list[Item]],
    travel: TravelTimes,
    on_round: Callable[[int, list[tuple[Choice, Item]]], None] = lambda n, picks: None,
    on_event: Callable[[Event], None] = lambda event: None,
    cancelled: Callable[[], bool] = lambda: False,
    on_phase: Callable[[str], None] = lambda phase: None,
    max_rounds: int = MAX_ROUNDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """Reserve `choices` for the signed-in attendee. See the module docstring for the loop.

    `fixed_items` turns a schedule into calendar items (reserved sessions, personal time, and
    anything else that must not be double-booked) so clashes are checked against what you
    actually hold after each round. Errors before the first write propagate; after that, the
    report always comes back, with `stopped` and `error` saying why it ended early.

    `on_event` reports each session as it's sent and decided. `cancelled` is checked before
    each round, never while a request is in flight, so a cancel can't leave a write in doubt.
    `on_phase` says what's happening ("read", "write" or "readback"): a caller whose `sleep`
    raises Cancelled must not do so during a readback, because a cancel stops further writes,
    never the reconciliation of the ones already sent.
    """
    report = Report()
    by_key = {o.key: o for c in choices if not c.walk_up for o in c.options}
    doubtful: set[str] = set()
    on_phase("read")
    try:
        schedule = client.get_schedule(event_id)
    except Cancelled:
        report.stopped = "cancelled"
        on_event(Event(0, "stopped", reason="cancelled"))
        return report
    report.schedule = schedule
    report.held = [by_key[k] for k in schedule.reserved if k in by_key]
    excluded: set[str] = set()
    unknown = Counter[str]()
    # Sessions the API said it reserved that the readback doesn't show yet (read-after-write
    # lag). Treated as held so they're never re-sent or double-booked around.
    accepted_not_visible: set[str] = set()
    newly: set[str] = set()
    # Sent, never confirmed either way: they may be reserved. Planned around as if held, so
    # their backups aren't reserved on top of them, and reported as in doubt at the end.
    presumed: set[str] = set()

    def held_now() -> set[str]:
        return set(schedule.reserved) | accepted_not_visible

    def calendar() -> list[Item]:
        maybe = (accepted_not_visible | presumed) - set(schedule.reserved)
        return fixed_items(schedule) + [by_key[k] for k in maybe if k in by_key]

    for round_number in range(1, max_rounds + 1):
        held_before = held_now()
        picks = plan_round(choices, calendar(), held_before | presumed, excluded, travel)
        if not picks:
            break
        if cancelled():
            report.stopped = "cancelled"
            break
        report.rounds = round_number
        on_round(round_number, picks)
        choice_of = {option.key: choice for choice, option in picks}
        for choice, option in picks:
            on_event(Event(round_number, "sending", choice, option))
        ids = [option.key for _, option in picks]

        failures: dict[str, BulkFailure] = {}
        accepted: set[str] = set()
        in_doubt: set[str] = set()
        stop: Stop | None = None
        outcome_unknown = False
        on_phase("write")
        try:
            result = client.reserve_sessions(event_id, ids)
            failures = {f.session_id: f for f in result.failed}
            accepted = set(result.successful)
            in_doubt = set(ids)
        except Cancelled as exc:
            # A write only waits after a 429, which performed nothing, so nothing is in doubt:
            # the batches before the wait are fully described by exc.partial.
            stop = "cancelled"
            if exc.partial is not None:
                failures = {f.session_id: f for f in exc.partial.failed}
                accepted = set(exc.partial.successful)
        except ApiError as exc:
            if exc.partial is not None:
                failures = {f.session_id: f for f in exc.partial.failed}
                accepted = set(exc.partial.successful)
            if isinstance(exc, OperationClosedError):
                stop = "closed"
            elif isinstance(exc, ThrottledError):
                stop = "throttled"
            elif isinstance(exc, WriteOutcomeUnknownError):
                outcome_unknown = True
                in_doubt = set(exc.sent or ids)
            else:
                stop = "error"
                report.error = str(exc)
        # "Already on your schedule" means we hold it, whatever the readback says.
        for session_id, failure in list(failures.items()):
            if failure.code == "alreadyScheduled":
                accepted.add(session_id)
                del failures[session_id]

        if outcome_unknown:
            sleep(IN_DOUBT_PAUSE_SECONDS + random.uniform(0, 1))  # noqa: S311 - jitter
        on_phase("readback")
        try:
            schedule = client.get_schedule(event_id)
            if accepted - set(schedule.reserved) and stop is None:
                sleep(IN_DOUBT_PAUSE_SECONDS)  # give a lagging readback one more chance
                schedule = client.get_schedule(event_id)
        except Cancelled:
            stop = "cancelled"
            readback = "Cancelled before your schedule could be read back"
            report.error = f"{report.error}; {readback}" if report.error else readback
            report.readback_failed = True
        except ApiError as exc:
            stop = "error"
            readback = f"Couldn't read your schedule back: {exc}"
            report.error = f"{report.error}; {readback}" if report.error else readback
            report.readback_failed = True
        else:
            report.readback_failed = False
        report.schedule = schedule
        now_held = set(schedule.reserved)
        # A session we gave up on may show up in a later readback: then it's simply booked.
        for session_id in sorted(presumed & now_held):
            presumed.discard(session_id)
            doubtful.discard(session_id)
            if session_id not in held_before and session_id not in newly and session_id in by_key:
                newly.add(session_id)
                report.reserved.append(by_key[session_id])
                choice = next(
                    (c for c in choices if session_id in {o.key for o in c.options}), None
                )
                on_event(Event(round_number, "booked", choice, by_key[session_id]))
        if stop:
            # The run ends here, so anything sent whose fate is still unknown stays unknown.
            doubtful |= in_doubt - now_held - accepted - failures.keys()

        for session_id in ids:
            if session_id in now_held or session_id in accepted:
                if session_id not in now_held:
                    accepted_not_visible.add(session_id)
                if session_id not in held_before and session_id not in newly:
                    newly.add(session_id)
                    report.reserved.append(by_key[session_id])
                on_event(Event(round_number, "booked", choice_of[session_id], by_key[session_id]))
                continue
            choice, item = choice_of[session_id], by_key[session_id]
            if session_id in failures:
                excluded.add(session_id)
                report.refused[session_id] = failures[session_id].reason
                on_event(Event(round_number, "refused", choice, item, failures[session_id].reason))
            elif session_id in in_doubt and stop is None:
                unknown[session_id] += 1
                if unknown[session_id] >= MAX_UNKNOWN_ATTEMPTS:
                    excluded.add(session_id)
                    presumed.add(session_id)
                    doubtful.add(session_id)
                    on_event(Event(round_number, "in_doubt", choice, item, "no confirmation"))
                else:
                    on_event(Event(round_number, "retrying", choice, item, "no confirmation yet"))
        if stop:
            report.stopped = stop
            break
    else:
        if plan_round(choices, calendar(), held_now() | presumed, excluded, travel):
            report.stopped = "round_limit"

    if report.stopped:
        on_event(Event(report.rounds, "stopped", reason=report.stopped, detail=report.error or ""))
    held = held_now()
    report.in_doubt = [by_key[k] for k in doubtful if k in by_key and k not in held]
    doubt_keys = {i.key for i in report.in_doubt}
    report.not_visible = [
        by_key[k] for k in accepted_not_visible - set(schedule.reserved) if k in by_key
    ]
    report.unfilled = [
        c
        for c in choices
        if not c.walk_up and not any(o.key in held or o.key in doubt_keys for o in c.options)
    ]
    return report
