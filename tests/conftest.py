"""Shared fixtures. Every session here is made up: never add real catalog data to tests."""

from __future__ import annotations

import json
import os
from collections.abc import Callable

import httpx
import pytest

from reinvent_planner.auth import Auth, MemoryTokenStore, TokenSet
from reinvent_planner.catalog import Catalog
from reinvent_planner.models import Event, Session

# Rich fixes the console width when the CLI module is imported if COLUMNS is set then, which
# would freeze a developer's shell width (e.g. COLUMNS=80) into every test. Clear it before any
# test imports the CLI; each test's own COLUMNS (see isolated_dirs) then applies.
os.environ.pop("COLUMNS", None)

EVENT_ID = "testconf2026"
ZONE = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep every test away from the real keychain, data and config directories."""
    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RIP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RIP_TOKEN_STORE", "file")
    # Rich wraps output to the terminal width; pin it so assertions don't depend on the shell.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("RIP_EVENT", raising=False)


def make_event(**overrides) -> Event:
    data = {
        "eventId": EVENT_ID,
        "name": "Test Conf 2026",
        "eventType": "Conference",
        "startDate": "2026-11-30T08:00:00.000-08:00",
        "endDate": "2026-12-04T18:00:00.000-08:00",
        "isOnline": False,
        "authenticationRequired": True,
        "timezone": ZONE,
        "address": {"city": "Las Vegas"},
    }
    data.update(overrides)
    return Event.model_validate(data)


def make_session(session_id: str, **overrides) -> Session:
    """A session on Tue Dec 1 2026. Pass time="HH:MM", length="60", venue=..., etc."""
    data = {
        "sessionId": session_id,
        "abbreviation": session_id.upper(),
        "title": f"Session {session_id}",
        "abstract": f"Abstract for {session_id}.",
        "type": "Breakout session",
        "level": "300 - Advanced",
        "venue": "Venetian",
        "room": "Level 3, Room A",
        "isReservable": True,
        "seatAvailability": "available",
        "sessionTime": {"date": "2026-12-01", "time": "10:00", "length": "60", "timezone": ZONE},
        "speakers": [{"name": "Ada Example"}],
        "topics": ["Artificial Intelligence"],
        "services": ["Amazon Bedrock"],
    }
    time_fields = {k: overrides.pop(k) for k in ("date", "time", "length") if k in overrides}
    if time_fields:
        data["sessionTime"] = {**data["sessionTime"], **time_fields}
    data.update(overrides)
    return Session.model_validate(data)


def session_json(session: Session) -> dict:
    return json.loads(session.model_dump_json(by_alias=True, exclude_none=True))


@pytest.fixture
def event() -> Event:
    return make_event()


@pytest.fixture
def catalog(tmp_path) -> Catalog:
    cat = Catalog(tmp_path / "catalog.sqlite3")
    yield cat
    cat.close()


def fresh_tokens(**overrides) -> TokenSet:
    import time

    values = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": time.time() + 3600,
        "email": "attendee@example.com",
    }
    values.update(overrides)
    return TokenSet(**values)


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def signed_in_auth(token_handler=None, tokens: TokenSet | None = None) -> Auth:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})

    return Auth(
        MemoryTokenStore(tokens or fresh_tokens()),
        http=mock_client(token_handler or default_handler),
    )
