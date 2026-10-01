"""CSV export for spreadsheets (Excel, Google Sheets, Numbers).

Written with the csv module, so commas, quotes and newlines in titles are escaped properly.
Cells that a spreadsheet could treat as a formula (starting with = + - @ or their full-width
forms, even after leading whitespace, or starting with a tab or carriage return) are prefixed
with a single quote, so a crafted session title can't run as a formula when the file is opened
(CSV injection).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

from .export_ics import write_file_atomic
from .models import SEAT_AVAILABILITY_LABELS, Session
from .planner import Item

COLUMNS = (
    "Day",
    "Date",
    "Start",
    "End",
    "Code",
    "Title",
    "Type",
    "Level",
    "Venue",
    "Room",
    "Seats",
    "On my list",
    "Rank",
    "Topics",
    "Services",
    "Speakers",
    "Session ID",
)

# Characters that start a formula, including full-width forms some importers normalize.
_FORMULA_STARTS = frozenset("=+-@＝＋－＠")
# Whitespace some importers trim before deciding a cell is a formula.
_TRIMMED = " \t\r\n\u00a0\u3000"


def safe_cell(value: object) -> str:
    """Text a spreadsheet will show as-is, never evaluate."""
    text = "" if value is None else str(value)
    first = text.lstrip(_TRIMMED)[:1]
    if first in _FORMULA_STARTS or text[:1] in ("\t", "\r"):
        return "'" + text
    return text


def rows(
    entries: Sequence[tuple[Item | None, Session]],
    zone: ZoneInfo | None,
    ranks: dict[str, int] | None = None,
) -> list[list[str]]:
    """One row per session. `Item` carries your list status; pass None for catalog-only rows."""
    ranks = ranks or {}
    out: list[list[str]] = []
    for item, session in entries:
        interval = session.interval(zone)
        if interval:
            start, end = (t.astimezone(zone) if zone else t for t in interval)
            day, date = f"{start:%a}", start.date().isoformat()
            if session.is_all_day_session:
                times = ("all day", "")
            else:
                times = (f"{start:%H:%M}", f"{end:%H:%M}")
        else:
            day, date, times = "", "", ("TBA", "")
        status = ", ".join(sorted(k.capitalize() for k in item.kinds)) if item else ""
        seats = SEAT_AVAILABILITY_LABELS.get(session.seat_availability or "", "")
        values = [
            day,
            date,
            *times,
            session.code,
            session.title,
            session.type,
            session.level,
            session.place,
            session.room,
            seats or session.seat_availability,
            status,
            ranks.get(session.session_id, ""),
            "; ".join(sorted(session.topics)),
            "; ".join(sorted(session.services)),
            "; ".join(session.speaker_names),
            session.session_id,
        ]
        out.append([safe_cell(v) for v in values])
    return out


def to_csv(table: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(table)
    return buffer.getvalue()


def write_csv(path: Path, text: str) -> None:
    """Save to a file, atomically and owner-only. UTF-8 with a byte-order mark, so Excel shows
    accented names correctly."""
    write_file_atomic(path, text.encode("utf-8-sig"))
