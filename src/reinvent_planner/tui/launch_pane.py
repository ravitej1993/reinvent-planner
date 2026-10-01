"""The Launch tab: reservation-day countdown, website copilot and API launch (see launch.py).

Only the logic in launch.Launch decides what GO may do; this file draws it. The ticking clock
is local arithmetic only: nothing here makes a network call unless a person pressed a button.
"""

from __future__ import annotations

import threading
import time
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Digits, Input, RichLog, Select, Static

from .. import launch, services
from ..launch import Copilot, Launch, LaunchError, Mark, Phase
from ..reserve import Event
from . import visuals

FAIR_USE = (
    "Fair use: nothing here fires by itself, polls, or retries full sessions. At your target "
    "time GO lights up and you press it: one press is one run, then a one-minute cooldown."
)
STOP_WORDS = {
    "closed": "Reservations aren't open through the API yet (409). Nothing else was sent.",
    "throttled": "Rate limited by the API. Wait a minute before trying again.",
    "round_limit": "Stopped after the maximum number of rounds. Press GO again later.",
    "error": "The API failed; check your schedule before trying again.",
    "cancelled": "Cancelled before the next round. Nothing else was sent.",
}


class LaunchPane(Vertical):
    DEFAULT_CSS = """
    LaunchPane { padding: 0 1; }
    #launch-top { height: auto; }
    #clock-box { width: auto; height: auto; padding: 0 2 0 0; }
    #tminus { color: $text-muted; }
    #clock { width: auto; }
    #launch-info { width: 1fr; height: auto; }
    #phase { text-style: bold; }
    #launch-controls { height: auto; margin-top: 1; }
    #launch-controls Input { width: 36; }
    #launch-controls Select { width: 34; }
    #launch-controls Button, #launch-controls Checkbox { margin-left: 1; }
    #api-box, #web-box { height: 1fr; margin-top: 1; }
    #api-buttons, #web-buttons { height: auto; }
    #api-buttons Button, #web-buttons Button { margin-right: 1; }
    #go { min-width: 16; text-style: bold; }
    #preflight { height: auto; max-height: 50%; }
    #log { height: 1fr; border: round $accent; }
    #board { height: auto; margin: 1 0 0 0; }
    #now { text-style: bold; margin: 1 0; }
    #fair-use { color: $text-muted; height: auto; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("b", "mark('booked')", "Booked", show=False),
        Binding("f", "mark('full')", "Full", show=False),
        Binding("x", "mark('skipped')", "Skip", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="launch-pane")
        self.control = Launch(event_id=services.current_event())
        self.copilot: Copilot | None = None
        self.zone = services.event_zone()
        self._cancel = threading.Event()
        self._last_event_at = 0.0
        self._waiting_until = 0.0
        self._waiting_cancellable = True
        self._fingerprint = ""
        self._choices: list = []  # the picks shown at arming, for the live board
        self._events: list[Event] = []
        self.mode = "api"

    def compose(self) -> ComposeResult:
        with Horizontal(id="launch-top"):
            with Vertical(id="clock-box"):
                yield Static("T-MINUS", id="tminus")
                yield Digits("--:--:--", id="clock")
            with Vertical(id="launch-info"):
                yield Static("Not armed", id="phase")
                yield Static("", id="target-info")
                yield Static("", id="skew")
                yield Static(FAIR_USE, id="fair-use")
        with Horizontal(id="launch-controls"):
            yield Input(placeholder="YYYY-MM-DD HH:MM (event time)", id="target")
            yield Select(
                [("API launch (from Oct 8)", "api"), ("Website copilot (Oct 6)", "web")],
                value="api",
                allow_blank=False,
                id="mode",
            )
            yield Button("Arm", id="arm", variant="primary")
            yield Button("Disarm", id="disarm")
            yield Checkbox("Hide email", id="presentation")
        with Vertical(id="api-box"):
            with VerticalScroll(id="preflight-scroll"):
                yield Static("Arm to check your sign-in and see the plan.", id="preflight")
            with Horizontal(id="api-buttons"):
                yield Button("GO", id="go", variant="error", disabled=True)
                yield Button("Cancel run", id="cancel-run", disabled=True)
            yield Static("", id="board")
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        with Vertical(id="web-box"):
            yield Static("", id="now")
            with Horizontal(id="web-buttons"):
                yield Button("Booked (b)", id="mark-booked", variant="success")
                yield Button("Full (f)", id="mark-full", variant="warning")
                yield Button("Skip (x)", id="mark-skipped")
                yield Button("Check my schedule", id="check")
            with VerticalScroll():
                yield Static("", id="picks")

    def on_mount(self) -> None:
        self.query_one("#web-box").display = False
        self.query_one("#disarm", Button).display = False
        self.set_interval(0.25, self._tick)  # local arithmetic only; never a network call

    # -- the clock ---------------------------------------------------------------------

    def _tick(self) -> None:
        if self.control.armed and (self.control.observe() or self.control.big_jump):
            stepped = self.control.clock_stepped
            if self.control.big_jump:
                what = (
                    f"Your computer's clock jumped {stepped / 60:+.0f} min since arming."
                    if stepped
                    else "Your computer slept, or its clock changed, since arming."
                )
                note = Text(f"{what} Press Arm again before GO.", style="bold red")
            else:
                note = Text(
                    f"Your computer's clock was changed by {stepped:+.0f} s since arming; the "
                    "countdown adjusted. Arm again to re-check against the API.",
                    style="yellow",
                )
            self.query_one("#skew", Static).update(note)
        if (
            self.control.big_jump
            and self.control.armed
            and self.control.phase() is not Phase.RUNNING
        ):
            self.control.disarm()  # so Arm shows again (also after a jump seen mid-run)
        phase = self.control.phase()
        left = self.control.seconds_left() if self.control.target else None
        clock = launch.countdown(left)[2:] if left is not None else "--:--:--"
        self.query_one("#clock", Digits).update(clock.replace("d ", ":"))
        self.query_one("#phase", Static).update(self._phase_text(phase))
        go = self.query_one("#go", Button)
        go.disabled = self.mode != "api" or phase is not Phase.GO or not self.control.preflighted
        self.query_one("#cancel-run", Button).disabled = phase is not Phase.RUNNING
        armed = self.control.armed  # Arm is available whenever you're not armed, even cooling down
        self.query_one("#arm", Button).display = not armed
        self.query_one("#disarm", Button).display = armed and phase is not Phase.RUNNING

    def _phase_text(self, phase: Phase) -> Text:
        if phase is Phase.IDLE:
            return Text("Not armed", style="dim")
        if phase is Phase.ARMED:
            return Text("ARMED · counting down", style="bold cyan")
        if phase is Phase.GO:
            if self.mode == "web":
                return Text("OPEN · reserve on the website now", style="bold green")
            return Text("GO · press GO to reserve your ranked picks", style="bold green")
        if phase is Phase.RUNNING:
            waiting = int(self._waiting_until - time.monotonic() + 0.999)
            if waiting > 0:
                hint = (
                    "Cancel works now"
                    if self._waiting_cancellable
                    else "checking your schedule: this can't be cut short"
                )
                return Text(
                    f"⏸ WAITING ON THE API · retrying in {waiting} s ({hint})",
                    style="bold yellow",
                )
            quiet = int(time.monotonic() - self._last_event_at)
            return Text(f"RUNNING · {quiet} s since the last update", style="bold yellow")
        wait = int(self.control.cooldown_left() + 0.999)
        if phase is Phase.NOT_OPEN:
            return Text(f"NOT OPEN YET · GO is dark for {wait} s", style="bold magenta")
        return Text(f"COOLDOWN · GO is dark for {wait} s (fair use)", style="bold magenta")

    # -- arming ------------------------------------------------------------------------

    @on(Select.Changed, "#mode")
    def mode_changed(self, event: Select.Changed) -> None:
        if self.control.phase() is Phase.RUNNING:
            return
        if str(event.value) != self.mode and self.control.armed:
            self.control.disarm()  # each mode arms its own way; GO needs an API preflight
            self.copilot = None
            self.query_one("#preflight", Static).update(
                "Arm to check your sign-in and see the plan."
            )
            self.query_one("#now", Static).update("")
            self.query_one("#picks", Static).update("")
        self.mode = str(event.value)
        self.query_one("#api-box").display = self.mode == "api"
        self.query_one("#web-box").display = self.mode == "web"

    @on(Checkbox.Changed, "#presentation")
    def presentation_changed(self, event: Checkbox.Changed) -> None:
        self.app.presentation = event.value  # type: ignore[attr-defined]
        self.app.refresh_views()  # type: ignore[attr-defined]

    @on(Button.Pressed, "#arm")
    def arm_pressed(self) -> None:
        try:
            target = launch.parse_target(self.query_one("#target", Input).value, self.zone)
        except LaunchError as exc:
            self.app.notify(str(exc), severity="error", markup=False)
            return
        self.query_one("#target-info", Static).update(
            Text("Your target: " + launch.describe_target(target, self.zone))
        )
        if self.mode == "web":
            self._arm_copilot(target)
        else:
            self.query_one("#arm", Button).disabled = True
            self.query_one("#preflight", Static).update("Checking your sign-in and the plan…")
            self._preflight(target)

    @work(thread=True, group="launch", exit_on_error=False)
    def _preflight(self, target) -> None:
        outcome = services.launch_preflight(max(70, self.size.width - 4))
        self.app.call_from_thread(self._preflight_done, target, outcome)

    def _preflight_done(self, target, outcome: services.Outcome) -> None:
        self.query_one("#arm", Button).disabled = False
        body = self.query_one("#preflight", Static)
        if not outcome.ok:
            body.update(Text(outcome.error or "failed", style="red"))
            return
        body.update(Text.from_ansi(outcome.output))
        if self.mode != "api":
            return  # switched to the copilot while this was running
        preflight: services.Preflight = outcome.value
        self._fingerprint = preflight.fingerprint
        picks = services.launch_choices()
        self._choices = picks.value if picks.ok else []
        self._events = []
        self._draw_board()
        skew = preflight.skew
        self.control.arm(target, skew or 0.0, preflighted=True)
        if skew is None:
            note = "Couldn't read the API's clock (or it looked wrong); the countdown uses yours."
        elif abs(skew) >= 2:
            direction = "slow" if skew > 0 else "fast"
            note = f"Your clock is {abs(skew):.0f} s {direction}; the countdown uses the API's."
        else:
            note = "Your clock matches the API's."
        self.query_one("#skew", Static).update(Text(note, style="dim"))
        self._tick()

    def _arm_copilot(self, target) -> None:
        outcome = services.launch_choices()
        if not outcome.ok:
            self.app.notify(outcome.error or "failed", severity="error", markup=False)
            return
        self.copilot = Copilot(outcome.value)
        self.control.arm(target)
        self._draw_copilot()
        self._tick()
        self.query_one("#mark-booked", Button).focus()  # b / f / x work from here

    @on(Button.Pressed, "#disarm")
    def disarm_pressed(self) -> None:
        try:
            self.control.disarm()
        except LaunchError as exc:
            self.app.notify(str(exc), severity="warning", markup=False)
        self._tick()

    # -- API launch --------------------------------------------------------------------

    @on(Button.Pressed, "#go")
    def go_pressed(self) -> None:
        if self.mode == "api" and self.app.syncing():  # type: ignore[attr-defined]
            self.app.notify(
                "A sync, refresh or personal-time change is still running; press GO again "
                "in a moment.",
                severity="warning",
            )
            return
        if self.mode != "api" or not self.control.start_run():
            return  # only a lit GO does anything, and only once
        self._cancel.clear()
        self._events = []
        self._draw_board()
        self._last_event_at = time.monotonic()
        log = self.query_one("#log", RichLog)
        log.write(Text(f"▶ GO pressed at {time.strftime('%H:%M:%S')}", style="bold green"))
        self._tick()
        self._launch()

    @work(thread=True, group="launch", exit_on_error=False)
    def _launch(self) -> None:
        def on_event(event: Event) -> None:
            self.app.call_from_thread(self._log_event, event)

        outcome = services.Outcome(error="The run stopped unexpectedly; check your schedule.")
        try:
            outcome = services.launch_reserve(
                on_event,
                self._cancel.is_set,
                max(70, self.size.width - 4),
                fingerprint=self._fingerprint,
            )
        finally:  # whatever happens, the run is over: never leave GO or a quit stuck
            self.app.call_from_thread(self._launch_done, outcome)

    def _log_event(self, event: Event) -> None:
        self._last_event_at = time.monotonic()
        if event.kind == "waiting":
            self._waiting_until = time.monotonic() + float(event.detail or 0)
            self._waiting_cancellable = event.reason != "readback"
        else:
            self._waiting_until = 0.0
        self.query_one("#log", RichLog).write(event_line(event))
        if event.kind != "waiting":
            self._events.append(event)
            self._draw_board()

    def _draw_board(self) -> None:
        board = self.query_one("#board", Static)
        if not self._choices:
            board.update("")
            return
        board.update(
            visuals.render_launch_strip(
                self._choices, self._events, width=max(20, self.size.width - 2)
            )
        )

    def _launch_done(self, outcome: services.Outcome) -> None:
        self._waiting_until = 0.0
        report = outcome.value
        stopped = report.stopped if report is not None else "error"
        self.control.finish_run(stopped, sent=report is not None and report.rounds > 0)
        try:
            self._show_result(outcome, report)
        finally:
            self.app.run_finished()  # type: ignore[attr-defined]

    def _show_result(self, outcome: services.Outcome, report) -> None:
        log = self.query_one("#log", RichLog)
        if outcome.ok and report is not None:
            done = len(report.reserved)
            log.write(Text(f"■ Done: {done} newly reserved.", style="bold"))
            self.query_one("#preflight", Static).update(Text.from_ansi(outcome.output))
        else:
            log.write(Text(f"■ {outcome.error or 'failed'}", style="bold red"))
        self.app.refresh_views()  # type: ignore[attr-defined]
        self._tick()

    @on(Button.Pressed, "#cancel-run")
    def cancel_run(self) -> None:
        self._cancel.set()
        self.query_one("#log", RichLog).write(
            Text("… cancel requested: stopping before the next round", style="yellow")
        )

    def focus_start(self) -> None:
        """Opening the tab: straight to the target time, unless already armed."""
        if not self.control.armed:
            self.query_one("#target", Input).focus()

    def launch_imminent(self) -> bool:
        """Armed for the API launch and within a minute of GO, GO lit, or cooling down before
        GO lights again (e.g. after "not open yet"): the moments when a sync would only get in
        the way of the next press."""
        phase = self.control.phase()
        if self.mode != "api" or not self.control.preflighted:
            return False
        if phase in (Phase.GO, Phase.NOT_OPEN, Phase.COOLDOWN):
            return True
        return phase is Phase.ARMED and self.control.seconds_left() < 60

    def running(self) -> bool:
        return self.control.phase() is Phase.RUNNING

    # -- website copilot ---------------------------------------------------------------

    def action_mark(self, mark: str) -> None:
        if self.mode != "web" or self.copilot is None:
            return
        self.copilot.mark(Mark(mark))
        self._draw_copilot()

    @on(Button.Pressed, "#mark-booked")
    def booked(self) -> None:
        self.action_mark("booked")

    @on(Button.Pressed, "#mark-full")
    def full(self) -> None:
        self.action_mark("full")

    @on(Button.Pressed, "#mark-skipped")
    def skipped(self) -> None:
        self.action_mark("skipped")

    @on(Button.Pressed, "#check")
    def check_pressed(self) -> None:
        if self.copilot is None:
            self.app.notify("Arm the copilot first.", severity="warning")
            return
        if not self.copilot.may_read(time.time()):
            self.app.notify(
                f"At most one check every {launch.READ_INTERVAL_SECONDS} s.", severity="warning"
            )
            return
        self.copilot._last_read = time.time()  # claimed now, so a double press can't slip in
        self._check()

    @work(thread=True, group="launch", exit_on_error=False)
    def _check(self) -> None:
        outcome = services.copilot_check()
        self.app.call_from_thread(self._check_done, outcome)

    def _check_done(self, outcome: services.Outcome) -> None:
        if not outcome.ok or self.copilot is None:
            self.app.notify(outcome.error or "failed", severity="error", markup=False)
            return
        disagree = self.copilot.apply_schedule(outcome.value, time.time())
        if disagree:
            self.app.notify(
                "Marked booked, but not on your schedule: " + ", ".join(disagree),
                severity="warning",
                timeout=10,
                markup=False,
            )
        self._draw_copilot()

    def _draw_copilot(self) -> None:
        if self.copilot is None:
            return
        now = self.copilot.current()
        line = Text()
        if now is None:
            line.append("All picks handled. ", style="bold green")
            line.append("Press Check my schedule to confirm against your real schedule.")
        else:
            choice, option = now
            line.append("NOW  ", style="bold reverse green")
            line.append(f" #{choice.rank} ", style="bold")
            line.append(option.code, style="bold cyan")
            line.append(f"  {option.title}")
            if option.key != choice.primary.key:
                line.append(f"  (backup for {choice.primary.code})", style="yellow")
            if option.place:
                line.append(f"  · {option.place}", style="dim")
        self.query_one("#now", Static).update(line)
        glyph = {
            "confirmed": ("✓✓", "bold green"),
            "booked": ("✓ ", "green"),
            "skipped": ("– ", "dim"),
            "unfilled": ("✗ ", "red"),
            "todo": ("· ", ""),
        }
        picks = Text()
        for choice in self.copilot.choices:
            if choice.walk_up:
                continue
            status = self.copilot.status(choice)
            mark, style = glyph[status]
            picks.append(f"{mark} #{choice.rank:<3}", style=style)
            picks.append(" / ".join(o.code for o in choice.options), style=style)
            picks.append(f"  {status}\n", style="dim")
        self.query_one("#picks", Static).update(picks)


def event_line(event: Event) -> Text:
    """One live log line. Session text is kept literal (never markup)."""
    stamp = time.strftime("%H:%M:%S")
    line = Text(f"{stamp} ", style="dim")
    if event.kind == "stopped":
        line.append("■ ", style="bold magenta")
        line.append(STOP_WORDS.get(event.reason, event.reason), style="magenta")
        if event.detail:
            line.append(f" ({event.detail})", style="dim")
        return line
    item, choice = event.item, event.choice
    rank = f"#{choice.rank} " if choice else ""
    code = item.code if item else "?"
    if event.kind == "sending":
        line.append("→ ", style="cyan")
        line.append(f"{rank}{code} ", style="bold")
        line.append(item.title if item else "", style="dim")
        if event.backup and choice:
            line.append(f"  (backup for {choice.primary.code})", style="yellow")
    elif event.kind == "booked":
        line.append("✓ ", style="bold green")
        line.append(f"{rank}{code} booked", style="green")
    elif event.kind == "refused":
        line.append("✗ ", style="bold red")
        line.append(f"{rank}{code} ", style="red")
        line.append(event.reason or "refused", style="red")
        keys = [o.key for o in choice.options] if choice else []
        if item and item.key in keys and keys.index(item.key) < len(keys) - 1:
            line.append("  → a backup is next", style="yellow")
    elif event.kind == "waiting":
        line.append("⏸ ", style="yellow")
        if event.reason == "readback":
            line.append(
                f"checking your schedule; retrying in {event.detail} s (can't be cut short)",
                style="yellow",
            )
        else:
            line.append(f"the API asked us to wait; retrying in {event.detail} s", style="yellow")
    elif event.kind == "in_doubt":
        line.append("? ", style="bold yellow")
        line.append(
            f"{rank}{code} may be reserved (no confirmation); its backups are held back. "
            "Check your schedule.",
            style="yellow",
        )
    elif event.kind == "retrying":
        line.append("… ", style="yellow")
        line.append(f"{rank}{code} no confirmation yet; checking again", style="yellow")
    return line
