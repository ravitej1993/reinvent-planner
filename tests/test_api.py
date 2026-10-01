import json

import httpx
import pytest
from conftest import EVENT_ID, make_session, session_json, signed_in_auth

from reinvent_planner.api import (
    ApiError,
    BearerAuth,
    EdgeRefusedError,
    EventsClient,
    NotRegisteredError,
    OperationClosedError,
    RequestInvalidError,
    SignInRequiredError,
    WriteOutcomeUnknownError,
)


class Recorder:
    """A fake API: returns queued responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        # httpx re-sends the same Request object after a refresh, so snapshot the header.
        self.auth_headers: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auth_headers.append(request.headers.get("authorization"))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(recorder, auth=None, sleeps=None) -> EventsClient:
    sleeper = sleeps.append if sleeps is not None else (lambda s: None)
    return EventsClient(auth, transport=httpx.MockTransport(recorder), sleep=sleeper)


def page(sessions, next_token=None, total=None):
    body = {"items": [session_json(s) for s in sessions], "totalCount": total or len(sessions)}
    if next_token:
        body["nextToken"] = next_token
    return httpx.Response(200, json=body)


def schedule_response(reserved=(), favorites=()):
    return httpx.Response(
        200,
        json={
            "schedule": {
                "reserved": list(reserved),
                "favorites": list(favorites),
                "personalTime": [],
            }
        },
    )


def test_list_events_never_sends_a_token():
    rec = Recorder(httpx.Response(200, json={"items": []}))
    client(rec, auth=signed_in_auth()).list_events()
    assert "authorization" not in rec.requests[0].headers


def test_pagination_follows_next_token_past_short_pages():
    rec = Recorder(
        page([make_session("a")], next_token="t1", total=3),
        page([], next_token="t2", total=3),  # an empty page is not the end
        page([make_session("b"), make_session("c")], total=3),
    )
    sessions = client(rec).list_sessions(EVENT_ID, include_abstracts=False)
    assert [s.session_id for s in sessions] == ["a", "b", "c"]
    assert rec.requests[0].url.params["includeAbstracts"] == "false"
    assert rec.requests[1].url.params["nextToken"] == "t1"
    assert rec.requests[2].url.params["nextToken"] == "t2"


def test_repeated_page_token_stops_instead_of_looping():
    rec = Recorder(page([], next_token="same"), page([], next_token="same"))
    with pytest.raises(ApiError, match="same page token"):
        client(rec).list_sessions(EVENT_ID)


def test_catalog_read_sends_token_when_signed_in():
    rec = Recorder(page([]))
    client(rec, auth=signed_in_auth()).list_sessions(EVENT_ID)
    assert rec.requests[0].headers["authorization"] == "Bearer access-1"


def test_throttling_waits_retry_after_then_retries():
    sleeps = []
    rec = Recorder(
        httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
        page([make_session("a")]),
    )
    assert len(client(rec, sleeps=sleeps).list_sessions(EVENT_ID)) == 1
    assert sleeps == [7.0]


def test_401_refreshes_once_and_retries():
    rec = Recorder(httpx.Response(401), schedule_response(favorites=["a"]))
    schedule = client(rec, auth=signed_in_auth()).get_schedule(EVENT_ID)
    assert schedule.favorites == ["a"]
    assert rec.auth_headers == ["Bearer access-1", "Bearer access-2"]


def test_403_with_body_means_not_registered_and_is_not_retried():
    rec = Recorder(httpx.Response(403, json={"message": "not registered"}))
    with pytest.raises(NotRegisteredError):
        client(rec, auth=signed_in_auth()).get_schedule(EVENT_ID)
    assert len(rec.requests) == 1


def test_403_without_body_is_an_edge_refusal_that_reads_retry():
    sleeps = []
    rec = Recorder(httpx.Response(403), page([]))
    client(rec, sleeps=sleeps).list_sessions(EVENT_ID)
    assert len(rec.requests) == 2 and len(sleeps) == 1


def test_edge_refusal_eventually_surfaces():
    rec = Recorder(*[httpx.Response(403, text="<html>denied</html>")] * 5)
    with pytest.raises(EdgeRefusedError):
        client(rec).list_sessions(EVENT_ID)


def test_409_operation_closed_is_not_retried():
    rec = Recorder(httpx.Response(409, json={"message": "Reservations open Oct 8"}))
    with pytest.raises(OperationClosedError, match="Oct 8"):
        client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, ["a"])
    assert len(rec.requests) == 1


def test_400_is_not_retried():
    rec = Recorder(httpx.Response(400, json={"message": "sessionIds too long"}))
    with pytest.raises(RequestInvalidError, match="too long"):
        client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, ["a"])


def test_server_error_on_read_is_retried():
    rec = Recorder(httpx.Response(503), httpx.Response(500, text="oops"), page([]))
    client(rec).list_sessions(EVENT_ID)
    assert len(rec.requests) == 3


def test_server_error_on_write_is_never_resent():
    rec = Recorder(httpx.Response(500))
    with pytest.raises(WriteOutcomeUnknownError):
        client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, ["a"])
    assert len(rec.requests) == 1


def test_network_error_on_write_is_never_resent():
    rec = Recorder(httpx.ReadTimeout("timed out"))
    with pytest.raises(WriteOutcomeUnknownError):
        client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, ["a"])
    assert len(rec.requests) == 1


def test_network_error_on_read_is_retried():
    rec = Recorder(httpx.ConnectError("down"), page([]))
    client(rec).list_sessions(EVENT_ID)
    assert len(rec.requests) == 2


def test_bulk_writes_are_batched_and_results_merged():
    def result(ids, failed=()):
        return httpx.Response(200, json={"result": {"successful": ids, "failed": list(failed)}})

    ids = [f"s{i}" for i in range(25)]
    rec = Recorder(
        result(ids[:10]),
        result(ids[10:19], failed=[{"sessionId": "s19", "code": "brandNewReason"}]),
        result(ids[20:]),
    )
    outcome = client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, [*ids, "s0"])
    assert len(rec.requests) == 3
    assert [len(json.loads(r.content)["sessionIds"]) for r in rec.requests] == [10, 10, 5]
    assert len(outcome.successful) == 24
    assert outcome.failed[0].reason == "refused"


def test_single_removal_treats_404_as_done():
    rec = Recorder(httpx.Response(404, json={"message": "not a favorite"}))
    client(rec, auth=signed_in_auth()).disassociate_favorite(EVENT_ID, "a")


def test_schedule_requires_sign_in_without_calling_the_api():
    rec = Recorder()
    with pytest.raises(SignInRequiredError):
        client(rec).get_schedule(EVENT_ID)
    assert rec.requests == []


def test_path_segments_are_encoded():
    rec = Recorder(httpx.Response(200, json={"session": session_json(make_session("x"))}))
    client(rec).get_session("evil/../event", "a b")
    assert rec.requests[0].url.raw_path == b"/v1/events/evil%2F..%2Fevent/sessions/a%20b"


def test_non_json_error_bodies_are_handled():
    rec = Recorder(httpx.Response(400, text="<html>Bad request</html>"))
    with pytest.raises(RequestInvalidError):
        client(rec).get_event(EVENT_ID)


def test_bearer_auth_refuses_other_hosts():
    flow = BearerAuth(signed_in_auth()).auth_flow(httpx.Request("GET", "https://evil.example/v1"))
    with pytest.raises(ApiError, match="Refusing"):
        next(flow)


def test_bearer_auth_refuses_plain_http():
    flow = BearerAuth(signed_in_auth()).auth_flow(
        httpx.Request("GET", "http://api.awsevents.com/v1")
    )
    with pytest.raises(ApiError, match="Refusing"):
        next(flow)


def test_redirects_are_not_followed():
    rec = Recorder(httpx.Response(302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(ApiError) as caught:
        client(rec)._request("GET", "/v1/events", auth="none", retry_unsafe=True)
    assert caught.value.status == 302 and len(rec.requests) == 1


def test_redirect_on_a_read_is_an_error_not_an_empty_success():
    rec = Recorder(httpx.Response(302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(ApiError) as caught:
        client(rec).list_events()
    assert not isinstance(caught.value, WriteOutcomeUnknownError)
    assert caught.value.status == 302 and len(rec.requests) == 1


def test_redirect_on_reserve_is_an_unknown_outcome():
    rec = Recorder(httpx.Response(302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(WriteOutcomeUnknownError) as caught:
        client(rec, auth=signed_in_auth()).reserve_sessions(EVENT_ID, ["a"])
    assert caught.value.status == 302 and caught.value.sent == ["a"]
    assert len(rec.requests) == 1


def test_server_error_on_a_write_after_throttling_is_still_an_unknown_outcome():
    throttled = httpx.Response(429, headers={"Retry-After": "1"})
    rec = Recorder(*[throttled] * 4, httpx.Response(503))
    with pytest.raises(WriteOutcomeUnknownError):
        client(rec, auth=signed_in_auth()).reserve_sessions(EVENT_ID, ["a"])
    assert len(rec.requests) == 5


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "NaN"])
def test_non_finite_retry_after_waits_the_default(value):
    sleeps = []
    rec = Recorder(httpx.Response(429, headers={"Retry-After": value}), page([]))
    client(rec, sleeps=sleeps).list_sessions(EVENT_ID)
    assert sleeps == [10.0]


def test_error_messages_from_the_api_lose_control_characters():
    message = "Reservations \x1b]8;;https://evil.example\x07open\x1b]8;;\x07\nOct 8\x1b[2J"
    rec = Recorder(httpx.Response(409, json={"message": message}))
    with pytest.raises(OperationClosedError) as caught:
        client(rec, auth=signed_in_auth()).reserve_sessions(EVENT_ID, ["a"])
    assert str(caught.value) == "Reservations ]8;;https://evil.exampleopen]8;;\nOct 8[2J"
    rec = Recorder(httpx.Response(400, json={"message": "bad\x9b31m id"}))
    with pytest.raises(RequestInvalidError, match=r"^bad31m id$"):
        client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, ["a"])


def test_any_json_403_means_not_registered():
    rec = Recorder(httpx.Response(403, json={"error": "NotRegistered"}))
    with pytest.raises(NotRegisteredError):
        client(rec, auth=signed_in_auth()).get_schedule(EVENT_ID)
    assert len(rec.requests) == 1


@pytest.mark.parametrize("bad", ["", ".", "..", "x" * 129])
def test_dot_segments_and_bad_ids_are_rejected(bad):
    rec = Recorder()
    with pytest.raises(RequestInvalidError):
        client(rec, auth=signed_in_auth()).disassociate_favorite(EVENT_ID, bad)
    assert rec.requests == []


def test_unexpected_response_shape_is_an_api_error():
    rec = Recorder(httpx.Response(200, json={"not": "a schedule"}))
    with pytest.raises(ApiError, match="unexpected shape"):
        client(rec, auth=signed_in_auth()).get_schedule(EVENT_ID)


def test_corrupt_response_body_on_a_write_is_an_unknown_outcome():
    rec = Recorder(httpx.DecodingError("bad gzip"))
    with pytest.raises(WriteOutcomeUnknownError):
        client(rec, auth=signed_in_auth()).associate_favorites(EVENT_ID, ["a"])
    assert len(rec.requests) == 1


def test_live_endpoint_list_matches_the_client():
    """test_live.CALLED is hand-maintained; fail here if api.py gains a path it doesn't list."""
    import inspect
    import re

    import test_live

    from reinvent_planner import api

    source = inspect.getsource(api)
    templates = {
        re.sub(r"\{_seg\((\w+)\)\}", lambda m: "{" + _camel(m.group(1)) + "}", raw)
        for raw in re.findall(r'f?"(/v1/events[^"]*)"', source)
    }
    listed = {path for _method, path in test_live.CALLED}
    assert templates == listed, f"api.py paths {sorted(templates - listed)} missing from CALLED"


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def test_the_server_clock_reading_is_per_thread():
    """The review's P2-M5: a sync on another thread overwrote the preflight's skew reading."""
    import threading

    from reinvent_planner import api

    api.clear_last_server_date()
    recorder = Recorder(httpx.Response(200, json={"events": []}, headers={"date": "mine"}))
    client(recorder).list_events()

    def other_thread():
        other = Recorder(httpx.Response(200, json={"events": []}, headers={"date": "theirs"}))
        client(other).list_events()

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()
    assert api.last_server_date()[0] == "mine"
