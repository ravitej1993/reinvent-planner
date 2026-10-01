"""Reservation-day launch control: the logic behind the app's Launch tab, with no UI in it.

Two modes:

- Website copilot (e.g. Oct 6, when only the event website takes reservations): it walks your
  ranked picks one at a time while *you* reserve on the website, and never writes anything.
- API launch (from Oct 8): at your target time the GO control lights up and *you* press it; one
  press runs the same ranked reserve as `rip reserve`, narrated live.

Fair use is built in, not bolted on. Nothing here polls, retries on a timer, or fires by itself.
The countdown is local arithmetic only. One press means one bounded run, followed by a cooldown,
and a "not open yet" answer (409) is followed by a cooldown too, never by an automatic retry.
Only one run can happen at a time on this computer (a lock that `rip reserve` also takes), and a
journal written before every write lets the next start tell you what a killed run left in doubt.
"""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from .catalog import data_dir
from .planner import Item
from .reserve import Choice

RUN_COOLDOWN_SECONDS = 60  # one API quota window between human-started runs
NOT_OPEN_COOLDOWN_SECONDS = 60
READ_INTERVAL_SECONDS = 10  # the copilot's "check my schedule", at most this often


class LaunchError(Exception):
    """Launch control can't do that; the message says why."""


# -- target time -------------------------------------------------------------------------


def parse_target(text: str, zone: ZoneInfo) -> datetime:
    """'2026-10-08 09:00' in the event's time zone, as an aware UTC instant."""
    try:
        naive = datetime.strptime(" ".join(text.split()), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise LaunchError("Enter the time as YYYY-MM-DD HH:MM, in the event's time.") from exc
    local = naive.replace(tzinfo=zone)
    # A time skipped or repeated by a DST change: refuse rather than guess.
    if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise LaunchError(f"{text} doesn't exist in {zone.key} (a clock change); pick another.")
    if local.fold == 0 and local.replace(fold=1).utcoffset() != local.utcoffset():
        raise LaunchError(f"{text} happens twice in {zone.key} (a clock change); pick another.")
    return local.astimezone(UTC)


def describe_target(target: datetime, zone: ZoneInfo, here: ZoneInfo | None = None) -> str:
    """'Thu Oct 8, 09:00 PDT', plus your own time when you're somewhere else."""

    def fmt(moment: datetime) -> str:
        return f"{moment:%a %b} {moment.day}, {moment:%H:%M %Z}"

    there = target.astimezone(zone)
    mine = target.astimezone(here) if here else target.astimezone()
    if mine.utcoffset() == there.utcoffset():
        return fmt(there)
    return f"{fmt(there)} (your time: {fmt(mine)})"


def countdown(seconds: float) -> str:
    """'T-2d 04:05:06', 'T-04:05:06', or 'T-00:00:00' once reached."""
    left = max(0, math.ceil(seconds))  # never show 0 before it's really time
    days, rest = divmod(left, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"T-{days}d {clock}" if days else f"T-{clock}"


# -- the API launch state machine --------------------------------------------------------


class Phase(Enum):
    IDLE = "idle"  # no target, or not armed
    ARMED = "armed"  # counting down; GO is dark
    GO = "go"  # target reached; GO is lit and waits for a person
    RUNNING = "running"  # one run in progress
    COOLDOWN = "cooldown"  # a run just ended; GO is dark for a while
    NOT_OPEN = "not_open"  # the API said reservations aren't open; cooling down


def _steady_source() -> tuple[Callable[[], float], bool]:
    """A clock nobody sets, for spotting wall-clock steps, and whether it keeps counting while
    the computer sleeps. Linux's CLOCK_BOOTTIME and macOS's CLOCK_MONOTONIC (clock_gettime)
    do; time.monotonic, the fallback elsewhere (e.g. Windows), may not."""
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return (lambda: time.clock_gettime(time.CLOCK_BOOTTIME)), True
    if sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
        return (lambda: time.clock_gettime(time.CLOCK_MONOTONIC)), True
    return time.monotonic, False


steady_clock, STEADY_COUNTS_SLEEP = _steady_source()


# Where the steady clock may not count sleep, a bigger jump is taken to be sleep, not a step.
MAX_CLOCK_STEP_SECONDS = 300
MAX_SKEW_SECONDS = 300  # a Date header further off than this is a stale proxy, not the API
NOT_OPEN_COOLDOWNS = (60, 120, 300)  # repeated "not open" answers wait longer each time
MAX_COOLDOWN_SECONDS = max(NOT_OPEN_COOLDOWNS)
# "Not open" answers count as a streak only when they come this close together, so practice
# presses on earlier days don't lengthen the wait on the real day.
STREAK_WINDOW_SECONDS = 120


@dataclass
class Launch:
    """Decides only what the GO control may do. It makes no calls and has no timers, so the
    app can tick it as often as it likes without any network traffic.

    GO needs an API preflight for the current target (`arm(..., preflighted=True)`), so arming
    the website copilot can never light it. The cooldown after a run is saved with the event,
    so reopening the app or switching to `rip reserve` doesn't skip it."""

    clock: Callable[[], float] = time.time
    steady: Callable[[], float] = steady_clock
    steady_counts_sleep: bool = STEADY_COUNTS_SLEEP
    event_id: str | None = None  # where the cooldown is kept; None keeps it in memory
    target: datetime | None = None
    skew: float = 0.0  # server clock minus ours, measured at arming, corrected for steps
    clock_stepped: float = 0.0  # total wall-clock steps seen since arming (for display)
    big_jump: bool = False  # a step too big to trust the preflight: arm again
    _armed: bool = False
    _preflighted: bool = False
    _running: bool = False
    _cooldown_until: float = 0.0
    _not_open: bool = False
    _strikes: int = 0
    _last: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.event_id:
            saved = load_cooldown(self.event_id, self.clock())
            self._cooldown_until = saved.until
            self._not_open, self._strikes = saved.not_open, saved.strikes

    def now(self) -> float:
        return self.clock() + self.skew

    def arm(self, target: datetime, skew: float = 0.0, *, preflighted: bool = False) -> None:
        if self._running:
            raise LaunchError("A run is in progress.")
        self.target, self._armed, self._preflighted = target, True, preflighted
        self.skew = skew if abs(skew) <= MAX_SKEW_SECONDS else 0.0
        self.clock_stepped, self._last, self.big_jump = 0.0, None, False

    def disarm(self) -> None:
        if self._running:
            raise LaunchError("A run is in progress; cancel it first.")
        self._armed = self._preflighted = False

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def preflighted(self) -> bool:
        return self._armed and self._preflighted

    def observe(self) -> float:
        """Call on each tick. If the wall clock was stepped (e.g. NTP fixed it, or you set it by
        hand), move the skew so the countdown stays on the API's clock. Returns the step seen.

        A step over MAX_CLOCK_STEP_SECONDS also takes back the preflight (GO needs a fresh Arm)
        where the steady clock counts sleep; where it may not, such a jump is taken to be sleep
        and ignored. Gradual NTP slewing moves both clocks together and isn't seen, which is
        fine: its error is bounded by the skew measured at arming."""
        wall, steady = self.clock(), self.steady()
        last, self._last = self._last, (wall, steady)
        if last is None:
            return 0.0
        step = (wall - last[0]) - (steady - last[1])
        if abs(step) <= 1.0:
            return 0.0
        if step > 0 and not self.steady_counts_sleep:
            # The wall clock ran ahead of a steady clock that may not count sleep: that's
            # either sleep (the wall clock is right) or a step (it isn't). Don't guess: the
            # preflight is taken back, and GO needs a fresh Arm.
            self.big_jump = True
            self._preflighted = False
            return 0.0
        if abs(step) > MAX_CLOCK_STEP_SECONDS:
            self.big_jump = True
            self._preflighted = False
        self.skew -= step
        self.clock_stepped += step
        return step

    def seconds_left(self) -> float:
        return self.target.timestamp() - self.now() if self.target else 0.0

    def cooldown_left(self) -> float:
        # The end itself is pulled in (not just the number shown), so a clock set back after a
        # run can't keep GO dark for longer than the longest cooldown.
        self._cooldown_until = min(self._cooldown_until, self.clock() + MAX_COOLDOWN_SECONDS)
        return max(0.0, self._cooldown_until - self.clock())

    def phase(self) -> Phase:
        if self._running:
            return Phase.RUNNING
        if self.cooldown_left() > 0:
            return Phase.NOT_OPEN if self._not_open else Phase.COOLDOWN
        if not self._armed or self.target is None:
            return Phase.IDLE
        return Phase.GO if self.seconds_left() <= 0 else Phase.ARMED

    def start_run(self) -> bool:
        """The GO press. True means: run now (exactly once). Anything but a lit GO after an
        API preflight is ignored."""
        if self.phase() is not Phase.GO or not self._preflighted:
            return False
        self._running = True
        return True

    def finish_run(self, stopped: str | None, *, sent: bool = True) -> None:
        """After a run. With an event, the run itself saved the cooldown (it's shared with
        `rip reserve`), so read it back; otherwise work it out here. A run that sent nothing
        (cancelled at once, or refused before starting) costs no cooldown."""
        self._running = False
        if not sent:
            if self.event_id:
                saved = load_cooldown(self.event_id, self.clock())
                self._cooldown_until, self._not_open = saved.until, saved.not_open
                self._strikes = saved.strikes
            return
        if self.event_id:
            saved = load_cooldown(self.event_id, self.clock())
            self._cooldown_until, self._not_open, self._strikes = (
                saved.until,
                saved.not_open,
                saved.strikes,
            )
            if saved.until > self.clock():
                return
        streak = live_strikes(
            Cooldown(self._cooldown_until, self._not_open, self._strikes), self.clock()
        )
        self._cooldown_until, self._not_open, self._strikes = next_cooldown(
            self.clock(), stopped, streak
        )


@dataclass
class Cooldown:
    until: float = 0.0  # wall-clock time
    not_open: bool = False
    strikes: int = 0  # "not open" answers in a row


def live_strikes(cooldown: Cooldown, now: float) -> int:
    """The "not open" streak, or 0 if the last cooldown ended more than a moment ago."""
    return cooldown.strikes if now - cooldown.until <= STREAK_WINDOW_SECONDS else 0


def next_cooldown(now: float, stopped: str | None, strikes: int) -> tuple[float, bool, int]:
    """After a run: one quota window, or a growing wait after each "not open" in a row (pass
    `live_strikes(...)`, not the raw count)."""
    if stopped == "closed":
        wait = NOT_OPEN_COOLDOWNS[min(strikes, len(NOT_OPEN_COOLDOWNS) - 1)]
        return now + wait, True, strikes + 1
    return now + RUN_COOLDOWN_SECONDS, False, 0


def load_cooldown(event_id: str, now: float | None = None) -> Cooldown:
    try:
        data = json.loads((_state_dir() / f"cooldown-{_safe(event_id)}.json").read_text())
        until, not_open, strikes = float(data["until"]), data["not_open"], data["strikes"]
    except (OSError, ValueError, KeyError, TypeError):
        return Cooldown()
    if not (math.isfinite(until) and type(strikes) is int and 0 <= strikes <= 100):
        return Cooldown()
    # A cooldown never ends later than the longest one from now: a clock set back after a run
    # (or a hand-edited file) can't keep GO dark for longer than that.
    now = time.time() if now is None else now
    return Cooldown(min(until, now + MAX_COOLDOWN_SECONDS), not_open is True, strikes)


def save_cooldown(event_id: str, cooldown: Cooldown) -> None:
    _write_atomic(
        _state_dir() / f"cooldown-{_safe(event_id)}.json",
        json.dumps(
            {"until": cooldown.until, "not_open": cooldown.not_open, "strikes": cooldown.strikes}
        ),
    )


def clock_skew(date_header: str | None, received_at: float) -> float | None:
    """Server clock minus ours, from an HTTP Date header. The header has 1-second resolution,
    so this reads up to a second low (GO can light up to a second late, never early). A value
    beyond MAX_SKEW_SECONDS comes from a stale proxy or captive portal, and is ignored."""
    if not date_header:
        return None
    from email.utils import parsedate_to_datetime

    try:
        server = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if server.tzinfo is None:
        return None
    skew = server.timestamp() - received_at
    return skew if abs(skew) <= MAX_SKEW_SECONDS else None


def fingerprint(choices: Sequence[Choice]) -> str:
    """Identifies a set of picks (ranks, options in order with their times and places,
    walk-ups), so GO can refuse to run picks that changed after the preflight showed them,
    including a `rip sync` that moved a session."""
    import hashlib

    def when(moment: datetime | None) -> str | None:
        return moment.isoformat() if moment else None

    shape = [
        (
            c.rank,
            c.walk_up,
            [(o.key, when(o.start), when(o.end), o.venue, o.room) for o in c.options],
        )
        for c in choices
    ]
    return hashlib.sha256(json.dumps(shape).encode()).hexdigest()


# -- website copilot ---------------------------------------------------------------------


class Mark(Enum):
    BOOKED = "booked"
    FULL = "full"
    SKIPPED = "skipped"


@dataclass
class Copilot:
    """Walks ranked picks for reserving by hand on the website. Marks are your own notes: they
    never feed into an API reservation (a run always reads your real schedule first), and a
    schedule check overrides them."""

    choices: Sequence[Choice]
    marks: dict[str, Mark] = field(default_factory=dict)  # option key -> your mark
    confirmed: set[str] = field(default_factory=set)  # seen on your schedule
    _last_read: float = -1e18

    def _live(self) -> list[Choice]:
        return [c for c in self.choices if not c.walk_up]

    def done(self, choice: Choice) -> bool:
        return any(
            self.marks.get(o.key) is Mark.BOOKED or o.key in self.confirmed for o in choice.options
        ) or any(self.marks.get(o.key) is Mark.SKIPPED for o in choice.options)

    def option(self, choice: Choice) -> Item | None:
        """The option to try next for this pick: the pick, or the first backup not marked full."""
        for option in choice.options:
            if self.marks.get(option.key) is not Mark.FULL:
                return option
        return None

    def current(self) -> tuple[Choice, Item] | None:
        for choice in self._live():
            if self.done(choice):
                continue
            option = self.option(choice)
            if option is not None:
                return choice, option
        return None

    def mark(self, mark: Mark) -> None:
        now = self.current()
        if now is not None:
            self.marks[now[1].key] = mark

    def may_read(self, clock: float) -> bool:
        since = clock - self._last_read
        return since >= READ_INTERVAL_SECONDS or since < 0  # a clock set back never locks it

    def apply_schedule(self, reserved: Iterable[str], clock: float) -> list[str]:
        """What your real schedule says. Returns the codes you marked booked that it doesn't
        show, so the app can point them out."""
        self._last_read = clock
        self.confirmed = set(reserved)
        by_key = {o.key: o for c in self.choices for o in c.options}
        return [
            by_key[key].code
            for key, mark in self.marks.items()
            if mark is Mark.BOOKED and key not in self.confirmed and key in by_key
        ]

    def status(self, choice: Choice) -> str:
        if any(o.key in self.confirmed for o in choice.options):
            return "confirmed"
        if any(self.marks.get(o.key) is Mark.BOOKED for o in choice.options):
            return "booked"
        if any(self.marks.get(o.key) is Mark.SKIPPED for o in choice.options):
            return "skipped"
        if self.option(choice) is None:
            return "unfilled"
        return "todo"


# -- one run at a time, and a journal of what was sent -----------------------------------


def _state_dir() -> Path:
    path = data_dir() / "launch"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextlib.contextmanager
def reservation_lock(event_id: str, wait: float = 0.0) -> Iterator[None]:
    """Held for the whole of a reserve run, by the app and by `rip reserve`. It's an OS file
    lock, so it's released when the process ends, even if it's killed. `wait` seconds are
    allowed for a brief holder (another tab checking for a cut-off run) to let go."""
    path = _state_dir() / f"reserve-{_safe(event_id)}.lock"
    try:
        handle = open(path, "a+b")  # noqa: SIM115 - held open for the lock's lifetime
    except OSError as exc:
        raise LaunchError(
            f"Couldn't open {path} ({exc.strerror or exc}). Check the folder's permissions, or "
            "set RIP_DATA_DIR to a folder you own on this computer."
        ) from exc
    try:
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    _lock(handle)
                    break
                except OSError as exc:
                    if exc.errno not in HELD_ERRNOS or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        except OSError as exc:
            if exc.errno in HELD_ERRNOS:
                raise LaunchError(
                    "Another reservation run is in progress on this computer (in `rip reserve` "
                    "or the app). Let it finish first."
                ) from exc
            raise LaunchError(
                f"Couldn't lock {path} ({exc.strerror or exc}). If your data folder is on a "
                "network drive, set RIP_DATA_DIR to a folder on this computer."
            ) from exc
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


# errno values that mean "someone else holds the lock"; anything else is a real failure.
HELD_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK}

if sys.platform == "win32":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_in_progress(event_id: str) -> bool:
    """Whether some process holds the reservation lock right now (a run is going)."""
    try:
        handle = open(_state_dir() / f"reserve-{_safe(event_id)}.lock", "a+b")  # noqa: SIM115
    except OSError:
        return False
    try:
        try:
            _lock(handle)
        except OSError as exc:
            return exc.errno in HELD_ERRNOS
        _unlock(handle)
        return False
    finally:
        handle.close()


@dataclass
class Journal:
    """Session IDs sent in a run, written *before* each write and removed when the run ends.
    Finding one on the next start means a run was cut off with writes possibly in flight."""

    event_id: str

    @property
    def path(self) -> Path:
        return _state_dir() / f"journal-{_safe(self.event_id)}.json"

    def record(self, round_number: int, session_ids: Sequence[str]) -> None:
        sent = self.load() or {"event": self.event_id, "sent": [], "started": time.time()}
        sent["sent"] = list(dict.fromkeys([*sent["sent"], *session_ids]))
        sent["round"] = round_number
        _write_atomic(self.path, json.dumps(sent))

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("sent"), list):
            return None
        data["sent"] = [s for s in data["sent"] if isinstance(s, str)]
        return data

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def _write_atomic(path: Path, text: str) -> None:
    """Write, flush to disk, then rename into place, so a crash or power cut leaves either the
    old file or the new one, never a torn one."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def _safe(event_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in event_id)[:128]
