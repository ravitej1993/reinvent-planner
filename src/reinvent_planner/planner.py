"""Schedule checks and the reservation checklist.

Two things make a plan unworkable: sessions that overlap, and back-to-back sessions in
venues too far apart to walk between in time. Everything here is pure logic over `Item`s, so
it's easy to test and doesn't touch the network.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from .auth import config_dir
from .models import PersonalTime, Session

Kind = Literal["reserved", "favorite", "ranked", "backup", "personal"]


@dataclass
class Item:
    key: str
    code: str
    title: str
    start: datetime | None
    end: datetime | None
    venue: str | None
    room: str | None
    kinds: set[str] = field(default_factory=set)
    seat_availability: str | None = None
    all_day: bool = False

    @property
    def place(self) -> str:
        """Where it is, for display: the venue, else the start of the room."""
        return self.venue or (self.room or "").split("|", 1)[0].strip() or "?"

    @property
    def timed(self) -> bool:
        return self.start is not None and self.end is not None and not self.all_day


def item_from_session(session: Session, zone: ZoneInfo | None, kind: Kind) -> Item:
    interval = session.interval(zone)
    return Item(
        key=session.session_id,
        code=session.code,
        title=session.title,
        start=interval[0] if interval else None,
        end=interval[1] if interval else None,
        venue=session.venue,
        room=session.room,
        kinds={kind},
        seat_availability=session.seat_availability,
        all_day=bool(session.is_all_day_session),
    )


def item_from_personal_time(entry: PersonalTime) -> Item:
    interval = entry.interval()
    return Item(
        key=f"personal:{entry.personal_time_id}",
        code="PERSONAL",
        title=entry.title,
        start=interval[0] if interval else None,
        end=interval[1] if interval else None,
        venue=entry.location,
        room=None,
        kinds={"personal"},
    )


def merge_items(items: Iterable[Item]) -> list[Item]:
    """Combine duplicates (e.g. a session that's both reserved and a favorite)."""
    merged: dict[str, Item] = {}
    for item in items:
        if item.key in merged:
            merged[item.key].kinds |= item.kinds
        else:
            merged[item.key] = item
    return sorted(merged.values(), key=_sort_key)


def _sort_key(item: Item) -> tuple:
    return (item.start is None, item.start or datetime.max.replace(tzinfo=None), item.code)


# ---------------------------------------------------------------------------
# Travel times
# ---------------------------------------------------------------------------


# The monorail is only worth mentioning when it beats walking by at least this many minutes:
# the figures are rounded, and a one-minute edge is within the model's error.
MONORAIL_MARGIN_MINUTES = 2


@dataclass(frozen=True)
class Monorail:
    """An off-peak Las Vegas Monorail trip between two venues (door to door, before the room
    buffer), oriented from the first venue to the second."""

    from_station: str
    to_station: str
    minutes: int
    walking: int  # minutes of walking within `minutes`
    ride: int  # minutes on the train

    @property
    def route(self) -> str:
        return f"{self.from_station} → {self.to_station}"

    def reversed(self) -> Monorail:
        return Monorail(self.to_station, self.from_station, self.minutes, self.walking, self.ride)

    def beats(self, walking_minutes: int | None) -> bool:
        """Clearly faster than walking (by the margin), so worth suggesting."""
        return walking_minutes is None or self.minutes <= walking_minutes - MONORAIL_MARGIN_MINUTES


def _whole(value) -> int:
    """An integer from a TOML value; reject booleans and fractional numbers instead of
    silently truncating them."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"expected a whole number, got {value!r}")
    return int(value)


def corrections_path(event_id: str) -> Path:
    return config_dir() / "venues" / f"{event_id}.local.toml"


def save_corrections(event_id: str, minutes: dict[str, int]) -> Path:
    """Write the user's corrections file (just a [minutes] table, easy to share)."""
    path = corrections_path(event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Your own travel-time corrections for {event_id}, layered over the bundled table.",
        "# Written by `rip venues set`; safe to edit or share with teammates.",
        "[minutes]",
    ]
    # json.dumps gives a valid TOML basic string, whatever the key contains.
    lines += [f"{json.dumps(pair)} = {int(value)}" for pair, value in sorted(minutes.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_VENUE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _pair(text: str) -> frozenset[str]:
    a, _, b = text.partition("|")
    return frozenset((a.strip(), b.strip()))


class TravelTableError(ValueError):
    """A user's travel-time override file couldn't be read."""


class TravelTimes:
    def __init__(
        self,
        aliases: dict[str, list[str]],
        minutes: dict[str, int],
        same: int,
        unknown: int,
        names: dict[str, str] | None = None,
        walking: dict[str, dict] | None = None,
        room_aliases: dict[str, dict[str, list[str]]] | None = None,
        monorail: dict[str, dict] | None = None,
        notes: dict[str, str] | None = None,
        no_shuttle: list[str] | None = None,
        shuttle_venues: list[str] | None = None,
        same_complex: list[str] | None = None,
    ):
        self._order = list(aliases)
        # Match longer aliases first so "caesars forum" wins over a shorter overlapping alias.
        self._aliases = sorted(
            ((alias.lower(), key) for key, names in aliases.items() for alias in names),
            key=lambda pair: -len(pair[0]),
        )
        self._minutes: dict[frozenset[str], int] = {}
        for pair, value in minutes.items():
            a, _, b = pair.partition("|")
            self._minutes[frozenset((a.strip(), b.strip()))] = _whole(value)
        self.same_venue_minutes = same
        self.unknown_minutes = unknown
        self.names = dict(names or {})
        self._notes = {_pair(k): str(v) for k, v in (notes or {}).items()}
        self._no_shuttle = {_pair(k) for k in (no_shuttle or [])}
        self._shuttle_venues = set(shuttle_venues or [])
        self._same_complex = {_pair(k) for k in (same_complex or [])}
        self.has_shuttle_info = bool(shuttle_venues)
        self.ignored_corrections: list[str] = []  # pairs the table doesn't know, or same-venue
        for key in aliases:
            if not _VENUE_KEY.match(key):
                raise ValueError(f"venue keys may use only letters, digits, - and _: {key!r}")
        self.corrected: set[frozenset[str]] = set()  # pairs overridden by `rip venues set`
        self._monorail: dict[tuple[str, str], Monorail] = {}
        for pair, value in (monorail or {}).items():
            a, _, b = pair.partition("|")
            self._monorail[(a.strip(), b.strip())] = Monorail(
                from_station=str(value["from"]),
                to_station=str(value["to"]),
                minutes=_whole(value["minutes"]),
                walking=_whole(value.get("walking", 0)),
                ride=_whole(value.get("ride", 0)),
            )
        # Some venues span buildings the catalog doesn't tell apart ("Wynn/Encore"); the room
        # name does. {venue key: [(word in room, building key)]}, longest words first.
        self._room_aliases = {
            key: sorted(
                ((word.lower(), sub) for sub, words in subs.items() for word in words),
                key=lambda pair: -len(pair[0]),
            )
            for key, subs in (room_aliases or {}).items()
        }
        self._walking: dict[frozenset[str], tuple[int, int]] = {}
        for pair, value in (walking or {}).items():
            a, _, b = pair.partition("|")
            self._walking[frozenset((a.strip(), b.strip()))] = (
                _whole(value["meters"]),
                _whole(value["minutes"]),
            )

    @classmethod
    def load(cls, event_id: str) -> TravelTimes:
        """The user's full override file if present, else the bundled file, else defaults;
        then the user's own corrections (`rip venues set`) on top."""
        override = config_dir() / "venues" / f"{event_id}.toml"
        bundled = resources.files("reinvent_planner") / "data" / f"{event_id}.toml"
        if override.exists():
            table = cls._read(override)
        elif bundled.is_file():
            table = cls.from_toml(bundled.read_text(encoding="utf-8"))
        else:
            table = cls({}, {}, same=5, unknown=30)
        local = corrections_path(event_id)
        if local.exists():
            try:
                corrections = tomllib.loads(local.read_text(encoding="utf-8")).get("minutes", {})
                table.apply_corrections(corrections)
            except (tomllib.TOMLDecodeError, TypeError, ValueError, AttributeError) as exc:
                raise TravelTableError(
                    f"Your travel-time corrections {local} are invalid: {exc}"
                ) from exc
        return table

    @classmethod
    def _read(cls, path: Path) -> TravelTimes:
        try:
            return cls.from_toml(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise TravelTableError(f"Your travel-time file {path} is invalid: {exc}") from exc

    @classmethod
    def from_toml(cls, text: str) -> TravelTimes:
        data = tomllib.loads(text)
        return cls(
            data.get("aliases", {}),
            data.get("minutes", {}),
            same=int(data.get("same_venue_minutes", 5)),
            unknown=int(data.get("unknown_minutes", 30)),
            names=data.get("names", {}),
            walking=data.get("walking", {}),
            room_aliases=data.get("room_aliases", {}),
            monorail=data.get("monorail", {}),
            notes=data.get("notes", {}),
            no_shuttle=data.get("no_shuttle"),
            shuttle_venues=data.get("shuttle_venues"),
            same_complex=data.get("same_complex"),
        )

    def venue_keys(self) -> list[str]:
        """Venue keys in table order (as listed under [aliases])."""
        return list(self._order)

    def name(self, key: str) -> str:
        return self.names.get(key, key)

    def planned(self, a: str, b: str) -> int | None:
        """Planning minutes between two venue keys, if the table has them."""
        if a == b:
            return self.same_venue_minutes
        return self._minutes.get(frozenset((a, b)))

    def note(self, a: str, b: str) -> str | None:
        return self._notes.get(frozenset((a, b)))

    def shuttle(self, a: str, b: str) -> bool | str | None:
        """Whether a conference shuttle ran between the venues, per the table's source:
        True, False, "indoors" (one complex: walk), or None when the source doesn't say."""
        pair = frozenset((a, b))
        if not self.has_shuttle_info or a == b:
            return None
        if pair in self._same_complex:
            return "indoors"
        if pair in self._no_shuttle:
            return False
        return True if {a, b} <= self._shuttle_venues else None

    def keys_with_figures(self) -> list[str]:
        """Venue keys that appear in at least one travel figure (so not bare room groupings
        like "Wynn/Encore", which only exist to be split into buildings)."""
        used = {k for pair in self._minutes for k in pair}
        return [k for k in self._order if k in used]

    def apply_corrections(self, minutes: dict[str, object]) -> None:
        """Layer the user's own figures (`rip venues set`) over the table."""
        known = set(self._order)
        for pair, value in minutes.items():
            key = _pair(pair)
            figure = _whole(value)
            if not 0 < figure <= 180:
                raise ValueError(f"{pair}: minutes must be between 1 and 180, not {figure}")
            if len(key) != 2 or (known and not key <= known):
                self.ignored_corrections.append(pair)  # a typo, or a venue this table lacks
                continue
            self._minutes[key] = figure
            self.corrected.add(key)

    def resolve(self, text: str) -> str | None:
        """A venue key from what a user typed: a key, a display name, or an alias."""
        wanted = text.strip().lower()
        for key in self._order:
            if wanted in (key.lower(), self.name(key).lower()):
                return key
        return self.venue_key(text) if any(a in wanted for a, _ in self._aliases) else None

    def monorail(self, a: str, b: str) -> Monorail | None:
        """The off-peak monorail trip from venue key `a` to `b`, if the table lists one."""
        if (a, b) in self._monorail:
            return self._monorail[(a, b)]
        if (b, a) in self._monorail:
            return self._monorail[(b, a)].reversed()
        return None

    def walking(self, a: str, b: str) -> tuple[int, int] | None:
        """Measured (meters, minutes) of street walking between two venue keys, if known."""
        return self._walking.get(frozenset((a, b)))

    @property
    def has_table(self) -> bool:
        return bool(self._aliases)

    def is_known(self, venue: str) -> bool:
        """Whether a venue name matches one of the table's aliases."""
        text = venue.lower()
        return any(alias in text for alias, _ in self._aliases)

    def venue_key(self, venue: str | None) -> str | None:
        if not venue:
            return None
        text = venue.lower()
        for alias, key in self._aliases:
            if alias in text:
                return key
        return text.strip()

    def place_key(self, venue: str | None, room: str | None) -> str | None:
        """The venue key for an item, falling back to its room when `venue` is empty.

        re:Invent 2026 leaves `venue` empty for Wynn/Encore and Caesars Palace sessions and
        starts `room` with the venue instead ("Wynn/Encore | Level 2 | ..."). The room is only
        used when it starts with a venue this table knows, so events without a table (or
        rooms like "Vision Hall") don't produce made-up room-to-room transfers.
        """
        key: str | None = None
        if venue:
            key = self.venue_key(venue)
        elif room:
            head = room.split("|", 1)[0].lower()
            key = next((k for alias, k in self._aliases if alias in head), None)
        if key in self._room_aliases and room:
            text = room.lower()
            key = next((sub for word, sub in self._room_aliases[key] if word in text), key)
        return key

    def minutes(self, a: str | None, b: str | None) -> tuple[int, bool] | None:
        """Travel minutes between two venue names and whether the figure is known.

        None means there's nothing to check (a venue is missing, e.g. personal time).
        """
        return self._minutes_for(self.venue_key(a), self.venue_key(b))

    def minutes_between(self, a: Item, b: Item) -> tuple[int, bool] | None:
        """Like `minutes`, for two items, using the room when a venue is missing."""
        return self._minutes_for(self.place_key(a.venue, a.room), self.place_key(b.venue, b.room))

    def _minutes_for(self, ka: str | None, kb: str | None) -> tuple[int, bool] | None:
        if ka is None or kb is None:
            return None
        if ka == kb:
            return self.same_venue_minutes, True
        known = self._minutes.get(frozenset((ka, kb)))
        if known is not None:
            return known, True
        return self.unknown_minutes, False


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    kind: Literal["overlap", "tight"]
    first: Item
    second: Item
    gap_minutes: int = 0
    needed_minutes: int = 0
    estimate_known: bool = True

    def describe(self) -> str:
        if self.kind == "overlap":
            return f"{self.first.code} and {self.second.code} overlap"
        guess = "" if self.estimate_known else " (rough guess, venue not in travel table)"
        return (
            f"{self.first.code} → {self.second.code}: {self.gap_minutes} min to get from "
            f"{self.first.place} to {self.second.place}, needs ~{self.needed_minutes}{guess}"
        )


def clash(a: Item, b: Item, travel: TravelTimes) -> Issue | None:
    if not (a.timed and b.timed):
        return None
    first, second = (a, b) if a.start <= b.start else (b, a)  # type: ignore[operator]
    if second.start < first.end:  # type: ignore[operator]
        return Issue("overlap", first, second)
    need = travel.minutes_between(first, second)
    if need is None:
        return None
    gap = int((second.start - first.end).total_seconds() // 60)  # type: ignore[operator]
    needed, known = need
    if gap < needed:
        return Issue("tight", first, second, gap, needed, known)
    return None


def blocks(a: Item, b: Item, travel: TravelTimes) -> Issue | None:
    """A clash that stops a reservation or marks a pick as not fitting: only a real overlap.

    Travel between venues never blocks (a product decision): re:Invent runs on a 30-minute
    grid, so almost every back-to-back pair in different venues is tight, and whether to make
    the dash is the attendee's call. Travel problems are warnings, from `find_issues`.
    All-day and untimed items never block: the API is the judge for those.
    """
    issue = clash(a, b, travel)
    return issue if issue is not None and issue.kind == "overlap" else None


def find_issues(items: Sequence[Item], travel: TravelTimes) -> list[Issue]:
    """Every overlap, plus tight transfers between each item and the next thing after it."""
    timed = sorted((i for i in items if i.timed), key=lambda i: (i.start, i.code))
    issues: list[Issue] = []
    for index, a in enumerate(timed):
        next_start: datetime | None = None
        for b in timed[index + 1 :]:
            if b.start >= a.end:  # type: ignore[operator]
                # Only the first thing(s) after `a` finishes can be a tight transfer from `a`.
                if next_start is None:
                    next_start = b.start
                if b.start != next_start:
                    break
                issue = clash(a, b, travel)
                if issue:
                    issues.append(issue)
            else:
                issues.append(Issue("overlap", a, b))
    return issues


METERS_PER_MILE = 1609.344


def distance_parts(meters: int) -> tuple[str, str]:
    """("2.4 km", "1.5 mi"), with "<0.1" rather than a misleading "0.0"."""

    def one_decimal(value: float, unit: str) -> str:
        return f"<0.1 {unit}" if round(value, 1) < 0.1 else f"{value:.1f} {unit}"

    return one_decimal(meters / 1000, "km"), one_decimal(meters / METERS_PER_MILE, "mi")


def format_distance(meters: int) -> str:
    """Kilometres and miles, e.g. "1.6 km (1.0 mi)"."""
    km, mi = distance_parts(meters)
    return f"{km} ({mi})"


def group_by_day(items: Iterable[Item], zone: ZoneInfo | None) -> dict[date | None, list[Item]]:
    days: dict[date | None, list[Item]] = defaultdict(list)
    for item in sorted(items, key=_sort_key):
        if item.start is None:
            days[None].append(item)
        else:
            days[(item.start.astimezone(zone) if zone else item.start).date()].append(item)
    return dict(days)


# ---------------------------------------------------------------------------
# Reservation checklist
# ---------------------------------------------------------------------------


@dataclass
class BackupStatus:
    item: Item
    clashes_with: list[Item]
    reserved: bool = False


@dataclass
class ChecklistEntry:
    rank: int
    item: Item
    status: Literal["reserve", "reserved", "clash", "unscheduled"]
    clashes_with: list[Item] = field(default_factory=list)
    backups: list[BackupStatus] = field(default_factory=list)
    # Tight transfers to or from this pick among what's kept: warnings, never blockers.
    travel_warnings: list[Issue] = field(default_factory=list)


def build_checklist(
    ranked: Sequence[tuple[int, Item, Sequence[Item]]],
    fixed: Sequence[Item],
    travel: TravelTimes,
) -> list[ChecklistEntry]:
    """Walk your picks in rank order and keep the ones that fit.

    `fixed` is what's already on your calendar (reservations and personal time). Each pick
    either fits, is already reserved, or clashes with something you've already kept, in which
    case it's still listed so you know to reserve it only if a higher pick falls through.

    Backups are checked in a second pass against everything kept, including lower-ranked
    picks, so a backup that would collide with a later pick is flagged rather than looking safe.
    """
    kept: list[Item] = list(fixed)
    fixed_keys = {i.key for i in fixed}
    entries: list[ChecklistEntry] = []
    for rank, item, _backups in sorted(ranked, key=lambda r: r[0]):
        if item.key in fixed_keys:
            entry = ChecklistEntry(rank, item, "reserved")
        elif not item.timed:
            entry = ChecklistEntry(rank, item, "unscheduled")
        else:
            clashes = [k for k in kept if blocks(item, k, travel)]
            entry = ChecklistEntry(rank, item, "clash" if clashes else "reserve", clashes)
            if not clashes:
                kept.append(item)
        entries.append(entry)
    tight = [i for i in find_issues(kept, travel) if i.kind == "tight"]
    for entry in entries:
        entry.travel_warnings = [i for i in tight if entry.item.key in (i.first.key, i.second.key)]
    # By the pick's key, not its rank: two picks can share a rank.
    backups_by_pick = {item.key: backups for _rank, item, backups in ranked}
    for entry in entries:
        for backup in backups_by_pick.get(entry.item.key, ()):
            others = [
                k
                for k in kept
                if k.key not in (entry.item.key, backup.key) and blocks(backup, k, travel)
            ]
            entry.backups.append(BackupStatus(backup, others, reserved=backup.key in fixed_keys))
    return entries
