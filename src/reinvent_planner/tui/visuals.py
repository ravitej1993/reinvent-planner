"""Views of one day worth a screenshot: a map of the Las Vegas Strip with your route, and a
timeline of the day.

Both are pure functions from planner `Item`s to rich `Text`: no Textual, no I/O, no clock.
Catalog text (titles, venues, rooms) only ever goes in as plain text, never markup, so a title
like "[bold]x[/]" shows literally, and control characters are blanked so a title can't break
lines or send terminal escapes. Colour never carries meaning alone: every state has a glyph
too (✓ reserved, #N ranked, ★ favorite, ↺ backup, ◆ personal, ⚠ tight, ✗ overlap, ░ travel).
Output depends only on the arguments: no dict-order, locale or current-time dependence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from ..planner import Issue, Item, TravelTimes, distance_parts, find_issues
from ..reserve import Choice, Event
from .themes import PALETTE

# Hand-placed positions for the re:Invent 2026 venue keys, as (x, y) in 0..1: x runs west to
# east across the Strip, y north to south. Not to scale: spread out so labels fit.
STRIP_X = 0.46
LAYOUT_2026: dict[str, tuple[float, float]] = {
    "encore": (0.62, 0.02),
    "wynn_encore": (0.60, 0.075),
    "wynn": (0.58, 0.13),
    "venetian": (0.58, 0.30),
    "caesars_forum": (0.66, 0.46),  # east, behind the LINQ
    "caesars_palace": (0.40, 0.53),  # west, across the Strip (label drawn to the left)
    "mgm": (0.58, 0.96),  # the south end
}
# Flavour: the Sphere, and the monorail with the stations the badge covered (2025).
SPHERE = (0.74, 0.24)
MONORAIL_X = 0.94
MONORAIL_STATIONS = (0.40, 0.53, 0.63, 0.94)  # Harrah's/LINQ, Flamingo, Horseshoe, MGM

MAP_MIN_WIDTH = 72  # narrower than this, the map becomes a metro-line list
SIDE_BY_SIDE_WIDTH = 120  # from here, the itinerary sits beside the map
MAP_COLUMNS = 56
MAP_ROWS = 20

_KIND_ORDER = ("reserved", "ranked", "favorite", "backup", "personal")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def clean(text: str | None) -> str:
    """Catalog text made safe to draw: control characters (newlines, ESC...) become spaces."""
    return "".join(ch if ch.isprintable() else " " for ch in (text or ""))


def kind_of(item: Item, ranks: Mapping[str, int] | None = None) -> str:
    """The one kind that colours an item: reserved beats ranked beats favorite, and so on."""
    if ranks and item.key in ranks:
        return "reserved" if "reserved" in item.kinds else "ranked"
    return next((kind for kind in _KIND_ORDER if kind in item.kinds), "other")


def glyph_of(item: Item, ranks: Mapping[str, int] | None = None) -> str:
    kind = kind_of(item, ranks)
    if kind == "ranked":
        return f"#{ranks[item.key]}" if ranks and item.key in ranks else "#"
    return {"reserved": "✓", "favorite": "★", "backup": "↺", "personal": "◆"}.get(kind, "·")


def _local(moment: datetime, zone: ZoneInfo | None) -> datetime:
    return moment.astimezone(zone) if zone else moment


def _day_label(day: date) -> str:
    """ "Tue 1 Dec", without strftime (which follows the locale)."""
    return f"{_DAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _fit(text: Text, width: int) -> Text:
    """A copy cut to ``width`` cells (with …), without invisible trailing spaces."""
    text = text.copy()
    text.truncate(max(width, 0), overflow="ellipsis")
    _trim(text)
    return text


def _visible(style: str | Style | None) -> bool:
    """Whether a space in this style shows (a fill), so trimming it would shorten a block."""
    if not style:
        return False
    parsed = Style.parse(style) if isinstance(style, str) else style
    return parsed.bgcolor is not None or bool(parsed.reverse)


def _trim(text: Text) -> None:
    """Drop trailing spaces, but never a filled cell: the tail of a timeline block is spaces."""
    keep = max((span.end for span in text.spans if _visible(span.style)), default=0)
    if _visible(text.style):
        keep = len(text.plain)
    plain = text.plain
    end = len(plain)
    while end > keep and plain[end - 1].isspace():
        end -= 1
    if end < len(plain):  # right_crop(0) would empty the text
        text.right_crop(len(plain) - end)


def _stops(items: Sequence[Item]) -> list[Item]:
    """Timed items in time order: the day's route."""
    return sorted((i for i in items if i.timed), key=lambda i: (i.start, i.end, i.code, i.key))


def _issue_index(items: Sequence[Item], travel: TravelTimes) -> dict[tuple[str, str], Issue]:
    """planner.find_issues, keyed by (first key, second key)."""
    return {(i.first.key, i.second.key): i for i in find_issues(list(items), travel)}


# ---------------------------------------------------------------------------
# The Strip map
# ---------------------------------------------------------------------------


@dataclass
class _Leg:
    first: Item
    second: Item
    text: str
    bad: bool  # too tight, or an overlap
    short: str = ""  # the same without miles, for narrow widths

    def fitting(self, width: int) -> str:
        """The full text if it fits ``width`` cells, else the one without miles."""
        return self.text if cell_len(self.text) <= width or not self.short else self.short


def render_strip_map(
    items: list[Item],
    travel: TravelTimes,
    *,
    width: int,
    highlight: str | None = None,
    places: Mapping[str, tuple[float, float]] | None = None,
    ranks: Mapping[str, int] | None = None,
    zone: ZoneInfo | None = None,
) -> Text:
    """One day's route on a map of the Strip, with an itinerary of stops and transfers.

    ``items`` are one day's items; timed ones become numbered stops in time order. Transfers
    too tight to make (planner.find_issues) are red and marked ⚠. ``highlight`` is an item key
    to pick out. ``places`` maps venue keys to (lat, lon); without it the hand-placed 2026
    layout is used for the venues it knows. Venues with no position, other events, and widths
    under 72 get a metro-line list instead of the map.
    """
    width = max(width, 20)
    stops = _stops(items)
    issues = _issue_index(stops, travel)
    keys = [travel.place_key(stop.venue, stop.room) for stop in stops]
    legs = _legs(stops, keys, issues, travel)
    positions, drawn_strip = _positions(travel, places)

    header = _map_header(stops, legs, zone)
    route_keys = {k for k in keys if k is not None}
    on_map = route_keys & set(positions)
    use_map = width >= MAP_MIN_WIDTH and bool(positions) and (on_map or not route_keys)
    side_by_side = use_map and width >= SIDE_BY_SIDE_WIDTH
    itinerary_width = width - MAP_COLUMNS - 2 if side_by_side else width
    itinerary = _itinerary(stops, keys, legs, travel, itinerary_width, highlight, ranks, zone)
    if not use_map:
        lines = [header, *_metro(stops, keys, legs, travel, positions, width, highlight)]
        return Text("\n").join(_fit(line, width) for line in [*lines, Text(""), *itinerary])

    columns = MAP_COLUMNS if side_by_side else min(width, MAP_COLUMNS + 8)
    map_lines = _draw_map(stops, keys, legs, travel, positions, drawn_strip, columns, highlight)
    off_map = sorted(
        {_venue_name(s, k, travel) for s, k in zip(stops, keys, strict=True) if k not in on_map}
    )
    if off_map:
        map_lines.append(Text("off map: " + ", ".join(off_map), style=PALETTE["muted"]))
    if side_by_side:
        right = width - columns - 2
        body = [
            Text.assemble(
                _fit(left, columns).copy() if left else Text(""),
                " " * (columns - (left.cell_len if left else 0) + 2),
                _fit(itin, right) if itin else "",
            )
            for left, itin in _zip_longest(map_lines, itinerary)
        ]
        lines = [header, *body]
    else:
        lines = [header, *map_lines, Text(""), *itinerary]
    return Text("\n").join(_fit(line, width) for line in lines)


def _zip_longest(a: list[Text], b: list[Text]) -> list[tuple[Text | None, Text | None]]:
    size = max(len(a), len(b))
    return [(a[i] if i < len(a) else None, b[i] if i < len(b) else None) for i in range(size)]


def _positions(
    travel: TravelTimes, places: Mapping[str, tuple[float, float]] | None
) -> tuple[dict[str, tuple[float, float]], bool]:
    """(x, y) in 0..1 per venue key, and whether this is the 2026 Strip layout."""
    if places:
        coords = {k: v for k, v in sorted(places.items()) if _valid_coord(v)}
        if coords:
            return _project(coords), False
    known = set(travel.venue_keys())
    layout = {k: v for k, v in LAYOUT_2026.items() if k in known}
    return layout, bool(layout)


def _valid_coord(value: object) -> bool:
    try:
        lat, lon = value  # type: ignore[misc]
        return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90
    except (TypeError, ValueError):
        return False


def _project(coords: Mapping[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """Lat/lon to 0..1 (north up, west left), stretched to fill the map."""
    lats = [lat for lat, _ in coords.values()]
    lons = [lon for _, lon in coords.values()]
    lat_span = (max(lats) - min(lats)) or 1.0
    lon_span = (max(lons) - min(lons)) or 1.0
    return {
        key: (0.15 + 0.6 * (lon - min(lons)) / lon_span, 0.02 + 0.94 * (max(lats) - lat) / lat_span)
        for key, (lat, lon) in coords.items()
    }


def _venue_name(item: Item, key: str | None, travel: TravelTimes) -> str:
    if key is not None and key in travel.venue_keys():
        return clean(travel.name(key))
    return clean(item.place)


def _legs(
    stops: list[Item],
    keys: list[str | None],
    issues: Mapping[tuple[str, str], Issue],
    travel: TravelTimes,
) -> list[_Leg]:
    legs = []
    for (a, ka), (b, kb) in pairwise(zip(stops, keys, strict=True)):
        issue = issues.get((a.key, b.key))
        free = int((b.start - a.end).total_seconds() // 60)  # type: ignore[operator]
        if (issue is not None and issue.kind == "overlap") or free < 0:
            legs.append(_Leg(a, b, "✗ overlap", True))
            continue
        tight = issue is not None and issue.kind == "tight"
        rest = f" · {free}′ free"
        rest += f" ⚠ TIGHT, needs ~{issue.needed_minutes}′" if tight else " ✓"  # type: ignore[union-attr]
        if ka and kb and ka != kb:
            walk = travel.walking(ka, kb)
            train = travel.monorail(ka, kb)
            if train is not None and train.beats(walk[1] if walk else None):
                rest += f" · or monorail {train.minutes}′"
        full, short = _how_far(ka, kb, a, b, travel)
        legs.append(_Leg(a, b, full + rest, tight, short + rest))
    return legs


def _how_far(
    ka: str | None, kb: str | None, a: Item, b: Item, travel: TravelTimes
) -> tuple[str, str]:
    """How far the leg is: (with miles, without them). Only a measured walk has a distance."""
    if ka is None or kb is None:
        return "no venue", "no venue"
    if ka == kb:
        return "same venue", "same venue"
    walk = travel.walking(ka, kb)
    if walk is not None:
        meters, minutes = walk
        km, mi = distance_parts(meters)
        return f"{minutes}′ walk ({km} / {mi})", f"{minutes}′ walk ({km})"
    need = travel.minutes_between(a, b)
    if need is None:
        return "no venue", "no venue"
    minutes, known = need
    text = f"~{minutes}′ to get there" if known else f"~{minutes}′? (venue not in table)"
    return text, text


def _map_header(stops: list[Item], legs: list[_Leg], zone: ZoneInfo | None) -> Text:
    header = Text("▌STRIP", style=PALETTE["title"])
    if stops:
        header.append(f" · {_day_label(_local(stops[0].start, zone).date())}", PALETTE["label"])  # type: ignore[arg-type]
    header.append(f" · {len(stops)} stop{'s' * (len(stops) != 1)}", PALETTE["muted"])
    bad = sum(leg.bad for leg in legs)
    if bad:
        header.append(f" · ⚠ {bad} tight/overlap", PALETTE["tight"])
    elif legs:
        header.append(" · ✓ all transfers fit", PALETTE["ok"])
    return header


def _itinerary(
    stops: list[Item],
    keys: list[str | None],
    legs: list[_Leg],
    travel: TravelTimes,
    width: int,
    highlight: str | None,
    ranks: Mapping[str, int] | None,
    zone: ZoneInfo | None,
) -> list[Text]:
    if not stops:
        return [Text("No timed sessions this day.", style=PALETTE["muted"])]
    lines: list[Text] = []
    for index, (stop, key) in enumerate(zip(stops, keys, strict=True)):
        line = Text.assemble(
            (f"{index + 1:>2} ", PALETTE["stop"]),
            (_local(stop.start, zone).strftime("%H:%M"), PALETTE["text"]),  # type: ignore[arg-type]
            " ",
            (f" {glyph_of(stop, ranks)} ", PALETTE[kind_of(stop, ranks)]),
            " ",
            (_venue_name(stop, key, travel), PALETTE["label"]),
            " ",
            (clean(stop.code), PALETTE["muted"]),
            " ",
            (clean(stop.title), PALETTE["text"]),
        )
        if stop.key == highlight:
            line.stylize(PALETTE["highlight"])
        lines.append(line)
        if index < len(legs):
            leg = legs[index]
            style = PALETTE["tight"] if leg.bad else PALETTE["muted"]
            lines.append(Text(f"    ↓ {leg.fitting(width - 6)}", style=style))
    return lines


def _metro(
    stops: list[Item],
    keys: list[str | None],
    legs: list[_Leg],
    travel: TravelTimes,
    positions: Mapping[str, tuple[float, float]],
    width: int,
    highlight: str | None,
) -> list[Text]:
    """The narrow or no-map fallback: the day's venues as stations on a line, north first
    (by position, else alphabetically), each with its stop numbers."""
    if not stops:
        return []
    numbers: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    for index, (stop, key) in enumerate(zip(stops, keys, strict=True)):
        name = _venue_name(stop, key, travel)
        venue = key if key is not None else f"?{name}"
        numbers.setdefault(venue, []).append(index + 1)
        names[venue] = name
    bad = _bad_venues(legs, stops, keys)
    lit = {k for s, k in zip(stops, keys, strict=True) if s.key == highlight}

    def order(venue: str) -> tuple:
        position = positions.get(venue)
        return (position is None, position[1] if position else 0.0, names[venue].lower(), venue)

    lines = [Text(" N", style=PALETTE["muted"])]
    for venue in sorted(numbers, key=order):
        lines.append(Text(" ┃", style=PALETTE["road"]))
        line = Text.assemble(
            (" ●", PALETTE["venue.stop"]),
            "  ",
            (names[venue], PALETTE["venue"]),
            "  ",
            ("·".join(str(n) for n in numbers[venue]), PALETTE["stop"]),
        )
        if venue in bad:
            line.append(" ⚠", PALETTE["tight"])
        if venue in lit:
            line.stylize(PALETTE["highlight"], 3)
        lines.append(line)
    lines.append(Text(" ┃", style=PALETTE["road"]))
    return lines


def _bad_venues(legs: list[_Leg], stops: list[Item], keys: list[str | None]) -> set[str]:
    key_of = {
        s.key: (k if k is not None else f"?{clean(s.place)}")
        for s, k in zip(stops, keys, strict=True)
    }
    return {key_of[i.key] for leg in legs if leg.bad for i in (leg.first, leg.second)}


class _Canvas:
    """A grid of characters with styles; venue labels reserve cells so they don't collide."""

    def __init__(self, columns: int, rows: int):
        self.columns, self.rows = columns, rows
        self.chars = [[" "] * columns for _ in range(rows)]
        self.styles: list[list[str | None]] = [[None] * columns for _ in range(rows)]
        self.reserved = [[False] * columns for _ in range(rows)]

    def put(self, row: int, col: int, text: str, style: str | None, *, reserve=False) -> None:
        if not 0 <= row < self.rows:
            return
        for offset, ch in enumerate(text):
            c = col + offset
            if 0 <= c < self.columns:
                self.chars[row][c] = ch if cell_len(ch) == 1 else "?"
                self.styles[row][c] = style
                self.reserved[row][c] |= reserve

    def free(self, row: int, col: int, length: int) -> bool:
        return 0 <= row < self.rows and not any(
            self.reserved[row][c] for c in range(max(col, 0), min(col + length, self.columns))
        )

    def lines(self) -> list[Text]:
        out = []
        for chars, styles in zip(self.chars, self.styles, strict=True):
            line = Text()
            run_start = 0
            for index in range(1, len(chars) + 1):  # one span per run of same-styled cells
                if index == len(chars) or styles[index] != styles[run_start]:
                    line.append("".join(chars[run_start:index]), styles[run_start])
                    run_start = index
            _trim(line)
            out.append(line)
        return out


def _draw_map(
    stops: list[Item],
    keys: list[str | None],
    legs: list[_Leg],
    travel: TravelTimes,
    positions: Mapping[str, tuple[float, float]],
    strip: bool,
    columns: int,
    highlight: str | None,
) -> list[Text]:
    canvas = _Canvas(columns, MAP_ROWS)

    def col(x: float) -> int:
        return round(x * (columns - 1))

    def row(y: float) -> int:
        return round(y * (MAP_ROWS - 1))

    if strip:
        road = col(STRIP_X)
        for r in range(MAP_ROWS):
            canvas.put(r, road, "║", PALETTE["road"])
        for offset, letter in enumerate("STRIP"):
            canvas.put(row(0.66) + offset, road, letter, PALETTE["road"])
        rail = col(MONORAIL_X)
        for r in range(MAP_ROWS):
            canvas.put(r, rail, "┊", PALETTE["monorail"])
        for y in MONORAIL_STATIONS:
            canvas.put(row(y), rail, "▪", PALETTE["monorail"])
        canvas.put(row(SPHERE[1]), col(SPHERE[0]), "◍ Sphere", PALETTE["landmark"])
    canvas.put(0, 0, "N↑", PALETTE["muted"])

    numbers: dict[str, list[int]] = {}
    for index, key in enumerate(keys):
        if key is not None:
            numbers.setdefault(key, []).append(index + 1)
    bad = _bad_venues(legs, stops, keys)
    lit = {k for s, k in zip(stops, keys, strict=True) if s.key == highlight}
    route = {k for k in keys if k is not None}

    # Route venues first, so they get their preferred rows; then the others, north to south.
    # Idle venues only when they're real places (not groupings like "Wynn/Encore").
    real = set(travel.keys_with_figures()) if strip else set(positions)
    shown = [k for k in positions if k in route or k in real]
    order = sorted(shown, key=lambda k: (k not in route, positions[k][1], k))
    for key in order:
        x, y = positions[key]
        name = clean(travel.name(key))
        stops_text = "·".join(str(n) for n in numbers.get(key, []))
        marker = "●" if key in route else "○"
        warn = "⚠" if key in bad else ""
        west = strip and x < STRIP_X
        parts = [warn, stops_text, name, marker] if west else [marker, name, stops_text, warn]
        label = " ".join(part for part in parts if part)
        length = len(label)
        start = col(x) - length + 1 if west else col(x)
        start = min(max(start, 0), max(columns - length, 0))
        target = row(y)
        for shift in (0, 1, -1, 2, -2, 3):
            if canvas.free(target + shift, start, length):
                target += shift
                break
        style = PALETTE["venue.stop"] if key in route else PALETTE["venue.idle"]
        if key in lit:
            style = f"{style} {PALETTE['highlight']}"
        canvas.put(target, start, label, style, reserve=True)
        if warn:
            spot = start if west else start + length - 1
            canvas.put(target, spot, "⚠", PALETTE["tight"], reserve=True)
    lines = canvas.lines()
    key = Text.assemble(
        ("● ", PALETTE["venue.stop"]),
        ("your venues  ", PALETTE["muted"]),
        ("○ ", PALETTE["venue.idle"]),
        ("others", PALETTE["muted"]),
    )
    if strip:
        key.append_text(
            Text.assemble(
                ("  ║ ", PALETTE["road"]),
                ("Strip  ", PALETTE["muted"]),
                ("┊▪ ", PALETTE["monorail"]),
                ("monorail", PALETTE["muted"]),
            )
        )
    return [*lines, key]


# ---------------------------------------------------------------------------
# The day timeline
# ---------------------------------------------------------------------------


def render_timeline(
    items: list[Item],
    zone: ZoneInfo | None,
    *,
    width: int,
    start_hour: int | None = None,
    end_hour: int | None = None,
    ranks: Mapping[str, int] | None = None,
    travel: TravelTimes | None = None,
    now: datetime | None = None,
) -> Text:
    """One day as a horizontal Gantt chart: hour ticks, one lane per overlapping group, blocks
    by kind (✓ reserved, #N ranked, ★ favorite, ↺ backup, ◆ personal), and ░ for travel
    between venues (red when planner.find_issues says it's too tight, given ``travel``).

    The window fits the day: at least 08:00-19:00, widened to the earliest start and latest
    end (up to midnight). Explicit hours win; anything they leave out is listed below the
    chart, never silently dropped. ‹ and › mark items that continue past an edge.
    """
    width = max(width, 12)
    stops = _stops(items)
    day = _local(stops[0].start, zone).date() if stops else None  # type: ignore[arg-type]

    def minute(moment: datetime) -> int:
        """Minutes since midnight starting the day (negative before it, >1440 after)."""
        local = _local(moment, zone)
        return (local.date() - day).days * 1440 + local.hour * 60 + local.minute  # type: ignore[operator]

    if start_hour is None:
        earliest = min((minute(s.start) for s in stops), default=8 * 60)  # type: ignore[arg-type]
        start_hour = min(max(earliest // 60, 0), 8)
    if end_hour is None:
        latest = max((minute(s.end) for s in stops), default=19 * 60)  # type: ignore[arg-type]
        end_hour = max(min(-(-latest // 60), 24), 19)
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError(f"need 0 <= start_hour < end_hour <= 24, got {start_hour}..{end_hour}")
    first, last = start_hour * 60, end_hour * 60

    def col(minutes: int) -> int:
        offset = min(max(minutes, first), last) - first
        return min(math.floor(offset * width / (last - first)), width)

    lines = _ticks(width, start_hour, end_hour)
    if now is not None and day is not None and first <= minute(now) < last:
        spot = min(col(minute(now)), width - 1)
        ruler = lines[1]
        lines[1] = Text.assemble(ruler[:spot], ("▼", PALETTE["now"]), ruler[spot + 1 :])

    lanes = _lanes(stops)
    grid = [_Canvas(width, 1) for _ in range(max(lanes.values(), default=-1) + 1)]
    before: list[Item] = []
    after: list[Item] = []
    clipped = False
    for stop in stops:
        begins, ends = minute(stop.start), minute(stop.end)  # type: ignore[arg-type]
        if ends <= first or begins >= last:
            (before if ends <= first else after).append(stop)
            continue
        early, late = begins < first, ends > last
        clipped |= early or late
        c0 = min(col(begins), width - 1)
        c1 = max(col(ends), c0 + 1)
        body = f"{'‹' if early else ''}{glyph_of(stop, ranks)} {clean(stop.code)}"
        body = body[: c1 - c0].ljust(c1 - c0)
        if late:
            body = body[:-1] + "›"
        grid[lanes[stop.key]].put(0, c0, body, PALETTE[kind_of(stop, ranks)], reserve=True)

    issues = _issue_index(stops, travel) if travel is not None else {}
    for a, b in pairwise(stops):
        if b.start <= a.end or not _different_places(a, b, travel):  # type: ignore[operator]
            continue
        leave, arrive = minute(a.end), minute(b.start)  # type: ignore[arg-type]
        if leave < first or arrive > last:
            continue  # one end is off the chart
        c0, c1 = col(leave), col(arrive)
        if c1 <= c0:
            continue
        issue = issues.get((a.key, b.key))
        tight = issue is not None and issue.kind == "tight"
        style = PALETTE["tight"] if tight else PALETTE["travel"]
        for lane_index in (lanes[b.key], lanes[a.key]):
            if grid[lane_index].free(0, c0, c1 - c0):
                grid[lane_index].put(0, c0, ("⚠" if tight else "░") + "░" * (c1 - c0 - 1), style)
                break

    if not stops:
        lines.append(Text("No timed sessions this day.", style=PALETTE["muted"]))
    lines.extend(line for lane in grid if (line := lane.lines()[0]).plain.strip())
    if before or after:
        lines.append(_outside(before, after, start_hour, end_hour, zone))
    lines.append(_legend(width, clipped=clipped or bool(before or after)))
    return Text("\n").join(_fit(line, width) for line in lines)


def _outside(
    before: list[Item], after: list[Item], start_hour: int, end_hour: int, zone: ZoneInfo | None
) -> Text:
    """What explicit hours left off the chart, so it can't look like free time."""

    def listing(items: list[Item]) -> str:
        return ", ".join(
            f"{clean(i.code)} {_local(i.start, zone).strftime('%H:%M')}"  # type: ignore[arg-type]
            for i in items
        )

    parts = []
    if before:
        parts.append(f"before {start_hour:02d}:00: {listing(before)}")
    if after:
        parts.append(f"after {end_hour % 24:02d}:00: {listing(after)}")
    return Text("⚠ " + " · ".join(parts), style=PALETTE["warning"])


def _lanes(stops: list[Item]) -> dict[str, int]:
    """First-fit lane per item: overlapping items stack into separate lanes."""
    ends: list[datetime] = []
    lanes: dict[str, int] = {}
    for stop in stops:
        for index, end in enumerate(ends):
            if end <= stop.start:  # type: ignore[operator]
                ends[index] = stop.end  # type: ignore[assignment]
                lanes[stop.key] = index
                break
        else:
            ends.append(stop.end)  # type: ignore[arg-type]
            lanes[stop.key] = len(ends) - 1
    return lanes


def _different_places(a: Item, b: Item, travel: TravelTimes | None) -> bool:
    if travel is not None:
        ka, kb = travel.place_key(a.venue, a.room), travel.place_key(b.venue, b.room)
        return ka is not None and kb is not None and ka != kb
    if not (a.venue or a.room) or not (b.venue or b.room):
        return False
    return a.place.strip().lower() != b.place.strip().lower()


def _ticks(width: int, start_hour: int, end_hour: int) -> list[Text]:
    """Hour labels over a ruler; labels thin out (every 2, 3... hours) when space is short."""
    hours = end_hour - start_hour
    per_hour = width / hours
    step = next(s for s in range(1, hours + 1) if per_hour * s >= 3 or s == hours)
    labels = [" "] * width
    ruler = ["─"] * width
    for h in range(start_hour, end_hour + 1):
        c = min(math.floor((h - start_hour) * per_hour), width - 1)
        ruler[c] = "┬" if h < end_hour else "┐"
        if (h - start_hour) % step == 0 and c + 2 <= width:
            labels[c : c + 2] = list(f"{h % 24:02d}")
    return [
        Text("".join(labels).rstrip(), style=PALETTE["label"]),
        Text("".join(ruler), style=PALETTE["muted"]),
    ]


def _legend(width: int, *, clipped: bool = False) -> Text:
    """The glyph key, in the most readable form that fits: full names, short ones, or glyphs."""
    entries = [
        ("✓", "reserved", "res", "reserved"),
        ("#N", "ranked", "rank", "ranked"),
        ("★", "favorite", "fav", "favorite"),
        ("◆", "personal", "own", "personal"),
        ("░", "travel", "walk", "travel"),
        ("⚠", "tight", "tight", "tight"),
    ]
    if clipped:
        entries.append(("‹ ›", "earlier/later", "more", "muted"))
    for form in ("long", "short", "glyph"):
        legend = Text()
        for glyph, long, short, style in entries:
            name = {"long": f" {long}  ", "short": f"{short} ", "glyph": " "}[form]
            badge = f" {glyph} " if form == "long" else glyph
            legend.append_text(Text.assemble((badge, PALETTE[style]), (name, PALETTE["muted"])))
        legend.rstrip()
        if legend.cell_len <= width:
            return legend
    return legend


# ---------------------------------------------------------------------------
# The launch board
# ---------------------------------------------------------------------------

LAUNCH_CELL_MAX = 24


@dataclass
class _Cell:
    choice: Choice
    state: str = "pending"  # sending, booked, next, unfilled, retrying
    item: Item | None = None


def render_launch_strip(choices: Sequence[Choice], events: Sequence[Event], *, width: int) -> Text:
    """A "launch sequence" board: one cell per pick (walk-ups excluded) in rank order, filled
    in from the run's events so far, then a one-line summary.

    · pending   → sending (b: a backup)   ✓ booked   ✗ refused, trying the next option
    ? sent but never confirmed (may be reserved; its backups are held back)
    ✗ unfilled (no options left)   … retrying.   A "stopped" event dims what's still pending.
    """
    width = max(width, 12)
    cells = [
        _Cell(c) for c in sorted(choices, key=lambda c: (c.rank, c.primary.key)) if not c.walk_up
    ]
    by_pick = {(cell.choice.rank, cell.choice.primary.key): cell for cell in cells}
    stopped: Event | None = None
    for event in events:
        if event.kind == "stopped":
            stopped = event
            continue
        if event.choice is None or not event.choice.options:
            continue
        cell = by_pick.get((event.choice.rank, event.choice.primary.key))
        if cell is None or cell.state in ("booked", "unfilled", "in_doubt"):
            continue
        item = event.item or cell.item
        if event.kind == "sending":
            cell.state, cell.item = "sending", item
        elif event.kind == "booked":
            cell.state, cell.item = "booked", item
        elif event.kind == "refused":
            keys = [option.key for option in cell.choice.options]
            left = item is not None and item.key in keys and keys.index(item.key) < len(keys) - 1
            cell.state, cell.item = ("next" if left else "unfilled"), item
        elif event.kind == "retrying":
            cell.state = "retrying"
        elif event.kind == "in_doubt":  # sent, never confirmed: may be reserved
            cell.state, cell.item = "in_doubt", item

    if not cells:
        return Text("No picks to reserve.", style=PALETTE["muted"])
    labels = [_launch_cell(cell, stopped is not None) for cell in cells]
    cell_width = min(max(label.cell_len for label in labels) + 2, LAUNCH_CELL_MAX, width)
    per_row = max(1, (width + 1) // (cell_width + 1))
    lines = []
    for start in range(0, len(labels), per_row):
        row = Text()
        for label in labels[start : start + per_row]:
            cell = _fit(Text(" ").append_text(label), cell_width)
            cell.pad_right(cell_width - cell.cell_len)
            cell.stylize(label.style or "", 0, cell_width)
            row.append_text(cell)
            row.append(" ")
        lines.append(row)
    lines.append(_launch_summary(cells, stopped))
    return Text("\n").join(_fit(line, width) for line in lines)


def _launch_cell(cell: _Cell, stopped: bool) -> Text:
    rank = f"#{cell.choice.rank}"
    item = cell.item or cell.choice.primary
    backup = item.key != cell.choice.primary.key
    code = f"b:{clean(item.code)}" if backup else clean(item.code)
    if cell.state == "sending":
        return Text(f"→ {rank} {code}", style=PALETTE["label"])
    if cell.state == "booked":
        return Text(f"✓ {rank} {code}", style=PALETTE["ranked" if backup else "reserved"])
    if cell.state == "next":
        return Text(f"✗ {rank} {code} → next", style=PALETTE["warning"])
    if cell.state == "unfilled":
        return Text(f"✗ {rank} unfilled", style=PALETTE["tight"])
    if cell.state == "retrying":
        return Text(f"… {rank}", style=PALETTE["warning"])
    if cell.state == "in_doubt":
        return Text(f"? {rank} {code} check", style=PALETTE["warning"])
    return Text(
        f"· {rank} {code}", style=f"dim {PALETTE['muted']}" if stopped else PALETTE["muted"]
    )


def _launch_summary(cells: list[_Cell], stopped: Event | None) -> Text:
    booked = [c for c in cells if c.state == "booked"]
    on_backup = sum(c.item is not None and c.item.key != c.choice.primary.key for c in booked)
    unfilled = sum(c.state == "unfilled" for c in cells)
    in_doubt = sum(c.state == "in_doubt" for c in cells)
    summary = Text(f"{len(booked)}/{len(cells)} booked", style=PALETTE["title"])
    if on_backup:
        summary.append(f" · {on_backup} on backup", PALETTE["muted"])
    if in_doubt:
        summary.append(f" · ? {in_doubt} may be reserved: check your schedule", PALETTE["warning"])
    if unfilled:
        summary.append(f" · ✗ {unfilled} unfilled", PALETTE["tight"])
    if stopped is not None:
        reason = clean(stopped.reason) or "stopped"
        summary.append(f" · ■ stopped ({reason})", PALETTE["warning"])
    return summary


__all__ = [
    "LAYOUT_2026",
    "PALETTE",
    "clean",
    "glyph_of",
    "kind_of",
    "render_launch_strip",
    "render_strip_map",
    "render_timeline",
]
