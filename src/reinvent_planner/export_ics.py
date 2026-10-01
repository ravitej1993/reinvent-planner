"""Calendar (.ics) export.

Each event gets a UID derived from the session ID plus an increasing SEQUENCE, so calendar
subscriptions and clients such as Apple Calendar and Outlook update existing entries instead
of duplicating them. (Google Calendar's one-off file import skips events it already has.)
Times carry the event's timezone.
Reserved sessions are CONFIRMED and block your time; favorites are TENTATIVE and show as free.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar
from icalendar import Event as VEvent

from . import __version__
from .models import SEAT_AVAILABILITY_LABELS, Session, clean_text
from .planner import Item

PRODID = f"-//reinvent-planner//{__version__}//EN"
# An IANA zone name. icalendar writes X- properties unescaped, so nothing else goes in one.
_ZONE_KEY = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$")


def uid_for(item: Item, event_id: str) -> str:
    safe_key = re.sub(r"[^A-Za-z0-9._:-]", "-", item.key)
    return f"{safe_key}@{event_id}.reinvent-planner"


def describe(item: Item, session: Session | None) -> str:
    lines: list[str] = []
    status = ", ".join(sorted(k.capitalize() for k in item.kinds))
    if status:
        lines.append(f"On your list as: {status}")
    if session is not None:
        facts = [
            ("Type", session.type),
            ("Level", session.level),
            (
                "Seats",
                SEAT_AVAILABILITY_LABELS.get(
                    session.seat_availability or "", session.seat_availability
                ),
            ),
            ("Room", session.room),
            ("Topics", ", ".join(sorted(session.topics))),
            ("Services", ", ".join(sorted(session.services))),
            ("Speakers", ", ".join(session.speaker_names)),
        ]
        lines.extend(f"{label}: {value}" for label, value in facts if value)
        if session.abstract:
            lines.extend(["", session.abstract.strip()])
    return "\n".join(lines)


def build_calendar(
    entries: Sequence[tuple[Item, Session | None]],
    *,
    event_id: str,
    name: str,
    zone: ZoneInfo | None,
    refresh_hours: int | None = None,
) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    # One line, no control characters: a CR or LF here would crash icalendar or start a line.
    cal.add("x-wr-calname", " ".join(clean_text(name).split()) or "reinvent-planner")
    if refresh_hours:  # for subscribed feeds: how often clients should re-fetch
        cal.add(
            "refresh-interval", timedelta(hours=refresh_hours), parameters={"VALUE": "DURATION"}
        )
        cal.add("x-published-ttl", f"PT{refresh_hours}H")
    if zone is not None and not (zone.key and _ZONE_KEY.match(zone.key)):
        zone = None  # an odd zone name would also reach every TZID parameter: don't use it
    if zone is not None:
        cal.add("x-wr-timezone", zone.key)
    stamp = datetime.now(UTC).replace(microsecond=0)

    for item, session in entries:
        if item.start is None or item.end is None:
            continue  # nothing to put on a calendar yet
        vevent = VEvent()
        vevent.add("uid", uid_for(item, event_id))
        vevent.add("dtstamp", stamp)
        vevent.add("last-modified", stamp)
        # Increases with every export, so clients that honor SEQUENCE treat it as an update.
        vevent.add("sequence", int(stamp.timestamp()) // 60)
        start = item.start.astimezone(zone) if zone else item.start
        end = item.end.astimezone(zone) if zone else item.end
        if item.all_day:
            # The session's own local date: converting to another zone could shift the day.
            vevent.add("dtstart", item.start.date())
            vevent.add("dtend", item.end.date())
        else:
            vevent.add("dtstart", start)
            vevent.add("dtend", end)
        title = item.title if item.code == "PERSONAL" else f"{item.code} – {item.title}"
        vevent.add("summary", title)
        location = " | ".join(part for part in (item.venue, item.room) if part)
        if location:
            vevent.add("location", location)
        description = describe(item, session)
        if description:
            vevent.add("description", description)
        if session is not None and session.type:
            vevent.add("categories", [session.type])
        confirmed = bool(item.kinds & {"reserved", "personal"})
        vevent.add("status", "CONFIRMED" if confirmed else "TENTATIVE")
        vevent.add("transp", "OPAQUE" if confirmed else "TRANSPARENT")
        cal.add_component(vevent)

    cal.add_missing_timezones()
    return cal.to_ical()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "sessions"


def write_calendar(path: Path, data: bytes) -> None:
    write_file_atomic(path, data)


def write_file_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` all at once: a temp file in the same directory, renamed over the
    target. Readers never see half a file, and a symlink at `path` is replaced rather than
    written through. A new file is owner-only (0600); an existing one keeps its mode. The
    directory must exist (a mistyped path is an error, not a new folder)."""
    if path.exists() and not path.is_symlink() and not os.access(path, os.W_OK):
        raise PermissionError(13, "the file is read-only", str(path))  # as a plain write would
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rip-", suffix=".tmp")
    try:
        with contextlib.suppress(FileNotFoundError):
            existing = path.lstat()
            if stat.S_ISREG(existing.st_mode) and hasattr(os, "fchmod"):  # not on Windows
                os.fchmod(fd, stat.S_IMODE(existing.st_mode))
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
