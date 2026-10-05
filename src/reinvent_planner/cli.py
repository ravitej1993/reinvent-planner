"""Command-line interface: `reinvent-planner` (or `rip`)."""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import errno
import functools
import math
import os
import pathlib
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Annotated, TypeVar
from zoneinfo import ZoneInfo

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.errors import MarkupError
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import (
    DEFAULT_EVENT_ID,
    EVENT_ID_PATTERN,
    __version__,
    calendar_feed,
    launch,
    venues_measure,
)
from .api import (
    ApiError,
    EventsClient,
    SignInRequiredError,
    WriteOutcomeUnknownError,
)
from .auth import (
    BUILDER_ID_PROFILE_URL,
    Auth,
    AuthError,
    NotSignedInError,
    config_dir,
    default_token_store,
)
from .catalog import Catalog, CatalogError, SearchFilters
from .export_csv import rows as export_rows
from .export_csv import to_csv as export_to_csv
from .export_csv import write_csv
from .export_ics import build_calendar, slugify, write_calendar, write_file_atomic
from .models import (
    SEAT_AVAILABILITY_LABELS,
    PersonalTime,
    PersonalTimeInput,
    Schedule,
    Session,
    clean_text,
)
from .planner import (
    Item,
    TravelTableError,
    TravelTimes,
    blocks,
    build_checklist,
    corrections_path,
    find_issues,
    group_by_day,
    item_from_personal_time,
    item_from_session,
    merge_items,
    save_corrections,
)
from .planner import format_distance as _distance
from .reserve import Choice, plan_round
from .reserve import run as run_reservations
from .tls import TLSConfigError

# Tracebacks must never print local variables: they can hold tokens.
app = typer.Typer(
    help="Plan your AWS re:Invent schedule with the official AWS Events API. "
    "Not affiliated with or endorsed by AWS.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
fav_app = typer.Typer(
    help="Add, remove and list favorites on your event schedule.", no_args_is_help=True
)
rank_app = typer.Typer(
    help="Rank the sessions you want most (stored locally; never sent anywhere).",
    no_args_is_help=True,
)
app.add_typer(fav_app, name="fav")
app.add_typer(rank_app, name="rank")
time_app = typer.Typer(
    help="Add, edit and remove personal time (lunches, meetings) on your event schedule.",
    no_args_is_help=True,
)
app.add_typer(time_app, name="time")
calendar_app = typer.Typer(
    help="A calendar feed that updates itself, published as a secret GitHub Gist.",
    no_args_is_help=True,
)
app.add_typer(calendar_app, name="calendar")


class _RoutedConsole:
    """A console whose output can be redirected for the current thread or task only.

    The interactive app runs CLI code on several threads at once and captures each call's
    output (services.captured); routing through a ContextVar means those captures never
    block or leak into each other, and code that isn't capturing prints normally.
    """

    def __init__(self, default: Console):
        self._default = default
        self._target: contextvars.ContextVar[Console | None] = contextvars.ContextVar(
            f"console-{id(self)}", default=None
        )

    def route(self, target: Console) -> contextvars.Token:
        return self._target.set(target)

    def unroute(self, token: contextvars.Token) -> None:
        self._target.reset(token)

    def __getattr__(self, name: str):
        return getattr(self._target.get() or self._default, name)


console = _RoutedConsole(Console())
err = _RoutedConsole(Console(stderr=True))

F = TypeVar("F", bound=Callable[..., object])


class InputError(Exception):
    """Something the user typed can't be used (a bad day, time or length)."""


def handle_errors(fn: F) -> F:
    """Turn expected failures into a short message and exit code 1, without a traceback."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            ApiError,
            AuthError,
            CatalogError,
            TravelTableError,
            InputError,
            calendar_feed.FeedError,
            launch.LaunchError,
            venues_measure.MeasureError,
            TLSConfigError,
        ) as exc:
            err.print(f"[red]Error:[/] {escape(clean_text(str(exc)))}")
            raise typer.Exit(1) from None
        except httpx.HTTPError as exc:
            err.print(f"[red]Network error:[/] {escape(clean_text(str(exc)))}")
            raise typer.Exit(1) from None
        except MarkupError as exc:  # a bug (unescaped text in markup); say so, don't trace
            err.print(Text(f"Error: something in the output couldn't be displayed ({exc}).", "red"))
            raise typer.Exit(1) from None

    return wrapper  # type: ignore[return-value]


class State:
    event_id: str = DEFAULT_EVENT_ID


state = State()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"reinvent-planner {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    event: Annotated[
        str,
        typer.Option("--event", "-e", envvar="RIP_EVENT", help="Event ID (see `rip events`)."),
    ] = DEFAULT_EVENT_ID,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version", callback=version_callback, is_eager=True, help="Show the version."
        ),
    ] = None,
) -> None:
    if not EVENT_ID_PATTERN.fullmatch(event):
        raise typer.BadParameter(
            "use 1-128 letters, digits, '.', '_' or '-' (not starting with '.').",
            param_hint="'--event' / RIP_EVENT",
        )
    state.event_id = event


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _auth() -> Auth:
    return Auth(default_token_store())


def _client(*, need_auth: bool = False, sleep=None) -> EventsClient:
    try:
        auth: Auth | None = _auth()
    except AuthError:
        if need_auth:
            raise
        auth = None  # no keychain available; public reads still work
    if need_auth and (auth is None or not auth.is_signed_in()):
        raise SignInRequiredError("You are not signed in. Run `rip login`.")
    return EventsClient(auth, sleep=sleep) if sleep else EventsClient(auth)


def _catalog() -> Catalog:
    return Catalog()


def _zone(cat: Catalog) -> ZoneInfo | None:
    return cat.zone(state.event_id)


def _save_schedule(
    client: EventsClient | None, cat: Catalog, schedule: Schedule, *, refresh_feed: bool = True
) -> None:
    """Cache a freshly downloaded schedule and bring the published calendar feed up to date
    if what it shows has changed (e.g. you reserved on the website)."""
    cat.save_schedule(state.event_id, schedule)
    if refresh_feed:
        _refresh_feed(client, cat, schedule, from_read=True)


def _fetched(stamp: str) -> str:
    """When the cached schedule was fetched, in this computer's local time."""
    try:
        moment = datetime.fromisoformat(stamp).astimezone()
    except ValueError:
        return escape(stamp)
    return f"{moment:%a %b} {moment.day}, {moment:%H:%M}"


def _schedule(
    client: EventsClient, cat: Catalog, *, refresh: bool = True, refresh_feed: bool = True
) -> Schedule:
    """Fresh schedule when signed in, else the last cached copy, else empty."""
    if refresh and client.signed_in():
        schedule = client.get_schedule(state.event_id)
        _save_schedule(client, cat, schedule, refresh_feed=refresh_feed)
        return schedule
    cached = cat.load_schedule(state.event_id)
    if cached:
        schedule, fetched_at = cached
        err.print(
            f"[dim]Using your schedule as of {_fetched(fetched_at)}. Sign in to refresh it.[/]"
        )
        return schedule
    return Schedule()


def _schedule_or_cache(
    client: EventsClient, cat: Catalog, *, offline: bool = False, refresh_feed: bool = True
) -> Schedule:
    """Like `_schedule`, but falls back to the cached copy if the API can't be reached."""
    try:
        return _schedule(client, cat, refresh=not offline, refresh_feed=refresh_feed)
    except (ApiError, AuthError, httpx.HTTPError) as exc:
        if isinstance(exc, SignInRequiredError | AuthError):
            err.print(
                f"[yellow]Your sign-in needs renewing ({escape(str(exc))}); using the cached "
                "schedule. Run `rip login`.[/]"
            )
        else:
            err.print(
                f"[dim]Couldn't refresh your schedule ({escape(str(exc))}); using the cache.[/]"
            )
        return _schedule(client, cat, refresh=False)


def _when(item: Item | Session, zone: ZoneInfo | None, *, short: bool = False) -> str:
    if isinstance(item, Session):
        interval = item.interval(zone)
        start, end, all_day = (
            (*interval, bool(item.is_all_day_session)) if interval else (None, None, False)
        )
    else:
        start, end, all_day = item.start, item.end, item.all_day
    if start is None or end is None:
        return "TBA"
    if zone is not None:
        start, end = start.astimezone(zone), end.astimezone(zone)
    day = f"{start:%a} {start.day}" if short else f"{start:%a %b} {start.day}"
    if all_day:
        return f"{day} all day"
    return f"{day} {start:%H:%M}–{end:%H:%M}"


def _seats(value: str | None) -> str:
    if not value:
        return ""
    label = escape(SEAT_AVAILABILITY_LABELS.get(value, value))  # unknown bands are raw API text
    color = {
        "available": "green",
        "limited": "yellow",
        "veryLimited": "red",
        "unavailable": "red",
    }.get(value)
    return f"[{color}]{label}[/]" if color else label


def _marks(session_id: str, schedule: Schedule, ranks: dict[str, int]) -> str:
    marks = []
    if session_id in schedule.reserved:
        marks.append("[green]R[/]")
    if session_id in schedule.favorites:
        marks.append("[yellow]F[/]")
    if session_id in ranks:
        marks.append(f"#{ranks[session_id]}")
    return " ".join(marks)


def _require_catalog(cat: Catalog) -> None:
    if cat.session_count(state.event_id) == 0:
        raise CatalogError(f"The {state.event_id} catalog isn't downloaded yet. Run `rip sync`.")


def _plan_items(cat: Catalog, schedule: Schedule, *, quiet: bool = False) -> list[Item]:
    zone = _zone(cat)
    ranked = cat.plan_items(state.event_id)
    wanted = [*schedule.reserved, *schedule.favorites, *(p.session_id for p in ranked if p.rank)]
    sessions = cat.get_sessions(state.event_id, wanted)
    items: list[Item] = []
    for sid in schedule.reserved:
        if sid in sessions:
            items.append(item_from_session(sessions[sid], zone, "reserved"))
    for sid in schedule.favorites:
        if sid in sessions:
            items.append(item_from_session(sessions[sid], zone, "favorite"))
    for p in ranked:
        if p.rank and p.session_id in sessions:
            items.append(item_from_session(sessions[p.session_id], zone, "ranked"))
    items.extend(item_from_personal_time(pt) for pt in schedule.personal_time)
    missing = [sid for sid in wanted if sid not in sessions]
    if missing and not quiet:
        err.print(
            f"[yellow]{len(missing)} session(s) on your schedule aren't in the local catalog. "
            "Run `rip sync`.[/]"
        )
    return merge_items(items)


# ---------------------------------------------------------------------------
# sign-in
# ---------------------------------------------------------------------------


@app.command()
@handle_errors
def login(
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the sign-in URL instead of opening it.")
    ] = False,
) -> None:
    """Sign in with your AWS Builder ID. Tokens are stored in your OS keychain."""

    def announce(url: str) -> None:
        console.print("Sign in with your AWS Builder ID in the browser.")
        console.print(f"[dim]If the browser doesn't open, visit:[/]\n{url}", soft_wrap=True)

    tokens = _auth().login(announce=announce, launch_browser=not no_browser)
    console.print(f"[green]Signed in[/] as {escape(tokens.email or 'your Builder ID')}.")


@app.command()
@handle_errors
def logout(
    builder_id: Annotated[
        bool,
        typer.Option("--builder-id", help="Also end your AWS Builder ID session in the browser."),
    ] = False,
) -> None:
    """Revoke your sign-in and delete the stored tokens."""
    auth = _auth()
    revoked = auth.logout()
    if revoked:
        console.print(
            "[green]Signed out.[/] Your refresh token is revoked and local tokens are deleted."
        )
    else:
        console.print(
            "[yellow]Local tokens deleted, but revocation couldn't be confirmed.[/] "
            f"You can end all sessions at {BUILDER_ID_PROFILE_URL}."
        )
    if builder_id:
        auth.end_builder_id_session(
            announce=lambda url: console.print(f"Opening {url}", soft_wrap=True)
        )
        console.print("[green]Your Builder ID browser session has ended.[/]")
    else:
        console.print(
            "[dim]Your AWS Builder ID browser session is still active. "
            "Run `rip logout --builder-id` "
            f"or visit {BUILDER_ID_PROFILE_URL} to end it.[/]"
        )


@app.command()
@handle_errors
def whoami() -> None:
    """Show who is signed in."""
    tokens = _auth().current()
    if tokens is None:
        console.print("Not signed in. Run `rip login`.")
        raise typer.Exit(1)
    expires = datetime.fromtimestamp(tokens.expires_at).astimezone()
    console.print(f"Signed in as {escape(tokens.email or 'unknown')}.")
    console.print(
        f"[dim]Access token refreshes automatically; current one expires {expires:%H:%M %Z}.[/]"
    )


# ---------------------------------------------------------------------------
# interactive app
# ---------------------------------------------------------------------------


@app.command()
@handle_errors
def tui() -> None:
    """Open the interactive app in this terminal: everything the commands do, plus a day
    timeline, a Strip map and the reservation-day Launch tab."""
    from .tui.app import run as run_app

    run_app(state.event_id)


@app.command()
@handle_errors
def ui(
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the link instead of opening a browser.")
    ] = False,
    port: Annotated[
        int | None, typer.Option("--port", help="Local port (default: a random free one).")
    ] = None,
) -> None:
    """Open the interactive app in your browser. It runs only on this computer (127.0.0.1);
    nothing is hosted, and the link works only for you."""
    try:
        from . import webui
    except ImportError as exc:
        raise InputError("Browser mode isn't available in this build; try `rip tui`.") from exc
    try:  # a bad event ID or port, or a port already in use, is caught before serving starts
        webui.serve(state.event_id, open_browser=not no_browser, port=port)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    except OSError as exc:
        raise InputError(f"Couldn't start the local server: {exc.strerror or exc}") from exc


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


@app.command()
@handle_errors
def events(
    past: Annotated[bool, typer.Option("--past", help="Include past events.")] = False,
) -> None:
    """List AWS events available through the API."""
    with _client() as client:
        items = client.list_events(include_past=past)
    table = Table("Event ID", "Name", "Dates", "City", "Sign-in needed")
    for e in items:
        city = e.address.city if e.address and e.address.city else ("Online" if e.is_online else "")
        dates = f"{e.start_date[:10]} → {e.end_date[:10]}"
        table.add_row(
            escape(e.event_id),
            escape(e.name),
            escape(dates),
            escape(city),
            "yes" if e.authentication_required else "no",
        )
    console.print(table)


def _sync(client: EventsClient, cat: Catalog, *, no_abstracts: bool, force: bool) -> None:
    event = client.get_event(state.event_id)
    cat.save_event(event)
    sessions: list[Session] = []
    expected = 0
    with console.status("Downloading the catalog…") as status:
        for page in client.iter_session_pages(state.event_id, include_abstracts=not no_abstracts):
            sessions.extend(page.items)
            expected = page.total_count
            status.update(f"Downloading the catalog… {len(sessions)}/{expected}")
    result = cat.replace_sessions(event, sessions, force=force)
    console.print(f"[green]Synced {result.total} sessions[/] for {escape(event.name)}.")
    if expected and expected != result.total:
        err.print(f"[yellow]The API reported {expected} sessions but returned {result.total}.[/]")
    for title, rows in (
        ("New", [(s.code, s.title) for s in result.added]),
        ("Removed", result.removed),
        ("Changed", [(s.code, f"{s.title} ({', '.join(f)})") for s, f in result.changed]),
    ):
        if rows:
            console.print(f"\n[bold]{title} ({len(rows)})[/]")
            for code, text in rows[:15]:
                console.print(f"  {escape(code)}  {escape(text)}")
            if len(rows) > 15:
                console.print(f"  … and {len(rows) - 15} more")
    travel = TravelTimes.load(state.event_id)
    unknown = [v for v in cat.venues(state.event_id) if not travel.is_known(v)]
    if travel.has_table and unknown:
        err.print(
            f"[yellow]{len(unknown)} venue name(s) aren't in the travel-time table, so walks "
            "to or from them are only warned about, never treated as blocking:[/] "
            + escape(", ".join(unknown))
            + "\n[dim]Add them under [aliases] in <config dir>/venues/"
            f"{state.event_id}.toml, or open an issue so the bundled table can be fixed.[/]"
        )
    # Report before the schedule refresh: the catalog is already committed, so if the refresh
    # failed after that, these alerts would be lost for good (the next sync compares against
    # the new baseline).
    _report_filling(cat, result.filling)
    refreshed = False
    if client.signed_in():
        try:
            _save_schedule(client, cat, client.get_schedule(state.event_id))
            refreshed = True
        except ApiError as exc:
            err.print(
                f"[yellow]Catalog synced, but your schedule wasn't refreshed: {escape(str(exc))}[/]"
            )
    if not refreshed:
        # Session times or rooms may have moved even if the schedule didn't: re-render.
        _refresh_feed(None, cat, from_read=True)


def _report_filling(cat: Catalog, filling: list[tuple[Session, str, str]]) -> None:
    """The API gives no seat counts, only bands; say when sessions you care about fill up."""
    if not filling:
        return
    cached = cat.load_schedule(state.event_id)
    schedule = cached[0] if cached else Schedule()
    mine = {p.session_id for p in cat.plan_items(state.event_id)} | set(schedule.favorites)
    mine -= set(schedule.reserved)  # already holding a seat
    yours = [(s, before, after) for s, before, after in filling if s.session_id in mine]
    if yours:
        console.print(f"\n[bold yellow]Filling up on your list ({len(yours)})[/]")
        for s, before, after in yours:
            change = (
                f"{escape(SEAT_AVAILABILITY_LABELS.get(before, before))} → {_seats(after)}"
                if before
                else f"now {_seats(after)}"
            )
            console.print(f"  {escape(s.code)}  {escape(s.title)}: {change}")
    others = len(filling) - len(yours)
    if others:
        console.print(f"[dim]{others} other session(s) also filled up since the last sync.[/]")


@app.command()
@handle_errors
def sync(
    no_abstracts: Annotated[
        bool, typer.Option("--no-abstracts", help="Skip abstracts for a smaller, faster download.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Replace the local catalog even if it shrank a lot.")
    ] = False,
) -> None:
    """Download the full catalog, report changes, and flag your picks that are filling up."""
    with _client() as client, _catalog() as cat:
        _sync(client, cat, no_abstracts=no_abstracts, force=force)


@app.command()
@handle_errors
def setup(
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the sign-in URL instead of opening it.")
    ] = False,
) -> None:
    """First run: sign in (if needed), download the catalog, and show what to do next."""
    auth = _auth()
    signed_in = False
    if auth.is_signed_in():
        try:
            auth.access_token()  # refreshes if needed; fails if the sign-in has died
            signed_in = True
        except NotSignedInError:
            console.print("Your saved sign-in has expired; signing in again.")
    if signed_in:
        console.print(f"Already signed in as {escape(auth.current().email or 'your Builder ID')}.")
    else:

        def announce(url: str) -> None:
            console.print("Step 1/2: sign in with your AWS Builder ID in the browser.")
            console.print(f"[dim]If the browser doesn't open, visit:[/]\n{url}", soft_wrap=True)

        tokens = auth.login(announce=announce, launch_browser=not no_browser)
        console.print(f"[green]Signed in[/] as {escape(tokens.email or 'your Builder ID')}.")
    console.print("Step 2/2: downloading the catalog.")
    try:
        with EventsClient(auth) as client, _catalog() as cat:
            _sync(client, cat, no_abstracts=False, force=False)
    except (ApiError, CatalogError, httpx.HTTPError) as exc:
        prefix = "The download failed" if signed_in else "Signed in, but the download failed"
        err.print(f"[red]{prefix}:[/] {escape(str(exc))}")
        err.print("Run `rip sync` to try again.")
        raise typer.Exit(1) from None
    console.print(
        "\n[bold]You're set.[/] Next:\n"
        "  rip search agents --level 300 --day tue   find sessions\n"
        "  rip rank set CODE 1 --backup CODE2        rank what you want most\n"
        "  rip checklist --out checklist.md          your list for reserving on the website\n"
        "  rip ics                                   add your plan to your calendar\n"
        "  rip --help                                everything else"
    )


@app.command()
@handle_errors
def search(
    query: Annotated[
        str | None, typer.Argument(help="Words to match in title, abstract, speakers, tags.")
    ] = None,
    type_: Annotated[
        list[str] | None, typer.Option("--type", "-t", help="Session type, e.g. workshop.")
    ] = None,
    level: Annotated[str | None, typer.Option("--level", "-l", help="Level, e.g. 300.")] = None,
    day: Annotated[
        list[str] | None, typer.Option("--day", "-d", help="mon…fri or YYYY-MM-DD.")
    ] = None,
    venue: Annotated[str | None, typer.Option("--venue", help="Venue name contains.")] = None,
    topic: Annotated[list[str] | None, typer.Option("--topic", help="Topic contains.")] = None,
    service: Annotated[
        list[str] | None, typer.Option("--service", help="AWS service contains.")
    ] = None,
    role: Annotated[list[str] | None, typer.Option("--role", help="Role contains.")] = None,
    area: Annotated[
        list[str] | None, typer.Option("--area", help="Area of interest contains.")
    ] = None,
    reservable: Annotated[
        bool,
        typer.Option(
            "--reservable",
            help="Only sessions the catalog marks reservable (none are before reservations open).",
        ),
    ] = False,
    available: Annotated[
        bool, typer.Option("--available", help="Only sessions with seats or walk-up.")
    ] = False,
    feature: Annotated[
        list[str] | None,
        typer.Option("--feature", help="Feature contains, e.g. hands-on, discussion, lecture."),
    ] = None,
    industry: Annotated[
        list[str] | None, typer.Option("--industry", help="Industry contains, e.g. financial.")
    ] = None,
    favorites: Annotated[bool, typer.Option("--favorites", help="Only your favorites.")] = False,
    reserved: Annotated[
        bool, typer.Option("--reserved", help="Only sessions you've reserved.")
    ] = False,
    laptop: Annotated[
        bool,
        typer.Option(
            "--laptop",
            help="Only types that need a laptop: bootcamps, builders' sessions, exam prep, "
            "gamified learning, workshops.",
        ),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum results (0 for all).")] = 50,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use the cached schedule for --favorites/--reserved.")
    ] = False,
) -> None:
    """Search the local catalog. Marks: R reserved, F favorite, #n your rank.

    Filters combine (AND); repeating a filter, like --type workshop --type builders, means OR.
    --favorites and --reserved together mean either.
    """
    with _client() as client, _catalog() as cat:
        _require_catalog(cat)
        only_ids: frozenset[str] | None = None
        if favorites or reserved:
            mine = _schedule_or_cache(client, cat, offline=offline)
            only_ids = frozenset(
                (mine.favorites if favorites else []) + (mine.reserved if reserved else [])
            )
            if not only_ids and cat.load_schedule(state.event_id) is None:
                err.print("[yellow]No schedule available. Run `rip login`, then `rip schedule`.[/]")
        filters = SearchFilters(
            query=query,
            types=type_ or (),
            level=level,
            days=day or (),
            venue=venue,
            topics=topic or (),
            services=service or (),
            roles=role or (),
            areas=area or (),
            features=feature or (),
            industries=industry or (),
            only_ids=only_ids,
            laptop_required=laptop,
            reservable_only=reservable,
            available_only=available,
            limit=limit or None,
        )
        results = cat.search(state.event_id, filters)
        zone = _zone(cat)
        cached = cat.load_schedule(state.event_id)
        schedule = cached[0] if cached else Schedule()
        ranks = {p.session_id: p.rank for p in cat.plan_items(state.event_id) if p.rank}
    headers = ("", "Code", "Title", "Type", "Level", "When", "Venue", "Seats")
    rows = [
        (
            _marks(s.session_id, schedule, ranks),
            escape(s.code),
            escape(s.title),
            escape(s.type or ""),
            escape((s.level or "").split(" ")[0]),
            _when(s, zone, short=True),
            escape(s.place or ""),
            _seats(s.seat_availability),
        )
        for s in results
    ]
    # Hide columns this event doesn't fill in (many events have no venue or seat data).
    keep = [i for i, h in enumerate(headers) if h in ("Code", "Title") or any(r[i] for r in rows)]
    table = Table(*(headers[i] for i in keep))
    for row in rows:
        table.add_row(*(row[i] for i in keep))
    console.print(table)
    console.print(f"[dim]{len(results)} result(s).[/]")
    if reservable and not results:
        with _catalog() as cat:
            none_marked = not cat.db.execute(
                "SELECT 1 FROM sessions WHERE event_id = ? AND is_reservable = 1 LIMIT 1",
                (state.event_id,),
            ).fetchone()
        if none_marked:
            err.print(
                "[yellow]No session in the catalog is marked reservable yet. That's normal "
                "before reserved seating opens; run `rip sync` after it does.[/]"
            )


@app.command()
@handle_errors
def show(code: Annotated[str, typer.Argument(help="Session code, e.g. AIM301.")]) -> None:
    """Show one session in full."""
    with _catalog() as cat:
        s = cat.resolve(state.event_id, code)
        zone = _zone(cat)
    console.print(f"[bold]{escape(s.code)} – {escape(s.title)}[/]")
    rows = [
        ("When", _when(s, zone)),
        ("Where", " | ".join(p for p in (s.venue, s.room) if p)),
        ("Type", s.type),
        ("Level", s.level),
        (
            "Seats",
            _seats(s.seat_availability)
            if s.seat_availability
            else "Reservable"
            if s.is_reservable
            else "Not marked reservable (yet)",
        ),
        ("Speakers", ", ".join(s.speaker_names)),
        ("Topics", ", ".join(s.topics)),
        ("Services", ", ".join(s.services)),
        ("Areas", ", ".join(s.areas_of_interest)),
        ("Session ID", s.session_id),
    ]
    for label, value in rows:
        if value:
            console.print(f"[dim]{label:>10}[/]  {value if label == 'Seats' else escape(value)}")
    if s.abstract:
        console.print()
        console.print(escape(s.abstract.strip()))


venues_app = typer.Typer(
    help="Travel times between venues: show them, correct them, or measure a new event's.",
    invoke_without_command=True,
)
app.add_typer(venues_app, name="venues")


@venues_app.callback()
@handle_errors
def venues(ctx: typer.Context) -> None:
    """Travel times between venues: what `plan`, `checklist` and warnings check against."""
    if ctx.invoked_subcommand is not None:
        return
    travel = TravelTimes.load(state.event_id)
    if not travel.has_table:
        console.print(
            f"No travel-time table for {state.event_id}. For a multi-venue event, "
            "`rip venues measure` can build one."
        )
        return
    keys = travel.keys_with_figures()
    with _catalog() as cat:
        in_use = {
            travel.place_key(r["venue"], r["room"])
            for r in cat.db.execute(
                "SELECT DISTINCT venue, room FROM sessions WHERE event_id = ?", (state.event_id,)
            )
        } - {None}
    if in_use:
        keys = [k for k in keys if k in in_use] or keys
    # Below this width a table would squeeze the venue names away, so each pair gets its own
    # lines instead (and the matrix, which would truncate names, is left out).
    wide = console.width >= VENUES_TABLE_MIN_WIDTH
    if wide:
        table = Table("Minutes to allow", *(escape(travel.name(k)) for k in keys))
        for a in keys:
            cells = []
            for b in keys:
                minutes = travel.planned(a, b)
                mark = "*" if frozenset((a, b)) in travel.corrected else ""
                cells.append(
                    "—" if a == b else (f"{minutes}{mark}" if minutes is not None else "?")
                )
            table.add_row(escape(travel.name(a)), *cells)
        console.print(table)
    walks = Table("Between")
    walks.add_column("Walk", no_wrap=True)  # never split "0.6 km (0.4 mi) · 8 min" across lines
    walks.add_column("Monorail")  # word-wraps at the spaces around "→"
    if travel.has_shuttle_info:
        walks.add_column("Shuttle (2025)", no_wrap=True)
    walks.add_column("Allow", no_wrap=True)
    pairs = [(a, b) for i, a in enumerate(keys) for b in keys[i + 1 :]]
    notes: list[str] = []
    for a, b in sorted(pairs, key=lambda p: (travel.walking(*p) or (10**9, 0))[0]):
        walk, rail = travel.walking(a, b), travel.monorail(a, b)
        if rail is None:
            rail_text = "[dim]no help[/]" if walk else "?"  # stations too far from these venues
        else:
            route = (
                f"{rail.minutes} min · {escape(_short_station(rail.from_station))} → "
                f"{escape(_short_station(rail.to_station))}"
            )
            rail_text = (
                f"[green]{route}[/]"
                if rail.beats(walk[1] if walk else None)
                else f"[dim]{route}[/]"
            )
        pair = escape(f"{travel.name(a)} ↔ {travel.name(b)}")
        walk_text = f"{_distance(walk[0])} · {walk[1]} min" if walk else "?"
        shuttle_text = None
        if travel.has_shuttle_info:
            shuttle_text = {True: "yes", False: "[yellow]no[/]", "indoors": "walk (indoors)"}.get(
                travel.shuttle(a, b), "[dim]?[/]"
            )
        minutes = travel.planned(a, b)
        mark = "*" if frozenset((a, b)) in travel.corrected else ""
        allow_text = f"{minutes} min{mark}" if minutes is not None else "?"
        if wide:
            walks.add_row(
                pair, walk_text, rail_text, *([shuttle_text] if shuttle_text else []), allow_text
            )
        else:
            console.print(f"[bold]{pair}[/]  allow {allow_text}")
            console.print(f"  walk {walk_text}")
            extras = [f"monorail {rail_text}"]
            if shuttle_text:
                extras.append(f"shuttle (2025) {shuttle_text}")
            console.print("  " + " · ".join(extras))
        if note := travel.note(a, b):
            notes.append(f"{travel.name(a)} ↔ {travel.name(b)}: {note}")
    if wide:
        console.print(walks)
    for note in notes:
        console.print(f"  [dim]•[/] {escape(note)}")
    if travel.corrected:
        console.print(
            "[dim]* your own correction (`rip venues set`; undo with `rip venues reset`).[/]"
        )
    for pair in travel.ignored_corrections:
        err.print(
            f"[yellow]Ignored correction {escape(pair)!s}: not two different venues of this "
            f"table. Check {escape(str(corrections_path(state.event_id)))}.[/]"
        )
    console.print(
        "[dim]Allow = the street walk + 10 min to reach the room, rounded up to 5, capped at 45 "
        "(take a shuttle beyond that). Monorail = an off-peak tip (green when it clearly beats "
        "walking): walks to and from the stations measured, boarding (5 min) and ride modelled; "
        "allow more at the :00/:30 rush and after keynotes. Shuttles and the monorail were "
        "reportedly included with the badge in 2025; check the 2026 attendee guide. Walks and "
        "stations from OpenStreetMap. These only drive warnings: `rip reserve` never refuses a "
        "session for travel time.[/]"
    )


def _venue_pair(travel: TravelTimes, first: str, second: str) -> tuple[str, str]:
    keys = []
    for text in (first, second):
        key = travel.resolve(text)
        usable = travel.keys_with_figures()
        if key is None or key not in usable:
            names = ", ".join(travel.name(k) for k in usable)
            raise InputError(f"Unknown venue {text!r}. Try one of: {names}.")
        keys.append(key)
    if keys[0] == keys[1]:
        raise InputError("Pick two different venues.")
    return keys[0], keys[1]


def _load_corrections() -> dict[str, int]:
    path = corrections_path(state.event_id)
    if not path.exists():
        return {}
    try:
        return dict(tomllib.loads(path.read_text(encoding="utf-8")).get("minutes", {}))
    except (tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        raise TravelTableError(f"Your travel-time corrections {path} are invalid: {exc}") from exc


@venues_app.command("set")
@handle_errors
def venues_set(
    first: Annotated[str, typer.Argument(help="A venue, e.g. Venetian.")],
    second: Annotated[str, typer.Argument(help="Another venue, e.g. Wynn.")],
    minutes: Annotated[int, typer.Argument(min=1, max=180, help="Minutes to allow between them.")],
) -> None:
    """Correct the time to allow between two venues (e.g. after trying it on day 1)."""
    travel = TravelTimes.load(state.event_id)
    a, b = _venue_pair(travel, first, second)
    corrections = {
        k: v for k, v in _load_corrections().items() if _pair_keys(k) != frozenset((a, b))
    }
    corrections["|".join(sorted((a, b)))] = minutes
    path = save_corrections(state.event_id, corrections)
    console.print(
        f"{escape(travel.name(a))} ↔ {escape(travel.name(b))}: now {minutes} min "
        f"(was {travel.planned(a, b)}). Saved in {escape(str(path))}"
    )


@venues_app.command("reset")
@handle_errors
def venues_reset(
    first: Annotated[str | None, typer.Argument(help="A venue (omit to reset all).")] = None,
    second: Annotated[str | None, typer.Argument(help="Another venue.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Undo your corrections: one pair, or all of them."""
    path = corrections_path(state.event_id)
    if first is None:
        if not path.exists():
            console.print("You have no travel-time corrections.")
            return
        _confirm("Remove all your travel-time corrections?", yes)
        path.unlink(missing_ok=True)
        console.print("All your travel-time corrections are removed.")
        return
    if second is None:
        raise InputError("Give two venues, or none to reset everything.")
    travel = TravelTimes.load(state.event_id)
    a, b = _venue_pair(travel, first, second)
    corrections = _load_corrections()
    kept = {k: v for k, v in corrections.items() if _pair_keys(k) != frozenset((a, b))}
    if kept == corrections:
        console.print(
            f"There's no correction for {escape(travel.name(a))} ↔ {escape(travel.name(b))}."
        )
        return
    save_corrections(state.event_id, kept) if kept else path.unlink(missing_ok=True)
    console.print(
        f"{escape(travel.name(a))} ↔ {escape(travel.name(b))} is back to the table's figure."
    )


@venues_app.command("measure")
@handle_errors
def venues_measure_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing travel table (backed up first).")
    ] = False,
    regeocode: Annotated[
        bool,
        typer.Option("--regeocode", help="Look venues up again instead of reusing saved ones."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Build a travel-time table for this event by measuring walks on OpenStreetMap.

    For multi-venue events. Only the event's public venue names and city are sent, to
    OpenStreetMap's geocoder and walking router. You check where each venue was found before
    anything is measured or saved.
    """
    with _catalog() as cat:
        _require_catalog(cat)
        event = cat.get_event(state.event_id)
        names = venues_measure.normalize_names(
            [
                r["venue"]
                for r in cat.db.execute(
                    "SELECT DISTINCT venue FROM sessions WHERE event_id = ? "
                    "AND venue IS NOT NULL AND venue != '' ORDER BY venue",
                    (state.event_id,),
                )
            ]
        )
        venueless = cat.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE event_id = ? AND (venue IS NULL OR venue = '')",
            (state.event_id,),
        ).fetchone()[0]
    if venueless:
        err.print(
            f"[yellow]{venueless} session(s) list no venue, so they aren't measured. If their "
            "rooms are in other buildings, add those to the table by hand.[/]"
        )
    if len(names) < 2:
        console.print(
            f"{state.event_id} lists {len(names)} venue(s) in its catalog, so there's nothing to "
            "measure: travel between rooms of one venue needs no table."
        )
        return
    override = config_dir() / "venues" / f"{state.event_id}.toml"
    bundled = resources.files("reinvent_planner") / "data" / f"{state.event_id}.toml"
    existing_text = override.read_text(encoding="utf-8") if override.exists() else None
    if override.exists() or bundled.is_file():
        if not force:
            where = (
                f"your file {override}" if override.exists() else "a bundled, hand-checked table"
            )
            raise InputError(
                f"{state.event_id} already has a travel table ({where}). Use --force to replace "
                "it with a measured one."
            )
        current = TravelTimes.load(state.event_id).keys_with_figures()
        console.print(
            f"[yellow]This replaces the current table ({len(current)} venues"
            + (", hand-checked" if not override.exists() else ", your file, backed up first")
            + f") with a measured one ({len(names)} venues).[/]"
        )
    city = event.address.city if event and event.address else None
    console.print(f"Venues: {escape(', '.join(names))}" + (f" (in {escape(city)})" if city else ""))
    _confirm(
        "Look these up on OpenStreetMap? Only the venue names"
        + (" and the city" if city else " are sent (no city is known, so worldwide),")
        + (" are sent," if city else "")
        + " to nominatim.openstreetmap.org and routing.openstreetmap.de.",
        yes,
    )
    cached = (
        {} if regeocode or existing_text is None else venues_measure.cached_places(existing_text)
    )
    progress = lambda msg: err.print(f"[dim]{escape(msg)}[/]")  # noqa: E731
    with venues_measure.Measurer() as measurer:
        places, missing = venues_measure.geocode_all(measurer, names, city, progress, cached)
        if missing:
            raise venues_measure.MeasureError(
                f"OpenStreetMap couldn't find: {', '.join(missing)}. Nothing was saved; you can "
                f"write the table by hand at {override}."
            )
        far = venues_measure.outliers(places)
        table = Table("Venue", "Matched on OpenStreetMap")
        for place in places:
            flag = " [yellow]⚠ far from the others[/]" if place.key in far else ""
            table.add_row(escape(place.name), escape(place.label) + flag)
        console.print(table)
        if far:
            err.print("[yellow]A venue far from the rest is often a wrong match.[/]")
        _confirm("Do these look right? (Walks are measured next.)", yes)
        walks = venues_measure.walk_all(measurer, places, progress)
    long_walks = [pair for pair, (_m, minutes) in walks.items() if minutes > 60]
    if long_walks:
        err.print(
            f"[yellow]{len(long_walks)} walk(s) take over an hour, which usually means a wrong "
            "match above.[/]"
        )
        _confirm("Save the table anyway?", yes)
    text = venues_measure.build_table(state.event_id, places, walks)
    try:  # never leave an unreadable table behind
        text.encode("utf-8")  # e.g. a lone surrogate in a name
        tomllib.loads(text)
        TravelTimes.from_toml(text)
    except (tomllib.TOMLDecodeError, ValueError, KeyError, TypeError) as exc:
        raise venues_measure.MeasureError(
            f"The measured table came out invalid ({exc}); nothing was saved."
        ) from exc
    override.parent.mkdir(parents=True, exist_ok=True)
    # Write the new table in full first; only then move the old one aside and swap in the new,
    # so a failed write never leaves you without a table.
    fd, tmp_name = tempfile.mkstemp(dir=override.parent, prefix=".measure-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        backup = None
        if override.exists():
            backup = override.with_name(f"{override.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
            os.replace(override, backup)
        os.replace(tmp_name, override)
    except BaseException:
        pathlib.Path(tmp_name).unlink(missing_ok=True)
        raise
    console.print(
        f"[green]Saved[/] {override}"
        + (f" (your previous file is at {backup})" if backup else "")
        + ". `rip venues` shows the table; "
        + ("restore the backup to undo." if backup else "deleting the file undoes this.")
    )


def _pair_keys(text: str) -> frozenset[str]:
    a, _, b = text.partition("|")
    return frozenset((a.strip(), b.strip()))


# ---------------------------------------------------------------------------
# schedule and favorites
# ---------------------------------------------------------------------------


def _print_agenda(items: list[Item], zone: ZoneInfo | None, travel: TravelTimes) -> None:
    issues = find_issues(items, travel)
    flagged = {i.first.key for i in issues} | {i.second.key for i in issues}
    labels = {
        "reserved": "[green]R[/]",
        "favorite": "[yellow]F[/]",
        "ranked": "#",
        "personal": "[cyan]P[/]",
    }
    for day, day_items in group_by_day(items, zone).items():
        console.print(
            f"\n[bold]{day:%A, %B} {day.day}[/]" if day else "\n[bold]Time not announced[/]"
        )
        for item in day_items:
            kinds = [k for k in sorted(item.kinds) if k in labels]
            # Pad by the visible width: the markup in the labels would throw off a :<6 pad.
            marks = " ".join(labels[k] for k in kinds) + " " * max(0, 6 - (2 * len(kinds) - 1))
            warn = " [red]⚠[/]" if item.key in flagged else ""
            when = _when(item, zone).split(" ", 3)[-1] if item.start else "TBA"
            where = escape(" | ".join(p for p in (item.venue, item.room) if p))
            console.print(
                f"  {when:<12} {marks} {escape(item.code):<10} {escape(item.title)}{warn}"
            )
            if where:
                console.print(f"  {'':<12} {'':<6} [dim]{where}[/]")
    if issues:
        console.print(f"\n[bold red]{len(issues)} problem(s)[/]")
        for issue in issues:
            console.print(f"  [red]⚠[/] {escape(issue.describe())}")
    elif items:
        console.print("\n[green]No overlaps or tight transfers.[/]")


@app.command()
@handle_errors
def schedule(
    offline: Annotated[
        bool, typer.Option("--offline", help="Use the cached copy; don't call the API.")
    ] = False,
) -> None:
    """Show your reservations, favorites and personal time, with conflicts flagged."""
    with _client() as client, _catalog() as cat:
        if not offline and not client.signed_in():
            raise SignInRequiredError("You are not signed in. Run `rip login` (or use --offline).")
        sched = _schedule(client, cat, refresh=not offline)
        items = [i for i in _plan_items(cat, sched) if i.kinds - {"ranked"}]
        console.print(
            f"{len(sched.reserved)} reserved · {len(sched.favorites)} favorites · "
            f"{len(sched.personal_time)} personal time"
        )
        _print_agenda(items, _zone(cat), TravelTimes.load(state.event_id))


@app.command()
@handle_errors
def plan(
    offline: Annotated[
        bool, typer.Option("--offline", help="Use the cached schedule; don't call the API.")
    ] = False,
) -> None:
    """Your whole plan (reserved, favorites, ranked, personal time) by day, with conflicts and
    transfers you can't make in time flagged."""
    with _client() as client, _catalog() as cat:
        _require_catalog(cat)
        sched = _schedule(client, cat, refresh=not offline)
        _print_agenda(_plan_items(cat, sched), _zone(cat), TravelTimes.load(state.event_id))


VENUES_TABLE_MIN_WIDTH = 90

# Short monorail station names for the narrow `rip venues` column (warnings use full names).
_STATION_SHORT = {
    "MGM Grand": "MGM",
    "Horseshoe/Paris": "Horseshoe",
    "Flamingo/Caesars Palace": "Flamingo",
    "Harrah's/The LINQ": "LINQ",
}


def _short_station(name: str) -> str:
    return _STATION_SHORT.get(name, name)


def _walk_note(issue, travel: TravelTimes) -> str:
    """How the trip is made: the walk, and the monorail when it's faster."""
    a, b = issue.first, issue.second
    ka, kb = travel.place_key(a.venue, a.room), travel.place_key(b.venue, b.room)
    if not (ka and kb):
        return ""
    walk, rail = travel.walking(ka, kb), travel.monorail(ka, kb)
    parts = [f"{_distance(walk[0])}, {walk[1]} min on foot"] if walk else []
    if rail and rail.beats(walk[1] if walk else None):
        parts.append(f"or ~{rail.minutes} min by monorail off-peak, {rail.route}")
    if note := travel.note(ka, kb):
        parts.append(note)
    return escape(f" ({'; '.join(parts)})") if parts else ""


def _conflict_warnings(
    cat: Catalog,
    schedule: Schedule,
    sessions: list[Session],
    kind,
    *,
    as_of: str | None = None,
    alternatives: bool = False,
) -> list[str]:
    """Warn if sessions about to join your plan overlap it or can't be reached in time.

    Checked against your reservations, favorites, ranked picks and personal time, as they are
    *before* the action (so call this before changing anything). Sessions already on the plan
    are skipped: their issues aren't new. Warnings only; nothing is ever blocked for travel.

    Each session is checked on its own against the plan, so one can't mask another's issue.
    With `alternatives` (a pick and its backups: you'd attend only one), that's all. Otherwise
    (several favorites you mean to attend together), clashes among them are reported too.
    """
    zone = _zone(cat)
    return _item_warnings(
        cat,
        schedule,
        [item_from_session(s, zone, kind) for s in sessions],
        as_of=as_of,
        alternatives=alternatives,
    )


def _item_warnings(
    cat: Catalog,
    schedule: Schedule,
    items: list[Item],
    *,
    as_of: str | None = None,
    alternatives: bool = False,
    skip_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """`_conflict_warnings` for ready-made items (e.g. a personal time block).

    `skip_keys` leaves plan entries out of the comparison (e.g. the entry being edited).
    """
    travel = TravelTimes.load(state.event_id)
    plan = [i for i in _plan_items(cat, schedule, quiet=True) if i.key not in skip_keys]
    in_plan = {i.key for i in plan}
    new = [i for i in items if i.key not in in_plan]
    issues = []
    for option in new:
        issues += [
            issue
            for issue in find_issues(merge_items([*plan, option]), travel)
            if option.key in (issue.first.key, issue.second.key)
        ]
    if not alternatives and len(new) > 1:
        new_keys = {i.key for i in new}
        issues += [
            issue
            for issue in find_issues(new, travel)
            if {issue.first.key, issue.second.key} <= new_keys
        ]
    lines = [
        f"  [yellow]⚠[/] {escape(issue.describe())}{_walk_note(issue, travel)}" for issue in issues
    ]
    if issues and as_of:
        lines.append(f"  [dim](checked against your schedule as of {_fetched(as_of)})[/]")
    return lines


# Set while the interactive app runs a CLI function (services.captured): there's no terminal to
# prompt on there, so a confirmation that wasn't given up front is an error, not a hang.
NON_INTERACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "non_interactive", default=False
)


def _confirm(message: str, yes: bool) -> None:
    if not yes and NON_INTERACTIVE.get():
        raise InputError("This needs a confirmation the app can't ask for here.")
    if not yes:
        # Prompts print raw (no rich), and include API text such as titles: drop control
        # characters so nothing can inject terminal escape sequences.
        typer.confirm("".join(ch for ch in message if ch.isprintable()), abort=True)


@fav_app.command("add")
@handle_errors
def fav_add(
    codes: Annotated[list[str], typer.Argument(help="Session codes to favorite.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Mark sessions as favorites on your event schedule."""
    with _client(need_auth=True) as client, _catalog() as cat:
        sessions = [cat.resolve(state.event_id, c) for c in codes]
        for s in sessions:
            console.print(f"  {escape(s.code)}  {escape(s.title)}")
        before = _schedule_or_cache(client, cat)
        for line in _conflict_warnings(cat, before, sessions, "favorite"):
            err.print(line)
        _confirm(f"Add {len(sessions)} favorite(s) to your {state.event_id} schedule?", yes)
        by_id = {s.session_id: s for s in sessions}
        try:
            result = client.associate_favorites(state.event_id, list(by_id))
        except WriteOutcomeUnknownError as exc:
            err.print(f"[yellow]{escape(str(exc))}[/] Checking your schedule…")
            result = None
        after = client.get_schedule(state.event_id)  # the API's source of truth
        cat.save_schedule(state.event_id, after)
        _refresh_feed(client, cat, after)
        if result is not None:
            for failure in result.failed:
                name = (
                    by_id[failure.session_id].code
                    if failure.session_id in by_id
                    else failure.session_id
                )
                console.print(f"  [yellow]✗[/] {escape(name)}: {failure.reason}")
        confirmed = [s for s in sessions if s.session_id in after.favorites]
        console.print(f"[green]{len(confirmed)} of {len(sessions)} now in your favorites.[/]")


@fav_app.command("rm")
@handle_errors
def fav_rm(
    code: Annotated[str, typer.Argument(help="Session code to remove from favorites.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Remove a session from your favorites."""
    with _client(need_auth=True) as client, _catalog() as cat:
        s = cat.resolve(state.event_id, code)
        _confirm(f"Remove {s.code} ({s.title}) from your favorites?", yes)
        failure: ApiError | None = None
        try:
            client.disassociate_favorite(state.event_id, s.session_id)
        except ApiError as exc:  # it may still have gone through: the readback decides
            failure = exc
        after = client.get_schedule(state.event_id)
        cat.save_schedule(state.event_id, after)
        _refresh_feed(client, cat, after)
        if s.session_id in after.favorites:
            raise failure or ApiError(f"{s.code} is still a favorite. Check `rip fav ls`.")
        if failure is not None:  # refused, but it isn't a favorite now either way
            console.print(f"{escape(s.code)} isn't one of your favorites.")
            return
        console.print(f"[green]Removed {escape(s.code)} from your favorites.[/]")


@fav_app.command("ls")
@handle_errors
def fav_ls() -> None:
    """List your favorites."""
    with _client() as client, _catalog() as cat:
        sched = _schedule(client, cat)
        sessions = cat.get_sessions(state.event_id, sched.favorites)
        zone = _zone(cat)
        table = Table("Code", "Title", "When", "Venue", "Seats")
        for sid in sched.favorites:
            s = sessions.get(sid)
            if s is None:
                table.add_row(
                    escape(sid), "[dim](not in local catalog — run `rip sync`)[/]", "", "", ""
                )
                continue
            table.add_row(
                escape(s.code),
                escape(s.title),
                _when(s, zone),
                escape(s.venue or ""),
                _seats(s.seat_availability),
            )
        console.print(table)


# ---------------------------------------------------------------------------
# ranking and checklist
# ---------------------------------------------------------------------------


@rank_app.command("set")
@handle_errors
def rank_set(
    code: Annotated[str, typer.Argument(help="Session code.")],
    rank: Annotated[int, typer.Argument(min=1, max=999, help="1 is your top pick.")],
    backup: Annotated[
        list[str] | None,
        typer.Option("--backup", "-b", help="A session to try if this one is full."),
    ] = None,
) -> None:
    """Rank a session, optionally with backups."""
    with _catalog() as cat:
        s = cat.resolve(state.event_id, code)
        backups = [cat.resolve(state.event_id, b) for b in backup or []]
        cached = cat.load_schedule(state.event_id)
        warnings = _conflict_warnings(  # against the plan as it was before this change
            cat,
            cached[0] if cached else Schedule(),
            [s, *backups],
            "ranked",
            as_of=cached[1] if cached else None,
            alternatives=True,
        )
        cat.set_rank(state.event_id, s.session_id, rank)
        for b in backups:
            cat.add_backup(state.event_id, b.session_id, s.session_id)
    console.print(
        f"Ranked {escape(s.code)} #{rank}"
        + (f" with {len(backups)} backup(s)." if backups else ".")
    )
    for line in warnings:
        err.print(line)
    _refresh_feed_if_ranked()


@rank_app.command("rm")
@handle_errors
def rank_rm(code: Annotated[str, typer.Argument(help="Session code.")]) -> None:
    """Remove a session (and its backups) from your ranking."""
    with _catalog() as cat:
        s = cat.resolve(state.event_id, code)
        removed = cat.remove_plan_item(state.event_id, s.session_id)
    console.print(f"Removed {escape(s.code)}." if removed else f"{escape(s.code)} wasn't ranked.")
    if removed:
        _refresh_feed_if_ranked()


@rank_app.command("ls")
@handle_errors
def rank_ls() -> None:
    """List your ranking."""
    with _catalog() as cat:
        items = cat.plan_items(state.event_id)
        sessions = cat.get_sessions(state.event_id, [i.session_id for i in items])
        zone = _zone(cat)
    table = Table("Rank", "Code", "Title", "When", "Venue")
    for item in (i for i in items if i.rank):
        s = sessions.get(item.session_id)
        if not s:
            continue
        table.add_row(
            f"#{item.rank}", escape(s.code), escape(s.title), _when(s, zone), escape(s.venue or "")
        )
        for b in (i for i in items if i.backup_for == item.session_id):
            bs = sessions.get(b.session_id)
            if bs:
                table.add_row(
                    "  ↳",
                    escape(bs.code),
                    escape(bs.title),
                    _when(bs, zone),
                    escape(bs.venue or ""),
                )
    console.print(table)


@app.command()
@handle_errors
def checklist(
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Also write it as Markdown.")
    ] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Use the cached schedule.")] = False,
) -> None:
    """Your ranked picks in order, checked against what's already on your calendar.

    Use it to reserve on the event website: reserve every RESERVE row top to bottom, and fall
    back to the backups listed under a pick if it's full.
    """
    with _client() as client, _catalog() as cat:
        _require_catalog(cat)
        zone = _zone(cat)
        sched = _schedule(client, cat, refresh=not offline)
        plan_items = cat.plan_items(state.event_id)
        ids = [p.session_id for p in plan_items] + sched.reserved
        sessions = cat.get_sessions(state.event_id, ids)

        def item(sid: str, kind) -> Item | None:
            return item_from_session(sessions[sid], zone, kind) if sid in sessions else None

        fixed = [i for i in (item(sid, "reserved") for sid in sched.reserved) if i]
        fixed += [item_from_personal_time(pt) for pt in sched.personal_time]
        ranked = []
        for p in plan_items:
            primary = item(p.session_id, "ranked") if p.rank else None
            if primary and p.rank:
                backups = [
                    b
                    for b in (
                        item(x.session_id, "backup")
                        for x in plan_items
                        if x.backup_for == p.session_id
                    )
                    if b
                ]
                ranked.append((p.rank, primary, backups))
        entries = build_checklist(ranked, fixed, TravelTimes.load(state.event_id))

    if not entries:
        console.print("Nothing ranked yet. Use `rip rank set CODE 1 --backup CODE2`.")
        return
    status_text = {
        "reserve": "[green]RESERVE[/]",
        "reserved": "[dim]done[/]",
        "clash": "[yellow]if a higher pick fails[/]",
        "unscheduled": "[dim]time TBA[/]",
    }
    table = Table("Rank", "Do", "Code", "When", "Title")
    lines = [f"# {state.event_id} reservation checklist", ""]
    for e in entries:
        clash = (
            f" — clashes with {', '.join(c.code for c in e.clashes_with)}" if e.clashes_with else ""
        )
        table.add_row(
            f"#{e.rank}",
            status_text[e.status],
            escape(e.item.code),
            _when(e.item, zone),
            escape(e.item.title) + (f"[dim]{escape(clash)}[/]" if clash else ""),
        )
        box = "x" if e.status == "reserved" else " "
        lines.append(
            f"- [{box}] **#{e.rank} {e.item.code}** {_when(e.item, zone)} — "
            f"{_one_line(e.item.title)}{clash}"
        )
        for w in e.travel_warnings:  # under their own pick, in the table and the file
            note = f"⚠ {w.describe()}"
            table.add_row("", "", "", "", f"[yellow]{escape(note)}[/]")
            lines.append(f"  - {_one_line(note)}")
        for b in e.backups:
            note = (
                f" (clashes with {', '.join(c.code for c in b.clashes_with)})"
                if b.clashes_with
                else ""
            )
            table.add_row(
                "",
                "[dim]↳ backup (reserved)[/]" if b.reserved else "[dim]↳ backup[/]",
                escape(b.item.code),
                _when(b.item, zone),
                escape(b.item.title) + (f"[dim]{escape(note)}[/]" if note else ""),
            )
            box = "x" if b.reserved else " "
            lines.append(
                f"  - [{box}] backup {b.item.code} {_when(b.item, zone)} — "
                f"{_one_line(b.item.title)}{note}"
            )
    console.print(table)
    if out:
        _write_output(out, "\n".join(lines) + "\n", "utf-8")
        console.print(f"\nWrote {escape(str(out))}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@app.command("csv")
@handle_errors
def csv_export(
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="File to write (default: <event>-plan.csv), or - for stdout (UTF-8, no BOM).",
        ),
    ] = None,
    all_sessions: Annotated[
        bool, typer.Option("--all", help="The whole catalog, not just your plan.")
    ] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Use the cached schedule.")] = False,
) -> None:
    """Export your plan (or the whole catalog) as a spreadsheet-ready CSV file."""
    with _client() as client, _catalog() as cat:
        _require_catalog(cat)
        zone = _zone(cat)
        sched = _schedule(client, cat, refresh=not offline)
        items = {i.key: i for i in _plan_items(cat, sched) if "personal" not in i.kinds}
        ranks = {p.session_id: p.rank for p in cat.plan_items(state.event_id) if p.rank}
        if all_sessions:
            sessions = cat.search(state.event_id, SearchFilters(limit=None))
        else:
            found = cat.get_sessions(state.event_id, list(items))
            sessions = [found[k] for k in items if k in found]
    entries = [(items.get(s.session_id), s) for s in sessions]
    entries.sort(key=lambda e: (e[1].interval(zone) is None, e[1].interval(zone) or (), e[1].code))
    text = export_to_csv(export_rows(entries, zone, ranks))
    if out is not None and str(out) == "-":
        # Write UTF-8 bytes directly: a redirected stdout may use a legacy encoding (cp1252 on
        # Windows) that can't represent names in the catalog. No BOM here; for Excel, use --out.
        try:
            sys.stdout.flush()
            sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.buffer.flush()
        except OSError as exc:
            # The reader stopped early (e.g. `| head`): not an error. On Windows that's EINVAL,
            # not a BrokenPipeError. Point stdout at devnull so Python's shutdown flush doesn't
            # raise again.
            if not isinstance(exc, BrokenPipeError) and exc.errno not in (
                errno.EPIPE,
                errno.EINVAL,
            ):
                raise
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        return
    path = out or Path(f"{state.event_id}-{'catalog' if all_sessions else 'plan'}.csv")
    try:
        write_csv(path, text)
    except OSError as exc:
        raise InputError(f"Couldn't write {path}: {exc.strerror or exc}") from exc
    console.print(f"Wrote {len(entries)} session(s) to {escape(str(path))}")


@app.command()
@handle_errors
def ics(
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Where to write .ics files.")
    ] = Path("calendar"),
    split_by_type: Annotated[
        bool, typer.Option("--split-by-type", help="One file per session type too.")
    ] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Use the cached schedule.")] = False,
) -> None:
    """Export your plan as calendar files that update in place when re-imported."""
    with _client() as client, _catalog() as cat:
        _require_catalog(cat)
        zone = _zone(cat)
        sched = _schedule(client, cat, refresh=not offline)
        items = _plan_items(cat, sched)
        sessions = cat.get_sessions(state.event_id, [i.key for i in items])
    entries = [(i, sessions.get(i.key)) for i in items]
    event_name = state.event_id

    files: dict[str, list] = {f"{event_name}-plan": entries}
    reserved = [e for e in entries if e[0].kinds & {"reserved", "personal"}]
    if reserved:
        files[f"{event_name}-reserved"] = reserved
    if split_by_type:
        for item, session in entries:
            if session and session.type:
                kind = session.type
            else:
                kind = "personal time" if "personal" in item.kinds else "other"
            files.setdefault(f"{event_name}-{slugify(kind)}", []).append((item, session))

    try:
        out_dir.mkdir(parents=True, exist_ok=True)  # a folder of calendars: create it
    except OSError as exc:
        raise InputError(f"Couldn't create {out_dir}: {exc.strerror or exc}") from exc
    for name, group in files.items():
        path = out_dir / f"{name}.ics"
        data = build_calendar(group, event_id=state.event_id, name=name, zone=zone)
        try:
            write_calendar(path, data)
        except OSError as exc:
            raise InputError(f"Couldn't write {path}: {exc.strerror or exc}") from exc
        console.print(f"Wrote {len(group)} event(s) to {escape(str(path))}")


# ---------------------------------------------------------------------------
# reservations
# ---------------------------------------------------------------------------


def _guarded_reservations(
    client: EventsClient,
    choices: list[Choice],
    *,
    fixed_items,
    travel: TravelTimes,
    on_round=lambda number, picks: None,
    locked: bool = False,
    **hooks,
):
    """A reserve run as both `rip reserve` and the app's Launch tab do it: only one at a time on
    this computer, with a journal of what's being sent written before each write. The journal
    is removed when the run ends; one left behind means a run was cut off (see
    `_note_cut_off_run`)."""
    journal = launch.Journal(state.event_id)
    lock = contextlib.nullcontext() if locked else launch.reservation_lock(state.event_id, 1.0)
    with lock:  # `locked`: the caller already holds it (the app takes it at the GO press)
        cooldown = launch.load_cooldown(state.event_id)
        left = math.ceil(cooldown.until - time.time())  # load_cooldown caps the end
        if left > 0:
            raise launch.LaunchError(
                f"Reservations weren't open a moment ago; wait {left} s before trying again."
                if cooldown.not_open
                else f"A reservation run just finished; wait {left} s (fair use: at most one "
                "run a minute)."
            )

        def record_then_announce(number: int, picks: list[tuple[Choice, Item]]) -> None:
            journal.record(number, [option.key for _, option in picks])
            on_round(number, picks)

        report = run_reservations(
            client,
            state.event_id,
            choices,
            fixed_items=fixed_items,
            travel=travel,
            on_round=record_then_announce,
            **hooks,
        )
        journal.clear()
        if report.rounds:  # a run that sent nothing costs no cooldown
            launch.save_cooldown(
                state.event_id,
                launch.Cooldown(
                    *launch.next_cooldown(
                        time.time(), report.stopped, launch.live_strikes(cooldown, time.time())
                    )
                ),
            )
    return report


def _note_cut_off_run(cat: Catalog, client: EventsClient) -> list[str]:
    """If an earlier run was cut off mid-write, say what it sent and what your schedule now
    shows, then forget it. Returns the codes that are not on the schedule. While a run is in
    progress (it holds the lock), its journal is live: it's left alone."""
    journal = launch.Journal(state.event_id)
    if not journal.path.exists():
        return []
    try:
        with launch.reservation_lock(state.event_id, wait=0.5):
            # Read under the lock: a run that died a moment ago has finished writing.
            schedule = client.get_schedule(state.event_id)
            cat.save_schedule(state.event_id, schedule)
            return _report_journal(cat, schedule, journal)
    except launch.LaunchError:
        err.print("[dim]A reservation run is in progress on this computer.[/]")
        return []


def _report_journal(cat: Catalog, schedule: Schedule, journal: launch.Journal) -> list[str]:
    found = journal.load()
    if not found:
        err.print(
            "[yellow]An earlier reservation run was cut off, and its record couldn't be "
            "read.[/] Check `rip schedule` for what went through."
        )
        journal.clear()
        return []
    if not found["sent"]:
        journal.clear()
        return []
    reserved = set(schedule.reserved)
    names = {}
    for session_id in found["sent"]:
        try:
            names[session_id] = cat.resolve(state.event_id, session_id).code
        except CatalogError:
            names[session_id] = session_id
    landed = [names[s] for s in found["sent"] if s in reserved]
    missing = [names[s] for s in found["sent"] if s not in reserved]
    err.print(
        "[yellow]An earlier reservation run was cut off while sending.[/] Your schedule now "
        + (f"shows {escape(', '.join(landed))}" if landed else "shows none of what it sent")
        + (f"; not reserved: {escape(', '.join(missing))}." if missing else ".")
    )
    journal.clear()
    return missing


def _one_line(text: str) -> str:
    """For a Markdown list item: a line break in API text would start a new line."""
    return " ".join(text.split())


def _write_output(path: Path, text: str, encoding: str) -> None:
    """Write a file you asked for, whole or not at all (see export_ics.write_file_atomic: a
    symlink at `path` is replaced, never written through)."""
    try:
        write_file_atomic(Path(path), text.encode(encoding))
    except OSError as exc:
        raise InputError(f"Couldn't write {path}: {exc.strerror or exc}") from exc


def _reservation_choices(cat: Catalog, codes: list[str] | None) -> list[Choice]:
    """Your picks in rank order, as reservation choices.

    A walk-up pick (no reservation taken) becomes a `walk_up` choice: it's never sent, its
    backups are *not* reserved in its place, and it keeps its time free from lower picks only.
    Walk-up backups are simply dropped.
    """
    zone = _zone(cat)
    if codes:
        ordered = [(rank, cat.resolve(state.event_id, c), []) for rank, c in enumerate(codes, 1)]
    else:
        items = cat.plan_items(state.event_id)
        sessions = cat.get_sessions(state.event_id, [i.session_id for i in items])
        ordered = [
            (
                p.rank,
                sessions[p.session_id],
                [
                    sessions[b.session_id]
                    for b in items
                    if b.backup_for == p.session_id and b.session_id in sessions
                ],
            )
            for p in items
            if p.rank and p.session_id in sessions
        ]
    choices: list[Choice] = []
    for rank, primary, backups in ordered:
        if primary.is_walk_up:
            choices.append(Choice(rank, [item_from_session(primary, zone, "ranked")], walk_up=True))
            continue
        options = [
            item_from_session(s, zone, "ranked") for s in (primary, *backups) if not s.is_walk_up
        ]
        choices.append(Choice(rank, options))
    return choices


def _fixed_items(cat: Catalog):
    """What reservations must not collide with: your reservations and personal time."""
    zone = _zone(cat)
    warned: set[str] = set()

    def fixed(schedule: Schedule) -> list[Item]:
        sessions = cat.get_sessions(state.event_id, schedule.reserved)
        missing = [sid for sid in schedule.reserved if sid not in sessions and sid not in warned]
        if missing:
            warned.update(missing)
            err.print(
                f"[yellow]{len(missing)} reservation(s) aren't in the local catalog, so clashes "
                "with them can't be checked. Run `rip sync`.[/]"
            )
        items = [item_from_session(sessions[s], zone, "reserved") for s in sessions]
        return items + [item_from_personal_time(pt) for pt in schedule.personal_time]

    return fixed


def _warn_if_stale(cat: Catalog) -> None:
    last = cat.last_sync(state.event_id)
    if last and datetime.now(UTC) - datetime.fromisoformat(last) > timedelta(hours=24):
        err.print(f"[yellow]Catalog last synced {last}. Times may have changed: run `rip sync`.[/]")


def _print_report(report, choices: list[Choice], zone: ZoneInfo | None) -> None:
    for item in report.reserved:
        console.print(
            f"  [green]✓[/] {escape(item.code)}  {_when(item, zone)}  {escape(item.title)}"
        )
    for item in report.not_visible:
        console.print(
            f"    [dim]{escape(item.code)} was accepted by the API but doesn't show in your "
            "schedule yet; check `rip schedule` in a minute.[/]"
        )
    for item in report.in_doubt:
        console.print(
            f"  [yellow]?[/] {escape(item.code)}: may or may not be reserved, so its backups "
            "weren't tried; check `rip schedule` before trying again"
        )
    names = {o.key: o.code for c in choices for o in c.options}
    for session_id, reason in report.refused.items():
        console.print(f"  [red]✗[/] {escape(names.get(session_id, session_id))}: {escape(reason)}")
    for choice in report.unfilled:
        console.print(
            f"  [yellow]–[/] #{choice.rank} {escape(choice.primary.code)}: nothing reserved"
        )
    messages = {
        "closed": "[yellow]Reservations aren't open through the API right now.[/] Anything "
        "listed above as reserved went through; nothing else changed.",
        "throttled": "[yellow]Rate limit reached. Wait a minute and run it again.[/]",
        "round_limit": "[yellow]Stopped after the maximum number of rounds; run it again to "
        "continue.[/]",
        "cancelled": "[yellow]Cancelled before the next round.[/] Anything listed above as "
        "reserved went through; nothing else was sent.",
        "error": f"[red]Stopped: {escape(report.error or 'the API failed')}[/] Anything listed "
        "above as reserved went through. Check `rip schedule` before running it again.",
    }
    if report.stopped:
        console.print(messages[report.stopped])
    console.print(
        f"[green]{len(report.reserved)} newly reserved.[/] Run `rip schedule` to see your calendar."
    )


@app.command()
@handle_errors
def reserve(
    codes: Annotated[
        list[str] | None,
        typer.Argument(help="Sessions to reserve, in priority order. Default: your ranking."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be reserved; change nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Reserve your ranked picks in order, trying backups when a pick is full.

    Reads your schedule first, reserves in rank order, then reads it back to confirm; refused
    picks fall through to their backups. For re:Invent 2026 the API accepts reservations from
    Oct 8 (the website opens Oct 6). Before then this reports "not open" and changes nothing.

    Exit status: 0 when every pick is reserved, 2 when it finished with picks unfilled, and
    1 when it stopped early (not open, rate limited, or an API failure).
    """
    with _client(need_auth=True) as client, _catalog() as cat:
        _require_catalog(cat)
        _warn_if_stale(cat)
        zone = _zone(cat)
        travel = TravelTimes.load(state.event_id)
        choices = _reservation_choices(cat, codes)
        for choice in choices:
            if choice.walk_up:
                console.print(
                    f"[dim]#{choice.rank} {escape(choice.primary.code)} is walk-up (no "
                    "reservation taken); keeping its time free from lower picks and not "
                    "reserving its backups.[/]"
                )
        if not any(not c.walk_up for c in choices):
            console.print("Nothing to reserve. Rank sessions with `rip rank set CODE 1`.")
            return

        schedule = client.get_schedule(state.event_id)
        cat.save_schedule(state.event_id, schedule)
        _note_cut_off_run(cat, client)
        fixed_items = _fixed_items(cat)
        calendar = fixed_items(schedule)
        held = set(schedule.reserved)
        first_round = plan_round(choices, calendar, held, set(), travel)

        table = Table("Rank", "Try", "Code", "When", "Title")
        first_keys = {o.key for _, o in first_round}
        for choice, option in first_round:
            role = "pick" if option is choice.primary else "[yellow]backup[/]"
            table.add_row(
                f"#{choice.rank}",
                role,
                escape(option.code),
                _when(option, zone),
                escape(option.title),
            )
        console.print(table)
        picked = {o.key for _, o in first_round}
        for issue in find_issues(merge_items([*calendar, *(o for _, o in first_round)]), travel):
            if issue.kind == "tight" and picked & {issue.first.key, issue.second.key}:
                err.print(
                    f"  [yellow]⚠[/] {escape(issue.describe())}{_walk_note(issue, travel)} "
                    "[dim](will still be reserved)[/]"
                )
        # Rank-aware preview: a pick is judged against the calendar plus higher walk-ups.
        committed = list(calendar)
        later: list[str] = []
        unsatisfied = False
        for choice in sorted(choices, key=lambda c: c.rank):
            if choice.walk_up:
                committed.append(choice.primary)
                continue
            if any(o.key in held for o in choice.options):
                console.print(f"[dim]#{choice.rank} already reserved.[/]")
                continue
            unsatisfied = True
            fits = [o for o in choice.options if not any(blocks(o, k, travel) for k in committed)]
            if not fits:
                console.print(
                    f"[yellow]#{choice.rank} {escape(choice.primary.code)}: none of its options "
                    "fit around your reservations, personal time and higher walk-up picks; "
                    "skipping.[/]"
                )
            later += [o.code for o in fits if o.key not in first_keys]
        if later:
            console.print(
                "If something above is refused, these may be tried next, in rank order: "
                + ", ".join(escape(code) for code in dict.fromkeys(later))
            )
        if not first_round:
            console.print("Nothing new to reserve.")
            if unsatisfied:
                raise typer.Exit(2)
            return
        if dry_run:
            console.print(
                f"[dim]Dry run: nothing was reserved ({len(first_round)} would be tried first).[/]"
            )
            return
        _confirm(
            f"Reserve {len(first_round)} session(s) now on your {state.event_id} schedule, "
            "then the alternatives listed if any are refused?",
            yes,
        )

        def on_round(number: int, picks: list[tuple[Choice, Item]]) -> None:
            if number > 1:
                codes_ = ", ".join(escape(o.code) for _, o in picks)
                console.print(f"Round {number}: trying {codes_}")

        try:
            with console.status("Reserving…"):
                report = _guarded_reservations(
                    client, choices, fixed_items=fixed_items, travel=travel, on_round=on_round
                )
        except KeyboardInterrupt:
            err.print(
                "\n[yellow]Interrupted. A reservation may have been in flight: check "
                "`rip schedule` before running this again.[/]"
            )
            raise typer.Exit(130) from None
        if report.schedule is not None and not report.readback_failed:
            cat.save_schedule(state.event_id, report.schedule)
            _refresh_feed(client, cat, report.schedule)

    _print_report(report, choices, zone)
    if report.stopped:
        raise typer.Exit(1)
    if report.unfilled:
        raise typer.Exit(2)


@app.command()
@handle_errors
def cancel(
    code: Annotated[str, typer.Argument(help="Session code whose reservation to cancel.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Cancel one reservation, freeing the seat for someone else."""
    with _client(need_auth=True) as client, _catalog() as cat:
        s = cat.resolve(state.event_id, code)
        schedule = client.get_schedule(state.event_id)
        if s.session_id not in schedule.reserved:
            console.print(f"{escape(s.code)} isn't reserved on your schedule.")
            cat.save_schedule(state.event_id, schedule)
            return
        _confirm(f"Cancel your reservation for {s.code} ({s.title})?", yes)
        failure: ApiError | None = None
        try:
            client.cancel_reservation(state.event_id, s.session_id)
        except ApiError as exc:  # it may still have gone through: the readback decides
            failure = exc
        after = client.get_schedule(state.event_id)
        cat.save_schedule(state.event_id, after)
        _refresh_feed(client, cat, after)
        if s.session_id in after.reserved:
            raise failure or ApiError(f"{s.code} still shows as reserved. Check `rip schedule`.")
        console.print(f"[green]Cancelled {escape(s.code)}.[/]")


# ---------------------------------------------------------------------------
# personal time
# ---------------------------------------------------------------------------

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_FULL_NAMES = dict(
    zip(
        _DAY_NAMES,
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        strict=True,
    )
)


def _event_day(cat: Catalog, day: str) -> date:
    """A weekday (mon…sun) during the event, or a YYYY-MM-DD date."""
    text = day.strip().lower()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    # "tue", "tues" or "tuesday", but not just any word starting with "tue" ("monkey").
    full = _DAY_FULL_NAMES.get(text[:3], "")
    if len(text) < 3 or not full.startswith(text):
        raise InputError(f"{day!r} isn't a day: use mon…sun or YYYY-MM-DD.")
    event = cat.get_event(state.event_id)
    if event is None:
        raise CatalogError(f"Unknown event dates for {state.event_id}. Run `rip sync` first.")
    zone = _zone(cat)  # the zone times are entered in, so days and times agree
    first = datetime.fromisoformat(event.start_date).astimezone(zone).date()
    last = datetime.fromisoformat(event.end_date).astimezone(zone).date()
    wanted = _DAY_NAMES.index(text[:3])
    candidate = first
    while candidate <= last:
        if candidate.weekday() == wanted:
            return candidate
        candidate += timedelta(days=1)
    raise InputError(f"The event ({first} to {last}) has no {day}.")


def _clock(value: str) -> tuple[int, int]:
    text = value.strip().lower().replace(" ", "")
    if text.isascii() and text.isdigit() and len(text) != 4:
        # strptime("%H%M") would read "10" as 01:00 and "130" as 13:00: refuse, don't guess.
        raise InputError(f"{value!r} is ambiguous: use HH:MM (e.g. 10:00 or 13:30).")
    for fmt in ("%H:%M", "%I:%M%p", "%I%p", "%H%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.hour, parsed.minute
        except ValueError:
            continue
    raise InputError(f"{value!r} isn't a time: use 24-hour HH:MM (12:30) or 12:30pm.")


def _local(cat: Catalog, day: date, clock: str) -> datetime:
    zone = _zone(cat)
    if zone is None:
        raise CatalogError("Unknown event timezone. Run `rip sync` first.")
    hour, minute = _clock(clock)
    return datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute), tzinfo=zone)


MAX_BLOCK = timedelta(hours=24)


def _end_after(start: datetime, end_day: date, end_clock: str, cat: Catalog) -> datetime:
    """The end time on `end_day`, or the next day when it's earlier than the start (a block
    that runs past midnight, e.g. 23:00-01:00). An end equal to the start is left alone, so it's
    reported as a mistake rather than read as a 24-hour block."""
    end = _local(cat, end_day, end_clock)
    if end < start:
        end += timedelta(days=1)
    return end


def _entry_from(
    start: datetime, end: datetime, title: str, note: str | None, where: str | None
) -> PersonalTimeInput:
    if end - start > MAX_BLOCK:
        raise InputError("A personal time block can be at most 24 hours long.")
    try:
        return PersonalTimeInput.from_local(
            start, end, title=title, description=note, location=where
        )
    except (ValueError, ValidationError) as exc:
        detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        raise InputError(f"Can't use that time block: {detail}") from None


def _sorted_personal(schedule: Schedule) -> list[PersonalTime]:
    return sorted(
        schedule.personal_time, key=lambda pt: (pt.start_date_time, pt.title, pt.personal_time_id)
    )


def _find_personal(schedule: Schedule, ref: str, *, allow_number: bool = True) -> PersonalTime:
    """By the number `rip time ls` shows, or the entry ID (or a unique start of it).

    Numbers can shift if entries change between `ls` and now, so they're only accepted when
    the user will see and confirm the entry (not with --yes).
    """
    entries = _sorted_personal(schedule)
    by_id = [pt for pt in entries if pt.personal_time_id == ref]
    if ref.isascii() and ref.isdigit() and not by_id:
        if not allow_number:
            prefixed = [pt for pt in entries if pt.personal_time_id.startswith(ref)]
            if len(ref) >= 4 and len(prefixed) == 1:
                return prefixed[0]  # an ID prefix that happens to be all digits
            raise InputError(
                "With --yes, give the entry ID from `rip time ls` rather than its number: "
                "numbers can shift if entries change."
            )
        if 1 <= int(ref) <= len(entries):
            return entries[int(ref) - 1]
    matches = [pt for pt in entries if pt.personal_time_id == ref]
    if not matches and len(ref) >= 4:
        matches = [pt for pt in entries if pt.personal_time_id.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise InputError(f"No personal time {ref!r}. See `rip time ls`.")
    raise InputError(f"{ref!r} matches several entries; use more of the ID.")


def _describe_block(pt: PersonalTime | PersonalTimeInput, zone: ZoneInfo | None) -> str:
    utc = ZoneInfo("UTC")
    where = f" · {pt.location}" if pt.location else ""
    try:
        start = datetime.fromisoformat(pt.start_date_time).replace(tzinfo=utc)
        end = datetime.fromisoformat(pt.end_date_time).replace(tzinfo=utc)
    except (TypeError, ValueError):  # a malformed time from the API: still list it
        return f"(time unreadable) · {pt.title}{where}"
    if zone is not None:
        start, end = start.astimezone(zone), end.astimezone(zone)
    return f"{start:%a %b} {start.day} {start:%H:%M}–{end:%H:%M} · {pt.title}{where}"


def _block_item(entry: PersonalTimeInput, key: str) -> Item:
    utc = ZoneInfo("UTC")
    return Item(
        key=key,
        code="PERSONAL",
        title=entry.title,
        start=datetime.fromisoformat(entry.start_date_time).replace(tzinfo=utc),
        end=datetime.fromisoformat(entry.end_date_time).replace(tzinfo=utc),
        venue=entry.location,
        room=None,
        kinds={"personal"},
    )


@time_app.command("ls")
@handle_errors
def time_ls(
    offline: Annotated[bool, typer.Option("--offline", help="Use the cached schedule.")] = False,
) -> None:
    """List your personal time, numbered for `rip time edit` and `rip time rm`."""
    with _client() as client, _catalog() as cat:
        schedule = _schedule_or_cache(client, cat, offline=offline)
        zone = _zone(cat)
    entries = _sorted_personal(schedule)
    if not entries:
        console.print(
            "No personal time yet. Add some with "
            '`rip time add "Lunch" --day tue --start 12:00 --end 13:00`.'
        )
        return
    table = Table("#", "When", "What", "Where", "ID")
    for n, pt in enumerate(entries, 1):
        when = _describe_block(pt, zone).split(" · ", 1)[0]
        table.add_row(
            str(n),
            when,
            escape(pt.title),
            escape(pt.location or ""),
            escape(pt.personal_time_id[:8]),
        )
    console.print(table)


@time_app.command("add")
@handle_errors
def time_add(
    title: Annotated[str, typer.Argument(help='What it is, e.g. "Team lunch".')],
    day: Annotated[
        str, typer.Option("--day", "-d", help="mon…fri during the event, or YYYY-MM-DD.")
    ],
    start: Annotated[str, typer.Option("--start", "-s", help="Local start time, e.g. 12:00.")],
    end: Annotated[str, typer.Option("--end", help="Local end time, e.g. 13:00.")],
    where: Annotated[
        str | None, typer.Option("--where", "-w", help="Location (free text).")
    ] = None,
    note: Annotated[
        str | None, typer.Option("--note", help="Longer description (defaults to the title).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Add a block of personal time to your event schedule (times in the event's timezone)."""
    with _client(need_auth=True) as client, _catalog() as cat:
        start_at = _local(cat, _event_day(cat, day), start)
        entry = _entry_from(
            start_at, _end_after(start_at, start_at.date(), end, cat), title, note, where
        )
        zone = _zone(cat)
        schedule = client.get_schedule(state.event_id)
        before_ids = {pt.personal_time_id for pt in schedule.personal_time}
        twins = [pt for pt in schedule.personal_time if entry.same_as(pt)]
        if twins:
            same = any(
                pt.location == entry.location and pt.description == entry.description
                for pt in twins
            )
            console.print(
                "That exact block is already on your schedule."
                if same
                else "A block with the same title and times is already on your schedule; "
                "change it with `rip time edit`."
            )
            cat.save_schedule(state.event_id, schedule)
            return
        console.print(f"  {escape(_describe_block(entry, zone))}")
        for line in _item_warnings(cat, schedule, [_block_item(entry, "personal:new")]):
            err.print(line)
        _confirm("Add this personal time to your schedule?", yes)
        unknown: WriteOutcomeUnknownError | None = None
        try:
            client.create_personal_time(state.event_id, entry)
        except WriteOutcomeUnknownError as exc:
            unknown = exc  # never re-sent: re-sending would add a second entry
        after = client.get_schedule(state.event_id)
        cat.save_schedule(state.event_id, after)
        _refresh_feed(client, cat, after)
    if any(
        entry.same_as(pt) and pt.personal_time_id not in before_ids for pt in after.personal_time
    ):
        console.print("[green]Added.[/] See `rip time ls`.")
    elif unknown is not None:
        raise ApiError(
            "The API didn't confirm the new entry and it isn't on your schedule yet. Check "
            "`rip time ls` in a minute before adding it again, to avoid a duplicate."
        )
    else:
        raise ApiError(
            "The API accepted the entry but it isn't on your schedule. Check `rip time ls`."
        )


@time_app.command("edit")
@handle_errors
def time_edit(
    ref: Annotated[str, typer.Argument(help="The number from `rip time ls`, or the entry ID.")],
    title: Annotated[str | None, typer.Option("--title", help="New title.")] = None,
    day: Annotated[str | None, typer.Option("--day", "-d", help="New day.")] = None,
    start: Annotated[
        str | None, typer.Option("--start", "-s", help="New local start time.")
    ] = None,
    end: Annotated[str | None, typer.Option("--end", help="New local end time.")] = None,
    where: Annotated[str | None, typer.Option("--where", "-w", help="New location.")] = None,
    note: Annotated[str | None, typer.Option("--note", help="New description.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Change a personal time entry. Anything you don't pass stays as it is."""
    with _client(need_auth=True) as client, _catalog() as cat:
        zone = _zone(cat)
        schedule = client.get_schedule(state.event_id)
        current = _find_personal(schedule, ref, allow_number=not yes)
        utc = ZoneInfo("UTC")
        old_start = datetime.fromisoformat(current.start_date_time).replace(tzinfo=utc)
        old_end = datetime.fromisoformat(current.end_date_time).replace(tzinfo=utc)
        if zone is not None:
            old_start, old_end = old_start.astimezone(zone), old_end.astimezone(zone)
        new_day = _event_day(cat, day) if day else old_start.date()
        if start:
            new_start = _local(cat, new_day, start)
        else:
            new_start = old_start.replace(year=new_day.year, month=new_day.month, day=new_day.day)
        if end:
            new_end = _end_after(new_start, new_start.date(), end, cat)
        else:
            new_end = new_start + (old_end - old_start)  # moving the start keeps the length
        entry = _entry_from(
            new_start,
            new_end,
            title or current.title,
            note if note is not None else current.description,
            where if where is not None else current.location,
        )
        console.print(f"  before: {escape(_describe_block(current, zone))}")
        console.print(f"  after:  {escape(_describe_block(entry, zone))}")
        key = f"personal:{current.personal_time_id}"
        for line in _item_warnings(
            cat, schedule, [_block_item(entry, key + ":edited")], skip_keys=frozenset({key})
        ):
            err.print(line)
        _confirm("Save this change?", yes)
        with contextlib.suppress(WriteOutcomeUnknownError):  # the readback below decides
            client.update_personal_time(state.event_id, current.personal_time_id, entry)
        after = client.get_schedule(state.event_id)
        cat.save_schedule(state.event_id, after)
        _refresh_feed(client, cat, after)
    if any(
        pt.personal_time_id == current.personal_time_id and entry.same_as(pt)
        for pt in after.personal_time
    ):
        console.print(f"[green]Saved:[/] {escape(_describe_block(entry, zone))}")
    else:
        raise ApiError("The change doesn't show on your schedule. Check `rip time ls`.")


@time_app.command("rm")
@handle_errors
def time_rm(
    ref: Annotated[str, typer.Argument(help="The number from `rip time ls`, or the entry ID.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Remove a personal time entry."""
    with _client(need_auth=True) as client, _catalog() as cat:
        zone = _zone(cat)
        schedule = client.get_schedule(state.event_id)
        current = _find_personal(schedule, ref, allow_number=not yes)
        _confirm(f"Remove {_describe_block(current, zone)}?", yes)
        client.delete_personal_time(state.event_id, current.personal_time_id)  # 404 = already gone
        after = client.get_schedule(state.event_id)
        cat.save_schedule(state.event_id, after)
        _refresh_feed(client, cat, after)
    if any(pt.personal_time_id == current.personal_time_id for pt in after.personal_time):
        raise ApiError("It still shows on your schedule. Check `rip time ls`.")
    console.print(f"[green]Removed:[/] {escape(_describe_block(current, zone))}")


# ---------------------------------------------------------------------------
# live calendar feed
# ---------------------------------------------------------------------------

FEED_REFRESH_HOURS = 1
FEED_WARN_EVERY_SECONDS = 3600


def _feed_ics(
    cat: Catalog, schedule: Schedule, *, personal: bool, ranked: bool
) -> tuple[bytes, int]:
    """The feed's calendar: reserved and favorite sessions, plus the opt-in extras."""
    allowed = (
        {"reserved", "favorite"}
        | ({"personal"} if personal else set())
        | ({"ranked"} if ranked else set())
    )
    # Keep only the allowed kinds on each item too: the event description says "On your list
    # as: …", which would otherwise reveal ranked status on a favorite.
    items = [
        dataclasses.replace(i, kinds=i.kinds & allowed)
        for i in _plan_items(cat, schedule, quiet=True)
        if i.kinds & allowed
    ]
    sessions = cat.get_sessions(state.event_id, [i.key for i in items])
    entries = [(i, sessions.get(i.key)) for i in items]
    event = cat.get_event(state.event_id)
    name = f"{event.name if event else state.event_id} (reinvent-planner)"
    ics = build_calendar(
        entries,
        event_id=state.event_id,
        name=name,
        zone=_zone(cat),
        refresh_hours=FEED_REFRESH_HOURS,
    )
    return ics, sum(1 for i in items if i.start is not None)


def _refresh_feed(
    client: EventsClient | None,
    cat: Catalog,
    schedule: Schedule | None = None,
    *,
    from_read: bool = False,
) -> None:
    """Update the published feed if what it shows has changed since it was last published.

    Compares a hash of the rendered feed with the one saved after the last successful update,
    so it updates for any visible change (a new reservation, a session moved to another room)
    and never for nothing, and a failed update is retried next time. Best effort: it never
    fails the command, and never creates a gist; if the gist is gone or not writable,
    automatic updates pause until `rip calendar publish`. On read commands a repeating failure
    is reported at most once an hour.
    """
    feed = None
    try:
        feed = calendar_feed.load(state.event_id)
        if feed is None or feed.deleted or not feed.auto:
            return
        if schedule is None:
            if client is None:
                cached = cat.load_schedule(state.event_id)
                schedule = cached[0] if cached else Schedule()
            else:
                schedule = _schedule_or_cache(client, cat, refresh_feed=False)
        ics, _count = _feed_ics(
            cat, schedule, personal=feed.include_personal, ranked=feed.include_ranked
        )
        digest = calendar_feed.content_hash(ics)
        if digest == feed.published_hash:
            return  # subscribers already see exactly this
        calendar_feed.update(feed, ics)
        feed.published_hash, feed.last_failure = digest, 0.0
        calendar_feed.save(state.event_id, feed)
        err.print("[dim]Calendar feed updated.[/]")
    except calendar_feed.FeedGone as exc:
        with contextlib.suppress(Exception):
            feed.auto = False
            calendar_feed.save(state.event_id, feed)
        err.print(f"[yellow]{escape(str(exc))} Automatic feed updates are paused.[/]")
    except Exception as exc:
        # The feed is secondary: never fail the command for it. Expected failures are throttled
        # on reads; anything else is a bug and is always named, never hidden.
        expected = isinstance(
            exc, calendar_feed.FeedError | ApiError | CatalogError | OSError | httpx.HTTPError
        )
        now = time.time()
        if not expected:
            err.print(
                f"[yellow]Couldn't update your calendar feed (unexpected {type(exc).__name__}: "
                f"{escape(str(exc))}). Please report this.[/]"
            )
            return
        if from_read and feed is not None and now - feed.last_failure < FEED_WARN_EVERY_SECONDS:
            return
        with contextlib.suppress(Exception):
            if feed is not None:
                feed.last_failure = now
                calendar_feed.save(state.event_id, feed)
        err.print(f"[yellow]Couldn't update your calendar feed: {escape(str(exc))}[/]")


def _refresh_feed_if_ranked() -> None:
    """Ranking is local, so rank changes only reach a feed that shares ranked picks."""
    try:
        feed = calendar_feed.load(state.event_id)
    except calendar_feed.FeedError:
        return
    if feed is not None and feed.include_ranked and feed.auto and not feed.deleted:
        with _catalog() as cat:
            _refresh_feed(None, cat)


def _feed_description() -> str:
    return f"{state.event_id} schedule, published by reinvent-planner"


@calendar_app.command("publish")
@handle_errors
def calendar_publish(
    personal: Annotated[
        bool | None,
        typer.Option(
            "--personal/--no-personal", help="Include your personal time (off by default)."
        ),
    ] = None,
    ranked: Annotated[
        bool | None,
        typer.Option(
            "--ranked/--no-ranked", help="Include your private ranked picks (off by default)."
        ),
    ] = None,
    auto: Annotated[
        bool | None,
        typer.Option(
            "--auto/--no-auto",
            help="Update the feed after every schedule change (on by default).",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    expected_login: Annotated[
        str | None,
        typer.Option(
            "--expected-login",
            hidden=True,
            help="Refuse to create a gist unless `gh` is signed into this GitHub account.",
        ),
    ] = None,
) -> None:
    """Publish (or update) your plan as a calendar feed you can subscribe to.

    It goes into a secret GitHub Gist under the account the `gh` CLI is signed into. Anyone
    with the link can read it; nobody can find it without the link.
    """
    with _client() as client, _catalog() as cat:
        _require_catalog(cat)
        existing = calendar_feed.load(state.event_id)
        carried: list[str] = []
        if existing is not None and existing.deleted:
            carried, existing = list(existing.pending_delete), None
        opts = calendar_feed.Feed(
            gist_id=existing.gist_id if existing else "0" * 32,
            owner=existing.owner if existing else "x",
            filename=existing.filename if existing else "",
            include_personal=personal
            if personal is not None
            else bool(existing and existing.include_personal),
            include_ranked=ranked
            if ranked is not None
            else bool(existing and existing.include_ranked),
            auto=auto if auto is not None else (existing.auto if existing else True),
            pending_delete=list(existing.pending_delete) if existing else carried,
        )
        schedule = _schedule_or_cache(client, cat, refresh_feed=False)
        ics, count = _feed_ics(
            cat, schedule, personal=opts.include_personal, ranked=opts.include_ranked
        )
        what = ["reserved sessions", "favorites"]
        what += ["personal time"] if opts.include_personal else []
        what += ["ranked picks"] if opts.include_ranked else []
        widened = existing is not None and (
            (opts.include_personal and not existing.include_personal)
            or (opts.include_ranked and not existing.include_ranked)
        )
        narrowed = existing is not None and (
            (existing.include_personal and not opts.include_personal)
            or (existing.include_ranked and not opts.include_ranked)
        )

        def confirm_new_gist(reason: str) -> str:
            login = calendar_feed.github_login()
            if expected_login is not None and login != expected_login:
                # The app confirmed one account (yes=True); `gh` has since switched.
                raise calendar_feed.FeedError(
                    f"`gh` is now signed into {login}, not {expected_login} as confirmed; "
                    "nothing new was published."
                )
            if existing is not None and login != existing.owner:
                console.print(
                    f"[yellow]Note: `gh` is signed into [bold]{escape(login)}[/], but your feed "
                    f"was on [bold]{escape(existing.owner)}[/].[/]"
                )
            console.print(
                f"{reason}This puts {count} event(s) ({', '.join(what)}) in a [bold]new secret "
                f"GitHub Gist[/] on the GitHub account [bold]{escape(login)}[/]. Anyone with the "
                "link can read it and its revision history; it isn't listed or searchable."
            )
            _confirm("Publish?", yes)
            return login

        def new_gist(reason: str, previous) -> calendar_feed.Feed:
            login = confirm_new_gist(reason)
            created = calendar_feed.create(
                state.event_id, ics, description=_feed_description(), settings=opts
            )
            feed = calendar_feed.claim(state.event_id, created, previous)
            if feed.gist_id == created.gist_id:  # ours won: record what it now shows
                feed.published_hash = calendar_feed.content_hash(ics)
                calendar_feed.save(state.event_id, feed)
            if previous is not None and login != previous.owner:
                # The old gist is still online on the other account: track it, and say so.
                entry = f"{previous.owner}/{previous.gist_id}"
                if entry not in feed.pending_delete:
                    feed.pending_delete.append(entry)
                    calendar_feed.save(state.event_id, feed)
                console.print(
                    f"[yellow]Your old feed is still online on {escape(previous.owner)}: delete it "
                    f"at https://gist.github.com/{entry} (or sign `gh` into that account and run "
                    "`rip calendar publish` again).[/]"
                )
            return feed

        def delete_old(old: calendar_feed.Feed, feed: calendar_feed.Feed) -> None:
            """Delete the previous gist; if that fails, say so and remember to retry."""
            try:
                calendar_feed.unpublish(old)
                console.print(
                    "[yellow]The old link no longer works: its gist and history are deleted.[/]"
                )
            except calendar_feed.FeedError as exc:
                entry = f"{old.owner}/{old.gist_id}"
                if entry in feed.pending_delete:
                    return  # already queued and reported (e.g. it's on another account)
                feed.pending_delete.append(entry)
                calendar_feed.save(state.event_id, feed)
                console.print(
                    f"[yellow]Couldn't delete the old gist (with its history): {escape(str(exc))}\n"
                    f"Delete it at https://gist.github.com/{old.owner}/{old.gist_id} , or run "
                    "`rip calendar publish` again later to retry.[/]"
                )

        pending_before = set(opts.pending_delete)
        feed = existing
        if existing is None:
            feed = new_gist("", None)
        elif narrowed:
            # A gist keeps every revision, so sharing less needs a fresh gist; the old one (with
            # its history) is then deleted.
            feed = new_gist("Sharing less needs a new link. ", existing)
            delete_old(existing, feed)
        else:
            switched = calendar_feed.github_login() != existing.owner if widened else False
            if widened and not switched:
                added = [w for w in what[2:]]
                console.print(
                    f"This adds {', '.join(added)} to your existing feed (same link) on "
                    f"[bold]{escape(existing.owner)}[/]. Anyone with the link can read it and "
                    "its history."
                )
                _confirm("Share more?", yes)
            try:
                if switched:
                    raise calendar_feed.FeedGone("signed into another account")
                calendar_feed.update(existing, ics)
                existing.include_personal = opts.include_personal
                existing.include_ranked = opts.include_ranked
                existing.auto = opts.auto
                existing.published_hash = calendar_feed.content_hash(ics)
                existing.last_failure = 0.0
                calendar_feed.save(state.event_id, existing)
            except calendar_feed.FeedGone:
                feed = new_gist(
                    "Your feed's gist is gone or not writable by this account. ", existing
                )
        if pending_before:
            left = calendar_feed.retry_pending(feed, only=pending_before)
            calendar_feed.save(state.event_id, feed)
            for entry in left:
                console.print(
                    f"[yellow]An old gist still needs deleting: https://gist.github.com/{entry}[/]"
                )
    verb = "Updated" if existing and existing.url == feed.url else "Published"
    console.print(f"[green]{verb} {count} event(s).[/] Subscribe with this link:\n  {feed.url}")
    if existing is None or existing.url != feed.url:
        console.print(
            "[dim]Google Calendar: Other calendars → + → From URL (refreshes every 12–24 h).\n"
            f"Apple Calendar: File → New Calendar Subscription → {feed.webcal_url}\n"
            "Outlook: Add calendar → Subscribe from web.\n"
            + (
                "It updates itself after `rip reserve`, `fav`, `cancel` and `time` changes, and "
                "when `rip sync` or `rip schedule` finds changes made on the website."
                if feed.auto
                else "Run `rip calendar publish` again after changes."
            )
            + "[/]"
        )


@calendar_app.command("url")
@handle_errors
def calendar_url() -> None:
    """Show your feed's subscription link."""
    feed = calendar_feed.load(state.event_id)
    if feed is None or feed.deleted:
        console.print("No feed yet. Create one with `rip calendar publish`.")
        raise typer.Exit(1)
    console.print(feed.url)


@calendar_app.command("unpublish")
@handle_errors
def calendar_unpublish(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete the feed's gist. Subscribed calendars stop updating (and may show it as broken)."""
    feed = calendar_feed.load(state.event_id)
    if feed is None or feed.deleted:
        if feed is not None and calendar_feed.retry_pending(feed) == []:
            calendar_feed.forget(state.event_id)
            console.print("The remaining old gists are deleted.")
            return
        console.print("There's no published feed.")
        return
    _confirm(f"Delete the gist behind {feed.url}?", yes)
    calendar_feed.unpublish(feed)  # raises (keeping the record) unless the delete is confirmed
    left = calendar_feed.retry_pending(feed)
    if left:
        feed.deleted, feed.auto = True, False  # the feed is gone; only old gists remain queued
        calendar_feed.save(state.event_id, feed)
        for entry in left:
            console.print(
                f"[yellow]An old gist still needs deleting: https://gist.github.com/{entry}[/]"
            )
    else:
        calendar_feed.forget(state.event_id)
    console.print("[green]Deleted.[/] Remove the subscription from your calendar app too.")
