"""Fill a throwaway data folder with a made-up re:Invent week, for screenshots and the demo GIF.

Every session here is invented: no real catalog data. It uses the event ID `reinvent2026` so
the bundled travel table (and so the Strip map) applies. Run it with scratch folders, never
your real ones (it refuses otherwise):

    export RIP_DATA_DIR="$(mktemp -d)" RIP_CONFIG_DIR="$(mktemp -d)" RIP_TOKEN_STORE=file
    uv run python scripts/demo/seed_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from reinvent_planner.auth import config_dir
from reinvent_planner.catalog import Catalog, data_dir
from reinvent_planner.models import Event, Schedule, Session

EVENT_ID = "reinvent2026"
ZONE = "America/Los_Angeles"

# (id, title, type, venue, room, day, start, minutes, seats)
SESSIONS = [
    (
        "dmo101",
        "Keynote overflow: the year in cloud",
        "Keynote",
        "Venetian",
        "Level 2, Hall C",
        "2026-12-01",
        "08:00",
        90,
        "available",
    ),
    (
        "dmo301",
        "Build agents that call your APIs",
        "Workshop",
        "MGM Grand",
        "Level 1, Room 116",
        "2026-12-01",
        "10:00",
        120,
        "limited",
    ),
    (
        "dmo302",
        "Agents in production: evals and guardrails",
        "Chalk talk",
        "Caesars Forum",
        "Level 1, Alliance 305",
        "2026-12-01",
        "10:30",
        60,
        "veryLimited",
    ),
    (
        "dmo210",
        "Serverless at 1M RPS",
        "Breakout session",
        "Wynn",
        "Convention Promenade, Lafite 7",
        "2026-12-01",
        "13:00",
        60,
        "available",
    ),
    (
        "dmo220",
        "Graviton cost teardown",
        "Breakout session",
        "Venetian",
        "Level 4, Delfino 4005",
        "2026-12-01",
        "13:30",
        60,
        "limited",
    ),
    (
        "dmo330",
        "Data lakes without the swamp",
        "Builders' session",
        "Encore",
        "Level 1, Encore Ballroom",
        "2026-12-01",
        "15:00",
        60,
        "available",
    ),
    (
        "dmo410",
        "Chaos engineering game day",
        "Workshop",
        "Caesars Palace",
        "Forum 124",
        "2026-12-01",
        "16:30",
        120,
        "unavailable",
    ),
]


def _default_dir(find, variable: str) -> Path:
    """Where the app would keep its files if `variable` weren't set."""
    saved = os.environ.pop(variable, None)
    try:
        return find().resolve()
    finally:
        if saved is not None:
            os.environ[variable] = saved


def _same_folder(a: Path, b: Path) -> bool:
    """By identity where both exist (catches other letter case on macOS and Windows, and
    symlinks); otherwise by normalised, case-folded path."""
    if a.exists() and b.exists():
        return os.path.samefile(a, b)
    return os.path.normcase(str(a.resolve())).casefold() == (
        os.path.normcase(str(b.resolve())).casefold()
    )


def check_scratch_dirs() -> None:
    """Refuse unless both folders are set, exist, aren't the real ones, and hold no catalog yet.
    The app treats an empty variable as unset (meaning your real folder), so the demo can't
    rely on the variables alone."""
    for variable, find in (("RIP_DATA_DIR", data_dir), ("RIP_CONFIG_DIR", config_dir)):
        value = os.environ.get(variable, "")
        if not value or not Path(value).is_dir():
            sys.exit(f"Set {variable} to an existing scratch folder first (see the docstring).")
        if _same_folder(Path(value), _default_dir(find, variable)):
            sys.exit(f"{variable} points at your real folder; use a scratch folder.")
    if (Path(os.environ["RIP_DATA_DIR"]) / "catalog.sqlite3").exists():
        sys.exit("RIP_DATA_DIR already holds a catalog; use a fresh, empty scratch folder.")


def main() -> None:
    check_scratch_dirs()
    event = Event.model_validate(
        {
            "eventId": EVENT_ID,
            "name": "Demo week (made-up sessions)",
            "eventType": "Conference",
            "startDate": "2026-11-30T08:00:00.000-08:00",
            "endDate": "2026-12-04T18:00:00.000-08:00",
            "isOnline": False,
            "authenticationRequired": True,
            "timezone": ZONE,
            "address": {"city": "Las Vegas"},
        }
    )
    sessions = [
        Session.model_validate(
            {
                "sessionId": sid,
                "abbreviation": sid.upper(),
                "title": title,
                "abstract": "A made-up session for the demo.",
                "type": kind,
                "level": "300 - Advanced",
                "venue": venue,
                "room": room,
                "isReservable": True,
                "seatAvailability": seats,
                "sessionTime": {
                    "date": day,
                    "time": start,
                    "length": str(minutes),
                    "timezone": ZONE,
                },
                "speakers": [{"name": "Demo Speaker"}],
                "topics": ["Artificial Intelligence"],
            }
        )
        for sid, title, kind, venue, room, day, start, minutes, seats in SESSIONS
    ]
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(event, sessions)
        cat.set_rank(EVENT_ID, "dmo301", 1)
        cat.add_backup(EVENT_ID, "dmo302", "dmo301")
        cat.set_rank(EVENT_ID, "dmo210", 2)
        cat.set_rank(EVENT_ID, "dmo220", 3)
        cat.set_rank(EVENT_ID, "dmo330", 4)
        cat.save_schedule(EVENT_ID, Schedule(reserved=["dmo101"], favorites=["dmo410"]))
    print(f"Demo week written to {os.environ['RIP_DATA_DIR']}")


if __name__ == "__main__":
    main()
