"""Local SQLite copy of an event's catalog, plus your cached schedule and ranked plan.

The API has no search or filtering, so we download the whole catalog and search it here.
The database lives in your user data directory with owner-only permissions: a registered
event's catalog isn't public, and your schedule is personal.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Event, Schedule, Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    event_id          TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    code              TEXT NOT NULL,
    title             TEXT NOT NULL,
    type              TEXT,
    level             TEXT,
    venue             TEXT,
    room              TEXT,
    start_utc         TEXT,
    end_utc           TEXT,
    is_reservable     INTEGER,
    seat_availability TEXT,
    data              TEXT NOT NULL,
    synced_at         TEXT NOT NULL,
    PRIMARY KEY (event_id, session_id)
);
CREATE INDEX IF NOT EXISTS sessions_by_code ON sessions (event_id, code COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS sessions_by_start ON sessions (event_id, start_utc);
CREATE TABLE IF NOT EXISTS schedules (
    event_id   TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_items (
    event_id   TEXT NOT NULL,
    session_id TEXT NOT NULL,
    rank       INTEGER,
    backup_for TEXT,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (event_id, session_id)
);
CREATE TABLE IF NOT EXISTS syncs (
    event_id  TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    total     INTEGER NOT NULL,
    added     INTEGER NOT NULL,
    removed   INTEGER NOT NULL,
    changed   INTEGER NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5 (
    event_id UNINDEXED, session_id UNINDEXED, code, title, abstract, speakers, tags,
    tokenize = 'porter unicode61'
);
"""

# Fields whose change is worth telling someone about after a sync.
WATCHED_FIELDS = ("title", "start_utc", "end_utc", "venue", "room")
AVAILABLE_BANDS = ("available", "limited", "veryLimited", "walkUp")
# How full a session is, least to most. The API gives bands, not seat counts.
FULLNESS = {"available": 0, "limited": 1, "veryLimited": 2, "unavailable": 3}
# Session types that need a laptop, per the re:Invent session-type guide. Labs say a laptop
# "may be required", so they aren't included.
LAPTOP_TYPES = ("bootcamp", "builders", "exam prep", "gamified", "workshop")
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class CatalogError(Exception):
    pass


@dataclass
class SyncResult:
    total: int
    added: list[Session] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)  # (code, title)
    changed: list[tuple[Session, list[str]]] = field(default_factory=list)
    # Seat availability that got worse since the last sync: (session, before, after).
    filling: list[tuple[Session, str, str]] = field(default_factory=list)


@dataclass
class SearchFilters:
    query: str | None = None
    types: Sequence[str] = ()
    level: str | None = None
    days: Sequence[str] = ()
    venue: str | None = None
    topics: Sequence[str] = ()
    services: Sequence[str] = ()
    roles: Sequence[str] = ()
    areas: Sequence[str] = ()
    features: Sequence[str] = ()
    industries: Sequence[str] = ()
    # Restrict to these session IDs (e.g. your favorites); None means no restriction.
    only_ids: frozenset[str] | None = None
    laptop_required: bool = False
    reservable_only: bool = False
    available_only: bool = False
    limit: int | None = 50


@dataclass
class PlanItem:
    session_id: str
    rank: int | None
    backup_for: str | None


def data_dir() -> Path:
    if override := os.environ.get("RIP_DATA_DIR"):
        return Path(override)
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "reinvent-planner"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "reinvent-planner"
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "reinvent-planner"
    )


def _make_private(path: str) -> None:
    """Create the database file owner-only (0600), or tighten an existing one, before SQLite
    opens it. A symlink is refused: it could point SQLite at someone else's file."""
    if os.path.islink(path):
        raise CatalogError(
            f"{path} is a symbolic link; refusing to open it. Replace it with a regular file."
        )
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise CatalogError(f"Couldn't open {path}: {exc.strerror or exc}.") from exc
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Catalog:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            directory = data_dir()
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / "catalog.sqlite3"
        self.path = str(path)
        if self.path != ":memory:" and os.name == "posix":
            _make_private(self.path)
        self.db = sqlite3.connect(self.path)
        # Another rip process (a sync in one terminal, the TUI in another) may hold the write
        # lock for a moment: wait for it instead of failing with "database is locked".
        self.db.execute("PRAGMA busy_timeout = 10000")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.has_fts = self._try_create_fts()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _try_create_fts(self) -> bool:
        try:
            self.db.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False  # this SQLite was built without FTS5; fall back to LIKE

    # -- events -----------------------------------------------------------------

    def save_event(self, event: Event) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO events VALUES (?, ?, ?)",
                (event.event_id, event.model_dump_json(by_alias=True), _now()),
            )

    def get_event(self, event_id: str) -> Event | None:
        row = self.db.execute("SELECT data FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return Event.model_validate_json(row["data"]) if row else None

    def zone(self, event_id: str) -> ZoneInfo | None:
        """The timezone to show times in: the one most sessions report, else the event's.

        Session timezones win because the event-level field isn't always right (one live
        event lists Europe/Ulyanovsk while all its sessions say Asia/Dubai).
        """
        row = self.db.execute(
            "SELECT json_extract(data, '$.sessionTime.timezone') AS tz, COUNT(*) AS n "
            "FROM sessions WHERE event_id = ? AND tz IS NOT NULL "
            "GROUP BY tz ORDER BY n DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if row and (zone := _zone_or_none(row["tz"])):
            return zone
        event = self.get_event(event_id)
        return event.zone() if event else None

    # -- sessions -----------------------------------------------------------------

    def replace_sessions(
        self, event: Event, sessions: Iterable[Session], *, force: bool = False
    ) -> SyncResult:
        """Store a complete catalog walk and report what changed since the last one.

        Refuses (unless `force`) when the walk returned nothing, or under half of what we
        had: that looks like an API glitch, and replacing would wipe the local catalog.
        """
        sessions = list(sessions)
        zone = event.zone()
        old = {
            row["session_id"]: row
            for row in self.db.execute(
                "SELECT session_id, code, title, start_utc, end_utc, venue, room, "
                "seat_availability FROM sessions WHERE event_id = ?",
                (event.event_id,),
            )
        }
        new_count = len({s.session_id for s in sessions})
        if old and not force and (new_count == 0 or new_count < len(old) / 2):
            raise CatalogError(
                f"The API returned {new_count} sessions, but the local catalog has {len(old)}. "
                "Not replacing it in case that's a glitch. Run `rip sync --force` if the "
                "catalog really shrank."
            )
        result = SyncResult(total=0)
        synced_at = _now()
        seen: set[str] = set()
        with self.db:
            for session in sessions:
                if session.session_id in seen:
                    continue
                seen.add(session.session_id)
                row = self._session_row(event.event_id, session, zone, synced_at)
                previous = old.get(session.session_id)
                if previous is None:
                    if old:  # a first sync isn't "new sessions"
                        result.added.append(session)
                else:
                    changed = [
                        name
                        for name in WATCHED_FIELDS
                        if (previous[name] or None) != (row[name] or None)
                    ]
                    if changed:
                        result.changed.append((session, changed))
                    before, after = previous["seat_availability"], row["seat_availability"]
                    if _got_fuller(before, after):
                        result.filling.append((session, before or "", after))
                self.db.execute(
                    "INSERT OR REPLACE INTO sessions VALUES (:event_id, :session_id, :code, "
                    ":title, :type, :level, :venue, :room, :start_utc, :end_utc, "
                    ":is_reservable, :seat_availability, :data, :synced_at)",
                    row,
                )
            for session_id, previous in old.items():
                if session_id not in seen:
                    result.removed.append((previous["code"], previous["title"]))
                    self.db.execute(
                        "DELETE FROM sessions WHERE event_id = ? AND session_id = ?",
                        (event.event_id, session_id),
                    )
            result.total = len(seen)
            if self.has_fts:
                self._rebuild_fts(event.event_id)
            self.db.execute(
                "INSERT INTO syncs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    synced_at,
                    result.total,
                    len(result.added),
                    len(result.removed),
                    len(result.changed),
                ),
            )
        return result

    @staticmethod
    def _session_row(event_id: str, s: Session, zone: ZoneInfo | None, synced_at: str) -> dict:
        interval = s.interval(zone)
        start, end = (
            (interval[0].astimezone(UTC).isoformat(), interval[1].astimezone(UTC).isoformat())
            if interval
            else (None, None)
        )
        return {
            "event_id": event_id,
            "session_id": s.session_id,
            "code": s.code,
            "title": s.title,
            "type": s.type,
            "level": s.level,
            "venue": s.venue,
            "room": s.room,
            "start_utc": start,
            "end_utc": end,
            "is_reservable": None if s.is_reservable is None else int(s.is_reservable),
            "seat_availability": s.seat_availability,
            "data": s.model_dump_json(by_alias=True, exclude_none=True),
            "synced_at": synced_at,
        }

    def _rebuild_fts(self, event_id: str) -> None:
        self.db.execute("DELETE FROM sessions_fts WHERE event_id = ?", (event_id,))
        for row in self.db.execute(
            "SELECT session_id, data FROM sessions WHERE event_id = ?", (event_id,)
        ).fetchall():
            s = Session.model_validate_json(row["data"])
            self.db.execute(
                "INSERT INTO sessions_fts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    s.session_id,
                    s.code,
                    s.title,
                    s.abstract or "",
                    " ".join(s.speaker_names),
                    " ".join(s.tags()),
                ),
            )

    def venues(self, event_id: str) -> list[str]:
        """Distinct places sessions are in: `venue`, or the start of `room` when it's empty."""
        rows = self.db.execute(
            "SELECT DISTINCT venue, room FROM sessions WHERE event_id = ?", (event_id,)
        )
        places = set()
        for r in rows:
            place = r["venue"] or (r["room"] or "").split("|", 1)[0].strip()
            if place:
                places.add(place)
        return sorted(places)

    def session_count(self, event_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE event_id = ?", (event_id,)
        ).fetchone()[0]

    def last_sync(self, event_id: str) -> str | None:
        row = self.db.execute(
            "SELECT MAX(synced_at) FROM syncs WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row[0] if row else None

    def get_sessions(self, event_id: str, session_ids: Iterable[str]) -> dict[str, Session]:
        ids = list(dict.fromkeys(session_ids))
        found: dict[str, Session] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                "SELECT session_id, data FROM sessions "  # noqa: S608 - only "?" placeholders
                f"WHERE event_id = ? AND session_id IN ({marks})",
                (event_id, *chunk),
            ):
                found[row["session_id"]] = Session.model_validate_json(row["data"])
        return found

    def resolve(self, event_id: str, ref: str) -> Session:
        """Find a session by its code (e.g. AIM301, any case) or its session ID."""
        ref = ref.strip()
        rows = self.db.execute(
            "SELECT data FROM sessions "
            "WHERE event_id = ? AND (session_id = ? OR code = ? COLLATE NOCASE)",
            (event_id, ref, ref),
        ).fetchall()
        if not rows:
            if self.session_count(event_id) == 0:
                raise CatalogError(f"The {event_id} catalog isn't downloaded yet. Run `rip sync`.")
            raise CatalogError(f"No session {ref!r} in {event_id}. Try `rip search {ref}`.")
        if len(rows) > 1:
            options = ", ".join(Session.model_validate_json(r["data"]).session_id for r in rows)
            raise CatalogError(f"{ref!r} matches several sessions ({options}); use the session ID.")
        return Session.model_validate_json(rows[0]["data"])

    def search(self, event_id: str, filters: SearchFilters) -> list[Session]:
        sql = "SELECT s.data FROM sessions s"
        where = ["s.event_id = ?"]
        params: list[object] = [event_id]
        if filters.query:
            words = re.findall(r"\w+", filters.query)
            if words and self.has_fts:
                sql += (
                    " JOIN sessions_fts f"
                    " ON f.event_id = s.event_id AND f.session_id = s.session_id"
                )
                where.append("sessions_fts MATCH ?")
                params.append(" ".join(f'"{w}"*' for w in words))
            elif words:
                for w in words:
                    where.append("(s.title LIKE ? OR s.code LIKE ? OR s.data LIKE ?)")
                    params.extend([f"%{w}%"] * 3)
        if filters.types:
            where.append("(" + " OR ".join("s.type LIKE ?" for _ in filters.types) + ")")
            params.extend(f"%{t}%" for t in filters.types)
        if filters.laptop_required:
            where.append("(" + " OR ".join("s.type LIKE ?" for _ in LAPTOP_TYPES) + ")")
            params.extend(f"%{t}%" for t in LAPTOP_TYPES)
        if filters.only_ids is not None and not filters.only_ids:
            return []
        if filters.level:
            where.append("s.level LIKE ?")
            params.append(f"{filters.level}%")
        if filters.venue:
            where.append("(s.venue LIKE ? OR (COALESCE(s.venue, '') = '' AND s.room LIKE ?))")
            params.extend([f"%{filters.venue}%", f"{filters.venue}%"])
        if filters.reservable_only:
            where.append("s.is_reservable = 1")
        if filters.available_only:
            where.append(f"s.seat_availability IN ({','.join('?' * len(AVAILABLE_BANDS))})")
            params.extend(AVAILABLE_BANDS)
        sql += (
            " WHERE " + " AND ".join(where) + " ORDER BY s.start_utc IS NULL, s.start_utc, s.code"
        )
        sessions = [Session.model_validate_json(r["data"]) for r in self.db.execute(sql, params)]

        zone = self.zone(event_id)
        if filters.only_ids is not None:  # filtered here: no SQL parameter-count limits
            sessions = [s for s in sessions if s.session_id in filters.only_ids]
        sessions = [s for s in sessions if _matches_tags(s, filters)]
        if filters.days:
            sessions = [s for s in sessions if _matches_day(s, filters.days, zone)]
        return sessions[: filters.limit] if filters.limit else sessions

    # -- schedule cache -------------------------------------------------------------

    def save_schedule(self, event_id: str, schedule: Schedule) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO schedules VALUES (?, ?, ?)",
                (event_id, schedule.model_dump_json(by_alias=True), _now()),
            )

    def load_schedule(self, event_id: str) -> tuple[Schedule, str] | None:
        row = self.db.execute(
            "SELECT data, fetched_at FROM schedules WHERE event_id = ?", (event_id,)
        ).fetchone()
        return (Schedule.model_validate_json(row["data"]), row["fetched_at"]) if row else None

    # -- ranked plan (local only) ----------------------------------------------------

    def set_rank(self, event_id: str, session_id: str, rank: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO plan_items (event_id, session_id, rank, backup_for, added_at) "
                "VALUES (?, ?, ?, NULL, ?) ON CONFLICT (event_id, session_id) "
                "DO UPDATE SET rank = excluded.rank, backup_for = NULL",
                (event_id, session_id, rank, _now()),
            )

    def add_backup(self, event_id: str, session_id: str, backup_for: str) -> None:
        if session_id == backup_for:
            raise CatalogError("A session can't be its own backup.")
        with self.db:
            self.db.execute(
                "INSERT INTO plan_items (event_id, session_id, rank, backup_for, added_at) "
                "VALUES (?, ?, NULL, ?, ?) ON CONFLICT (event_id, session_id) "
                "DO UPDATE SET rank = NULL, backup_for = excluded.backup_for",
                (event_id, session_id, backup_for, _now()),
            )

    def remove_plan_item(self, event_id: str, session_id: str) -> bool:
        with self.db:
            cur = self.db.execute(
                "DELETE FROM plan_items WHERE event_id = ? AND (session_id = ? OR backup_for = ?)",
                (event_id, session_id, session_id),
            )
        return cur.rowcount > 0

    def plan_items(self, event_id: str) -> list[PlanItem]:
        rows = self.db.execute(
            "SELECT session_id, rank, backup_for FROM plan_items WHERE event_id = ? "
            "ORDER BY rank IS NULL, rank, added_at",
            (event_id,),
        )
        return [PlanItem(r["session_id"], r["rank"], r["backup_for"]) for r in rows]


def _got_fuller(before: str | None, after: str | None) -> bool:
    """Whether seat availability got worse. From no data at all (as when reservations first
    open), only "very limited" and "full" are worth an alert."""
    if after not in FULLNESS:
        return False
    if before is None:
        return FULLNESS[after] >= FULLNESS["veryLimited"]
    return before in FULLNESS and FULLNESS[after] > FULLNESS[before]


def _zone_or_none(name: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _contains_any(values: Sequence[str], needles: Sequence[str]) -> bool:
    lowered = [v.lower() for v in values]
    return any(n.lower() in v for n in needles for v in lowered)


def _matches_tags(s: Session, f: SearchFilters) -> bool:
    return (
        (not f.topics or _contains_any(s.topics, f.topics))
        and (not f.services or _contains_any(s.services, f.services))
        and (not f.roles or _contains_any(s.roles, f.roles))
        and (not f.areas or _contains_any(s.areas_of_interest, f.areas))
        and (not f.features or _contains_any(s.features, f.features))
        and (not f.industries or _contains_any(s.industries, f.industries))
    )


def _matches_day(s: Session, days: Sequence[str], zone: ZoneInfo | None) -> bool:
    interval = s.interval(zone)
    if interval is None:
        return False
    local_day = interval[0].date()
    for day in days:
        key = day.strip().lower()[:3]
        if key in WEEKDAYS and local_day.weekday() == WEEKDAYS[key]:
            return True
        try:
            if date.fromisoformat(day.strip()) == local_day:
                return True
        except ValueError:
            continue
    return False
