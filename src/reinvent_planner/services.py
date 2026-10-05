"""Quiet access to the CLI's building blocks, for the interactive app.

The app reuses the CLI's code paths, so the two can't drift apart: same catalog queries, same
schedule handling, same warnings, same calendar-feed updates. The CLI prints through two Rich
consoles (`cli.console` and `cli.err`); here they're swapped for a recording console while a
call runs, and the recorded output (with colours, as ANSI text) is handed to the app to show in
a panel instead of being written to the terminal underneath it.
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from . import api, calendar_feed, cli, launch
from .api import ApiError, Cancelled, WriteOutcomeUnknownError
from .auth import Auth, AuthError, default_token_store
from .catalog import Catalog, CatalogError, SearchFilters
from .models import Schedule, Session
from .planner import Item, TravelTimes, group_by_day
from .reserve import Choice, Event, Report
from .tls import TLSConfigError


@dataclass
class Outcome:
    """What a call produced: its return value, its output (ANSI), and an error if it failed."""

    value: Any = None
    output: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@contextlib.contextmanager
def captured(width: int = 100) -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    recorder = Console(
        file=buffer, force_terminal=True, color_system="truecolor", width=width, soft_wrap=False
    )
    # Routed for this thread/task only (cli._RoutedConsole), so concurrent captures from the
    # app's UI thread and its workers never wait on each other or mix their output.
    # Never start a worker, thread pool task or asyncio task from inside a capture: those copy
    # the current context and would keep writing to this recorder after it's gone.
    tokens = cli.console.route(recorder), cli.err.route(recorder)
    quiet = cli.NON_INTERACTIVE.set(True)
    try:
        yield buffer
    finally:
        cli.NON_INTERACTIVE.reset(quiet)
        cli.console.unroute(tokens[0])
        cli.err.unroute(tokens[1])


def run(fn: Callable[..., Any], *args: Any, width: int = 100, **kwargs: Any) -> Outcome:
    """Call a CLI function or helper with its output captured and its errors turned into an
    Outcome, never an exception, so the app can always show what happened."""
    with captured(width) as buffer:
        try:
            value, error = fn(*args, **kwargs), None
        except typer.Exit as exc:
            value, error = None, None if exc.exit_code in (0, None) else "failed"
        except typer.Abort:
            value, error = None, "cancelled"
        except (
            ApiError,
            AuthError,
            CatalogError,
            cli.InputError,
            calendar_feed.FeedError,
            launch.LaunchError,
            TLSConfigError,
        ) as exc:
            value, error = None, str(exc)
        except httpx.HTTPError as exc:
            value, error = None, f"Network error: {exc}"
        except Exception as exc:
            value, error = None, f"Unexpected error: {exc}"
    output = buffer.getvalue()
    if error == "failed":  # the command printed its own error message; use that text
        plain = Text.from_ansi(output).plain.strip()
        error = plain.splitlines()[-1] if plain else "failed"
    return Outcome(value=value, output=output, error=error)


def use_event(event_id: str) -> None:
    cli.state.event_id = event_id


def current_event() -> str:
    return cli.state.event_id


# -- reads (safe to call any time) ---------------------------------------------------


def signed_in_as() -> str | None:
    try:
        tokens = Auth(default_token_store()).current()
    except (AuthError, TLSConfigError):
        return None
    return (tokens.email or "your Builder ID") if tokens else None


def catalog_status() -> tuple[int, str | None]:
    with Catalog() as cat:
        return cat.session_count(cli.state.event_id), cat.last_sync(cli.state.event_id)


def is_demo_catalog() -> bool:
    """The made-up week from scripts/demo/seed_demo.py, so screenshots say so."""
    with Catalog() as cat:
        event = cat.get_event(cli.state.event_id)
    return bool(event and "made-up" in (event.name or "").lower())


def filter_choices() -> dict[str, list[str]]:
    """Distinct values for the search dropdowns, from the synced catalog."""
    with Catalog() as cat:
        rows = cat.db.execute(
            "SELECT DISTINCT type, level FROM sessions WHERE event_id = ?", (cli.state.event_id,)
        ).fetchall()
        venues = cat.venues(cli.state.event_id)
    types = sorted({r["type"] for r in rows if r["type"]})
    levels = sorted({(r["level"] or "").split(" ")[0] for r in rows if r["level"]})
    return {"types": types, "levels": levels, "venues": venues}


@dataclass
class Row:
    session_id: str
    marks: str
    code: str
    title: str
    type: str
    level: str
    when: str
    venue: str
    seats: str


def search(filters: SearchFilters) -> list[Row]:
    """The same query as `rip search`, as rows for a table (marks use Rich markup)."""
    with Catalog() as cat:
        sessions = cat.search(cli.state.event_id, filters)
        zone = cat.zone(cli.state.event_id)
        cached = cat.load_schedule(cli.state.event_id)
        schedule = cached[0] if cached else Schedule()
        ranks = {p.session_id: p.rank for p in cat.plan_items(cli.state.event_id) if p.rank}
    return [
        Row(
            s.session_id,
            cli._marks(s.session_id, schedule, ranks),
            s.code,
            s.title,
            s.type or "",
            (s.level or "").split(" ")[0],
            cli._when(s, zone, short=True),
            s.place or "",
            cli._seats(s.seat_availability),
        )
        for s in sessions
    ]


def my_schedule_ids() -> tuple[set[str], set[str], dict[str, int]]:
    """(reserved, favorites, ranks) from the cached schedule and the local ranking."""
    with Catalog() as cat:
        cached = cat.load_schedule(cli.state.event_id)
        schedule = cached[0] if cached else Schedule()
        ranks = {p.session_id: p.rank for p in cat.plan_items(cli.state.event_id) if p.rank}
    return set(schedule.reserved), set(schedule.favorites), ranks


def session(session_id: str) -> Session:
    with Catalog() as cat:
        return cat.resolve(cli.state.event_id, session_id)


def show(session_id: str, width: int) -> Outcome:
    return run(cli.show, session_id, width=width)


def plan(width: int, offline: bool) -> Outcome:
    return run(cli.plan, offline=offline, width=width)


def checklist(width: int, offline: bool) -> Outcome:
    return run(cli.checklist, out=None, offline=offline, width=width)


def conflict_warnings(session_id: str, kind: str) -> Outcome:
    """The warnings `fav add` / `rank set` would show, before anything changes."""

    def compute() -> list[str]:
        with Catalog() as cat:
            cached = cat.load_schedule(cli.state.event_id)
            schedule = cached[0] if cached else Schedule()
            s = cat.resolve(cli.state.event_id, session_id)
            return cli._conflict_warnings(cat, schedule, [s], kind)

    return run(compute)


# -- actions (network or schedule changes; run these off the UI thread) -------------------


def sign_in(announce: Callable[[str], None], cancel: threading.Event | None = None) -> Outcome:
    def go() -> str:
        tokens = Auth(default_token_store()).login(
            announce=announce, launch_browser=True, cancel=cancel
        )
        return tokens.email or "your Builder ID"

    return run(go)


def sync() -> Outcome:
    def go() -> None:
        with cli._client() as client, Catalog() as cat:
            cli._sync(client, cat, no_abstracts=False, force=False)

    return run(go)


def set_favorite(session_id: str, favorite: bool) -> Outcome:
    """Add or remove a favorite on the real schedule, then read it back (like `rip fav`).
    The caller confirms with the user first."""

    def go() -> bool:
        with cli._client(need_auth=True) as client, Catalog() as cat:
            s = cat.resolve(cli.state.event_id, session_id)
            # Like `rip fav`: if the outcome is unknown, the readback below decides.
            with contextlib.suppress(WriteOutcomeUnknownError):
                if favorite:
                    client.associate_favorites(cli.state.event_id, [s.session_id])
                else:
                    client.disassociate_favorite(cli.state.event_id, s.session_id)
            after = client.get_schedule(cli.state.event_id)
            cat.save_schedule(cli.state.event_id, after)
            cli._refresh_feed(client, cat, after)
            return (s.session_id in after.favorites) == favorite

    outcome = run(go)
    if outcome.ok and outcome.value is False:
        outcome.error = "The change doesn't show on your schedule yet; try Refresh."
    return outcome


def cancel_reservation(session_id: str) -> Outcome:
    """Cancel one reservation (like `rip cancel`, which reads your schedule back). The caller
    confirms with the user first. Value: True if a cancellation was sent, False if the seat
    wasn't reserved any more (the app's cached schedule was stale)."""

    def go() -> bool:
        with cli._client(need_auth=True) as client, Catalog() as cat:
            schedule = client.get_schedule(cli.state.event_id)
            if session_id not in schedule.reserved:
                cli._save_schedule(client, cat, schedule)  # so the app stops offering it
                return False
        cli.cancel(session_id, yes=True)  # the ID, never an ambiguous code
        return True

    return run(go)


def set_rank(session_id: str, rank: int | None) -> Outcome:
    """Rank a session (local only), or remove it from the ranking when rank is None."""
    if rank is None:
        return run(cli.rank_rm, session_id)
    return run(cli.rank_set, session_id, rank, backup=None)


# -- launch control (see launch.py; the app's Launch tab) ------------------------------------


def event_zone() -> ZoneInfo:
    with Catalog() as cat:
        return cat.zone(cli.state.event_id) or ZoneInfo("America/Los_Angeles")


def launch_choices() -> Outcome:
    """Your ranked picks with their backups, as `rip reserve` would try them."""

    def go() -> list[Choice]:
        with Catalog() as cat:
            cli._require_catalog(cat)
            return cli._reservation_choices(cat, None)

    return run(go)


@dataclass
class Preflight:
    skew: float | None  # server clock minus ours, or None if unknown or implausible
    fingerprint: str  # the picks the plan showed (see launch.fingerprint)


def launch_preflight(width: int) -> Outcome:
    """Arming the API launch: one schedule read (it checks you're signed in and registered),
    the dry-run plan, any note about an earlier cut-off run, and the clock-skew reading from
    that read's Date header. Value: a Preflight."""

    def go() -> Preflight:
        api.clear_last_server_date()
        with Catalog() as cat:
            cli._require_catalog(cat)
            shown = launch.fingerprint(cli._reservation_choices(cat, None))
        cli.reserve(codes=None, dry_run=True, yes=False)
        stamp = api.last_server_date()
        return Preflight(launch.clock_skew(*stamp) if stamp else None, shown)

    return run(go, width=width)


def launch_reserve(
    on_event: Callable[[Event], None],
    cancelled: Callable[[], bool],
    width: int,
    *,
    fingerprint: str,
) -> Outcome:
    """The GO press: one guarded run of your ranked picks (the same as `rip reserve --yes`,
    after the preflight showed the plan and GO was pressed). Refused if the picks changed
    since the preflight. Waits (rate limits, read retries) are announced as "waiting" events
    and end the run if `cancelled()` becomes true. Value: the Report."""

    phase = ["read"]

    def wait(seconds: float) -> None:
        on_event(Event(0, "waiting", detail=f"{seconds:.0f}", reason=phase[0]))
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            if cancelled() and phase[0] != "readback":  # never cut a readback short
                raise Cancelled
            time.sleep(min(0.25, left))

    def go() -> Report:
        # The lock is taken at the press, not just around the writes, so `rip ui` sees the run
        # (and waits for it) from the first moment, not only once it starts sending.
        with (
            launch.reservation_lock(cli.state.event_id, wait=1.0),
            cli._client(need_auth=True, sleep=wait) as client,
            Catalog() as cat,
        ):
            cli._require_catalog(cat)
            journal = launch.Journal(cli.state.event_id)
            if journal.path.exists():  # a run in another tab died since this one armed
                cli._report_journal(cat, client.get_schedule(cli.state.event_id), journal)
            travel = TravelTimes.load(cli.state.event_id)
            choices = cli._reservation_choices(cat, None)
            if launch.fingerprint(choices) != fingerprint:
                raise launch.LaunchError(
                    "Your picks changed since you armed. Press Arm again to review them."
                )
            report = cli._guarded_reservations(
                client,
                choices,
                fixed_items=cli._fixed_items(cat),
                travel=travel,
                on_event=on_event,
                cancelled=cancelled,
                on_phase=lambda name: phase.__setitem__(0, name),
                locked=True,
            )
            # The report is shown first: the steps after it are best effort, and a failure there
            # (e.g. the database busy in another tab) mustn't hide what was reserved.
            cli._print_report(report, choices, cli._zone(cat))
            if report.schedule is not None and not report.readback_failed:
                try:
                    cat.save_schedule(cli.state.event_id, report.schedule)
                    cli._refresh_feed(client, cat, report.schedule)
                except Exception as exc:  # the run itself is over and reported
                    cli.err.print(
                        "[yellow]Couldn't save your updated schedule locally "
                        f"({escape(str(exc))}). Refresh to fetch it again.[/]"
                    )
            return report

    return run(go, width=width)


def copilot_check() -> Outcome:
    """One read of your real schedule for the website copilot. Value: reserved session IDs."""

    def go() -> set[str]:
        with cli._client(need_auth=True) as client, Catalog() as cat:
            schedule = client.get_schedule(cli.state.event_id)
            cat.save_schedule(cli.state.event_id, schedule)
            return set(schedule.reserved)

    return run(go)


# -- day views (the timeline and the Strip map) ---------------------------------------------


@dataclass
class DayPlan:
    days: dict[date, list[Item]]
    zone: ZoneInfo | None
    travel: TravelTimes
    ranks: dict[str, int]


def day_plan() -> DayPlan:
    """Your plan (cached schedule plus ranking), split by day, for the visual views."""
    with Catalog() as cat:
        cached = cat.load_schedule(cli.state.event_id)
        schedule = cached[0] if cached else Schedule()
        items = cli._plan_items(cat, schedule, quiet=True)
        zone = cli._zone(cat)
        ranks = {p.session_id: p.rank for p in cat.plan_items(cli.state.event_id) if p.rank}
    days = {d: v for d, v in group_by_day(items, zone).items() if d is not None}
    return DayPlan(days, zone, TravelTimes.load(cli.state.event_id), ranks)
