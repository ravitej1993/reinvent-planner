"""The More tab: calendar feed, exports, travel-time corrections and sign-out.

Everything that changes something runs in a thread worker, after a confirmation where the CLI
would ask one (then with yes=True, since the app can't answer the CLI's own prompt), and never
while the Launch tab is running reservations (nor, for GitHub and sign-out, while GO is about
to light). Reading the state shown here never touches the network; the sign-in check (a
keychain read) runs in a worker too.

Text from the catalog, the travel table or GitHub is shown as `Text`, never parsed as markup.
In presentation mode ("Hide email") gist IDs and links are hidden everywhere in this pane,
except in the box that "Show link" opens on request.
"""

from __future__ import annotations

import inspect
import io
import re
import sys
import threading
from collections.abc import Callable
from importlib import resources
from typing import Any, ClassVar

import httpx
import typer
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static

from .. import calendar_feed, cli, services
from ..api import ApiError
from ..auth import BUILDER_ID_PROFILE_URL, Auth, AuthError, config_dir
from ..catalog import CatalogError, SearchFilters
from ..export_csv import rows as csv_rows
from ..export_csv import to_csv
from ..export_ics import build_calendar
from ..launch import LaunchError
from ..models import Schedule, clean_text
from ..planner import TravelTableError, TravelTimes
from ..venues_measure import MeasureError
from .launch_pane import LaunchPane

MINUTES_RANGE = (1, 120)  # what the dialog accepts (the CLI allows up to 180)
EXPORT_PREFIX = "rip-export:"  # the delivery name of every export, to match Textual's events
# How long sign-out waits for the browser to end the Builder ID session (auth's default is 5
# minutes). The loopback ports stay taken meanwhile, so a sign-in would have to wait.
BUILDER_ID_WAIT_SECONDS = 120
HIDDEN = "(hidden)"
_GIST_URL = re.compile(r"(?:https?|webcal)://gist\.github(?:usercontent)?\.com/[^\s\"'<>]*", re.I)
_GIST_ID = re.compile(r"\b[0-9a-f]{20,64}\b")  # what calendar_feed accepts as a gist ID
# What cli.handle_errors turns into "Error: <message>".
_EXPECTED_ERRORS = (
    ApiError,
    AuthError,
    CatalogError,
    TravelTableError,
    cli.InputError,
    calendar_feed.FeedError,
    LaunchError,
    MeasureError,
)
_UNKNOWN: Any = object()  # the sign-in state before the first read finishes


def redact_gists(text: str) -> str:
    """`text` with every gist link and gist ID replaced by "(hidden)": anyone who has one can
    read the feed."""
    return _GIST_ID.sub(HIDDEN, _GIST_URL.sub(HIDDEN, text))


def run_quietly(fn: Callable[..., Any], /, **kwargs: Any) -> services.Outcome:
    """Like services.run, but a failure's whole message is the error.

    A CLI command's own handler (cli.handle_errors) prints its error wrapped to the capture's
    width, and services.run keeps only the last line: a long gh error would lose its start
    ("GitHub refused the request: ..."). So the undecorated command runs here, and the
    exception's own text is the error.
    """
    inner = inspect.unwrap(fn)
    with services.captured() as buffer:
        try:
            value, error = inner(**kwargs), None
        except typer.Exit as exc:
            value, error = None, None if exc.exit_code in (0, None) else "failed"
        except typer.Abort:
            value, error = None, "cancelled"
        except _EXPECTED_ERRORS as exc:
            value, error = None, clean_text(str(exc)).strip() or type(exc).__name__
        except httpx.HTTPError as exc:
            value, error = None, f"Network error: {clean_text(str(exc))}"
        except Exception as exc:
            value, error = None, f"Unexpected error: {clean_text(str(exc))}"
    output = buffer.getvalue()
    if error == "failed":  # the command printed its own reason and exited
        plain = Text.from_ansi(output).plain.strip()
        error = plain.splitlines()[-1] if plain else "failed"
    return services.Outcome(value=value, output=output, error=error)


def publish_as(expected_login: str, **options: Any) -> None:
    """`rip calendar publish --yes`, only while `gh` is still signed into the account the
    confirmation named: checked just before, and again by the CLI before it creates a gist."""
    login = calendar_feed.github_login()
    if login != expected_login:
        raise calendar_feed.FeedError(
            f"`gh` is now signed into {login}, not {expected_login} as you confirmed; nothing "
            "was published. Press Publish again to review."
        )
    inspect.unwrap(cli.calendar_publish)(yes=True, expected_login=expected_login, **options)


def builder_id_cancellable() -> bool:
    """Whether auth can stop waiting for the browser on request (a `cancel` event)."""
    return "cancel" in inspect.signature(Auth.end_builder_id_session).parameters


def end_builder_id_session(announce: Callable[[str], None], cancel: threading.Event) -> None:
    auth = cli._auth()
    options: dict[str, Any] = {"announce": announce, "timeout": BUILDER_ID_WAIT_SECONDS}
    if builder_id_cancellable():
        options["cancel"] = cancel
    auth.end_builder_id_session(**options)


def delivery_text(text: str, *, web: bool) -> str:
    """The text to give Textual's deliver_text so the file gets exactly `text`'s characters.

    Our CSV (csv module) and ICS (RFC 5545) files end lines with CRLF. Under textual-serve the
    text is encoded as it is, and so is a terminal save on macOS and Linux. But in a Windows
    terminal Textual writes the file in text mode, which turns every "\n" into "\r\n", so
    CRLF would become "\r\r\n": there the lines are handed over with "\n" and Windows puts
    the "\r" back. (A line break inside a quoted CSV cell becomes CRLF there too, which is
    what Excel on Windows expects anyway.)
    """
    if sys.platform == "win32" and not web:
        return text.replace("\r\n", "\n")
    return text


def _confirm_screen(question: str, details: Text | None = None):
    # Imported here: app.py imports this module, so a top-level import would be circular.
    from .app import ConfirmScreen

    return ConfirmScreen(question, details)


# -- exports (local only: the cached schedule, no network) --------------------------------


def _cached_schedule(cat) -> Schedule:
    cached = cat.load_schedule(cli.state.event_id)
    return cached[0] if cached else Schedule()


def csv_export_text(all_sessions: bool = False) -> tuple[str, str, int]:
    """(filename, text, rows) as `rip csv --offline [--all]` builds it. The text starts with a
    byte-order mark, like the CLI's files, so Excel shows accented names correctly; cells that
    could run as formulas are neutralised by export_csv."""
    event_id = cli.state.event_id
    with cli._catalog() as cat:
        cli._require_catalog(cat)
        zone = cli._zone(cat)
        schedule = _cached_schedule(cat)
        items = {
            i.key: i
            for i in cli._plan_items(cat, schedule, quiet=True)
            if "personal" not in i.kinds
        }
        ranks = {p.session_id: p.rank for p in cat.plan_items(event_id) if p.rank}
        if all_sessions:
            sessions = cat.search(event_id, SearchFilters(limit=None))
        else:
            found = cat.get_sessions(event_id, list(items))
            sessions = [found[k] for k in items if k in found]
    entries = [(items.get(s.session_id), s) for s in sessions]
    entries.sort(key=lambda e: (e[1].interval(zone) is None, e[1].interval(zone) or (), e[1].code))
    text = to_csv(csv_rows(entries, zone, ranks))
    filename = f"{event_id}-{'catalog' if all_sessions else 'plan'}.csv"
    return filename, "﻿" + text, len(entries)


def ics_export_text() -> tuple[str, str, int]:
    """(filename, text, events): your whole plan as one calendar file, as `rip ics --offline`
    writes its main file (reserved and personal time confirmed, the rest tentative)."""
    event_id = cli.state.event_id
    with cli._catalog() as cat:
        cli._require_catalog(cat)
        zone = cli._zone(cat)
        items = cli._plan_items(cat, _cached_schedule(cat), quiet=True)
        sessions = cat.get_sessions(event_id, [i.key for i in items])
    entries = [(i, sessions.get(i.key)) for i in items]
    name = f"{event_id}-plan"
    data = build_calendar(entries, event_id=event_id, name=name, zone=zone)
    return f"{name}.ics", data.decode("utf-8"), sum(1 for i in items if i.start and i.end)


# -- travel times ---------------------------------------------------------------------


def _table_without_corrections(event_id: str) -> TravelTimes | None:
    """The travel table as TravelTimes.load reads it, before your corrections are layered on,
    for the "table's figure" column."""
    override = config_dir() / "venues" / f"{event_id}.toml"
    bundled = resources.files("reinvent_planner") / "data" / f"{event_id}.toml"
    try:
        if override.exists():
            return TravelTimes.from_toml(override.read_text(encoding="utf-8"))
        if bundled.is_file():
            return TravelTimes.from_toml(bundled.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


# -- dialogs --------------------------------------------------------------------------


class PublishScreen(ModalScreen[dict | None]):
    """What the feed shares, before the confirmation. Returns calendar_publish's options."""

    DEFAULT_CSS = """
    PublishScreen { align: center middle; }
    #publish-box { width: 72; max-width: 95%; height: auto; border: thick $primary;
                   padding: 1 2; background: $surface; }
    #publish-box Checkbox { margin-top: 1; }
    #publish-buttons { height: auto; margin-top: 1; }
    #publish-buttons Button { margin-right: 2; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, personal: bool, ranked: bool, auto: bool):
        super().__init__()
        self.initial = (personal, ranked, auto)

    def compose(self) -> ComposeResult:
        personal, ranked, auto = self.initial
        with Vertical(id="publish-box"):
            yield Label(Text("Publish your calendar feed", style="bold"))
            yield Static(
                Text("Your reserved sessions and favorites are always included.", style="dim")
            )
            yield Checkbox("Include personal time", personal, id="publish-personal")
            yield Checkbox("Include ranked picks", ranked, id="publish-ranked")
            yield Checkbox("Auto-update after schedule changes", auto, id="publish-auto")
            with Horizontal(id="publish-buttons"):
                yield Button("Continue…", variant="primary", id="publish-continue")
                yield Button("Cancel", id="publish-cancel")

    @on(Checkbox.Changed)
    def keep_to_self(self, event: Checkbox.Changed) -> None:
        event.stop()  # the app's search filters listen for every Checkbox.Changed

    @on(Button.Pressed, "#publish-continue")
    def go_on(self) -> None:
        self.dismiss(
            {
                "personal": self.query_one("#publish-personal", Checkbox).value,
                "ranked": self.query_one("#publish-ranked", Checkbox).value,
                "auto": self.query_one("#publish-auto", Checkbox).value,
            }
        )

    @on(Button.Pressed, "#publish-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class CorrectionScreen(ModalScreen[tuple[str, str, int] | None]):
    """Two venues and the minutes to allow between them. Returns (key, key, minutes)."""

    DEFAULT_CSS = """
    CorrectionScreen { align: center middle; }
    #correction-box { width: 72; max-width: 95%; height: auto; border: thick $primary;
                      padding: 1 2; background: $surface; }
    #correction-box Select { margin-top: 1; }
    #correction-minutes { width: 20; margin-top: 1; }
    #correction-buttons { height: auto; margin-top: 1; }
    #correction-buttons Button { margin-right: 2; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, venues: list[tuple[str, str]]):
        super().__init__()
        self.venues = venues  # (display name, key)

    def compose(self) -> ComposeResult:
        options = [(Text(name), key) for name, key in self.venues]  # names: never markup
        with Vertical(id="correction-box"):
            yield Label(Text("Correct the time to allow between two venues", style="bold"))
            yield Select(options, prompt="From venue", id="correction-first")
            yield Select(options, prompt="To venue", id="correction-second")
            yield Input(
                placeholder=f"minutes ({MINUTES_RANGE[0]}-{MINUTES_RANGE[1]})",
                type="integer",
                id="correction-minutes",
            )
            with Horizontal(id="correction-buttons"):
                yield Button("Save", variant="primary", id="correction-save")
                yield Button("Cancel", id="correction-cancel")

    @on(Select.Changed)
    def keep_to_self(self, event: Select.Changed) -> None:
        event.stop()  # the app's search filters listen for every Select.Changed

    @on(Button.Pressed, "#correction-save")
    def save(self) -> None:
        first = self.query_one("#correction-first", Select).value
        second = self.query_one("#correction-second", Select).value
        text = self.query_one("#correction-minutes", Input).value.strip()
        if not isinstance(first, str) or not isinstance(second, str):
            self.app.notify("Pick two venues.", severity="warning")
            return
        if first == second:
            self.app.notify("Pick two different venues.", severity="warning")
            return
        low, high = MINUTES_RANGE
        if not (text.isascii() and text.isdigit()) or not low <= int(text) <= high:
            self.app.notify(f"Minutes: a whole number from {low} to {high}.", severity="warning")
            return
        self.dismiss((first, second, int(text)))

    @on(Button.Pressed, "#correction-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class SignOutScreen(ModalScreen[bool]):
    """The sign-out confirmation, with the option to end the Builder ID browser session."""

    DEFAULT_CSS = """
    SignOutScreen { align: center middle; }
    #sign-out-box { width: 80; max-width: 95%; height: auto; border: thick $warning;
                    padding: 1 2; background: $surface; }
    #sign-out-box Checkbox { margin-top: 1; }
    #sign-out-buttons { height: auto; margin-top: 1; }
    #sign-out-buttons Button { margin-right: 2; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "decline", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.builder_id = False

    def compose(self) -> ComposeResult:
        with Vertical(id="sign-out-box"):
            yield Label(
                Text(
                    "Sign out? Your refresh token is revoked and the stored tokens are deleted "
                    "from this computer."
                )
            )
            yield Checkbox(
                "Also end my Builder ID session in the browser", False, id="sign-out-builder-id"
            )
            with Horizontal(id="sign-out-buttons"):
                yield Button("Sign out", variant="warning", id="sign-out-yes")
                yield Button("Cancel", id="sign-out-no")

    @on(Checkbox.Changed)
    def builder_id_changed(self, event: Checkbox.Changed) -> None:
        event.stop()  # the app's search filters listen for every Checkbox.Changed
        self.builder_id = event.value

    @on(Button.Pressed, "#sign-out-yes")
    def accept(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#sign-out-no")
    def action_decline(self) -> None:
        self.dismiss(False)


# -- the pane -------------------------------------------------------------------------


class MorePane(VerticalScroll):
    DEFAULT_CSS = """
    MorePane { padding: 0 1; }
    MorePane .more-heading { text-style: bold; color: $accent; margin-top: 1; }
    MorePane .more-buttons { height: auto; margin-top: 1; }
    MorePane .more-buttons Button { margin-right: 1; }
    MorePane .more-stack { height: auto; margin-top: 1; }
    MorePane Static { height: auto; }
    #feed-link { border: round $accent; padding: 0 1; }
    #travel-table { height: auto; max-height: 14; margin-top: 1; }
    .more-note { color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__(id="more-pane")
        self._more_busy = False
        self._corrections: dict[str, int] = {}
        self._who: Any = _UNKNOWN  # str (who), None (signed out) or _UNKNOWN (reading)
        self._account_reads = 0  # only the newest read's answer is shown
        self._note: tuple[str, str | None] = ("", None)  # the last feed write: output, error
        self._link_asked_presenting = False  # Show link was pressed in presentation mode
        self._exports: dict[str, tuple[str, int]] = {}  # delivery key: (filename, rows)
        self._builder_id_cancel: threading.Event | None = None  # set while waiting

    def compose(self) -> ComposeResult:
        yield Static("Calendar feed", classes="more-heading")
        yield Static(id="feed-status")
        yield Static(id="feed-link")
        yield Static(id="feed-note", classes="more-note")
        with Horizontal(classes="more-buttons"):
            yield Button("Publish…", id="feed-publish", variant="primary")
            yield Button("Show link", id="feed-show-link")
            yield Button("Unpublish", id="feed-unpublish", variant="error")

        yield Static("Exports", classes="more-heading")
        yield Static(
            Text(
                "Built from your last-fetched schedule (press r to refresh it first). In a "
                "browser this downloads the file; in a terminal it's saved to your Downloads "
                "folder.",
                style="dim",
            )
        )
        with Vertical(classes="more-stack"):  # stacked: three long labels overflow 80 columns
            yield Button("Download my plan (CSV)", id="export-plan-csv")
            yield Button("Download all sessions (CSV)", id="export-all-csv")
            yield Button("Download calendar files (ICS)", id="export-ics")

        yield Static("Travel-time corrections", classes="more-heading")
        yield Static(id="travel-status")
        yield DataTable(id="travel-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="more-buttons"):
            yield Button("Add or change…", id="travel-add")
            yield Button("Reset selected", id="travel-reset")
            yield Button("Reset all", id="travel-reset-all", variant="error")

        yield Static("Account", classes="more-heading")
        yield Static(id="account-status")
        with Horizontal(classes="more-buttons"):
            yield Button("Sign out", id="sign-out", variant="warning")
            yield Button("Stop waiting", id="builder-id-cancel")

    def on_mount(self) -> None:
        self.query_one("#travel-table", DataTable).add_columns(
            "Between", "Your minutes", "Table's figure"
        )
        self.query_one("#feed-link", Static).display = False
        self.reload()

    # -- presentation mode -----------------------------------------------------------------

    def _presenting(self) -> bool:
        return bool(getattr(self.app, "presentation", False))

    def _shareable(self, text: str) -> str:
        """`text` as it may be shown now: without gist IDs and links in presentation mode."""
        return redact_gists(text) if self._presenting() else text

    def _tell(self, message: str, **kwargs: Any) -> None:
        """A notification that's never markup and, in presentation mode, shows no gist."""
        self.app.notify(self._shareable(message), markup=False, **kwargs)

    # -- reading local state ---------------------------------------------------------------

    def reload(self) -> None:
        """Redraw all four sections from local state (no network); the sign-in state is read
        again in a worker, since that's a keychain read."""
        self._draw_feed()
        self._draw_note()
        self._draw_travel()
        self._draw_account()
        self._account_reads += 1
        self._read_account(self._account_reads)

    def _feed(self) -> calendar_feed.Feed | None:
        return calendar_feed.load(services.current_event())

    def _draw_feed(self) -> None:
        status = self.query_one("#feed-status", Static)
        link = self.query_one("#feed-link", Static)
        if self._presenting() and not self._link_asked_presenting:
            link.display = False  # shown before presentation mode: ask again to see it
        try:
            feed = self._feed()
        except calendar_feed.FeedError as exc:
            status.update(Text(self._shareable(str(exc)), style="red"))
            self._set_feed_buttons(published=False, record=False)
            return
        if feed is None or feed.deleted:
            line = Text("Not published. ", style="bold")
            line.append(
                "Publish your plan as a calendar feed that Google, Apple or Outlook calendars "
                "can subscribe to (a secret GitHub Gist, via the gh CLI).",
                style="dim",
            )
            if feed is not None and feed.pending_delete:
                line.append(
                    f"\n{len(feed.pending_delete)} old gist(s) still need deleting: press "
                    "Unpublish to retry.",
                    style="yellow",
                )
            status.update(line)
            link.display = False
            self._set_feed_buttons(published=False, record=feed is not None)
            return
        what = ["reserved sessions", "favorites"]
        what += ["personal time"] if feed.include_personal else []
        what += ["ranked picks"] if feed.include_ranked else []
        line = Text("Published", style="bold green")
        line.append(f" on the GitHub account {feed.owner}.\n")
        line.append("Includes: " + ", ".join(what) + ".\n")
        line.append("Auto-update: ")
        line.append("on" if feed.auto else "off", style="bold" if feed.auto else "yellow")
        if not feed.auto:
            line.append(" (publish again after changes)", style="dim")
        line.append("\nLink: ")
        if self._presenting():
            line.append("hidden (press Show link)", style="dim")
        else:
            line.append(feed.url)
        status.update(line)
        self._set_feed_buttons(published=True, record=True)

    def _set_feed_buttons(self, *, published: bool, record: bool) -> None:
        self.query_one("#feed-show-link", Button).disabled = not published
        self.query_one("#feed-unpublish", Button).disabled = not record

    def _draw_note(self) -> None:
        """What the last feed write printed (the link and how to subscribe, what's left to
        delete, or why it failed)."""
        output, error = self._note
        text = Text.from_ansi(output.strip()) if output.strip() else Text()
        if error and error not in text.plain:
            text.append(("\n" if text.plain else "") + error, style="red")
        if self._presenting():
            text = Text(redact_gists(text.plain))
            if error is None and HIDDEN in text.plain:
                text.append("\nPress Show link for the subscription link.", style="dim")
        self.query_one("#feed-note", Static).update(text)

    def _draw_travel(self) -> None:
        status = self.query_one("#travel-status", Static)
        table = self.query_one("#travel-table", DataTable)
        table.clear()
        event_id = services.current_event()
        try:
            travel = TravelTimes.load(event_id)
            self._corrections = cli._load_corrections()
        except TravelTableError as exc:
            status.update(Text(str(exc), style="red"))
            self._corrections = {}
            return
        base = _table_without_corrections(event_id)
        for pair, minutes in sorted(self._corrections.items()):
            keys = sorted(cli._pair_keys(pair))
            a, b = (keys + keys)[:2]  # a same-venue typo has one key
            figure = base.planned(a, b) if base is not None and a != b else None
            ignored = pair in travel.ignored_corrections
            between = Text(f"{travel.name(a)} ↔ {travel.name(b)}")
            if ignored:
                between.append("  (ignored: not two venues of this table)", style="yellow")
            table.add_row(
                between,
                Text(f"{minutes} min"),
                Text(f"{figure} min" if figure is not None else "?"),
                key=pair,
            )
        has_table = bool(travel.keys_with_figures())
        self.query_one("#travel-add", Button).disabled = not has_table
        self.query_one("#travel-reset", Button).disabled = not self._corrections
        self.query_one("#travel-reset-all", Button).disabled = not self._corrections
        table.display = bool(self._corrections)
        if not has_table:
            status.update(Text(f"No travel-time table for {event_id}.", style="dim"))
        elif not self._corrections:
            status.update(
                Text(
                    "No corrections: warnings use the table's figures. Tried a walk and it "
                    "took longer (or shorter)? Add your own figure.",
                    style="dim",
                )
            )
        else:
            status.update(Text("Your own figures, used instead of the table's:", style="dim"))

    @work(thread=True, group="more-account", exit_on_error=False)
    def _read_account(self, read: int) -> None:
        try:
            who = services.signed_in_as()
        except Exception:
            who = None
        self.app.call_from_thread(self._account_read, read, who)

    def _account_read(self, read: int, who: str | None) -> None:
        if read == self._account_reads:  # an older read may finish last
            self._who = who
            self._draw_account()

    def _draw_account(self) -> None:
        who = self._who
        status = self.query_one("#account-status", Static)
        if who is _UNKNOWN:
            line = Text("Checking your sign-in…", style="dim")
        elif who is None:
            line = Text("Not signed in.", style="yellow")
        elif self._presenting():
            line = Text("Signed in.")
        else:
            line = Text(f"Signed in as {who}.")
        waiting = self._builder_id_cancel is not None
        if waiting:
            line.append(
                "\nEnding your Builder ID session: waiting for the browser tab (up to "
                f"{BUILDER_ID_WAIT_SECONDS // 60} minutes)…",
                style="dim",
            )
        status.update(line)
        self.query_one("#sign-out", Button).disabled = who is None or who is _UNKNOWN
        self.query_one("#builder-id-cancel", Button).display = waiting and builder_id_cancellable()

    # -- writes, off the UI thread ------------------------------------------------------

    def _reservation_running(self) -> bool:
        """A reservation run is going (then nothing here may change anything)."""
        running = getattr(self.app, "reservation_running", None)
        if callable(running):
            busy = bool(running())
        else:
            busy = any(pane.running() for pane in self.app.query(LaunchPane))
        if busy:
            self.app.notify(
                "A reservation run is in progress; try again when it's done.", severity="warning"
            )
        return busy

    def _launch_busy(self) -> bool:
        """A run is going, or GO is about to light (or is lit): no GitHub calls or sign-out,
        which would compete with the run for the network, the feed and the sign-in."""
        if self._reservation_running():
            return True
        imminent = getattr(self.app, "launch_imminent", None)
        if callable(imminent):
            soon = bool(imminent())
        else:
            soon = any(pane.launch_imminent() for pane in self.app.query(LaunchPane))
        if soon:
            self.app.notify(
                "GO is about to light (or is lit) on the Launch tab; try again after the run "
                "(or disarm first).",
                severity="warning",
            )
        return soon

    def _start_write(
        self,
        message: str,
        fn,
        /,
        *,
        refresh_app: bool = False,
        feed: bool = False,
        then: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        """Run a CLI command in a worker, one at a time. `feed`: show its output (the link and
        how to subscribe, or what's left to delete) under the feed status. `then`: what to do
        next if it worked."""
        if self._more_busy:
            self.app.notify("Still working on the last change…", severity="warning")
            return
        self._more_busy = True
        self._run_write(message, fn, kwargs, refresh_app, feed, then)

    @work(thread=True, group="more-write", exit_on_error=False)
    def _run_write(
        self, message: str, fn, kwargs: dict, refresh_app: bool, feed: bool, then
    ) -> None:
        outcome = services.Outcome(error="Something went wrong; nothing was reported.")
        try:
            outcome = run_quietly(fn, **kwargs)
        finally:
            self.app.call_from_thread(self._write_done, outcome, message, refresh_app, feed, then)

    def _write_done(
        self, outcome: services.Outcome, message: str, refresh_app: bool, feed: bool, then
    ) -> None:
        self._more_busy = False
        if outcome.ok:
            self._tell(message)
        else:
            self._tell(outcome.error or "failed", severity="error", timeout=12)
        if feed:
            self._note = (outcome.output, outcome.error)
        self.reload()
        if refresh_app and callable(getattr(self.app, "refresh_views", None)):
            self.app.refresh_views()  # type: ignore[attr-defined]
        if then is not None and outcome.ok:
            then()

    # -- calendar feed ---------------------------------------------------------------------

    @on(Button.Pressed, "#feed-publish")
    def publish_pressed(self) -> None:
        if self._launch_busy():
            return
        try:
            feed = self._feed()
        except calendar_feed.FeedError as exc:
            self._tell(str(exc), severity="error")
            return
        existing = feed if feed is not None and not feed.deleted else None
        start = (
            (existing.include_personal, existing.include_ranked, existing.auto)
            if existing
            else (False, False, True)
        )

        def chosen(options: dict | None) -> None:
            if options is None:
                return
            if self._more_busy:
                self.app.notify("Still working on the last change…", severity="warning")
                return
            # Which account gh is signed into now, to name it in the confirmation.
            self._more_busy = True
            self.app.notify("Checking which GitHub account gh is signed into…")
            self._look_up_login(existing, options)

        self.app.push_screen(PublishScreen(*start), chosen)

    @work(thread=True, group="more-write", exit_on_error=False)
    def _look_up_login(self, existing: calendar_feed.Feed | None, options: dict) -> None:
        outcome = services.Outcome(error="Couldn't read your GitHub account from `gh`.")
        try:
            outcome = run_quietly(calendar_feed.github_login)
        finally:
            self.app.call_from_thread(self._login_known, existing, options, outcome)

    def _login_known(
        self, existing: calendar_feed.Feed | None, options: dict, outcome: services.Outcome
    ) -> None:
        self._more_busy = False
        if not outcome.ok:
            self._tell(outcome.error or "failed", severity="error", timeout=12)
            self._note = (outcome.output, outcome.error)
            self._draw_note()
            return
        self._confirm_publish(existing, options, str(outcome.value))

    def _confirm_publish(
        self, existing: calendar_feed.Feed | None, options: dict, login: str
    ) -> None:
        what = ["reserved sessions", "favorites"]
        what += ["personal time"] if options["personal"] else []
        what += ["ranked picks"] if options["ranked"] else []
        details = Text()
        if existing is not None and existing.owner != login:
            old = existing.owner
            question = "Publish your feed on a different GitHub account?"
            details.append(
                f"gh is now signed in as @{login}, not @{old}: a NEW secret gist will be "
                f"created on @{login}; the old one on @{old} stays online until you unpublish "
                f"it from @{old}.\n",
                style="bold yellow",
            )
            details.append(
                f"The new feed is a secret GitHub Gist on GitHub account @{login}, created with "
                "the gh CLI, at a new link. "
            )
        elif existing is None:
            question = "Publish your calendar feed?"
            details.append(
                "A new secret GitHub Gist will be created with the gh CLI, as a secret gist on "
                f"GitHub account @{login}. "
            )
        else:
            narrowed = (existing.include_personal and not options["personal"]) or (
                existing.include_ranked and not options["ranked"]
            )
            if narrowed:
                question = "Publish your feed at a new link?"
                details.append(
                    "Sharing less needs a new link: a gist keeps every earlier version. A new "
                    f"secret GitHub Gist will be created with the gh CLI on GitHub account "
                    f"@{login}, and the old one is deleted with its history, so the old link "
                    "stops working. "
                )
            else:
                question = "Update your calendar feed?"
                details.append(
                    f"Your existing secret gist on GitHub account @{login} is updated with the "
                    f"gh CLI (same link). If it's gone, a new one is created on @{login}. "
                )
        details.append(f"It will contain: {', '.join(what)}. ")
        details.append(
            "Anyone with the link can read it and its revision history; it isn't listed or "
            "searchable."
        )
        details.append(
            "\nAuto-update: " + ("on" if options["auto"] else "off"),
            style="dim",
        )

        def answered(yes: bool | None) -> None:
            if not yes or self._launch_busy():
                return
            self.app.notify("Publishing your calendar feed…")
            self._start_write(
                "Calendar feed published.",
                publish_as,
                expected_login=login,  # refused if gh has switched accounts since
                personal=options["personal"],
                ranked=options["ranked"],
                auto=options["auto"],
                feed=True,
            )

        self.app.push_screen(_confirm_screen(question, details), answered)

    @on(Button.Pressed, "#feed-show-link")
    def show_link(self) -> None:
        try:
            feed = self._feed()
        except calendar_feed.FeedError as exc:
            self._tell(str(exc), severity="error")
            return
        box = self.query_one("#feed-link", Static)
        if feed is None or feed.deleted:
            box.display = False
            self.app.notify("No feed yet. Publish one first.", severity="warning")
            return
        # Asked for, so shown even in presentation mode.
        self._link_asked_presenting = self._presenting()
        text = Text("Subscribe with this link (select it to copy):\n", style="dim")
        text.append(feed.url + "\n", style="bold")
        text.append("Apple Calendar: ", style="dim")
        text.append(feed.webcal_url)
        box.update(text)
        box.display = True

    @on(Button.Pressed, "#feed-unpublish")
    def unpublish_pressed(self) -> None:
        if self._launch_busy():
            return
        try:
            feed = self._feed()
        except calendar_feed.FeedError as exc:
            self._tell(str(exc), severity="error")
            return
        if feed is None:
            self.app.notify("There's no published feed.", severity="warning")
            return
        if feed.deleted:
            question = "Try again to delete your old feed gists?"
            links = "\n".join(f"https://gist.github.com/{p}" for p in feed.pending_delete)
            details = Text(self._shareable(links))
            done = "Tried again to delete your old feed gists."
        else:
            question = "Delete your calendar feed?"
            details = Text(
                "Its secret GitHub Gist, with its history, is deleted with the gh CLI. "
                "Subscribed calendars stop updating (and may show it as broken): remove the "
                "subscription from your calendar app too."
            )
            done = "Calendar feed deleted."

        def answered(yes: bool | None) -> None:
            if not yes or self._launch_busy():
                return
            self.query_one("#feed-link", Static).display = False
            self._start_write(done, cli.calendar_unpublish, yes=True, feed=True)

        self.app.push_screen(_confirm_screen(question, details), answered)

    # -- exports ------------------------------------------------------------------------

    @on(Button.Pressed, "#export-plan-csv")
    def export_plan_csv(self) -> None:
        self._export(csv_export_text, "text/csv", all_sessions=False)

    @on(Button.Pressed, "#export-all-csv")
    def export_all_csv(self) -> None:
        self._export(csv_export_text, "text/csv", all_sessions=True)

    @on(Button.Pressed, "#export-ics")
    def export_ics(self) -> None:
        self._export(ics_export_text, "text/calendar")

    @work(thread=True, group="more-export", exit_on_error=False)
    def _export(self, build, mime_type: str, **kwargs) -> None:
        outcome = services.run(build, **kwargs)
        self.app.call_from_thread(self._hand_over, outcome, mime_type)

    def _hand_over(self, outcome: services.Outcome, mime_type: str) -> None:
        if not outcome.ok:
            self._tell(outcome.error or "failed", severity="error", timeout=10)
            return
        filename, text, count = outcome.value
        web = self.app.is_web
        # deliver_text, not deliver_binary: Textual's terminal driver writes a BytesIO in text
        # mode (and fails), while text works both there and under textual-serve.
        key = self.app.deliver_text(
            io.StringIO(delivery_text(text, web=web)),
            save_filename=filename,
            encoding="utf-8",
            mime_type=mime_type,
            name=f"{EXPORT_PREFIX}{filename}",
        )
        if key is None:
            self._tell(f"Couldn't hand over {filename}.", severity="error", timeout=10)
            return
        self._exports[key] = (filename, count)
        where = "downloading in your browser" if web else "saving it to your Downloads folder"
        self._tell(f"{filename}: {count} item(s), {where}…")

    def export_delivered(self, event: events.DeliveryComplete) -> bool:
        """For the app's DeliveryComplete handler: say where an export went. True if it was
        one of ours (its delivery name starts with EXPORT_PREFIX)."""
        if not (event.name or "").startswith(EXPORT_PREFIX):
            return False
        filename, _rows = self._exports.pop(event.key, (event.name.removeprefix(EXPORT_PREFIX), 0))
        if event.path is None or self.app.is_web:
            self._tell(f"{filename} downloaded.")
        elif self._presenting():
            self._tell(f"{filename} saved to your Downloads folder.")
        else:
            self._tell(f"{filename} saved: {event.path}")
        return True

    def export_failed(self, event: events.DeliveryFailed) -> bool:
        """For the app's DeliveryFailed handler: say an export failed. True if it was ours."""
        if not (event.name or "").startswith(EXPORT_PREFIX):
            return False
        filename, _count = self._exports.pop(event.key, (event.name.removeprefix(EXPORT_PREFIX), 0))
        self._tell(
            f"Couldn't save {filename}: {clean_text(str(event.exception))}",
            severity="error",
            timeout=12,
        )
        return True

    # -- travel-time corrections --------------------------------------------------------

    @on(Button.Pressed, "#travel-add")
    def add_correction(self) -> None:
        if self._reservation_running():
            return
        try:
            travel = TravelTimes.load(services.current_event())
        except TravelTableError as exc:
            self._tell(str(exc), severity="error")
            return
        venues = [(travel.name(k), k) for k in travel.keys_with_figures()]
        if not venues:
            self.app.notify("This event has no travel-time table.", severity="warning")
            return

        def chosen(result: tuple[str, str, int] | None) -> None:
            if result is not None and not self._reservation_running():
                first, second, minutes = result
                self._start_write(
                    f"{travel.name(first)} ↔ {travel.name(second)}: now {minutes} min.",
                    cli.venues_set,
                    first=first,
                    second=second,
                    minutes=minutes,
                )

        self.app.push_screen(CorrectionScreen(venues), chosen)

    def _selected_pair(self) -> str | None:
        table = self.query_one("#travel-table", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(Button.Pressed, "#travel-reset")
    def reset_selected(self) -> None:
        pair = self._selected_pair()
        if pair is None:
            self.app.notify("Select a correction first.", severity="warning")
            return
        keys = sorted(cli._pair_keys(pair))
        if len(keys) != 2:
            self.app.notify(
                f"{pair} isn't two venues; edit {cli.corrections_path(services.current_event())}.",
                severity="warning",
                markup=False,
            )
            return
        travel = TravelTimes.load(services.current_event())
        names = f"{travel.name(keys[0])} ↔ {travel.name(keys[1])}"

        def answered(yes: bool | None) -> None:
            if yes and not self._reservation_running():
                self._start_write(
                    f"{names} is back to the table's figure.",
                    cli.venues_reset,
                    first=keys[0],
                    second=keys[1],
                    yes=True,
                )

        self.app.push_screen(
            _confirm_screen(f"Remove your correction for {names}?"),
            answered,
        )

    @on(Button.Pressed, "#travel-reset-all")
    def reset_all(self) -> None:
        count = len(self._corrections)

        def answered(yes: bool | None) -> None:
            if yes and not self._reservation_running():
                self._start_write(
                    "All your travel-time corrections are removed.",
                    cli.venues_reset,
                    first=None,
                    second=None,
                    yes=True,
                )

        self.app.push_screen(
            _confirm_screen(
                f"Remove all {count} of your travel-time corrections?",
                Text("Warnings go back to the table's figures.", style="dim"),
            ),
            answered,
        )

    # -- account ----------------------------------------------------------------------------

    @on(Button.Pressed, "#sign-out")
    def sign_out_pressed(self) -> None:
        if self._launch_busy():
            return
        screen = SignOutScreen()

        def answered(yes: bool | None) -> None:
            if not yes or self._launch_busy():
                return
            # The tokens first; the Builder ID browser session (which waits for a browser tab)
            # afterwards, in a worker of its own so it never holds up anything else here.
            self._start_write(
                "Signed out.",
                cli.logout,
                refresh_app=True,
                then=self._end_builder_id if screen.builder_id else None,
                builder_id=False,
            )

        self.app.push_screen(screen, answered)

    def _end_builder_id(self) -> None:
        if self._builder_id_cancel is not None:
            return  # already waiting for the browser
        cancel = self._builder_id_cancel = threading.Event()
        self._tell(
            "Ending your Builder ID session: finish in the browser tab that opens.", timeout=20
        )
        self._draw_account()
        self._wait_for_builder_id(cancel)

    @work(thread=True, group="more-builder-id", exit_on_error=False)
    def _wait_for_builder_id(self, cancel: threading.Event) -> None:
        def announce(url: str) -> None:  # captured output never reaches the screen
            self.app.call_from_thread(self._tell, f"Opening {url}", timeout=30)

        outcome = services.Outcome(error="Something went wrong; nothing was reported.")
        try:
            outcome = run_quietly(end_builder_id_session, announce=announce, cancel=cancel)
        finally:
            self.app.call_from_thread(self._builder_id_done, outcome, cancel)

    def _builder_id_done(self, outcome: services.Outcome, cancel: threading.Event) -> None:
        self._builder_id_cancel = None
        if cancel.is_set():
            self._tell(
                "Stopped waiting for the browser. Your Builder ID session may still be active: "
                f"you can end it at {BUILDER_ID_PROFILE_URL}.",
                severity="warning",
                timeout=12,
            )
        elif outcome.ok:
            self._tell("Your Builder ID browser session has ended.")
        else:
            self._tell(
                f"{outcome.error} You can end your Builder ID session at {BUILDER_ID_PROFILE_URL}.",
                severity="error",
                timeout=12,
            )
        self._draw_account()

    @on(Button.Pressed, "#builder-id-cancel")
    def stop_waiting_for_builder_id(self) -> None:
        if self._builder_id_cancel is not None:
            self._builder_id_cancel.set()
