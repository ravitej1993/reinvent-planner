"""Build a travel-time table for any multi-venue event by measuring walks on OpenStreetMap.

The re:Invent 2026 table was made the same way (plus hand-checked extras such as the monorail
and the Wynn/Encore split). Only public information leaves your machine: the event's venue
names and its city, sent to OpenStreetMap's geocoder (nominatim.openstreetmap.org) and its
walking router (routing.openstreetmap.de). Requests are spaced at least a second apart, per
their usage policies, and identify this tool in the User-Agent.
"""

from __future__ import annotations

import math
import re
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from itertools import combinations

import httpx

from . import __version__
from .models import clean_text

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ROUTER_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/{a};{b}"
USER_AGENT = f"reinvent-planner/{__version__} (+https://pypi.org/project/reinvent-planner/)"
MAX_VENUES = 12  # keeps request volume polite: 12 lookups + 66 routes at most
REQUEST_SPACING_SECONDS = 1.1
ROOM_BUFFER_MINUTES = 10
CAP_MINUTES = 45


class MeasureError(Exception):
    """Measuring failed; the message says why."""


@dataclass
class Place:
    name: str  # as the catalog writes it
    key: str  # table key, e.g. "caesars_forum"
    lat: float
    lon: float
    label: str  # what OpenStreetMap matched, for the user to check


def allowance(walk_minutes: float) -> int:
    """Minutes to allow for a walk: + time to reach the room, rounded up to 5, capped."""
    return min(CAP_MINUTES, 5 * math.ceil((walk_minutes + ROOM_BUFFER_MINUTES) / 5))


def venue_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:64] or "venue"


class Measurer:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._sleep = sleep
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Measurer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, url: str, params: dict | None = None) -> object:
        wait = self._last + REQUEST_SPACING_SECONDS - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise MeasureError(f"Couldn't reach OpenStreetMap: {exc}") from exc
        finally:
            self._last = time.monotonic()
        if response.status_code != 200:
            raise MeasureError(f"OpenStreetMap answered HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise MeasureError("OpenStreetMap returned something unexpected.") from exc

    def geocode(self, name: str, city: str | None) -> Place | None:
        query = f"{name}, {city}" if city else name
        results = self._get(NOMINATIM_URL, {"q": query, "format": "json", "limit": 1})
        if not isinstance(results, list) or not results:
            return None
        hit = results[0]
        try:
            lat, lon = float(hit["lat"]), float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        if not _valid_coordinates(lat, lon):
            return None
        label = clean_text(str(hit.get("display_name", "")))[:100]
        return Place(name, venue_key(name), lat, lon, label)

    def walk(self, a: Place, b: Place) -> tuple[int, int]:
        """Street walking distance (meters) and time (minutes) between two places."""
        url = ROUTER_URL.format(a=f"{a.lon},{a.lat}", b=f"{b.lon},{b.lat}")
        data = self._get(url, {"overview": "false"})
        try:
            route = data["routes"][0]  # type: ignore[index]
            return round(float(route["distance"])), round(float(route["duration"]) / 60)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MeasureError(f"No walking route between {a.name} and {b.name}.") from exc


def build_table(
    event_id: str, places: list[Place], walks: dict[tuple[str, str], tuple[int, int]]
) -> str:
    """The TOML travel table, in the same format as the bundled re:Invent one."""
    q = _toml_string
    lines = [
        f"# Travel times for {event_id}, measured by `rip venues measure` on {date.today()}.",
        "# Street walks from OpenStreetMap (routing.openstreetmap.de, foot profile); allowance =",
        f"# walk + {ROOM_BUFFER_MINUTES} min to reach the room, rounded up to 5, capped at "
        f"{CAP_MINUTES}. Check the matched places below; edit or delete this file freely.",
        "",
        "same_venue_minutes = 5",
        "unknown_minutes = 30",
        "",
        "[aliases]",
        *(f"{p.key} = [{q(p.name.lower())}]" for p in places),
        "",
        "[names]",
        *(f"{p.key} = {q(p.name)}" for p in places),
        "",
        "# Where OpenStreetMap placed each venue (check these). Reused on reruns.",
        "[places]",
        *(
            f"{p.key} = {{ name = {q(p.name)}, lat = {p.lat:.6f}, lon = {p.lon:.6f}, "
            f"label = {q(p.label)} }}"
            for p in places
        ),
        "",
        "[minutes]",
        *(f'"{a}|{b}" = {allowance(w[1])}' for (a, b), w in walks.items()),
        "",
        "[walking]",
        *(f'"{a}|{b}" = {{ meters = {w[0]}, minutes = {w[1]} }}' for (a, b), w in walks.items()),
    ]
    return "\n".join(lines) + "\n"


def normalize_names(names: list[str]) -> list[str]:
    """Distinct venue names, ignoring case and surrounding whitespace (first spelling wins)."""
    seen: dict[str, str] = {}
    for name in names:
        clean = " ".join(name.split())
        if clean and clean.casefold() not in seen:
            seen[clean.casefold()] = clean
    return list(seen.values())


def geocode_all(
    measurer: Measurer,
    names: list[str],
    city: str | None,
    on_progress: Callable[[str], None],
    cached: dict[str, Place] | None = None,
) -> tuple[list[Place], list[str]]:
    """Find every venue (reusing `cached` places by name). Returns places and names not found."""
    if len(names) > MAX_VENUES:
        raise MeasureError(f"{len(names)} venues is more than this tool measures ({MAX_VENUES}).")
    cached = cached or {}
    places: list[Place] = []
    missing: list[str] = []
    keys: set[str] = set()
    for name in names:
        found = cached.get(name.casefold())
        if found is None:
            on_progress(f"Looking up {name}…")
            found = measurer.geocode(name, city)
        if found is None:
            missing.append(name)
            continue
        place = Place(name, venue_key(name), found.lat, found.lon, found.label)
        base, n = place.key[:60], 2
        while place.key in keys:  # two names that slug the same
            place.key, n = f"{base}_{n}", n + 1
        keys.add(place.key)
        places.append(place)
    return places, missing


def outliers(places: list[Place], limit_km: float = 5.0) -> set[str]:
    """Places far from the others: likely a wrong match (e.g. a same-named hall elsewhere).
    With only two, both are flagged when they're that far apart."""
    if len(places) == 2:
        a, b = places
        return {a.key, b.key} if _km((a.lat, a.lon), (b.lat, b.lon)) > limit_km else set()
    if len(places) < 2:
        return set()
    lats = sorted(p.lat for p in places)
    lons = sorted(p.lon for p in places)
    center = (lats[len(lats) // 2], lons[len(lons) // 2])
    return {p.key for p in places if _km((p.lat, p.lon), center) > limit_km}


def walk_all(
    measurer: Measurer, places: list[Place], on_progress: Callable[[str], None]
) -> dict[tuple[str, str], tuple[int, int]]:
    walks: dict[tuple[str, str], tuple[int, int]] = {}
    for a, b in combinations(places, 2):
        on_progress(f"Measuring {a.name} ↔ {b.name}…")
        walks[(a.key, b.key)] = measurer.walk(a, b)
    return walks


def cached_places(toml_text: str) -> dict[str, Place]:
    """Places saved by an earlier `measure` ([places]), keyed by casefolded name, so reruns
    don't query the geocoder again (its usage policy asks clients to cache)."""
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError:
        return {}
    found: dict[str, Place] = {}
    for key, value in (data.get("places") or {}).items():
        try:
            place = Place(
                str(value["name"]),
                key,
                float(value["lat"]),
                float(value["lon"]),
                str(value["label"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not _valid_coordinates(place.lat, place.lon):
            continue  # a bad hand edit: look it up again instead
        found[place.name.casefold()] = place
    return found


def _valid_coordinates(lat: float, lon: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = a, b
    h = (
        math.sin(math.radians(la2 - la1) / 2) ** 2
        + math.cos(math.radians(la1))
        * math.cos(math.radians(la2))
        * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(h))


def _toml_string(text: str) -> str:
    """A TOML basic string. Unlike json.dumps, it keeps non-ASCII characters (including emoji)
    as literal UTF-8 instead of surrogate escapes, which TOML rejects, and it escapes DEL.
    A lone surrogate (which can't be written as UTF-8) becomes U+FFFD."""
    out = []
    for ch in text:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        elif 0xD800 <= ord(ch) <= 0xDFFF:
            out.append("\ufffd")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
