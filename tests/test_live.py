"""Live checks against the real AWS Events API. Skipped by default.

    uv run pytest -m live                       # read-only checks
    RIP_LIVE_WRITES=1 uv run pytest -m live     # also a reversible favorites round trip

Read-only checks catch API drift: the published OpenAPI spec is compared with our models (fields,
optionality and types), the response wrappers we unwrap, and the (method, path) pairs we call;
live responses must parse. Signed-in checks use your real sign-in (run `rip login` first) and
are skipped if you're not signed in. They may refresh your tokens and save the new ones, just as
any `rip` command does. The only write adds one favorite and removes it again; it never touches
reservations.
"""

from __future__ import annotations

import os
import types
import typing

import httpx
import pytest
from pydantic import BaseModel

from reinvent_planner import DEFAULT_EVENT_ID
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import Auth, AuthError, default_token_store
from reinvent_planner.models import (
    BULK_FAILURE_LABELS,
    SEAT_AVAILABILITY_LABELS,
    BulkFailure,
    BulkResult,
    Event,
    EventAddress,
    PersonalTime,
    PersonalTimeInput,
    Schedule,
    Session,
    SessionPage,
    SessionTime,
    Speaker,
)

pytestmark = pytest.mark.live

SPEC_URL = "https://api.awsevents.com/v1/openapi.json"

# The user's own settings, captured at import (before conftest isolates each test), so live
# tests use the same token store `rip` does, e.g. RIP_TOKEN_STORE=file on a headless machine.
_REAL_ENV = {name: os.environ.get(name) for name in ("RIP_TOKEN_STORE", "RIP_CONFIG_DIR")}

# Every (method, path) the client calls; api.py builds exactly these.
CALLED = {
    ("get", "/v1/events"),
    ("get", "/v1/events/{eventId}"),
    ("get", "/v1/events/{eventId}/sessions"),
    ("get", "/v1/events/{eventId}/sessions/{sessionId}"),
    ("get", "/v1/events/{eventId}/schedule"),
    ("post", "/v1/events/{eventId}/reservations"),
    ("delete", "/v1/events/{eventId}/reservations/{sessionId}"),
    ("post", "/v1/events/{eventId}/favorites"),
    ("delete", "/v1/events/{eventId}/favorites/{sessionId}"),
    ("post", "/v1/events/{eventId}/personal-time"),
    ("put", "/v1/events/{eventId}/personal-time/{personalTimeId}"),
    ("delete", "/v1/events/{eventId}/personal-time/{personalTimeId}"),
}

# Response and request wrappers api.py unwraps, and the keys it reads from them.
WRAPPERS = {
    "ListEventsResponseContent": {"items"},
    "GetEventResponseContent": {"event"},
    "ListSessionsResponseContent": {"items", "totalCount", "nextToken"},
    "GetSessionResponseContent": {"session"},
    "GetScheduleResponseContent": {"schedule"},
    "ReserveSessionsResponseContent": {"result"},
    "AssociateFavoritesResponseContent": {"result"},
    "ReserveSessionsRequestContent": {"sessionIds"},
    "AssociateFavoritesRequestContent": {"sessionIds"},
}

MODELS = [
    ("Session", Session),
    ("SessionTime", SessionTime),
    ("Speaker", Speaker),
    ("Event", Event),
    ("EventAddress", EventAddress),
    ("Schedule", Schedule),
    ("PersonalTime", PersonalTime),
    ("BulkResult", BulkResult),
    ("BulkFailure", BulkFailure),
    ("ListSessionsResponseContent", SessionPage),
    ("PersonalTimeInput", PersonalTimeInput),
]


@pytest.fixture(autouse=True)
def real_sign_in(monkeypatch):
    """Undo conftest's isolation: use the user's real token store settings."""
    for name, value in _REAL_ENV.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.fixture
def auth():
    try:
        auth = Auth(default_token_store())
        if not auth.is_signed_in():
            pytest.skip("not signed in: run `rip login` first")
        return auth
    except AuthError as exc:
        pytest.skip(f"no usable sign-in: {exc}")


@pytest.fixture(scope="module")
def spec() -> dict:
    return httpx.get(SPEC_URL, timeout=30).raise_for_status().json()


def _schema(spec: dict, name: str) -> dict:
    return spec["components"]["schemas"][name]


def _resolve(spec: dict, prop: dict) -> dict:
    if "$ref" in prop:
        return _schema(spec, prop["$ref"].rsplit("/", 1)[-1])
    return prop


def _unwrap_optional(annotation):
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


def _type_matches(spec: dict, prop: dict, annotation) -> bool:
    """Coarse check that our annotation can hold what the spec says the field is."""
    target = _resolve(spec, prop)
    kind = target.get("type", "object" if "properties" in target else None)
    annotation = _unwrap_optional(annotation)
    origin = typing.get_origin(annotation) or annotation
    if kind == "string":
        return annotation is str
    if kind == "boolean":
        return annotation is bool
    if kind in ("number", "integer"):
        return annotation in (int, float)
    if kind == "array":
        if origin is not list:
            return False
        item_args = typing.get_args(annotation)
        return not item_args or _type_matches(spec, target.get("items", {}), item_args[0])
    if kind == "object":
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)
    return True  # unknown shapes: don't guess


@pytest.mark.parametrize(("schema", "model"), MODELS)
def test_models_match_the_published_spec(spec, schema, model):
    definition = _schema(spec, schema)
    properties = definition["properties"]
    fields = {(f.alias or name): f for name, f in model.model_fields.items()}
    missing = set(properties) - set(fields)
    assert not missing, f"The API added {schema} fields we don't model: {sorted(missing)}"
    required = set(definition.get("required", []))
    too_strict = {k for k, f in fields.items() if k in properties and f.is_required()} - required
    assert not too_strict, f"{schema}: we require fields the spec says are optional: {too_strict}"
    wrong_type = [
        k
        for k, f in fields.items()
        if k in properties and not _type_matches(spec, properties[k], f.annotation)
    ]
    assert not wrong_type, f"{schema}: types differ from the spec for {wrong_type}"


@pytest.mark.parametrize(("schema", "keys"), WRAPPERS.items())
def test_wrapper_keys_we_read_still_exist(spec, schema, keys):
    present = set(_schema(spec, schema)["properties"])
    assert keys <= present, f"{schema} no longer has {sorted(keys - present)}"


@pytest.mark.parametrize(
    ("schema", "known"),
    [("SeatAvailability", SEAT_AVAILABILITY_LABELS), ("BulkFailureCode", BULK_FAILURE_LABELS)],
)
def test_enum_values_are_all_known(spec, schema, known):
    values = set(_schema(spec, schema)["enum"])
    assert values <= set(known), f"New {schema} values: {sorted(values - set(known))}"


def test_every_method_and_path_we_call_still_exists(spec):
    available = {
        (method.lower(), path)
        for path, operations in spec["paths"].items()
        for method, op in operations.items()
        if isinstance(op, dict) and "operationId" in op
    }
    gone = CALLED - available
    assert not gone, f"Endpoints we call are gone from the API: {sorted(gone)}"


# Query parameters api.py sends, per operation. A renamed parameter would be silently ignored.
QUERY_PARAMS = {
    ("get", "/v1/events"): {"includePast"},
    ("get", "/v1/events/{eventId}/sessions"): {"includeAbstracts", "locale", "nextToken"},
}


@pytest.mark.parametrize(("endpoint", "names"), QUERY_PARAMS.items())
def test_query_parameters_we_send_still_exist(spec, endpoint, names):
    method, path = endpoint
    declared = {p["name"] for p in spec["paths"][path][method].get("parameters", [])}
    assert names <= declared, (
        f"{method.upper()} {path} no longer accepts {sorted(names - declared)}"
    )


def test_public_catalog_parses():
    with EventsClient() as client:
        public = [e for e in client.list_events() if not e.authentication_required]
        if not public:
            pytest.skip("no public event listed right now")
        sessions = client.list_sessions(public[0].event_id, include_abstracts=False)
    assert sessions, "a public event returned no sessions"


def test_signed_in_schedule_and_catalog_parse(auth):
    with EventsClient(auth) as client:
        schedule = client.get_schedule(DEFAULT_EVENT_ID)
        first_page = next(client.iter_session_pages(DEFAULT_EVENT_ID, include_abstracts=False))
    assert isinstance(schedule, Schedule)
    assert first_page.items and first_page.total_count >= len(first_page.items)


@pytest.mark.skipif(os.environ.get("RIP_LIVE_WRITES") != "1", reason="set RIP_LIVE_WRITES=1")
def test_favorite_round_trip(auth):
    """Adds one favorite, confirms it, removes it, confirms it's gone. Leaves no trace.

    If removal fails, the test fails naming the session, so it can be removed by hand.
    """
    with EventsClient(auth) as client:
        before = client.get_schedule(DEFAULT_EVENT_ID)
        page = next(client.iter_session_pages(DEFAULT_EVENT_ID, include_abstracts=False))
        target = next(
            (
                s
                for s in page.items
                if s.session_id not in before.favorites and s.session_id not in before.reserved
            ),
            None,
        )
        if target is None:
            pytest.skip("every session on the first page is already a favorite or reserved")
        try:
            result = client.associate_favorites(DEFAULT_EVENT_ID, [target.session_id])
            assert result.successful == [target.session_id], result
            assert target.session_id in client.get_schedule(DEFAULT_EVENT_ID).favorites
        finally:
            _remove_favorite(client, target)
        after = client.get_schedule(DEFAULT_EVENT_ID)
    assert target.session_id not in after.favorites
    assert sorted(after.favorites) == sorted(before.favorites)


def _remove_favorite(client: EventsClient, target: Session) -> None:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            client.disassociate_favorite(DEFAULT_EVENT_ID, target.session_id)  # 404 = already gone
            return
        except Exception as exc:  # retry once, then tell the user exactly what to clean up
            last_error = exc
    pytest.fail(
        f"Couldn't remove test favorite {target.code} ({last_error}). "
        f"Remove it with `rip fav rm {target.code}`."
    )


@pytest.mark.skipif(os.environ.get("RIP_LIVE_WRITES") != "1", reason="set RIP_LIVE_WRITES=1")
def test_personal_time_round_trip(auth):
    """Adds a 5-minute block late on the last event day, confirms it, deletes it. No trace left.

    The API returns no ID for a new entry, so it's found by its exact times and title.
    """
    from datetime import datetime, timedelta

    from reinvent_planner.models import PersonalTimeInput

    with EventsClient(auth) as client:
        event = client.get_event(DEFAULT_EVENT_ID)
        zone = event.zone()
        last_day = datetime.fromisoformat(event.end_date).astimezone(zone).date()
        start = datetime.combine(last_day, datetime.min.time(), tzinfo=zone) + timedelta(hours=23)
        entry = PersonalTimeInput.from_local(
            start, start + timedelta(minutes=5), title="reinvent-planner live test (safe to delete)"
        )
        before = client.get_schedule(DEFAULT_EVENT_ID)
        test_title = entry.title
        # A leftover from a crashed run carries the test title, so it's safe to clear first.
        for pt in before.personal_time:
            if pt.title == test_title:
                client.delete_personal_time(DEFAULT_EVENT_ID, pt.personal_time_id)
        before = client.get_schedule(DEFAULT_EVENT_ID)
        before_ids = {pt.personal_time_id for pt in before.personal_time}
        created = None
        try:
            client.create_personal_time(DEFAULT_EVENT_ID, entry)
            created = next(
                (
                    pt
                    for pt in client.get_schedule(DEFAULT_EVENT_ID).personal_time
                    if pt.personal_time_id not in before_ids and entry.same_as(pt)
                ),
                None,
            )
        finally:
            # Delete only entries that are new AND carry the test title, whether or not the
            # API normalized anything: never a real entry.
            for pt in client.get_schedule(DEFAULT_EVENT_ID).personal_time:
                if pt.personal_time_id not in before_ids and pt.title.strip() == test_title:
                    client.delete_personal_time(DEFAULT_EVENT_ID, pt.personal_time_id)
        after = client.get_schedule(DEFAULT_EVENT_ID)
    assert created is not None
    assert not any(entry.same_as(pt) for pt in after.personal_time)
    assert len(after.personal_time) == len(before.personal_time)
