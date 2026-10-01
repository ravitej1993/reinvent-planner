"""Sign-in for the AWS Events API: OAuth 2.0 authorization code flow with PKCE via AWS Builder ID.

Everything here follows the API's authentication guide:
https://docs.aws.amazon.com/events/latest/devguide/authentication.html

Security properties:
- The client ID is public and shared. PKCE protects the flow, so there is no secret.
- The callback server listens only on 127.0.0.1, on one of the API's reserved ports, and
  accepts exactly one request whose `state` matches.
- Tokens live in the operating system keychain by default. The file store is opt-in and
  written with owner-only permissions.
- Tokens are never logged, and are only ever sent to the token endpoints here and to
  api.awsevents.com (see `api.BearerAuth`).
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import hmac
import json
import math
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from .models import clean_text

AUTHORIZE_URL = "https://oauth.awsevents.com/oauth2/authorize"
TOKEN_URL = "https://oauth.awsevents.com/oauth2/token"  # noqa: S105 - an endpoint, not a secret
REVOKE_URL = "https://oauth.awsevents.com/oauth2/revoke"
LOGOUT_URL = "https://oauth.awsevents.com/logout"
BUILDER_ID_LOGOUT_URL = "https://idp.awsevents.com/oidc/logout"
BUILDER_ID_PROFILE_URL = "https://profile.aws.amazon.com"
CLIENT_ID = "7vmom55m1qstvq8i71ph127bfq"
SCOPE = "openid email events/access"
IDENTITY_PROVIDER = "AWSBuilderID"

# The API reserves loopback ports 8484-8489. Claude Code's awsevents MCP setup uses 8484,
# so start at 8485 to avoid colliding with a sign-in running there.
CALLBACK_PORTS = (8485, 8486, 8487, 8488, 8489, 8484)
CALLBACK_HOST = "127.0.0.1"
SIGN_IN_TIMEOUT_SECONDS = 300
REFRESH_MARGIN_SECONDS = 60

KEYRING_SERVICE = "reinvent-planner"
TOKEN_STORE_ENV = "RIP_TOKEN_STORE"  # noqa: S105 - an env var name, not a secret


class AuthError(Exception):
    """Sign-in or token handling failed."""


class NotSignedInError(AuthError):
    """There is no usable token. The user has to run `rip login`."""


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def new_code_verifier() -> str:
    """A fresh verifier: 86 characters from the base64url alphabet (within RFC 7636's 43-128)."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def redirect_uri(port: int) -> str:
    return f"http://{CALLBACK_HOST}:{port}/callback"


def authorize_url(port: int, state: str, verifier: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(port),
        "scope": SCOPE,
        "identity_provider": IDENTITY_PROVIDER,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def builder_id_logout_url(port: int) -> str:
    """URL that ends both the brokering session and the Builder ID session in the browser."""
    inner = f"{LOGOUT_URL}?" + urlencode(
        {"client_id": CLIENT_ID, "logout_uri": f"http://{CALLBACK_HOST}:{port}/logout"},
        quote_via=quote,
    )
    return f"{BUILDER_ID_LOGOUT_URL}?redirect_uri={quote(inner, safe='')}"


# ---------------------------------------------------------------------------
# Tokens and storage
# ---------------------------------------------------------------------------


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    email: str | None = None

    def expires_soon(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at - REFRESH_MARGIN_SECONDS

    def __repr__(self) -> str:  # never let tokens end up in logs or tracebacks
        return f"TokenSet(email={self.email!r}, expires_at={self.expires_at!r})"


class TokenStore(Protocol):
    def load(self) -> TokenSet | None: ...
    def save(self, tokens: TokenSet) -> None: ...
    def clear(self) -> None: ...


class KeyringTokenStore:
    """Stores tokens in the OS keychain (macOS Keychain, Windows Credential Manager, or
    Secret Service on Linux).

    Windows Credential Manager caps an entry at 2,560 bytes and stores text as UTF-16, so an
    entry holds at most 1,280 characters. Tokens are split into numbered chunks well under that.

    Saves alternate between two slots, "a" and "b": the new tokens are written into the unused
    slot, then `meta` is switched to point at it, then the old slot is deleted. So there's never
    a moment with no valid saved sign-in (another process reading mid-save sees the old one),
    and a save that fails part-way leaves the previous tokens untouched.
    """

    CHUNK_CHARS = 1000
    SLOTS = ("a", "b")
    NAMES = ("access_token", "refresh_token")
    _MAX_CHUNKS = 64

    def __init__(self, service: str = KEYRING_SERVICE):
        import keyring
        import keyring.backends.fail
        import keyring.errors

        if isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring):
            raise AuthError(
                "No OS keychain is available. Set RIP_TOKEN_STORE=file to store tokens in a "
                "file only your user can read instead."
            )
        self._keyring = keyring
        self._errors = keyring.errors
        self._service = service

    def _get(self, name: str) -> str | None:
        return self._keyring.get_password(self._service, name)

    def _delete(self, name: str) -> None:
        with contextlib.suppress(self._errors.PasswordDeleteError):
            self._keyring.delete_password(self._service, name)

    def _delete_chunks(self, prefix: str) -> None:
        """Delete prefix.0, prefix.1, ... without needing a count.

        Deletes from the highest chunk down, so an interrupted delete always leaves a run that
        starts at .0 and the next probe still finds it.
        """
        top = -1
        for i in range(self._MAX_CHUNKS):
            if self._get(f"{prefix}.{i}") is None:
                break
            top = i
        for i in range(top, -1, -1):
            self._delete(f"{prefix}.{i}")

    def _meta(self) -> dict | None:
        raw = self._get("meta")
        return json.loads(raw) if raw else None

    def _meta_or_none(self) -> dict | None:
        try:
            meta = self._meta()
        except (ValueError, TypeError):
            return None
        return meta if isinstance(meta, dict) else None

    def _read(self, prefix: str, chunks: int) -> str | None:
        parts = [self._get(f"{prefix}.{i}") for i in range(chunks)]
        return None if any(p is None for p in parts) else "".join(parts)  # type: ignore[arg-type]

    def load(self) -> TokenSet | None:
        try:
            meta = self._meta()
            if meta is None or meta.get("slot") not in self.SLOTS:
                return None
            slot = meta["slot"]
            access = self._read(f"access_token.{slot}", int(meta["access_chunks"]))
            refresh = self._read(f"refresh_token.{slot}", int(meta["refresh_chunks"]))
            if not (access and refresh):
                return None
            return TokenSet(access, refresh, float(meta["expires_at"]), meta.get("email"))
        except Exception as exc:  # keychain locked or denied, or corrupt entries
            raise AuthError(
                "Couldn't read your saved sign-in from the OS keychain. "
                "Run `rip logout`, then `rip login`."
            ) from exc

    def save(self, tokens: TokenSet, _retry: bool = False) -> None:
        slot = "a"
        try:
            previous = self._meta_or_none() or {}
            slot = "b" if previous.get("slot") == "a" else "a"
            counts = {}
            for name, value in zip(
                self.NAMES, (tokens.access_token, tokens.refresh_token), strict=True
            ):
                self._delete_chunks(f"{name}.{slot}")  # leftovers from an interrupted save
                parts = _chunks(value, self.CHUNK_CHARS)
                for i, part in enumerate(parts):
                    self._keyring.set_password(self._service, f"{name}.{slot}.{i}", part)
                counts[name] = len(parts)
            meta = {
                "slot": slot,
                "expires_at": tokens.expires_at,
                "email": tokens.email,
                "access_chunks": counts["access_token"],
                "refresh_chunks": counts["refresh_token"],
            }
            self._keyring.set_password(self._service, "meta", json.dumps(meta))
        except Exception as exc:
            # Only this uncommitted slot is cleaned up: `meta` still points at the old one, so
            # the working sign-in survives. A committed slot is never deleted.
            with contextlib.suppress(Exception):
                for name in self.NAMES:
                    self._delete_chunks(f"{name}.{slot}")
            raise AuthError(
                f"Couldn't save your sign-in to the OS keychain ({type(exc).__name__}). "
                "Set RIP_TOKEN_STORE=file to use a file only your user can read, then "
                "run `rip login` again."
            ) from exc
        if not _retry:
            try:
                intact = self.load() == tokens
            except AuthError:
                intact = True  # can't read it back just now; never undo a committed save
            if not intact:
                # Another rip process saved into the same slot at the same moment and the
                # chunks interleaved. Save once more into the other slot (if that fails, it only
                # cleans up its own, uncommitted slot).
                self.save(tokens, _retry=True)
                return
        old = "b" if slot == "a" else "a"
        with contextlib.suppress(Exception):
            for name in self.NAMES:
                self._delete_chunks(f"{name}.{old}")

    def clear(self) -> None:
        """Delete everything, without relying on `meta` (it may be missing or corrupt)."""
        self._delete("meta")
        for name in self.NAMES:
            for slot in self.SLOTS:
                self._delete_chunks(f"{name}.{slot}")
            self._delete_chunks(name)  # layout from pre-release builds
            self._delete(name)


def _chunks(value: str, size: int) -> list[str]:
    return [value[i : i + size] for i in range(0, len(value), size)] or [""]


class FileTokenStore:
    """Opt-in fallback: a JSON file readable only by the current user."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "tokens.json"

    def load(self) -> TokenSet | None:
        symlink = f"{self.path} is a symbolic link; refusing to read tokens from it."
        corrupt = f"{self.path} is unreadable or corrupt. Run `rip logout`, then `rip login`."
        if self.path.is_symlink():
            raise AuthError(symlink)
        # Checks run on the open descriptor, so the file can't be swapped between the check
        # and the read. O_NONBLOCK keeps a planted FIFO from hanging the open.
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
            flags |= getattr(os, name, 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuthError(symlink if exc.errno == errno.ELOOP else corrupt) from exc
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise AuthError(corrupt)
            if os.name == "posix":
                mode = stat.S_IMODE(info.st_mode)
                if mode & 0o077:
                    raise AuthError(
                        f"{self.path} is readable by other users (mode {mode:o}). "
                        f"Run `chmod 600 {self.path}` or `rip logout`."
                    )
                if info.st_uid != os.getuid():
                    raise AuthError(
                        f"{self.path} belongs to another user; refusing to read tokens from it. "
                        "Run `rip logout`, then `rip login`."
                    )
            try:
                raw = fh.read()
            except OSError as exc:
                raise AuthError(corrupt) from exc
        try:
            return TokenSet(**json.loads(raw.decode("utf-8")))
        except (ValueError, TypeError) as exc:
            raise AuthError(corrupt) from exc

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A fresh, exclusively created temp file (mode 0600) per save: no fixed name to
        # pre-create or symlink, and concurrent saves can't interleave.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(tokens), fh)
                fh.flush()
                os.fsync(fh.fileno())  # never rename a file whose contents aren't on disk yet
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class MemoryTokenStore:
    """For tests."""

    def __init__(self, tokens: TokenSet | None = None):
        self.tokens = tokens

    def load(self) -> TokenSet | None:
        return self.tokens

    def save(self, tokens: TokenSet) -> None:
        self.tokens = tokens

    def clear(self) -> None:
        self.tokens = None


def default_token_store() -> TokenStore:
    choice = os.environ.get(TOKEN_STORE_ENV, "keyring").strip().lower()
    if choice == "file":
        return FileTokenStore()
    if choice == "keyring":
        return KeyringTokenStore()
    raise AuthError(f"{TOKEN_STORE_ENV} must be 'keyring' or 'file', not {choice!r}.")


def config_dir() -> Path:
    if override := os.environ.get("RIP_CONFIG_DIR"):
        return Path(override)
    if sys.platform == "win32":
        return (
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / "reinvent-planner"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "reinvent-planner"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "reinvent-planner"


def _email_from_id_token(id_token: str | None) -> str | None:
    """Read the email claim for display only. The signature is not checked, so never trust
    this for access decisions. The API validates the access token itself."""
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, UnicodeDecodeError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    if not isinstance(email, str):
        return None
    return clean_text(email) or None


def _printable(text: str, limit: int = 200) -> str:
    """Strip control characters and cap length before showing text we didn't write."""
    cleaned = "".join(ch for ch in text if ch.isprintable())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise AuthError("The token endpoint returned a response that is not JSON.") from exc
    if not isinstance(data, dict):
        raise AuthError("The token endpoint returned an unexpected response.")
    return data


def _expires_in(value: object) -> float:
    """Seconds until the access token expires: the default 3600 when missing or unreadable,
    otherwise clamped to [2 × the refresh margin, 86400], so a bad value can't mean "never",
    or "already" (which would refresh on every call)."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 3600.0
    if not math.isfinite(seconds):
        return 3600.0
    return min(max(seconds, 2.0 * REFRESH_MARGIN_SECONDS), 86400.0)


def _tokens_from_response(data: dict, previous: TokenSet | None = None) -> TokenSet:
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError("The token endpoint returned an unexpected response.")
    expires_in = _expires_in(data.get("expires_in"))
    # A refresh response may or may not carry a new refresh token. If it does, the old one
    # may stop working, so always keep the newest.
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous.refresh_token if previous else None
    if not refresh:
        raise AuthError("The token endpoint did not return a refresh token.")
    email = _email_from_id_token(data.get("id_token")) or (previous.email if previous else None)
    return TokenSet(access, refresh, time.time() + expires_in, email)


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>reinvent-planner</title>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5">
<h1>{heading}</h1><p>{body}</p></body>"""


class _CallbackResult:
    def __init__(self) -> None:
        self.params: dict[str, str] | None = None
        self.event = threading.Event()
        self.lock = threading.Lock()


def _make_handler(
    expected_path: str,
    expected_state: str | None,
    result: _CallbackResult,
    done_heading: str,
    done_body: str,
):
    class Handler(BaseHTTPRequestHandler):
        # Each connection has its own thread; this caps how long an idle one lives.
        timeout = 5

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != expected_path or result.event.is_set():
                self.send_error(404)
                return
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            # Only the browser we sent can know `state`. Anything else is ignored, and we keep
            # waiting, so other local processes or web pages can't end or spoof the sign-in.
            if expected_state is not None and not hmac.compare_digest(
                params.get("state", ""), expected_state
            ):
                self.send_error(400, "This response doesn't match the sign-in rip started.")
                return
            with result.lock:
                if result.event.is_set():  # a concurrent request already completed it
                    self.send_error(404)
                    return
                result.params = params
                result.event.set()
            if "error" in params:
                heading, body = "Sign-in not completed", "Return to your terminal for details."
            else:
                heading, body = done_heading, done_body
            page = _DONE_PAGE.format(heading=heading, body=body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):  # the query string holds the auth code; never log it
            pass

    return Handler


class _LoopbackServer(ThreadingHTTPServer):
    """A callback server no other local process can share.

    HTTPServer sets SO_REUSEADDR, which on Windows lets another process bind the same port
    and receive the sign-in redirect. There we ask for exclusive use of the address instead.
    """

    allow_reuse_address = sys.platform != "win32"
    # Each connection gets its own thread, so idle or slow connections can't hold up the
    # browser's redirect. Don't wait for stragglers on shutdown.
    daemon_threads = True
    block_on_close = False

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Lets a quick re-login reuse a port in TIME_WAIT. On POSIX this never allows
            # binding over another process's active listener.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = CALLBACK_HOST, self.server_address[1]


def _bind(ports: tuple[int, ...], handler_factory) -> tuple[HTTPServer, int]:
    last_error: OSError | None = None
    for port in ports:
        try:
            return _LoopbackServer((CALLBACK_HOST, port), handler_factory), port
        except OSError as exc:
            last_error = exc
    raise AuthError(
        f"Could not listen on any reserved sign-in port ({', '.join(map(str, ports))}). "
        "Close other sign-ins and try again."
    ) from last_error


def _wait_for_callback(
    expected_path: str,
    open_url: Callable[[int], str],
    *,
    expected_state: str | None,
    ports: tuple[int, ...],
    done_heading: str,
    done_body: str,
    announce: Callable[[str], None],
    launch_browser: bool,
    timeout: float,
    cancel: threading.Event | None = None,
    what: str = "Sign-in",
) -> dict[str, str]:
    result = _CallbackResult()
    handler = _make_handler(expected_path, expected_state, result, done_heading, done_body)
    server, port = _bind(ports, handler)
    server.timeout = 0.5
    url = open_url(port)
    announce(url)
    if launch_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout
    try:
        while not result.event.is_set():
            if time.monotonic() > deadline:
                raise AuthError("Timed out waiting for the browser. Run the command again.")
            if cancel is not None and cancel.is_set():
                raise AuthError(f"{what} cancelled.")
            server.handle_request()  # returns within server.timeout (0.5 s) if nothing arrives
    finally:
        server.server_close()
    assert result.params is not None  # noqa: S101 - set together with the event
    return result.params


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class Auth:
    """Holds the signed-in attendee's tokens and keeps the access token fresh."""

    def __init__(self, store: TokenStore, http: httpx.Client | None = None):
        self.store = store
        self._http = http or httpx.Client(timeout=30.0, follow_redirects=False)
        self._tokens: TokenSet | None = None
        self._lock = threading.Lock()

    # -- sign in ---------------------------------------------------------------

    def login(
        self,
        *,
        announce: Callable[[str], None] = print,
        launch_browser: bool = True,
        ports: tuple[int, ...] = CALLBACK_PORTS,
        timeout: float = SIGN_IN_TIMEOUT_SECONDS,
        cancel: threading.Event | None = None,
    ) -> TokenSet:
        verifier = new_code_verifier()
        state = secrets.token_urlsafe(32)
        chosen: dict[str, int] = {}

        def open_url(port: int) -> str:
            chosen["port"] = port
            return authorize_url(port, state, verifier)

        params = _wait_for_callback(
            "/callback",
            open_url,
            expected_state=state,
            ports=ports,
            done_heading="Signed in",
            done_body="You can close this tab and return to your terminal.",
            announce=announce,
            launch_browser=launch_browser,
            timeout=timeout,
            cancel=cancel,
        )
        # The handler only accepts a response carrying our `state`; check again anyway.
        if not hmac.compare_digest(params.get("state", ""), state):
            raise AuthError("Sign-in response did not match this request (state mismatch).")
        if "error" in params:
            detail = _printable(params.get("error_description") or params["error"])
            raise AuthError(f"Sign-in was not completed: {detail}")
        code = params.get("code")
        if not code:
            raise AuthError("Sign-in response had no authorization code.")
        tokens = self._exchange_code(code, verifier, chosen["port"])
        self.store.save(tokens)
        self._tokens = tokens
        return tokens

    def _exchange_code(self, code: str, verifier: str, port: int) -> TokenSet:
        response = self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri(port),
                "code": code,
                "code_verifier": verifier,
            },
        )
        if response.status_code != 200:
            raise AuthError(
                f"Could not complete sign-in (HTTP {response.status_code}). Run `rip login` again."
            )
        return _tokens_from_response(_json(response))

    # -- tokens -------------------------------------------------------------------

    def current(self) -> TokenSet | None:
        if self._tokens is None:
            self._tokens = self.store.load()
        return self._tokens

    def is_signed_in(self) -> bool:
        return self.current() is not None

    def access_token(self) -> str:
        with self._lock:
            tokens = self.current()
            if tokens is None:
                raise NotSignedInError("You are not signed in. Run `rip login`.")
            if tokens.expires_soon():
                tokens = self._refresh(tokens)
            return tokens.access_token

    def force_refresh(self) -> str:
        """Called after a 401: refresh once. If this fails, the user must sign in again."""
        with self._lock:
            tokens = self.current()
            if tokens is None:
                raise NotSignedInError("You are not signed in. Run `rip login`.")
            return self._refresh(tokens).access_token

    def _load_settled(self) -> TokenSet | None:
        """Read the store; if it's empty, look once more in case another process is saving."""
        latest = self.store.load()
        if latest is None:
            time.sleep(0.2)
            latest = self.store.load()
        return latest

    def _refresh(self, tokens: TokenSet) -> TokenSet:
        """Get a fresh access token, tolerating other rip processes refreshing at the same time.

        Refresh tokens may rotate, so another process may already have replaced the one we
        hold. Re-read the store first and adopt newer tokens. After a rejection, sign out only
        if the store still holds exactly the token that was rejected: never on an empty read
        (another process may be mid-save) and never after the store moved on.
        """
        latest = self.store.load()
        if latest is not None and latest.access_token != tokens.access_token:
            if not latest.expires_soon():
                self._tokens = latest
                return latest
            tokens = latest
        for _ in range(2):
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": tokens.refresh_token,
                },
            )
            if response.status_code == 200:
                fresh = _tokens_from_response(_json(response), previous=tokens)
                self.store.save(fresh)
                self._tokens = fresh
                return fresh
            if response.status_code not in (400, 401):
                raise AuthError(f"Could not refresh your sign-in (HTTP {response.status_code}).")
            latest = self._load_settled()
            if latest is None:
                self._tokens = None
                raise NotSignedInError("You are not signed in. Run `rip login`.")
            if latest.refresh_token == tokens.refresh_token:
                # The store holds the very token that was rejected: it's dead. Signing in
                # again is the only fix, so drop it rather than retrying in a loop. Look once
                # more right before clearing, in case another process just saved fresh tokens.
                latest = self.store.load()
                if latest is None or latest.refresh_token == tokens.refresh_token:
                    if latest is not None:
                        self.store.clear()
                    self._tokens = None
                    raise NotSignedInError("Your sign-in has expired. Run `rip login` again.")
            # Another process rotated it in the meantime; use its tokens.
            if not latest.expires_soon():
                self._tokens = latest
                return latest
            tokens = latest
        raise AuthError("Your sign-in kept changing while refreshing. Run the command again.")

    # -- sign out -------------------------------------------------------------------

    def logout(self) -> bool:
        """Revoke the refresh token and delete both tokens locally.

        Returns False if revocation could not be confirmed. The local copies are deleted
        either way. An already-issued access token stays valid until it expires (60 minutes).
        """
        try:
            tokens = self.current()
        except AuthError:
            tokens = None  # unreadable storage: nothing to revoke, but still clear it
            revoked = False
        else:
            revoked = True
        if tokens is not None:
            try:
                response = self._http.post(
                    REVOKE_URL, data={"client_id": CLIENT_ID, "token": tokens.refresh_token}
                )
                revoked = response.status_code == 200
            except httpx.HTTPError:
                revoked = False
        self.store.clear()
        self._tokens = None
        return revoked

    def end_builder_id_session(
        self,
        *,
        announce: Callable[[str], None] = print,
        launch_browser: bool = True,
        ports: tuple[int, ...] = CALLBACK_PORTS,
        timeout: float = SIGN_IN_TIMEOUT_SECONDS,
        cancel: threading.Event | None = None,
    ) -> None:
        """Open the browser redirect chain that ends the Builder ID session in that browser.
        `cancel` stops the wait early (the app's "Stop waiting" button)."""
        _wait_for_callback(
            "/logout",
            builder_id_logout_url,
            expected_state=None,
            ports=ports,
            done_heading="Signed out",
            done_body=(
                "Your AWS Builder ID session in this browser has ended. You can close this tab."
            ),
            announce=announce,
            launch_browser=launch_browser,
            timeout=timeout,
            cancel=cancel,
            what="Builder ID sign-out",
        )
