"""The interactive app: search the catalog, open a session, favorite and rank it, and see your
plan and reservation checklist, without typing commands.

Runs in a terminal (`rip tui`) and, through textual-serve, in a local browser tab (`rip ui`).
Everything goes through `services`, which reuses the CLI's code, so both behave identically.
Changes to your real schedule (favorites) always ask first; ranking stays on this machine.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from datetime import UTC, date, datetime
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .. import DEFAULT_EVENT_ID, services
from ..catalog import SearchFilters
from . import themes, visuals
from .launch_pane import LaunchPane
from .more_pane import MorePane
from .personal_pane import PersonalPane

DAYS = [("Mon", "mon"), ("Tue", "tue"), ("Wed", "wed"), ("Thu", "thu"), ("Fri", "fri")]


def _day_name(day: date) -> str:
    return (
        f"{DAYS[day.weekday()][0] if day.weekday() < 5 else day.strftime('%a')} {day:%b} {day.day}"
    )


def _ansi(text: str) -> Text:
    return Text.from_ansi(text) if text else Text("")


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no before anything changes on the real schedule."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 80; max-width: 95%; height: auto; border: thick $warning; padding: 1 2;
           background: $surface; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 2; }
    """

    def __init__(self, question: str, details: Text | None = None):
        super().__init__()
        self.question, self.details = question, details

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(Text(self.question))  # contains a catalog code: never markup
            if self.details:
                yield Static(self.details)
            with Horizontal(id="buttons"):
                yield Button("Yes (y)", variant="warning", id="yes")
                yield Button("No", id="no")

    def on_mount(self) -> None:
        # "No" has the focus: an Enter meant for the button that opened this (a double press,
        # a key repeat) must never answer yes. Yes takes a click, Tab then Enter, or `y`.
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self._answer(True)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self._answer(False)

    def key_y(self) -> None:
        self._answer(True)

    def key_n(self) -> None:
        self._answer(False)

    def key_escape(self) -> None:
        self._answer(False)

    def _answer(self, yes: bool) -> None:
        if self.is_current:  # a second press in the same moment must not dismiss twice
            self.dismiss(yes)


class SessionScreen(ModalScreen[bool]):
    """One session: details, favorite / unfavorite, and rank. Returns True if something changed."""

    DEFAULT_CSS = """
    SessionScreen { align: center middle; }
    #panel { width: 100; max-width: 98%; height: 90%; border: thick $primary; background: $surface;
             padding: 0 1; }
    #details { height: 1fr; }
    #actions { height: auto; padding-top: 1; }
    #actions Button { margin-right: 1; }
    #rank { width: 12; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "close", "Close")]

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id
        self.changed = False

    def compose(self) -> ComposeResult:
        reserved, favorites, ranks = services.my_schedule_ids()
        favorite = self.session_id in favorites
        with Vertical(id="panel"):
            with VerticalScroll(id="details"):
                yield Static(id="body")
            with Horizontal(id="actions"):
                yield Button(
                    "Remove favorite" if favorite else "★ Favorite",
                    id="unfavorite" if favorite else "favorite",
                    variant="default" if favorite else "primary",
                )
                yield Input(
                    str(ranks.get(self.session_id, "")),
                    placeholder="rank",
                    type="integer",
                    id="rank",
                )
                yield Button("Save rank", id="save-rank")
                yield Button("Clear rank", id="clear-rank")
                if self.session_id in reserved:
                    yield Button("Cancel reservation", id="cancel-reservation", variant="error")
                yield Button("Close", id="close")
            if self.session_id in reserved:
                yield Label("[green]You hold a reserved seat for this session.[/]")

    def on_mount(self) -> None:
        outcome = services.show(self.session_id, width=max(60, self.app.size.width - 12))
        self.query_one("#body", Static).update(
            _ansi(outcome.output) if outcome.ok else Text(outcome.error or "", style="red")
        )

    def action_close(self) -> None:
        if self.is_current:
            self.dismiss(self.changed)

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        if self.is_current:
            self.dismiss(self.changed)

    def _run_blocks_writes(self) -> bool:
        running = getattr(self.app, "reservation_running", None)
        if running is not None and running():
            self.app.notify(
                "A reservation run is in progress; change your schedule when it's done.",
                severity="warning",
            )
            return True
        return False

    @on(Button.Pressed, "#cancel-reservation")
    def cancel_reservation(self) -> None:
        if self._run_blocks_writes():
            return
        code = services.session(self.session_id).code

        def answered(yes: bool | None) -> None:
            if yes and not self._run_blocks_writes():  # re-checked: a run may have started
                self._cancel_seat()

        self.app.push_screen(
            ConfirmScreen(
                f"Cancel your reservation for {code}? The seat goes back to other attendees, "
                "and it may not be there if you change your mind."
            ),
            answered,
        )

    @work(thread=True, exclusive=True, group="write", exit_on_error=False)
    def _cancel_seat(self) -> None:
        outcome = services.cancel_reservation(self.session_id)
        self.app.call_from_thread(self._cancel_done, outcome)

    def _cancel_done(self, outcome: services.Outcome) -> None:
        self.app.refresh_views()  # type: ignore[attr-defined]  (the schedule was re-read)
        if not outcome.ok:
            self.app.notify(outcome.error or "failed", severity="error", markup=False)
            return
        self.changed = True
        if outcome.value:
            self.app.notify("Reservation cancelled.", markup=False)
        else:  # the cached schedule was stale: the seat was already gone
            self.app.notify("It wasn't reserved on your schedule any more.", markup=False)
        if self.is_current:
            self.dismiss(True)

    @on(Button.Pressed, "#favorite")
    def favorite(self) -> None:
        self._change_favorite(True)

    @on(Button.Pressed, "#unfavorite")
    def unfavorite(self) -> None:
        self._change_favorite(False)

    def _change_favorite(self, favorite: bool) -> None:
        if self._run_blocks_writes():
            return
        code = services.session(self.session_id).code
        warnings = services.conflict_warnings(self.session_id, "favorite").value if favorite else []
        question = (
            f"Add {code} to your favorites on your re:Invent schedule?"
            if favorite
            else f"Remove {code} from your favorites?"
        )

        def answered(yes: bool | None) -> None:
            if yes and not self._run_blocks_writes():
                self._apply_favorite(favorite)

        # The CLI's warning lines are Rich markup with the API text inside already escaped.
        details = Text.from_markup("\n".join(warnings)) if warnings else None
        self.app.push_screen(ConfirmScreen(question, details), answered)

    @work(thread=True, exclusive=True, group="write", exit_on_error=False)
    def _apply_favorite(self, favorite: bool) -> None:
        outcome = services.set_favorite(self.session_id, favorite)
        self.app.call_from_thread(self._favorite_done, outcome, favorite)

    def _favorite_done(self, outcome: services.Outcome, favorite: bool) -> None:
        if not outcome.ok:
            self.app.notify(outcome.error or "failed", severity="error", timeout=8, markup=False)
            return
        self.changed = True
        self.app.notify("Added to favorites." if favorite else "Removed from favorites.")
        self.dismiss(True)

    @on(Button.Pressed, "#save-rank")
    def save_rank(self) -> None:
        value = self.query_one("#rank", Input).value.strip()
        if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= 999:
            self.app.notify("A rank is a whole number from 1 (your top pick).", severity="warning")
            return
        self._set_rank(int(value), f"Ranked #{value}.")

    @on(Button.Pressed, "#clear-rank")
    def clear_rank(self) -> None:
        self._set_rank(None, "Removed from your ranking.")

    # Off the UI thread: ranking can update a published calendar feed over the network.
    @work(thread=True, exclusive=True, group="write", exit_on_error=False)
    def _set_rank(self, rank: int | None, message: str) -> None:
        outcome = services.set_rank(self.session_id, rank)
        self.app.call_from_thread(self._rank_done, outcome, message)

    def _rank_done(self, outcome: services.Outcome, message: str) -> None:
        if not outcome.ok:
            self.app.notify(outcome.error or "failed", severity="error", markup=False)
            return
        self.changed = True
        warnings = [
            line
            for line in Text.from_ansi(outcome.output).plain.splitlines()
            if line.strip().startswith("⚠")
        ]
        self.app.notify(
            message + ("\n" + "\n".join(warnings) if warnings else ""), timeout=8, markup=False
        )


class PlannerApp(App):
    # Careful with private names: Textual's App uses some itself (e.g. _filters, _sync).
    TITLE = "reinvent-planner"
    presentation = False  # hide the signed-in email, for screenshots
    exit_when_idle = False  # a quit arrived mid-run: leave once the run ends
    _force_exit = False  # "Quit anyway" during a run
    CSS = """
    #status-bar { height: auto; padding: 0 1; }
    #status { width: 1fr; content-align: left middle; height: 3; }
    #status-bar Button { margin-left: 1; }
    #filters { height: auto; padding: 0 1; }
    #filters Input { width: 30; }
    #filters Select { width: 22; }
    #results { height: 1fr; }
    TabbedContent, ContentSwitcher, TabPane { height: 1fr; }
    #plan VerticalScroll, #checklist VerticalScroll { height: 1fr; }
    #day-bar { height: auto; padding: 0 1; }
    #day-bar Select { width: 24; }
    #day-hint { color: $text-muted; content-align: left middle; height: 3; }
    #timeline, #stripmap { margin: 1 1 0 1; height: auto; }
    .report { padding: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "show_tab('search')", "Search"),
        Binding("p", "show_tab('plan')", "My plan"),
        Binding("c", "show_tab('checklist')", "Checklist"),
        Binding("t", "show_tab('personal')", "Personal time"),
        Binding("l", "show_tab('launch')", "Launch"),
        Binding("m", "show_tab('more')", "More"),
        Binding("slash", "focus_search", "Find", show=False),
        Binding("r", "refresh", "Refresh schedule"),  # like `rip schedule`: may update the feed
        Binding("q", "quit", "Quit"),
        Binding("escape", "leave_input", "Leave the box", show=False),
    ]

    def __init__(self, event_id: str = DEFAULT_EVENT_ID):
        super().__init__()
        self.event_id = event_id
        services.use_event(event_id)

    # -- layout -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="status-bar"):
            yield Static(id="status")
            yield Button("Sign in", id="sign-in")
            yield Button("Cancel sign-in", id="cancel-sign-in", variant="error")
            yield Button("Sync catalog", id="sync", variant="primary")
        with TabbedContent(initial="search"):
            with TabPane("Search", id="search"):
                with Horizontal(id="filters"):
                    yield Input(placeholder="Search titles, speakers, topics…", id="query")
                    yield Select([], prompt="Any type", id="type")
                    yield Select([], prompt="Any level", id="level")
                    yield Select(DAYS, prompt="Any day", id="day")
                    yield Select([], prompt="Any venue", id="venue")
                    yield Checkbox("Laptop", id="laptop")
                    yield Checkbox("My favorites", id="favorites")
                yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
            with TabPane("My plan", id="plan"):
                with Horizontal(id="day-bar"):
                    yield Select([], prompt="Day", id="plan-day", allow_blank=True)
                    yield Static("  timeline · Strip map · agenda", id="day-hint")
                with VerticalScroll():
                    yield Static(id="timeline")
                    yield Static(id="stripmap")
                    yield Static(id="plan-body", classes="report")
            with TabPane("Checklist", id="checklist"), VerticalScroll():
                yield Static(id="checklist-body", classes="report")
            with TabPane("Personal time", id="personal"):
                yield PersonalPane()
            with TabPane("Launch", id="launch"):
                yield LaunchPane()
            with TabPane("More", id="more"):
                yield MorePane()
        yield Footer()

    def _fatal_error(self) -> None:
        """A deliberate override: Textual's crash report shows local variables, which could
        include tokens or API responses. Same report, without them (like the CLI's)."""
        import rich
        from rich.segment import Segments
        from rich.traceback import Traceback

        self.bell()
        traceback = Traceback(show_locals=False, width=None, suppress=[rich])
        self._exit_renderables.append(
            Segments(self.console.render(traceback, self.console.options))
        )
        self._close_messages_no_wait()

    def on_mount(self) -> None:
        themes.register(self)
        # RIP_THEME picks any Textual theme (e.g. textual-light); the command palette works too.
        wanted = os.environ.get("RIP_THEME", "rip-neon")
        self.theme = wanted if wanted in self.available_themes else "rip-neon"
        self.set_interval(60, self._keep_now_current)  # the timeline's ▼ now-marker
        if "web_driver" in os.environ.get("TEXTUAL_DRIVER", "") and os.name == "posix":
            # Under `rip ui`: if the server dies without stopping us (killed, crashed), we'd
            # otherwise live on for good, reparented, with nobody to talk to.
            server = os.getppid()
            if server == 1:  # the server died before we even started
                self.call_later(self.server_gone)
            else:
                threading.Thread(target=self._watch_server, args=(server,), daemon=True).start()
        self._sign_in_cancel: threading.Event | None = None
        self.query_one("#cancel-sign-in", Button).display = False
        table = self.query_one("#results", DataTable)
        table.add_columns("", "Code", "Title", "Type", "Level", "When", "Venue", "Seats")
        self._load_filters()
        self._update_status()
        self._run_search()

    # -- status and filters -------------------------------------------------------------

    def _update_status(self) -> None:
        who = services.signed_in_as()
        count, synced = services.catalog_status()
        parts = [
            f"[b]{escape(self.event_id)}[/]",
            ("signed in" if self.presentation else f"signed in as {escape(who)}")
            if who
            else "[yellow]not signed in[/]",
            f"{count} sessions"
            + (f", synced {synced[:16].replace('T', ' ')} UTC" if synced else ""),
        ]
        if not count:
            parts.append("[yellow]press Sync catalog to download it[/]")
        if services.is_demo_catalog():
            parts.insert(1, "[b reverse] DEMO [/]")  # made-up sessions (scripts/demo)
        self.query_one("#status", Static).update("  ·  ".join(parts))
        self.query_one("#sign-in", Button).display = who is None

    def _load_filters(self) -> None:
        choices = services.filter_choices()
        # Catalog text as Text, never markup (a str prompt would be parsed as markup).
        self.query_one("#type", Select).set_options((Text(t), t) for t in choices["types"])
        self.query_one("#level", Select).set_options((Text(v), v) for v in choices["levels"])
        self.query_one("#venue", Select).set_options((Text(v), v) for v in choices["venues"])

    # -- search ---------------------------------------------------------------------------

    def _search_filters(self) -> SearchFilters:
        def pick(widget_id: str) -> str | None:
            value = self.query_one(f"#{widget_id}", Select).value
            # Textual 8 marks "nothing selected" with Select.NULL (Select.BLANK is just False).
            return None if value is Select.NULL or value in (None, False, "") else str(value)

        only_ids = None
        if self.query_one("#favorites", Checkbox).value:
            _reserved, favorites, _ranks = services.my_schedule_ids()
            only_ids = frozenset(favorites)
        kind, day = pick("type"), pick("day")
        return SearchFilters(
            query=self.query_one("#query", Input).value or None,
            types=[kind] if kind else (),
            level=pick("level"),
            days=[day] if day else (),
            venue=pick("venue"),
            laptop_required=self.query_one("#laptop", Checkbox).value,
            only_ids=only_ids,
            limit=500,
        )

    def _run_search(self) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        try:
            rows = services.search(self._search_filters())
        except Exception as exc:
            self.notify(str(exc), severity="error", markup=False)
            return
        for row in rows:
            table.add_row(  # only marks and seats are our own markup; API text stays literal
                Text.from_markup(row.marks),
                Text(row.code),
                Text(row.title),
                Text(row.type),
                Text(row.level),
                Text(row.when),
                Text(row.venue),
                Text.from_markup(row.seats),
                key=row.session_id,
            )
        self.sub_title = f"{len(rows)} session(s)" + (" (first 500)" if len(rows) == 500 else "")

    @on(Input.Submitted, "#query")
    @on(Input.Changed, "#query")
    @on(Select.Changed)
    @on(Checkbox.Changed)
    def filters_changed(self) -> None:
        self._run_search()

    @on(DataTable.RowSelected, "#results")
    def open_session(self, event: DataTable.RowSelected) -> None:
        def closed(changed: bool | None) -> None:
            if changed:
                self._run_search()
                self._refresh_reports()

        self.push_screen(SessionScreen(str(event.row_key.value)), closed)

    # -- plan and checklist ------------------------------------------------------------------

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id in ("plan", "checklist"):
            self._refresh_reports()
        if event.pane.id == "launch":
            self.query_one(LaunchPane).focus_start()
        if event.pane.id == "personal":
            self.query_one(PersonalPane).reload()
        if event.pane.id == "more":
            self.query_one(MorePane).reload()

    def _refresh_reports(self, offline: bool = True) -> None:
        self._load_days()
        width = max(70, self.size.width - 4)
        plan = services.plan(width, offline=offline)
        self.query_one("#plan-body", Static).update(
            _ansi(plan.output) if plan.ok else Text(plan.error or "", style="yellow")
        )
        checklist = services.checklist(width, offline=offline)
        self.query_one("#checklist-body", Static).update(
            _ansi(checklist.output) if checklist.ok else Text(checklist.error or "", style="yellow")
        )

    def refresh_views(self) -> None:
        """Redraw everything that shows the catalog or your schedule (e.g. after a run)."""
        self._update_status()
        self._run_search()
        self._refresh_reports()
        self.query_one(PersonalPane).reload()
        self.query_one(MorePane).reload()

    def reservation_running(self) -> bool:
        return self.query_one(LaunchPane).running()

    def launch_imminent(self) -> bool:
        return self.query_one(LaunchPane).launch_imminent()

    # Deliveries (the More tab's exports) are reported to the app only: pass them on.
    @on(events.DeliveryComplete)
    def export_delivered(self, event: events.DeliveryComplete) -> None:
        self.query_one(MorePane).export_delivered(event)

    @on(events.DeliveryFailed)
    def export_failed(self, event: events.DeliveryFailed) -> None:
        self.query_one(MorePane).export_failed(event)

    # -- the visual day view ----------------------------------------------------------

    def _load_days(self) -> None:
        self._day_plan = services.day_plan()
        picker = self.query_one("#plan-day", Select)
        days = sorted(self._day_plan.days)
        current = picker.value
        picker.set_options((_day_name(d), d.isoformat()) for d in days)
        wanted = current if isinstance(current, str) else None
        today = datetime.now(self._day_plan.zone).date().isoformat()
        if wanted not in {d.isoformat() for d in days}:
            wanted = today if today in {d.isoformat() for d in days} else None
            wanted = wanted or (days[0].isoformat() if days else None)
        if wanted:
            picker.value = wanted
        self._draw_day()

    @on(Select.Changed, "#plan-day")
    def day_changed(self) -> None:
        self._draw_day()

    def on_resize(self) -> None:
        if getattr(self, "_day_plan", None) is None:
            return
        if (timer := getattr(self, "_resize_timer", None)) is not None:
            timer.stop()
        self._resize_timer = self.set_timer(0.05, self._draw_day)  # one redraw per drag burst

    def _keep_now_current(self) -> None:
        if self.query_one(TabbedContent).active == "plan":
            self._draw_day()

    def _draw_day(self) -> None:
        plan = getattr(self, "_day_plan", None)
        value = self.query_one("#plan-day", Select).value
        timeline = self.query_one("#timeline", Static)
        stripmap = self.query_one("#stripmap", Static)
        if plan is None or not isinstance(value, str):
            timeline.update(Text("Nothing planned yet: rank or favorite sessions.", style="dim"))
            stripmap.update("")
            return
        items = plan.days.get(date.fromisoformat(value), [])
        width = max(40, self.size.width - 4)
        timeline.update(
            visuals.render_timeline(
                items,
                plan.zone,
                width=width,
                ranks=plan.ranks,
                travel=plan.travel,
                now=datetime.now(UTC),
            )
        )
        stripmap.update(
            visuals.render_strip_map(
                items, plan.travel, width=width, zone=plan.zone, ranks=plan.ranks
            )
        )

    # -- actions -------------------------------------------------------------------------------

    async def _on_exit_app(self) -> None:
        """A deliberate override. Quit requests that bypass action_quit (the browser tab closing,
        a signal) wait for a reservation run to finish instead of cutting it off mid-run."""
        if self.query_one(LaunchPane).running() and not self._force_exit:
            self.exit_when_idle = True
            self.notify("Finishing the reservation run before closing…", timeout=30)
            return
        await super()._on_exit_app()

    def _watch_server(self, server_pid: int) -> None:
        while os.getppid() == server_pid:
            time.sleep(1.0)
        with contextlib.suppress(Exception):  # the app may already be shutting down
            self.call_from_thread(self.server_gone)

    def server_gone(self) -> None:
        """The `rip ui` server is gone: leave, after any reservation run in progress."""
        if self.query_one(LaunchPane).running():
            self.exit_when_idle = True
        else:
            self.exit()

    def run_finished(self) -> None:
        """Called by the Launch tab when a run ends."""
        if self.exit_when_idle:
            self.exit()

    async def action_quit(self) -> None:
        if self.query_one(LaunchPane).running():

            def answered(yes: bool | None) -> None:
                if yes:
                    self._force_exit = True
                    self.exit()

            self.push_screen(
                ConfirmScreen(
                    "A reservation run is in progress, and a reservation may be in flight. "
                    "Quit anyway? (Your next run or `rip reserve` will report what landed.)"
                ),
                answered,
            )
            return
        self.exit()

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_leave_input(self) -> None:
        """Escape leaves a text box, so the one-key shortcuts (s, p, c, t, l, m, q) work again."""
        if isinstance(self.focused, Input):
            if self.focused.id == "query":
                self.query_one("#results", DataTable).focus()
            else:
                self.set_focus(None)

    def action_focus_search(self) -> None:
        self.action_show_tab("search")
        self.query_one("#query", Input).focus()

    def _network_busy(self) -> bool:
        """One sync, refresh or reservation run at a time: they'd compete for the catalog and
        the calendar feed (and a thread worker can't be stopped once started)."""
        pane = self.query_one(LaunchPane)
        if pane.running():
            self.notify(
                "A reservation run is in progress; try again when it's done.", severity="warning"
            )
            return True
        if pane.launch_imminent():
            self.notify(
                "GO is about to light (or is lit): syncing now would hold it up. "
                "Disarm first if you really want to sync.",
                severity="warning",
            )
            return True
        if any(w.group == "network" and not w.is_finished for w in self.workers):
            self.notify("Still working on the last sync or refresh…", severity="warning")
            return True
        return False

    def syncing(self) -> bool:
        """A sync, refresh or personal-time change is running (GO waits for these)."""
        return any(w.group in ("network", "personal") and not w.is_finished for w in self.workers)

    def network_busy_quiet(self) -> bool:
        """A sync or refresh is running (without the notice _network_busy gives)."""
        return any(w.group == "network" and not w.is_finished for w in self.workers)

    def action_refresh(self) -> None:
        if not self._network_busy():
            self._refresh_online()

    @on(Button.Pressed, "#sync")
    def sync_pressed(self) -> None:
        if not self._network_busy():
            self._sync_catalog()

    @work(thread=True, group="network", exit_on_error=False)
    def _sync_catalog(self) -> None:
        self.call_from_thread(self.notify, "Downloading the catalog…")
        outcome = services.sync()
        self.call_from_thread(self._after_network, outcome, "Catalog synced.")

    @work(thread=True, group="network", exit_on_error=False)
    def _refresh_online(self) -> None:
        width = max(70, self.size.width - 4)
        outcome = services.plan(width, offline=False)  # also refreshes the cached schedule
        self.call_from_thread(
            self._after_network,
            outcome,
            "Schedule refreshed (your calendar feed too, if it changed).",
        )

    def _after_network(self, outcome: services.Outcome, message: str) -> None:
        if outcome.ok:
            self.notify(message, markup=False)  # may carry an email address
        else:
            self.notify(outcome.error or "failed", severity="error", timeout=10, markup=False)
        self._update_status()
        self._load_filters()
        self._run_search()
        self._refresh_reports()
        self.query_one(PersonalPane).reload()
        self.query_one(MorePane).reload()  # e.g. the Account section after signing in

    @on(Button.Pressed, "#sign-in")
    def sign_in_pressed(self) -> None:
        if self._sign_in_cancel is not None:
            return  # already waiting for the browser
        self._sign_in_cancel = threading.Event()
        self.query_one("#sign-in", Button).display = False
        self.query_one("#cancel-sign-in", Button).display = True
        self._sign_in(self._sign_in_cancel)

    @on(Button.Pressed, "#cancel-sign-in")
    def cancel_sign_in(self) -> None:
        if self._sign_in_cancel is not None:
            self._sign_in_cancel.set()

    def on_unmount(self) -> None:
        if getattr(self, "_sign_in_cancel", None) is not None:
            self._sign_in_cancel.set()  # don't leave a sign-in waiting after quitting

    @work(thread=True, group="sign-in", exit_on_error=False)
    def _sign_in(self, cancel: threading.Event) -> None:
        def announce(url: str) -> None:
            self.call_from_thread(
                self.notify,
                "Finish signing in in your browser. If no tab opened, visit:\n" + url,
                timeout=60,
                markup=False,
            )

        outcome = services.sign_in(announce, cancel)
        self.call_from_thread(self._sign_in_done, outcome)

    def _sign_in_done(self, outcome: services.Outcome) -> None:
        self._sign_in_cancel = None
        self.query_one("#cancel-sign-in", Button).display = False
        self.query_one("#sign-in", Button).display = True
        who = "" if self.presentation else f" as {outcome.value}"
        self._after_network(outcome, f"Signed in{who}." if outcome.ok else "")


def run(event_id: str = DEFAULT_EVENT_ID) -> None:
    PlannerApp(event_id).run()
