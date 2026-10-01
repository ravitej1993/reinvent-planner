"""Client for the AWS Events REST API (https://api.awsevents.com).

Behavior follows the API guide's error and quota rules:
- 401: refresh the token once and retry once. A second 401 means signing in again.
- 403 with a JSON body: signed in but not registered for the event. Never retried.
- 403 with no body: the edge is refusing our address. Reads back off and retry.
- 409: the operation is closed (e.g. reservations before they open). Never retried here.
- 429: wait `Retry-After` seconds, then retry. Nothing was performed, so this is safe for writes.
- 5xx and network errors: reads retry with backoff. Writes are never re-sent blindly,
  because the API has no idempotency key; callers must reconcile from GetSchedule.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal, TypeVar
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from . import __version__
from .auth import Auth, AuthError, NotSignedInError
from .models import (
    BulkResult,
    Event,
    PersonalTimeInput,
    Schedule,
    Session,
    SessionPage,
    clean_text,
)

API_HOST = "api.awsevents.com"
BASE_URL = f"https://{API_HOST}"
USER_AGENT = f"reinvent-planner/{__version__} (+https://pypi.org/project/reinvent-planner/)"

MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_SECONDS = 65
# ReserveSessions and AssociateFavorites are limited to 30 sessions per minute, counted per
# session. Small batches keep one refused batch from wasting much time.
BULK_BATCH_SIZE = 10

AuthMode = Literal["none", "optional", "required"]
T = TypeVar("T")


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
        # Set by bulk writes that fail part-way: results of the batches that completed, and
        # every session ID whose batch was sent (the failing batch included).
        self.partial: BulkResult | None = None
        self.sent: list[str] = []


class RequestInvalidError(ApiError):
    """400: the request was malformed. Not retried."""


class SignInRequiredError(ApiError):
    """401: no valid token."""


class NotRegisteredError(ApiError):
    """403 with a JSON body: signed in, but not registered for this event."""


class EdgeRefusedError(ApiError):
    """403 with no body: the edge is refusing requests from this address."""


class NotFoundError(ApiError):
    """404: the event, session or personal time entry does not exist."""


class OperationClosedError(ApiError):
    """409: the operation is closed right now, e.g. reservations before they open."""


class ThrottledError(ApiError):
    """429: this operation's per-minute quota is used up."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message, 429)
        self.retry_after = retry_after


class ServiceError(ApiError):
    """5xx: the service failed or is unavailable."""


class WriteOutcomeUnknownError(ApiError):
    """A write may or may not have happened (timeout, dropped connection or 5xx).

    Read GetSchedule and compare it with what you intended before sending anything again.
    """


class BearerAuth(httpx.Auth):
    """Adds the access token, but only to https://api.awsevents.com.

    A 401 triggers one refresh and one retry, as the API guide says.
    """

    def __init__(self, auth: Auth):
        self._auth = auth

    def auth_flow(self, request: httpx.Request):
        if request.url.scheme != "https" or request.url.host != API_HOST:
            raise ApiError(f"Refusing to send your access token to {request.url.host}.")
        request.headers["Authorization"] = f"Bearer {self._auth.access_token()}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._auth.force_refresh()}"
            yield request


# The most recent API response's Date header and our clock when it arrived, per thread: launch
# control's preflight reads it back in the same thread that made the request, so a request on
# another thread (a sync, a refresh) can't swap in its own reading.
_last_server_date = threading.local()


def last_server_date() -> tuple[str | None, float] | None:
    return getattr(_last_server_date, "value", None)


def clear_last_server_date() -> None:
    _last_server_date.value = None


class Cancelled(Exception):
    """Raised by a caller's `sleep` to stop while waiting (e.g. on a rate limit).

    The client only sleeps after an answer that performed nothing (a 429) or before re-sending
    a read, so stopping there never leaves a write in doubt. Like ApiError, it carries what a
    bulk write had already done (`partial`) and every ID whose batch was sent (`sent`)."""

    def __init__(self, *args: object):
        super().__init__(*args)
        self.partial: BulkResult | None = None
        self.sent: list[str] = []


class EventsClient:
    def __init__(
        self,
        auth: Auth | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ):
        self._auth = auth
        self._bearer = BearerAuth(auth) if auth else None
        self._sleep = sleep
        # The last response's Date header and when it arrived (by our clock), for launch
        # control's one-off clock-skew reading.
        self.last_date: tuple[str | None, float] | None = None
        self._http = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def signed_in(self) -> bool:
        return self._auth is not None and self._auth.is_signed_in()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EventsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- catalog ---------------------------------------------------------------

    def list_events(self, *, include_past: bool = False) -> list[Event]:
        params = {"includePast": "true"} if include_past else None
        data = self._get("/v1/events", params=params, auth="none")
        return _parse(lambda: [Event.model_validate(item) for item in data.get("items", [])])

    def get_event(self, event_id: str) -> Event:
        data = self._get(f"/v1/events/{_seg(event_id)}", auth="none")
        return _parse(lambda: Event.model_validate(data["event"]))

    def iter_session_pages(
        self, event_id: str, *, include_abstracts: bool = True, locale: str | None = None
    ) -> Iterator[SessionPage]:
        """Yield every page. Stops only when `nextToken` is absent, never on a short page."""
        token: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, str] = {}
            if not include_abstracts:
                params["includeAbstracts"] = "false"
            if locale:
                params["locale"] = locale
            if token:
                params["nextToken"] = token
            data = self._get(
                f"/v1/events/{_seg(event_id)}/sessions", params=params, auth="optional"
            )
            page = _parse(lambda data=data: SessionPage.model_validate(data))
            yield page
            token = page.next_token
            if not token:
                return
            if token in seen:
                raise ApiError(
                    "The API returned the same page token twice; stopping to avoid a loop."
                )
            seen.add(token)

    def list_sessions(self, event_id: str, **kwargs: Any) -> list[Session]:
        return [s for page in self.iter_session_pages(event_id, **kwargs) for s in page.items]

    def get_session(self, event_id: str, session_id: str) -> Session:
        data = self._get(
            f"/v1/events/{_seg(event_id)}/sessions/{_seg(session_id)}", auth="optional"
        )
        return _parse(lambda: Session.model_validate(data["session"]))

    # -- schedule ----------------------------------------------------------------

    def get_schedule(self, event_id: str) -> Schedule:
        data = self._get(f"/v1/events/{_seg(event_id)}/schedule", auth="required")
        return _parse(lambda: Schedule.model_validate(data["schedule"]))

    def associate_favorites(self, event_id: str, session_ids: list[str]) -> BulkResult:
        return self._bulk(f"/v1/events/{_seg(event_id)}/favorites", session_ids)

    def disassociate_favorite(self, event_id: str, session_id: str) -> None:
        path = f"/v1/events/{_seg(event_id)}/favorites/{_seg(session_id)}"
        self._delete(path)

    def reserve_sessions(self, event_id: str, session_ids: list[str]) -> BulkResult:
        """Reserve seats, in the order given. Batches go out in order, so put top picks first.

        Raises OperationClosedError (409) before reservations open. After
        WriteOutcomeUnknownError, read GetSchedule before sending anything again.
        """
        return self._bulk(f"/v1/events/{_seg(event_id)}/reservations", session_ids)

    def cancel_reservation(self, event_id: str, session_id: str) -> None:
        path = f"/v1/events/{_seg(event_id)}/reservations/{_seg(session_id)}"
        self._delete(path)

    # -- personal time ---------------------------------------------------------------

    def create_personal_time(self, event_id: str, entry: PersonalTimeInput) -> None:
        """Add a personal time entry. The API returns no ID: read GetSchedule to find it.

        Never re-sent automatically: re-sending adds a second entry. After
        WriteOutcomeUnknownError, look for the entry in GetSchedule before trying again.
        """
        self._request(
            "POST",
            f"/v1/events/{_seg(event_id)}/personal-time",
            json=entry.model_dump(by_alias=True, exclude_none=True),
            auth="required",
        )

    def update_personal_time(
        self, event_id: str, personal_time_id: str, entry: PersonalTimeInput
    ) -> None:
        """Replace an entry entirely (fields left out are cleared)."""
        self._request(
            "PUT",
            f"/v1/events/{_seg(event_id)}/personal-time/{_seg(personal_time_id)}",
            json=entry.model_dump(by_alias=True, exclude_none=True),
            auth="required",
        )

    def delete_personal_time(self, event_id: str, personal_time_id: str) -> None:
        self._delete(f"/v1/events/{_seg(event_id)}/personal-time/{_seg(personal_time_id)}")

    # -- internals -----------------------------------------------------------------

    def _bulk(self, path: str, session_ids: list[str]) -> BulkResult:
        combined = BulkResult()
        unique = list(dict.fromkeys(session_ids))
        sent: list[str] = []
        for start in range(0, len(unique), BULK_BATCH_SIZE):
            batch = unique[start : start + BULK_BATCH_SIZE]
            sent.extend(batch)
            try:
                response = self._request("POST", path, json={"sessionIds": batch}, auth="required")
            except (ApiError, Cancelled) as exc:
                exc.partial, exc.sent = combined, sent
                raise
            try:
                result = BulkResult.model_validate(_json_body(response).get("result", {}))
            except (ApiError, ValidationError) as exc:
                # The write went through but its answer is unreadable: the outcome is unknown.
                error = WriteOutcomeUnknownError(
                    "The API accepted the request but its answer couldn't be read. Check your "
                    "schedule before trying again.",
                    response.status_code,
                )
                error.partial, error.sent = combined, sent
                raise error from exc
            combined.successful.extend(result.successful)
            combined.failed.extend(result.failed)
        return combined

    def _delete(self, path: str) -> None:
        """Single removals are safe to retry: a 404 means it is already gone."""
        try:
            self._request("DELETE", path, auth="required", retry_unsafe=True)
        except NotFoundError:
            return

    def _get(self, path: str, *, params: dict | None = None, auth: AuthMode) -> dict:
        return _json_body(self._request("GET", path, params=params, auth=auth, retry_unsafe=True))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        auth: AuthMode,
        retry_unsafe: bool = False,
    ) -> httpx.Response:
        """Send a request, retrying only where the API guide says it is safe.

        `retry_unsafe` marks requests that may be re-sent after a 5xx or network error
        (reads and single removals). 429 is always retried because nothing was performed.
        """
        request_auth = self._auth_for(auth)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                response = self._http.request(
                    method, path, params=params, json=json, auth=request_auth
                )
            except NotSignedInError as exc:
                raise SignInRequiredError(str(exc), 401) from exc
            except AuthError as exc:
                # e.g. the token endpoint failed during a refresh, or the keychain refused a
                # save. This happens before sending, or after a 401 (which performs nothing),
                # so nothing is in doubt.
                raise ApiError(str(exc)) from exc
            except httpx.HTTPError as exc:  # transport failures, but also e.g. a corrupt body
                if retry_unsafe and not last:
                    self._sleep(_backoff(attempt))
                    continue
                if not retry_unsafe:
                    raise WriteOutcomeUnknownError(
                        "The connection failed before the API answered, so this change may or "
                        "may not have been applied. Check your schedule before trying again."
                    ) from exc
                raise ApiError(f"Could not reach the AWS Events API: {exc}") from exc

            self.last_date = (response.headers.get("date"), time.time())
            _last_server_date.value = self.last_date
            # Redirects aren't followed, and the API documents none: a 3xx is an error too.
            if response.status_code < 300:
                return response
            error = _error_for(response, signed_in=request_auth is not None)
            if not retry_unsafe and (
                isinstance(error, ServiceError) or 300 <= response.status_code < 400
            ):
                raise WriteOutcomeUnknownError(
                    f"The API answered HTTP {response.status_code} while applying this change, "
                    "so it may or may not have been applied. Check your schedule before trying "
                    "again.",
                    response.status_code,
                ) from error
            if last:
                raise error
            if isinstance(error, ThrottledError):
                self._sleep(min(error.retry_after, MAX_RETRY_AFTER_SECONDS))
                continue
            if isinstance(error, ServiceError | EdgeRefusedError) and retry_unsafe:
                self._sleep(_backoff(attempt))
                continue
            raise error
        raise AssertionError("unreachable")  # pragma: no cover

    def _auth_for(self, mode: AuthMode) -> BearerAuth | None:
        if mode == "none":
            return None
        if self._bearer is None or self._auth is None:
            if mode == "required":
                raise SignInRequiredError("You are not signed in. Run `rip login`.", 401)
            return None
        if mode == "optional" and not self._auth.is_signed_in():
            return None
        return self._bearer


def _seg(value: str) -> str:
    """Encode one path segment so an ID can never change the request path.

    Percent-encoding handles "/" and friends, but "." and ".." survive encoding and would be
    resolved as dot segments, so they (and empty IDs) are rejected outright.
    """
    if value in ("", ".", "..") or len(value) > 128:
        raise RequestInvalidError(f"{value!r} isn't a valid ID.")
    return quote(value, safe="")


def _backoff(attempt: int) -> float:
    return min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)  # noqa: S311 - jitter, not crypto


def _json_body(response: httpx.Response) -> dict:
    if response.status_code == 204 or not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError(
            "The API returned a response that is not JSON.", response.status_code
        ) from exc
    if not isinstance(data, dict):
        raise ApiError("The API returned an unexpected response.", response.status_code)
    return data


def _parse(build: Callable[[], T]) -> T:
    """Turn a response that doesn't match the documented shape into an ApiError."""
    try:
        return build()
    except (ValidationError, KeyError, TypeError, AttributeError) as exc:
        raise ApiError("The API returned a response in an unexpected shape.") from exc


def _is_json(response: httpx.Response) -> bool:
    try:
        response.json()
    except ValueError:
        return False
    return True


def _message(response: httpx.Response) -> str | None:
    """The body may be JSON, HTML from the edge, or empty, so read it defensively."""
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return clean_text(data["message"])
    return None


def _error_for(response: httpx.Response, *, signed_in: bool) -> ApiError:
    status = response.status_code
    message = _message(response)
    if status == 400:
        return RequestInvalidError(message or "The API rejected the request.", status)
    if status == 401:
        hint = "Your sign-in has expired." if signed_in else "This needs a signed-in attendee."
        return SignInRequiredError(f"{hint} Run `rip login`.", status)
    if status == 403:
        if _is_json(response):
            return NotRegisteredError(
                "You're signed in, but not registered for this event. Register on the event's "
                "own site; signing in again won't help.",
                status,
            )
        return EdgeRefusedError(
            "The API is refusing requests from your network. Slow down.", status
        )
    if status == 404:
        return NotFoundError(message or "Not found.", status)
    if status == 409:
        return OperationClosedError(
            message
            or "This operation isn't open right now (for example, reservations before they open).",
            status,
        )
    if status == 429:
        try:
            retry_after = float(response.headers.get("Retry-After", "10"))
        except ValueError:
            retry_after = 10.0
        if not math.isfinite(retry_after):  # "nan" and "inf" parse, but can't be slept on
            retry_after = 10.0
        return ThrottledError(message or "Rate limit reached.", max(retry_after, 0.0))
    if status >= 500:
        return ServiceError(message or f"The API failed (HTTP {status}).", status)
    return ApiError(message or f"Unexpected HTTP {status}.", status)
