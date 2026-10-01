"""Data models for the AWS Events API, written from its OpenAPI description.

The API documents every field beyond a few identifiers as optional, and says enum-like
values can grow over time. So these models accept unknown fields, keep enum-like values as
plain strings, and compute derived values (such as start and end times) defensively.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic.alias_generators import to_camel

# Control characters that could drive a terminal (ESC starts ANSI/OSC sequences) or reorder
# text (bidi overrides): C0 except tab and newline, DEL, C1, the bidi/format controls, the
# Unicode line and paragraph separators, zero-width space and the byte-order mark. ZWJ and
# ZWNJ (U+200D, U+200C) stay: emoji sequences and several scripts need them.
_CONTROL_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u2028-\u202e\u2066-\u2069\ufeff]"
)
# Longest session length accepted (a week, in minutes); anything longer is treated as unknown.
MAX_SESSION_MINUTES = 7 * 1440


MULTILINE_FIELDS = frozenset({"abstract", "description"})


def _one_line(text: str) -> str:
    return clean_text(text).replace("\n", " ").replace("\t", " ")


def clean_text(text: str) -> str:
    """Remove terminal and bidi control characters from text we didn't write, keeping \n and
    \t. Use it on anything from the network before it can reach a terminal."""
    return _CONTROL_CHARS.sub("", text)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    @field_validator("*", mode="after")
    @classmethod
    def _clean_strings(cls, value: Any, info: ValidationInfo) -> Any:
        # Only free text keeps its line breaks; in a one-line field (a title, a venue) a line
        # break could fake an extra line of output, so it becomes a space.
        clean = clean_text if info.field_name in MULTILINE_FIELDS else _one_line
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            return [clean(v) if isinstance(v, str) else v for v in value]
        return value


# Known values of SeatAvailability. Anything else is shown as-is.
SEAT_AVAILABILITY_LABELS = {
    "available": "Available",
    "limited": "Limited",
    "veryLimited": "Very limited",
    "unavailable": "Full",
    "walkUp": "Walk-up",
}

# Known values of BulkFailureCode. The API says to treat unknown values as a generic refusal.
BULK_FAILURE_LABELS = {
    "sessionNotReservable": "session does not take reservations",
    "scheduleConflict": "clashes with your schedule",
    "alreadyScheduled": "already on your schedule",
    "sessionFull": "session is full",
    "insufficientAccess": "your pass does not include this session",
    "timePassed": "session has already started or ended",
    "alreadyFavorited": "already a favorite",
    "notFavorited": "not a favorite",
    "other": "refused",
}


class EventAddress(ApiModel):
    city: str | None = None


class Event(ApiModel):
    event_id: str
    name: str
    event_type: str
    start_date: str
    end_date: str
    is_online: bool
    authentication_required: bool
    timezone: str | None = None
    timezone_abbreviation: str | None = None
    time_format: str | None = None
    address: EventAddress | None = None
    supported_language_codes: list[str] = Field(default_factory=list)

    def zone(self) -> ZoneInfo | None:
        return _zone(self.timezone)


class Speaker(ApiModel):
    name: str | None = None


class SessionTime(ApiModel):
    date: str | None = None
    time: str | None = None
    length: str | None = None
    timezone: str | None = None


class Session(ApiModel):
    session_id: str
    title: str
    abbreviation: str | None = None
    abstract: str | None = None
    type: str | None = None
    level: str | None = None
    venue: str | None = None
    room: str | None = None
    is_all_day_session: bool | None = None
    is_reservable: bool | None = None
    seat_availability: str | None = None
    session_time: SessionTime | None = None
    speakers: list[Speaker] = Field(default_factory=list)
    tracks: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    areas_of_interest: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    customer_personas: list[str] = Field(default_factory=list)
    experiences: list[str] = Field(default_factory=list)
    additional_activities: list[str] = Field(default_factory=list)
    focus_areas: list[str] = Field(default_factory=list)

    @property
    def code(self) -> str:
        """The short public session code (e.g. AIM3315), or the session ID if there is none."""
        return self.abbreviation or self.session_id

    @property
    def is_walk_up(self) -> bool:
        """Attend by walking up; no reservation is taken.

        Only `seatAvailability == "walkUp"` means that. `isReservable` is false for *every*
        session before reserved seating opens (seen on the live re:Invent 2026 catalog), so it
        can't be used to skip sessions: the API decides when a reservation is attempted.
        """
        return self.seat_availability == "walkUp"

    @property
    def place(self) -> str | None:
        """The venue, or when it's empty, the start of `room` (re:Invent 2026 puts Wynn/Encore
        and Caesars Palace there)."""
        if self.venue:
            return self.venue
        head = (self.room or "").split("|", 1)[0].strip()
        return head or None

    @property
    def speaker_names(self) -> list[str]:
        return [s.name for s in self.speakers if s.name]

    def tags(self) -> list[str]:
        """All taxonomy labels, for searching."""
        return [
            *self.tracks,
            *self.topics,
            *self.industries,
            *self.areas_of_interest,
            *self.roles,
            *self.services,
            *self.segments,
            *self.features,
            *self.customer_personas,
            *self.experiences,
            *self.focus_areas,
        ]

    def interval(self, default_zone: ZoneInfo | None) -> tuple[datetime, datetime] | None:
        """Return the session's timezone-aware (start, end), or None when it can't be determined.

        `sessionTime` gives a local date, a local start time and a length in minutes, plus a
        timezone only "when the source provides one", so the event's timezone is the fallback.
        """
        st = self.session_time
        if st is None or not st.date:
            return None
        zone = _zone(st.timezone) or default_zone
        if zone is None:
            return None
        try:
            day = date.fromisoformat(st.date)
        except ValueError:
            return None
        if self.is_all_day_session:
            start = datetime.combine(day, time(0, 0), tzinfo=zone)
            return _checked(start, timedelta(days=1))
        start_time = _parse_time(st.time)
        minutes = _parse_int(st.length)
        if start_time is None or minutes is None or not 0 < minutes <= MAX_SESSION_MINUTES:
            return None
        start = datetime.combine(day, start_time, tzinfo=zone)
        return _checked(start, timedelta(minutes=minutes))


class PersonalTime(ApiModel):
    personal_time_id: str
    start_date_time: str
    end_date_time: str
    title: str
    description: str
    location: str | None = None

    def interval(self) -> tuple[datetime, datetime] | None:
        """Personal time is always UTC, written without an offset."""
        try:
            start = datetime.fromisoformat(self.start_date_time).replace(tzinfo=ZoneInfo("UTC"))
            end = datetime.fromisoformat(self.end_date_time).replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            return None
        if not (_in_range(start) and _in_range(end)):
            return None
        return start, end


class PersonalTimeInput(ApiModel):
    """A personal time entry to create or replace. Times are UTC, written without an offset
    (the API rejects "Z" or "+01:00"), and the length must be a whole number of 5 minutes."""

    start_date_time: str
    end_date_time: str
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=250)
    location: str | None = Field(default=None, min_length=1, max_length=255)

    @classmethod
    def from_local(
        cls,
        start: datetime,
        end: datetime,
        *,
        title: str,
        description: str | None = None,
        location: str | None = None,
    ) -> PersonalTimeInput:
        """Build from timezone-aware local times, validating what the API would reject."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end <= start:
            raise ValueError("the end must be after the start")
        minutes = (end - start).total_seconds() / 60
        if start.second or end.second or minutes % 5:
            raise ValueError("times must be on the minute, and the length a multiple of 5 minutes")
        utc = ZoneInfo("UTC")
        fmt = "%Y-%m-%dT%H:%M:%S"
        return cls(
            start_date_time=start.astimezone(utc).strftime(fmt),
            end_date_time=end.astimezone(utc).strftime(fmt),
            title=title.strip(),
            description=(description or title).strip(),
            location=location.strip() if location and location.strip() else None,
        )

    def same_as(self, entry: PersonalTime) -> bool:
        """Whether an existing entry matches this one (used to spot a create that went through)."""
        return (
            entry.start_date_time == self.start_date_time
            and entry.end_date_time == self.end_date_time
            and entry.title == self.title
        )


class Schedule(ApiModel):
    reserved: list[str] = Field(default_factory=list)
    favorites: list[str] = Field(default_factory=list)
    personal_time: list[PersonalTime] = Field(default_factory=list)


class BulkFailure(ApiModel):
    session_id: str
    code: str
    conflicts_with: list[str] = Field(default_factory=list)

    @property
    def reason(self) -> str:
        return BULK_FAILURE_LABELS.get(self.code, BULK_FAILURE_LABELS["other"])


class BulkResult(ApiModel):
    successful: list[str] = Field(default_factory=list)
    failed: list[BulkFailure] = Field(default_factory=list)


class SessionPage(ApiModel):
    items: list[Session] = Field(default_factory=list)
    total_count: int = 0
    next_token: str | None = None


def _in_range(moment: datetime) -> bool:
    """Far enough from datetime's limits (years 1 and 9999) that converting to any timezone,
    or adding a week, can't overflow."""
    return 2 <= moment.year <= 9998


def _checked(start: datetime, length: timedelta) -> tuple[datetime, datetime] | None:
    """(start, start + length), or None when the dates are out of datetime's range. A bad
    date must not raise: callers (such as a catalog sync) treat None as "time not known"."""
    try:
        end = start + length
        if not (_in_range(start.astimezone(UTC)) and _in_range(end.astimezone(UTC))):
            return None
    except (OverflowError, ValueError):
        return None
    return start, end


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def _parse_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
