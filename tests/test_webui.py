"""`rip ui`'s secure wrapper around textual-serve. No browser; loopback only; the only
subprocesses are tiny stand-ins for the app (in the shutdown tests)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from textual_serve.download_manager import DownloadManager

from reinvent_planner import webui

EVENT_ID = "testconf2026"

# Every route textual-serve registers, with a sample path for each. If an upgrade adds a route,
# test_every_route_is_covered fails so the new route gets a look.
ROUTE_SAMPLES = {
    "/": "/",
    "/ws": "/ws",
    "/download/{key}": "/download/some-key",
    "/static": "/static/js/textual.js",
}


class FakeAppService:
    """Stands in for textual-serve's AppService so a WebSocket doesn't start a real app."""

    def __init__(self, command, *, argv, write_bytes, write_str, close, download_manager, debug):
        self.command = command
        self.argv = argv
        self.stopped = False
        self.size = None

    async def start(self, width, height):
        self.size = (width, height)

    async def stop(self):
        self.stopped = True

    async def send_bytes(self, data):
        pass

    async def set_terminal_size(self, width, height):
        pass

    async def blur(self):
        pass

    async def focus(self):
        pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(webui.webbrowser, "open", lambda url: opened.append(url) or True)
    return opened


@pytest.fixture(autouse=True)
def fake_app_service(monkeypatch):
    monkeypatch.setattr(webui.SecureServer, "app_service_class", FakeAppService)


@pytest.fixture(autouse=True)
def aiohttp_server_logger_is_left_alone():
    logger = logging.getLogger("aiohttp.server")
    level = logger.level
    yield
    assert logger.level == level
    assert webui._BAD_REQUESTS not in logger.filters  # removed again on cleanup


@pytest.fixture
async def ui():
    """A running SecureServer and a client that hasn't authenticated yet."""
    port = free_port()
    server = webui.SecureServer(webui.build_argv(EVENT_ID), port=port)
    app = await server._make_app()
    async with TestClient(TestServer(app, host="127.0.0.1", port=port)) as client:
        yield server, client


async def sign_in(server, client) -> None:
    response = await client.get(f"/?token={server._token}", allow_redirects=False)
    assert response.status == 302


# --- the token and the cookie ---


async def test_every_route_needs_the_token(ui):
    server, client = ui
    for path in ROUTE_SAMPLES.values():
        response = await client.get(path, headers={"Origin": server.origin})
        assert response.status == 403, path


async def test_every_route_is_covered(ui):
    _, client = ui
    routes = {resource.canonical for resource in client.app.router.resources()}
    assert routes == set(ROUTE_SAMPLES)


async def test_wrong_token_is_refused(ui):
    _, client = ui
    response = await client.get("/?token=not-the-token", allow_redirects=False)
    assert response.status == 403
    assert "Set-Cookie" not in response.headers


async def test_token_is_swapped_for_a_strict_httponly_cookie(ui):
    server, client = ui
    response = await client.get(f"/?token={server._token}", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/"
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith(f"{server.cookie_name}=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert server._token not in cookie


async def test_signed_in_client_can_load_every_route(ui):
    server, client = ui
    await sign_in(server, client)
    assert (await client.get("/")).status == 200
    assert (await client.get("/static/js/textual.js")).status == 200
    # Reaches textual-serve's handler (so it's past the guard), which has no such download.
    assert (await client.get("/download/some-key")).status == 404
    ws = await client.ws_connect("/ws", headers={"Origin": server.origin})
    await ws.close()


class ChunkSource:
    """Stands in for the app process of a download: answers each chunk request, a bit later."""

    app_service_id = "download-test"

    def __init__(self, manager: DownloadManager, chunks: list[bytes]):
        self.manager = manager
        self.chunks = list(chunks)
        self.requests = 0
        self.answers: set[asyncio.Future] = set()

    async def send_meta(self, data):
        assert data["type"] == "deliver_chunk_request"
        self.requests += 1

        async def answer():
            await asyncio.sleep(0.01)
            await self.manager.chunk_received(
                data["key"], self.chunks.pop(0) if self.chunks else b""
            )

        task = asyncio.ensure_future(answer())
        self.answers.add(task)
        task.add_done_callback(self.answers.discard)
        return True


async def offer_download(server, key, file_name, chunks, **meta) -> ChunkSource:
    source = ChunkSource(server.download_manager, chunks)
    await server.download_manager.create_download(
        app_service=source,
        delivery_key=key,
        file_name=file_name,
        open_method=meta.get("open_method", "download"),
        mime_type=meta.get("mime_type", "text/csv"),
        encoding=meta.get("encoding", "utf-8"),
        name="rip-export:x",
    )
    return source


async def test_download_file_name_cannot_break_the_header(ui):
    server, client = ui
    await sign_in(server, client)
    name = 'plan "x"\r\nSet-Cookie: a=b; é.csv'
    await offer_download(server, "k1", name, [b"a,b\r\n"])
    response = await client.get("/download/k1")
    assert response.status == 200 and await response.read() == b"a,b\r\n"
    assert response.headers["Content-Disposition"] == (
        'attachment; filename="plan__x___Set-Cookie__a_b___.csv"; '
        "filename*=UTF-8''plan%20%22x%22__Set-Cookie%3A%20a%3Db%3B%20%C3%A9.csv"
    )
    assert "Set-Cookie" not in response.headers
    assert response.headers["Content-Type"] == "text/csv; charset=utf-8"


def test_content_disposition_keeps_a_plain_name_and_never_goes_empty():
    assert webui.content_disposition("inline", "testconf2026-plan.ics") == (
        "inline; filename=\"testconf2026-plan.ics\"; filename*=UTF-8''testconf2026-plan.ics"
    )
    assert webui.content_disposition("attachment", "") == (
        "attachment; filename=\"download\"; filename*=UTF-8''download"
    )


async def test_a_download_is_served_once_even_to_two_fetches_at_once(ui):
    server, client = ui
    await sign_in(server, client)
    chunks = [b"x" * 1000, b"y" * 1000, b"z" * 1000]
    await offer_download(server, "k2", "plan.csv", chunks)
    first, second = await asyncio.gather(client.get("/download/k2"), client.get("/download/k2"))
    statuses = sorted([first.status, second.status])
    assert statuses == [200, 409]
    winner, loser = (first, second) if first.status == 200 else (second, first)
    assert await winner.read() == b"".join(chunks)  # the whole file, not a share of it
    assert loser.headers["Content-Type"].startswith("text/plain")
    assert "Traceback" not in await loser.text()
    # And any later fetch too.
    assert (await client.get("/download/k2")).status == 409


async def test_head_on_a_download_does_not_use_it_up(ui):
    server, client = ui
    await sign_in(server, client)
    source = await offer_download(server, "k3", "plan.csv", [b"data"])
    head = await client.head("/download/k3")
    assert head.status == 200 and "filename=" in head.headers["Content-Disposition"]
    assert source.requests == 0
    response = await client.get("/download/k3")
    assert response.status == 200 and await response.read() == b"data"


async def test_cookie_from_another_run_is_refused(ui):
    server, client = ui
    other = webui.SecureServer(["true"], port=1234)
    client.session.cookie_jar.update_cookies({server.cookie_name: other._session})
    assert (await client.get("/")).status == 403


async def test_token_alone_does_not_work_as_a_cookie(ui):
    server, client = ui
    client.session.cookie_jar.update_cookies({server.cookie_name: server._token})
    assert (await client.get("/")).status == 403


# --- Host and Origin ---


@pytest.mark.parametrize(
    "host",
    ["localhost:{port}", "evil.example", "127.0.0.1", "127.0.0.1:{other}", "[::1]:{port}"],
)
async def test_wrong_host_is_refused_even_when_signed_in(ui, host):
    server, client = ui
    await sign_in(server, client)
    port = client.port
    response = await client.get("/", headers={"Host": host.format(port=port, other=port + 1)})
    assert response.status == 403


async def test_wrong_host_cannot_exchange_the_token(ui):
    server, client = ui
    response = await client.get(
        f"/?token={server._token}", headers={"Host": "rebind.example"}, allow_redirects=False
    )
    assert response.status == 403
    assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize(
    "origin", [None, "http://evil.example", "http://localhost:{port}", "null", "https://{host}"]
)
async def test_cross_origin_websocket_is_refused(ui, origin):
    server, client = ui
    await sign_in(server, client)
    headers = {}
    if origin is not None:
        headers["Origin"] = origin.format(port=client.port, host=server.authority)
    with pytest.raises(aiohttp.WSServerHandshakeError) as error:
        await client.ws_connect("/ws", headers=headers)
    assert error.value.status == 403


async def test_only_get_and_head_are_allowed(ui):
    server, client = ui
    await sign_in(server, client)
    assert (await client.post("/")).status == 405
    assert (await client.head("/")).status == 200


async def test_websocket_sessions_are_capped(ui, monkeypatch):
    server, client = ui
    monkeypatch.setattr(webui, "MAX_SESSIONS", 2)
    await sign_in(server, client)
    headers = {"Origin": server.origin}
    first = await client.ws_connect("/ws", headers=headers)
    second = await client.ws_connect("/ws", headers=headers)
    with pytest.raises(aiohttp.WSServerHandshakeError) as error:
        await client.ws_connect("/ws", headers=headers)
    assert error.value.status == 503
    await first.close()
    await second.close()


@pytest.mark.parametrize(
    ("query", "size"),
    [
        ("width=100000&height=-5", (500, 10)),
        ("width=1&height=99999999999999999999", (20, 200)),
        ("width=wide&height=", (80, 24)),
        ("", (80, 24)),
        ("width=120&height=40", (120, 40)),
    ],
)
async def test_terminal_size_from_the_browser_is_clamped(ui, query, size):
    server, client = ui
    await sign_in(server, client)
    ws = await client.ws_connect(f"/ws?{query}", headers={"Origin": server.origin})
    await wait_for(lambda: len(server._services) == 1)
    (service,) = server._services
    assert service.size == size
    await ws.close()


async def test_resize_from_the_browser_is_clamped(monkeypatch):
    sent = []

    async def send_meta(self, data):
        sent.append(data)
        return True

    monkeypatch.setattr(webui.TrackedAppService, "send_meta", send_meta)
    service = webui.TrackedAppService(
        "label only",
        argv=["true"],
        write_bytes=_ignore,
        write_str=_ignore,
        close=_ignore,
        download_manager=DownloadManager(),
        debug=False,
    )
    await service.set_terminal_size(10**9, "40")
    await service.set_terminal_size(None, float("inf"))
    assert sent == [
        {"type": "resize", "width": 500, "height": 40},
        {"type": "resize", "width": 80, "height": 24},
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30", 30), ("-3", 20), ("501", 500), ("1e9", 7), ("", 7), (None, 7), ([1], 7), (12.9, 20)],
)
def test_to_int_clamps_or_falls_back_to_the_default(value, expected):
    assert webui._to_int(value, 7, (20, 500)) == expected


class FakeWebSocket:
    def __init__(self, **kwargs):
        self.closed = False

    async def prepare(self, request):
        pass

    async def close(self):
        self.closed = True

    async def send_bytes(self, data):
        pass

    async def send_str(self, data):
        pass


async def test_cancelled_session_stops_its_app_and_stays_cancelled(monkeypatch):
    started = asyncio.Event()

    class HangingAppService(FakeAppService):
        async def start(self, width, height):
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(webui.web, "WebSocketResponse", FakeWebSocket)
    server = webui.SecureServer(webui.build_argv(EVENT_ID), port=1234)
    server.app_service_class = HangingAppService
    task = asyncio.create_task(server.handle_websocket(make_mocked_request("GET", "/ws")))
    await started.wait()
    (service,) = server._services
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.stopped
    assert not server._services
    assert server._active_sessions == 0


# --- response headers and page contents ---


async def test_security_headers_on_every_response(ui):
    server, client = ui
    responses = [await client.get("/")]  # refused
    await sign_in(server, client)
    responses += [await client.get("/"), await client.get("/static/css/xterm.css")]
    for response in responses:
        headers = response.headers
        csp = headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "unsafe-eval" not in csp
        assert "script-src 'self'" in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert not [name for name in headers if name.lower().startswith("access-control-")]


async def test_page_loads_nothing_from_other_origins(ui):
    server, client = ui
    await sign_in(server, client)
    html = await (await client.get("/")).text()
    assert "googleapis" not in html
    urls = re.findall(r"""(?:\s(?:src|href)=|url\()\s*["']([^"']+)""", html)
    assert urls
    for url in urls:
        assert url.startswith(f"{server.origin}/static/"), url
    assert f'data-session-websocket-url="ws://{server.authority}/ws"' in html


async def test_inline_script_is_allowed_only_by_its_nonce(ui):
    server, client = ui
    await sign_in(server, client)
    response = await client.get("/")
    html = await response.text()
    nonce = re.search(r"'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
    inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", html)
    assert inline_scripts == [f' nonce="{nonce}"']
    assert not re.search(r"\son[a-z]+=", html, re.IGNORECASE)
    # A fresh nonce for every page load.
    again = await client.get("/")
    assert nonce not in again.headers["Content-Security-Policy"]


async def test_page_asks_before_a_reload_or_close_stops_the_app(ui):
    server, client = ui
    await sign_in(server, client)
    response = await client.get("/")
    html = await response.text()
    nonce = re.search(r"'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
    script = re.search(rf'<script nonce="{re.escape(nonce)}">(.*?)</script>', html, re.S)
    code = script.group(1)
    assert 'window.addEventListener("beforeunload", confirmLeave);' in code
    assert "event.preventDefault();" in code
    assert 'event.returnValue = "";' in code
    # Restart is a deliberate navigation, so it doesn't ask.
    assert 'window.removeEventListener("beforeunload", confirmLeave);' in code


async def test_url_is_printed_once_and_browser_opened(capsys, no_real_browser):
    port = free_port()
    server = webui.SecureServer(["true"], port=port, open_browser=True)
    app = await server._make_app()
    async with TestClient(TestServer(app, host="127.0.0.1", port=port)) as client:
        await sign_in(server, client)
        await client.get("/")
    out = capsys.readouterr()
    assert out.out.count(server._token) == 1
    assert server.url in out.out
    assert server._token not in out.err
    assert no_real_browser == [server.url]


# --- binding and serve() ---


def test_bind_socket_uses_loopback_ipv4_and_a_random_port():
    sock = webui.bind_socket()
    try:
        assert sock.family == socket.AF_INET
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port != 0
    finally:
        sock.close()


def test_bind_socket_asks_windows_for_exclusive_use_of_the_port(monkeypatch):
    options = []

    class RecordingSocket(socket.socket):
        def setsockopt(self, *args):
            options.append(args)  # never reaches the OS: off Windows the option is made up

    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)
    monkeypatch.setattr(socket, "socket", RecordingSocket)
    for platform, expected in [("linux", []), ("win32", [(socket.SOL_SOCKET, exclusive, 1)])]:
        options.clear()
        monkeypatch.setattr(webui.sys, "platform", platform)
        webui.bind_socket().close()
        assert options == expected, platform


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_bind_socket_rejects_bad_ports(port):
    with pytest.raises(ValueError):
        webui.bind_socket(port)


def test_serve_binds_loopback_and_disables_the_access_log(monkeypatch):
    calls = {}

    def fake_run_app(app, **kwargs):
        app.close()  # the unawaited _make_app() coroutine
        calls.update(kwargs)

    monkeypatch.setattr(webui.web, "run_app", fake_run_app)
    port = free_port()
    webui.serve(EVENT_ID, open_browser=False, port=port)
    sock = calls["sock"]
    try:
        assert sock.getsockname() == ("127.0.0.1", port)
        assert calls["access_log"] is None
        assert "host" not in calls and "port" not in calls
    finally:
        sock.close()


def test_serve_warns_that_linux_openers_expose_the_url():
    assert "/proc" in webui.serve.__doc__
    assert "--no-browser" in webui.serve.__doc__


def test_serve_rejects_bad_event_id_before_binding(monkeypatch):
    monkeypatch.setattr(webui, "bind_socket", lambda port: pytest.fail("bound a socket"))
    with pytest.raises(ValueError):
        webui.serve("x; rm -rf ~", open_browser=False)


# --- event IDs and the command line ---


@pytest.mark.parametrize("event_id", ["reinvent2026", "a.b-c_D9", "x" * 128])
def test_valid_event_ids(event_id):
    assert webui.validate_event_id(event_id) == event_id


@pytest.mark.parametrize(
    "event_id",
    ["", "a b", "x;id", "$(id)", "`id`", "a\n", "a/b", "../x", "é", "x" * 129, None],
)
def test_bad_event_ids_are_rejected(event_id):
    with pytest.raises(ValueError):
        webui.validate_event_id(event_id)
    with pytest.raises(ValueError):
        webui.build_argv(event_id)


def test_argv_runs_the_tui_module_with_the_event_id():
    argv = webui.build_argv(EVENT_ID)
    assert argv == [sys.executable, "-m", "reinvent_planner.tui", EVENT_ID]


def test_argv_keeps_an_awkward_interpreter_path_as_one_argument(monkeypatch):
    python = r"C:\Users\First Last\it's $HOME\python.exe"
    monkeypatch.setattr(webui.sys, "executable", python)
    assert webui.build_argv(EVENT_ID)[0] == python


async def test_app_process_runs_argv_directly_never_through_a_shell(tmp_path, monkeypatch):
    """A real child, from a path with spaces, through the real TrackedAppService (any OS)."""

    async def no_shell(*args, **kwargs):
        raise AssertionError("used a shell")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_shell)
    monkeypatch.setattr(webui, "STOP_TIMEOUT", 0.3)
    folder = tmp_path / "dir with spaces & 'quotes'"
    folder.mkdir()
    script = folder / "child.py"
    script.write_text(
        "import pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n"
        "time.sleep(60)\n"
    )
    ready = folder / "ready"
    odd_argument = "$HOME; echo pwned & 'x' \"y\""
    service = webui.TrackedAppService(
        "label only",
        argv=[sys.executable, str(script), str(ready), odd_argument],
        write_bytes=_ignore,
        write_str=_ignore,
        close=_ignore,
        download_manager=DownloadManager(),
        debug=False,
    )
    await service.start(80, 24)
    await wait_for(ready.exists)
    assert ready.read_text() == odd_argument
    await webui.stop_app_service(service)
    assert service.process.returncode is not None


def test_app_runs_in_its_own_process_group_on_every_os(monkeypatch):
    monkeypatch.setattr(webui.sys, "platform", "linux")
    assert webui._own_process_group() == {"start_new_session": True}
    monkeypatch.setattr(webui.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    assert webui._own_process_group() == {"creationflags": 0x200}


async def _ignore(*args, **kwargs):
    pass


# --- the token works once ---


async def test_used_link_redirects_the_browser_that_used_it(ui):
    server, client = ui
    await sign_in(server, client)
    response = await client.get(f"/?token={server._token}", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/"
    assert "Set-Cookie" not in response.headers


async def test_used_link_is_refused_everywhere_else(ui):
    server, client = ui
    await sign_in(server, client)
    client.session.cookie_jar.clear()
    response = await client.get(f"/?token={server._token}", allow_redirects=False)
    assert response.status == 403
    body = await response.text()
    assert body == webui.LINK_USED
    assert server._token not in body
    assert "Set-Cookie" not in response.headers


async def test_wrong_token_does_not_use_up_the_link(ui):
    server, client = ui
    assert (await client.get("/?token=guess", allow_redirects=False)).status == 403
    await sign_in(server, client)


# --- /static/ serves files, not directory listings ---


@pytest.mark.parametrize("path", ["/static/", "/static/js", "/static/js/", "/static/fonts/"])
async def test_static_directories_are_not_listed(ui, path):
    server, client = ui
    await sign_in(server, client)
    response = await client.get(path)
    assert response.status == 404
    assert "textual.js" not in await response.text()


# --- raw HTTP: malformed requests, duplicate Host, absolute-form URIs ---


async def raw_request(port: int, data: bytes) -> bytes:
    """Send ``data`` as-is and return the response's status line and headers."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    finally:
        writer.close()


async def test_duplicate_host_is_refused_without_a_cookie(ui):
    server, client = ui
    port = client.port
    head = await raw_request(
        port,
        f"GET /?token={server._token} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\nHost: evil.example\r\n\r\n".encode(),
    )
    assert re.match(rb"HTTP/1\.[01] 400 ", head), head
    assert b"set-cookie" not in head.lower()
    assert not server._token_used


@pytest.mark.parametrize("path", ["/", "/static/js/textual.js", "/ws"])
async def test_absolute_form_uri_still_needs_the_secret(ui, path):
    server, client = ui
    port = client.port
    for authority in (f"127.0.0.1:{port}", "evil.example"):
        head = await raw_request(
            port,
            f"GET http://{authority}{path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Origin: {server.origin}\r\n\r\n".encode(),
        )
        assert re.match(rb"HTTP/1\.[01] 403 ", head), (authority, path, head)


async def test_malformed_requests_log_nothing(ui, caplog):
    _, client = ui
    port = client.port
    caplog.set_level(logging.DEBUG)
    for data in [
        b"NOT HTTP AT ALL\r\n\r\n",
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: x\r\n\r\n".encode(),
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: nope\r\n\r\n".encode(),
        b"GET /\x00 HTTP/1.1\r\n\r\n",
    ]:
        # aiohttp answers 400 or just hangs up; either is fine, as long as nothing is logged.
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await raw_request(port, data)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# --- shutdown stops every app subprocess ---


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_shutdown_awaits_stop_on_every_session(ui):
    server, client = ui
    await sign_in(server, client)
    ws = await client.ws_connect("/ws", headers={"Origin": server.origin})
    await wait_for(lambda: len(server._services) == 1)
    (service,) = server._services
    await client.app.shutdown()
    assert service.stopped
    await ws.close()


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on Windows")
async def test_closing_the_terminal_shuts_down_as_ctrl_c_does(monkeypatch):
    """The apps have their own sessions, so a hang-up must stop them via the server."""
    loop = asyncio.get_running_loop()
    handlers = {}
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: bool(handlers.pop(sig, 0)))
    port = free_port()
    server = webui.SecureServer(["true"], port=port)
    app = await server._make_app()
    async with TestClient(TestServer(app, host="127.0.0.1", port=port)):
        with pytest.raises(web.GracefulExit):
            handlers[signal.SIGHUP]()
    assert signal.SIGHUP not in handlers


@pytest.mark.parametrize("ignores_sigterm", [False, True])
async def test_shutdown_leaves_no_app_process_behind(tmp_path, monkeypatch, ignores_sigterm):
    """A real (tiny) app process that never quits by itself, or even ignores SIGTERM."""
    monkeypatch.setattr(webui.SecureServer, "app_service_class", webui.TrackedAppService)
    monkeypatch.setattr(webui, "STOP_TIMEOUT", 0.3)
    ready = tmp_path / "ready"
    child = (
        "import pathlib, signal, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignores_sigterm else "")
        + "pathlib.Path(sys.argv[1]).touch()\n"
        "time.sleep(60)\n"
    )
    port = free_port()
    server = webui.SecureServer([sys.executable, "-c", child, str(ready)], port=port)
    app = await server._make_app()
    async with TestClient(TestServer(app, host="127.0.0.1", port=port)) as client:
        await sign_in(server, client)
        ws = await client.ws_connect("/ws", headers={"Origin": server.origin})
        await wait_for(ready.exists)
        (service,) = server._services
        process = service.process
        if sys.platform != "win32":
            # Its own process group, so a terminal's Ctrl+C reaches only the server.
            assert os.getpgid(process.pid) == process.pid != os.getpgrp()
        await client.app.shutdown()
        assert process.returncode is not None
        if sys.platform != "win32":  # os.kill(pid, 0) would terminate it on Windows
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
        await ws.close()


# --- the Finder launcher ---

LAUNCHER = Path(__file__).parent.parent / "scripts" / "reinvent-planner-ui.command"


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="a macOS/bash launcher")


@posix_only
def test_launcher_runs_reinvent_planner_never_a_bare_rip():
    text = LAUNCHER.read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert 'exec reinvent-planner ui "$@"' in code
    assert not [line for line in code if re.search(r"(^|[\s;&|(`])rip(\s|$)", line)]
    assert os.access(LAUNCHER, os.X_OK)
    assert "/Users/" not in text


@posix_only
def test_launcher_execs_reinvent_planner_from_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in [
        ("reinvent-planner", 'echo "reinvent-planner $*"'),
        ("rip", "echo WRONG-RIP; exit 99"),
    ]:
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 (fixed argv: bash and our own script)
        ["/bin/bash", str(LAUNCHER), "--no-browser"],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "reinvent-planner ui --no-browser\n"


# --- a HEAD request doesn't use up the link ---


async def test_head_on_the_link_is_refused_and_does_not_use_it(ui):
    server, client = ui
    response = await client.head(f"/?token={server._token}", allow_redirects=False)
    assert response.status == 405
    assert "Set-Cookie" not in response.headers
    assert not server._token_used
    await sign_in(server, client)


# --- logging: drop malformed-request noise, keep real bugs ---


async def test_a_bug_in_a_handler_is_still_logged(caplog):
    async def boom(request):
        raise RuntimeError("a real bug")

    port = free_port()
    server = webui.SecureServer(["true"], port=port)
    app = await server._make_app()
    app.router.add_get("/boom", boom)
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(app, host="127.0.0.1", port=port)) as client:
        await sign_in(server, client)
        assert (await client.get("/boom")).status == 500
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors
    assert isinstance(errors[0].exc_info[1], RuntimeError)


def test_bad_request_filter_is_attached_once():
    logger = logging.getLogger("aiohttp.server")
    webui._BAD_REQUESTS.attach()
    webui._BAD_REQUESTS.attach()
    try:
        assert logger.filters.count(webui._BAD_REQUESTS) == 1
        webui._BAD_REQUESTS.detach()
        assert webui._BAD_REQUESTS in logger.filters  # another server still uses it
    finally:
        webui._BAD_REQUESTS.detach()
    assert webui._BAD_REQUESTS not in logger.filters


async def test_stopping_waits_for_a_reservation_run_to_finish(monkeypatch):
    """Round 2 of the review: Ctrl+C in `rip ui` killed a GO run within seconds."""
    import asyncio

    from reinvent_planner import launch

    busy = {"left": 3}

    def in_progress(event_id):
        busy["left"] -= 1
        return busy["left"] > 0

    stopped = []

    class Service:
        process = None

        async def stop(self):
            stopped.append(busy["left"])

    monkeypatch.setattr(launch, "run_in_progress", in_progress)
    monkeypatch.setattr(asyncio, "sleep", lambda s: _no_sleep())
    await webui.stop_app_service(Service(), "reinvent2026")
    assert stopped == [0]  # stopped only once the run no longer held the lock


async def _no_sleep():
    return None


def test_run_in_progress_sees_the_reservation_lock(tmp_path, monkeypatch):
    from reinvent_planner import launch

    monkeypatch.setenv("RIP_DATA_DIR", str(tmp_path))
    assert not launch.run_in_progress("ev1")
    with launch.reservation_lock("ev1"):  # a run holds it (a second handle conflicts too)
        assert launch.run_in_progress("ev1")
    assert not launch.run_in_progress("ev1")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_leftover_app_processes_are_killed_even_if_they_ignore_sigterm():
    """Verification round 2: a second Ctrl+C left the apps running for good."""
    import subprocess
    import time

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    time.sleep(0.3)  # let it install the handler
    webui._LIVE_CHILDREN[child.pid] = child  # (a Popen stands in for asyncio's Process)
    webui.kill_leftover_children()
    assert child.wait(5) is not None
    assert not webui._LIVE_CHILDREN


def test_content_disposition_drops_control_characters_and_separators():
    header = webui.content_disposition("attachment", "a\r\nb/c\\d.csv")
    assert "%0D" not in header and "%0A" not in header and "%2F" not in header
    assert "%5C" not in header
