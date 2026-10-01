"""The Personal time tab: your own blocks (lunch, meetings, travel) on your event schedule.

Lists them from the cached schedule, like `rip time ls --offline`, and adds, edits and removes
them through the CLI's own commands (`rip time add / edit / rm`), so the checks, the clash
warnings, the readback and the calendar-feed update are the same. Every change asks first;
only then does the command run, with yes=True, off the UI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from .. import cli, services
from ..catalog import Catalog
from ..models import PersonalTime, PersonalTimeInput, Schedule


def _day_label(day: date) -> str:
    return f"{day:%a %b} {day.day}"


def _event_days(cat: Catalog) -> list[date]:
    """The event's days in the zone times are entered in (as `cli._event_day` works them out)."""
    event = cat.get_event(cli.state.event_id)
    if event is None:
        return []
    zone = cli._zone(cat)
    try:
        first = datetime.fromisoformat(event.start_date).astimezone(zone).date()
        last = datetime.fromisoformat(event.end_date).astimezone(zone).date()
    except (TypeError, ValueError):
        return []
    days, day = [], first
    while day <= last and len(days) < 31:
        days.append(day)
        day += timedelta(days=1)
    return days


@dataclass
class _Snapshot:
    entries: list[PersonalTime] = field(default_factory=list)  # in `rip time ls` order
    zone: ZoneInfo | None = None
    days: list[date] = field(default_factory=list)


def _snapshot() -> _Snapshot:
    """Your personal time from the cached schedule, the event's zone and its days."""
    with Catalog() as cat:
        cached = cat.load_schedule(cli.state.event_id)
        schedule = cached[0] if cached else Schedule()
        return _Snapshot(cli._sorted_personal(schedule), cli._zone(cat), _event_days(cat))


def _local_interval(pt: PersonalTime, zone: ZoneInfo | None) -> tuple[datetime, datetime] | None:
    interval = pt.interval()
    if interval is None:
        return None
    start, end = interval
    return (start.astimezone(zone), end.astimezone(zone)) if zone else (start, end)


def _hhmm(clock: str) -> str:
    hour, minute = cli._clock(clock)
    return f"{hour:02d}:{minute:02d}"


@dataclass
class BlockForm:
    """What the form collected, checked, with the entry the CLI would build and its warnings."""

    title: str
    day: date
    start: str  # HH:MM
    end: str  # HH:MM
    where: str
    note: str
    entry: PersonalTimeInput | None = None
    warnings: list[str] = field(default_factory=list)  # Rich markup, API text escaped

    @property
    def when(self) -> str:
        overnight = " (ends the next day)" if self.end < self.start else ""
        return f"{_day_label(self.day)}, {self.start}–{self.end}{overnight}"


def _unchanged(new: BlockForm, old: BlockForm) -> bool:
    fields = ("title", "day", "start", "end", "where", "note")
    return all(getattr(new, f) == getattr(old, f) for f in fields)


def _preview(form: BlockForm, editing: PersonalTime | None) -> services.Outcome:
    """Build the entry exactly as `rip time add / edit` would (the zone, the 5-minute rule, the
    length limit), and the clash warnings against the cached schedule, without the network.
    Value: (entry, warnings)."""

    def go() -> tuple[PersonalTimeInput, list[str]]:
        with Catalog() as cat:
            start_at = cli._local(cat, form.day, form.start)
            end_at = cli._end_after(start_at, start_at.date(), form.end, cat)
            entry = cli._entry_from(start_at, end_at, form.title, form.note, form.where)
            cached = cat.load_schedule(cli.state.event_id)
            schedule = cached[0] if cached else Schedule()
            key = f"personal:{editing.personal_time_id}" if editing else "personal:new"
            warnings = cli._item_warnings(
                cat,
                schedule,
                [cli._block_item(entry, key + (":edited" if editing else ""))],
                skip_keys=frozenset({key}) if editing else frozenset(),
            )
            return entry, warnings

    return services.run(go)


class PersonalTimeForm(ModalScreen[BlockForm | None]):
    """Add or edit one block. Returns the checked form, or None when cancelled."""

    DEFAULT_CSS = """
    PersonalTimeForm { align: center middle; }
    #pt-box { width: 72; max-width: 95%; height: auto; border: thick $primary; padding: 1 2;
              background: $surface; }
    #pt-box Label { margin-top: 1; }
    #pt-times { height: auto; }
    #pt-times Input { width: 1fr; }
    #pt-error { color: $error; height: auto; margin-top: 1; }
    #pt-buttons { height: auto; margin-top: 1; }
    #pt-buttons Button { margin-right: 2; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "pt_cancel", "Cancel")]

    def __init__(
        self,
        heading: str,
        days: list[date],
        preview: Callable[[BlockForm], services.Outcome],
        current: BlockForm | None = None,
    ):
        super().__init__()
        self.heading = heading
        self.days = list(days)
        self.preview = preview
        self.current = current
        if current is not None and current.day not in self.days:
            self.days = sorted({*self.days, current.day})  # a block outside the event's days

    def compose(self) -> ComposeResult:
        now = self.current
        with Vertical(id="pt-box"):
            yield Static(Text(self.heading, style="bold"))
            yield Label("Title")
            yield Input(
                now.title if now else "", placeholder="Team lunch", max_length=128, id="pt-title"
            )
            yield Label("Day")
            yield Select(
                [(_day_label(d), d.isoformat()) for d in self.days],
                prompt="Pick a day",
                value=now.day.isoformat()
                if now
                else (self.days[0].isoformat() if self.days else Select.NULL),
                id="pt-day",
            )
            yield Label("Start and end (event time, HH:MM)")
            with Horizontal(id="pt-times"):
                yield Input(now.start if now else "", placeholder="12:00", id="pt-start")
                yield Input(now.end if now else "", placeholder="13:00", id="pt-end")
            yield Label("Where (optional)")
            yield Input(
                now.where if now else "", placeholder="Wynn buffet", max_length=255, id="pt-where"
            )
            yield Label("Note (optional; defaults to the title)")
            yield Input(now.note if now else "", max_length=250, id="pt-note")
            yield Static("", id="pt-error")
            with Horizontal(id="pt-buttons"):
                yield Button("Save", variant="primary", id="pt-save")
                yield Button("Cancel", id="pt-cancel")

    def on_mount(self) -> None:
        self.query_one("#pt-title", Input).focus()

    def _pt_value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _pt_refuse(self, message: str) -> None:
        self.query_one("#pt-error", Static).update(Text(message))  # may quote what was typed

    def checked(self) -> BlockForm | None:
        """The form, if everything in it is usable (else the reason is shown in the form)."""
        title = self._pt_value("pt-title")
        if not title:
            self._pt_refuse("Give it a title.")
            return None
        day = self.query_one("#pt-day", Select).value
        if not isinstance(day, str):
            self._pt_refuse("Pick a day.")
            return None
        times = []
        for widget_id, name in (("pt-start", "start"), ("pt-end", "end")):
            raw = self._pt_value(widget_id)
            if not raw:
                self._pt_refuse(f"Give a {name} time (HH:MM).")
                return None
            try:
                times.append(_hhmm(raw))
            except cli.InputError as exc:
                self._pt_refuse(f"{name.capitalize()}: {exc}")
                return None
        start, end = times
        if end == start:
            self._pt_refuse("The end must differ from the start.")
            return None  # an earlier end means the next day (23:00–01:00), as `rip time` has it
        form = BlockForm(
            title=title,
            day=date.fromisoformat(day),
            start=start,
            end=end,
            where=self._pt_value("pt-where"),
            note=self._pt_value("pt-note"),
        )
        outcome = self.preview(form)
        if not outcome.ok:
            self._pt_refuse(outcome.error or "Can't use that time block.")
            return None
        form.entry, form.warnings = outcome.value
        return form

    @on(Button.Pressed, "#pt-save")
    @on(Input.Submitted)
    def save(self) -> None:
        if not self.is_current:  # a second press in the same moment: already answered
            return
        form = self.checked()
        if form is not None:
            self.dismiss(form)

    @on(Button.Pressed, "#pt-cancel")
    def action_pt_cancel(self) -> None:
        if self.is_current:
            self.dismiss(None)


class PersonalPane(Vertical):
    """Your personal time: a table of blocks, with Add, Edit, Remove and Refresh."""

    DEFAULT_CSS = """
    PersonalPane { padding: 0 1; }
    #personal-bar { height: auto; }
    #personal-bar Button { margin-right: 1; }
    #personal-hint { color: $text-muted; height: 3; content-align: left middle; width: 1fr; }
    #personal-table { height: 1fr; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("a", "personal_add", "Add", show=False),
        Binding("e", "personal_edit", "Edit", show=False),
        Binding("delete", "personal_remove", "Remove", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="personal-pane")
        self._snap = _Snapshot()

    def compose(self) -> ComposeResult:
        with Horizontal(id="personal-bar"):
            yield Button("Add", id="personal-add", variant="primary")
            yield Button("Edit", id="personal-edit")
            yield Button("Remove", id="personal-remove", variant="error")
            yield Button("Refresh", id="personal-refresh")
            yield Static("  times in the event's timezone", id="personal-hint")
        yield DataTable(id="personal-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#personal-table", DataTable).add_columns(
            "#", "Day", "Time", "Title", "Where"
        )
        self.reload()

    # -- the table -------------------------------------------------------------------------

    def reload(self) -> None:
        """Redraw from the cached schedule (no network)."""
        try:
            self._snap = _snapshot()
        except Exception as exc:  # e.g. the database busy in another tab
            self.app.notify(f"Couldn't read your schedule: {exc}", severity="error", markup=False)
            return
        table = self.query_one("#personal-table", DataTable)
        row = table.cursor_row
        table.clear()
        for n, pt in enumerate(self._snap.entries, 1):
            interval = _local_interval(pt, self._snap.zone)
            if interval is None:
                day, when = "?", "(time unreadable)"
            else:
                start, end = interval
                day, when = _day_label(start.date()), f"{start:%H:%M}–{end:%H:%M}"
            table.add_row(  # API text stays literal: Text, never markup
                Text(str(n)),
                Text(day),
                Text(when),
                Text(pt.title),
                Text(pt.location or ""),
                key=pt.personal_time_id,
            )
        if self._snap.entries:
            table.move_cursor(row=min(row, len(self._snap.entries) - 1))
        hint = "  times in the event's timezone"
        if not self._snap.entries:
            hint = "  No personal time yet: press Add (or Refresh to read your schedule)."
        self.query_one("#personal-hint", Static).update(hint)

    def selected_block(self) -> PersonalTime | None:
        table = self.query_one("#personal-table", DataTable)
        if not self._snap.entries or table.row_count == 0:
            return None
        return self._snap.entries[min(max(table.cursor_row, 0), len(self._snap.entries) - 1)]

    def _personal_pick(self) -> PersonalTime | None:
        block = self.selected_block()
        if block is None:
            self.app.notify("Pick a block in the table first.", severity="warning")
        return block

    def _personal_blocked(self) -> bool:
        """A reservation run or a sync is going: a new block could change what the run plans
        around, and a sync could save a stale schedule over the change."""
        running = getattr(self.app, "reservation_running", None)
        if running is not None and running():
            self.app.notify(
                "A reservation run is in progress; change personal time when it's done.",
                severity="warning",
            )
            return True
        imminent = getattr(self.app, "launch_imminent", None)
        if imminent is not None and imminent():
            self.app.notify(
                "GO is about to light (or is lit): change personal time after the run, or "
                "disarm first.",
                severity="warning",
            )
            return True
        syncing = getattr(self.app, "network_busy_quiet", None)
        if syncing is not None and syncing():
            self.app.notify(
                "A sync or refresh is running; try again in a moment.", severity="warning"
            )
            return True
        return False

    def _personal_busy(self) -> bool:
        if self._personal_blocked():
            return True
        if any(w.group == "personal" and not w.is_finished for w in self.workers):
            self.app.notify("Still working on the last change or refresh…", severity="warning")
            return True
        return False

    # -- add / edit / remove -----------------------------------------------------------------

    @on(Button.Pressed, "#personal-add")
    def action_personal_add(self) -> None:
        if self._personal_busy():
            return

        def done(form: BlockForm | None) -> None:
            if form is not None:
                self._personal_confirm(
                    f"Add '{form.title}' on {form.when} to your re:Invent schedule?",
                    form.warnings,
                    cli.time_add,
                    title=form.title,
                    day=form.day.isoformat(),
                    start=form.start,
                    end=form.end,
                    where=form.where or None,
                    note=form.note or None,
                )

        self.app.push_screen(
            PersonalTimeForm("Add personal time", self._snap.days, lambda f: _preview(f, None)),
            done,
        )

    @on(Button.Pressed, "#personal-edit")
    @on(DataTable.RowSelected, "#personal-table")
    def action_personal_edit(self) -> None:
        if self._personal_busy() or (block := self._personal_pick()) is None:
            return
        interval = _local_interval(block, self._snap.zone)
        current = None
        if interval is not None:
            start, end = interval
            current = BlockForm(
                title=block.title,
                day=start.date(),
                start=f"{start:%H:%M}",
                end=f"{end:%H:%M}",
                where=block.location or "",
                # A description that just repeats the title was defaulted: leave it blank, so a
                # new title brings a new default rather than keeping the old one.
                note="" if block.description == block.title else block.description,
            )
        was = cli._describe_block(block, self._snap.zone).split(" · ", 1)[0]

        def done(form: BlockForm | None) -> None:
            if form is not None and current is not None and _unchanged(form, current):
                self.app.notify("Nothing changed.", markup=False)
                return
            if form is not None:
                self._personal_confirm(
                    f"Change '{block.title}' ({was}) to '{form.title}' on {form.when}?",
                    form.warnings,
                    cli.time_edit,
                    ref=block.personal_time_id,  # the ID: numbers can shift, and -y refuses them
                    title=form.title,
                    day=form.day.isoformat(),
                    start=form.start,
                    end=form.end,
                    where=form.where,  # "" clears it
                    note=form.note,  # "" defaults it to the title
                )

        self.app.push_screen(
            PersonalTimeForm(
                "Edit personal time", self._snap.days, lambda f: _preview(f, block), current
            ),
            done,
        )

    @on(Button.Pressed, "#personal-remove")
    def action_personal_remove(self) -> None:
        if self._personal_busy() or (block := self._personal_pick()) is None:
            return
        when = cli._describe_block(block, self._snap.zone).split(" · ", 1)[0]
        self._personal_confirm(
            f"Remove '{block.title}' ({when}) from your re:Invent schedule?",
            [],
            cli.time_rm,
            ref=block.personal_time_id,
        )

    def _personal_confirm(
        self, question: str, warnings: list[str], fn: Callable[..., Any], **kwargs: Any
    ) -> None:
        from .app import ConfirmScreen  # here, not at the top: app.py imports this module

        def answered(yes: bool | None) -> None:
            if yes and not self._personal_blocked():  # re-checked: a run may have started
                self._personal_write(fn, {**kwargs, "yes": True})

        # The CLI's warning lines are Rich markup with the API text inside already escaped.
        details = Text.from_markup("\n".join(warnings)) if warnings else None
        self.app.push_screen(ConfirmScreen(question, details), answered)

    @work(thread=True, group="personal", exit_on_error=False)
    def _personal_write(self, fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        outcome = services.run(fn, **kwargs)
        self.app.call_from_thread(self._personal_write_done, outcome)

    def _personal_write_done(self, outcome: services.Outcome) -> None:
        if not outcome.ok:
            self.app.notify(outcome.error or "failed", severity="error", timeout=10, markup=False)
            # e.g. the block was deleted on the website: fetch the real list rather than keep
            # showing the stale one.
            self._personal_read()
        else:
            # The command's last line: "Added.", "Saved: …", "Removed: …" or "already there".
            lines = [ln.strip() for ln in Text.from_ansi(outcome.output).plain.splitlines()]
            said = [ln for ln in lines if ln][-1:]
            self.app.notify(said[0] if said else "Done.", timeout=8, markup=False)
        self.reload()
        refresh = getattr(self.app, "refresh_views", None)
        if outcome.ok and callable(refresh):
            refresh()  # the plan and checklist show personal time too

    # -- refresh -------------------------------------------------------------------------------

    @on(Button.Pressed, "#personal-refresh")
    def personal_refresh(self) -> None:
        if not self._personal_busy():
            self._personal_read()

    @work(thread=True, group="personal", exit_on_error=False)
    def _personal_read(self) -> None:
        def go() -> None:  # the read `rip time ls` does: fresh if signed in, else the cache
            with cli._client() as client, cli._catalog() as cat:
                cli._schedule_or_cache(client, cat)

        outcome = services.run(go)
        self.app.call_from_thread(self._personal_read_done, outcome)

    def _personal_read_done(self, outcome: services.Outcome) -> None:
        note = Text.from_ansi(outcome.output).plain.strip()
        if not outcome.ok:
            self.app.notify(outcome.error or "failed", severity="error", timeout=10, markup=False)
        elif note:  # e.g. "Using your schedule as of … Sign in to refresh it."
            self.app.notify(note, severity="warning", timeout=8, markup=False)
        else:
            self.app.notify("Personal time refreshed.")
        self.reload()
