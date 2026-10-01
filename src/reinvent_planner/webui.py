"""`rip ui`: serve the Textual app in a local browser tab, locked to this machine and this run.

textual-serve has no authentication, and the app acts on the user's real re:Invent schedule, so
this wraps its aiohttp application with a guard:

- the socket binds 127.0.0.1 only, on a random free port by default;
- every request needs this run's secret: the browser opens ``/?token=...``, which is swapped
  (once only) for an HttpOnly, SameSite=Strict cookie and a redirect to ``/``;
- the Host header must be exactly ``127.0.0.1:<port>`` (DNS rebinding), and a WebSocket's Origin
  exactly ``http://127.0.0.1:<port>`` (cross-site WebSockets);
- responses carry a strict CSP and no CORS headers, and the page loads nothing from a CDN;
- at most ``MAX_SESSIONS`` WebSockets (each one is an app subprocess) run at once; each app runs
  in its own process group, so a Ctrl+C reaches only the server, which on shutdown stops every
  app (asks it to quit, then terminates it, then kills it);
- the page asks before a reload or close, which would stop its app, perhaps mid-reservation;
- /static/ serves files only, never directory listings;
- each file download can be fetched once, with a Content-Disposition no file name can break.

The token is printed once, inside the URL, and never logged (the access log is off).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hmac
import logging
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from asyncio.subprocess import Process
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import hdrs, web
from aiohttp.http_exceptions import HttpProcessingError
from textual_serve.app_service import AppService
from textual_serve.server import Server

from . import EVENT_ID_PATTERN as _EVENT_ID_PATTERN
from . import launch

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
EVENT_ID_PATTERN = _EVENT_ID_PATTERN  # shared with the CLI
MAX_SESSIONS = 3
TITLE = "re:Invent Planner"
TOKEN_PARAM = "token"  # noqa: S105 (a query parameter name, not a secret)
# Cookies aren't scoped by port, so name the cookie after the port: two `rip ui` runs side by
# side then don't overwrite each other's sessions.
COOKIE_PREFIX = "rip_ui_"
SHUTDOWN_TIMEOUT = 3.0
# How long an app gets to quit, and then to die after SIGTERM, before it's killed.
STOP_TIMEOUT = 2.0
RUN_GRACE_SECONDS = 180.0  # longest wait for a reservation run to finish before stopping
# The terminal size a browser may ask for, in cells; anything outside is clamped to these.
WIDTH_RANGE = (20, 500)
HEIGHT_RANGE = (10, 200)
FONT_SIZE_RANGE = (6, 72)
LINK_USED = "This link was already used; restart `rip ui` for a new one."

_NONCE_KEY = web.RequestKey("csp_nonce", str)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# textual-serve's app_index.html, minus the Google Fonts stylesheet (the bundled Roboto Mono is
# used instead), the logo, and inline event handlers (the CSP allows only this nonce'd script).
INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="referrer" content="no-referrer" />
    <title>{{ application.name }}</title>
    <link rel="stylesheet" href="{{ config.static.url }}css/xterm.css" />
    <script src="{{ config.static.url }}js/textual.js"></script>
    <style>
      @font-face {
        font-family: "Roboto Mono";
        src: url("{{ config.static.url }}fonts/RobotoMono-VariableFont_wght.ttf")
          format("truetype");
        font-weight: 100 700;
        font-style: normal;
      }
      @font-face {
        font-family: "Roboto Mono";
        src: url("{{ config.static.url }}fonts/RobotoMono-Italic-VariableFont_wght.ttf")
          format("truetype");
        font-weight: 100 700;
        font-style: italic;
      }
      body {
        background: #0c181f;
      }
      .dialog-container {
        position: absolute;
        width: 100vw;
        height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 10;
      }
      .shade {
        position: absolute;
        width: 100%;
        height: 100%;
        background: #0c181f;
        background-image: url("{{ config.static.url }}images/background.png");
      }
      .intro {
        width: 640px;
        height: 240px;
        font-size: 16px;
        z-index: 20;
        font-family: "Roboto Mono", menlo, monospace;
        text-align: center;
        color: rgba(255, 255, 255, 0.95);
        background-color: #12232d;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 32px;
      }
      body.-first-byte .intro-dialog,
      body.-first-byte .intro-dialog .shade {
        opacity: 0;
        transition: opacity 0.3s ease-out;
        display: none;
      }
      body .textual-terminal {
        opacity: 0;
        transition: opacity 0.3s ease-out;
      }
      body.-first-byte .textual-terminal {
        opacity: 1;
        transition: opacity 0.3s ease-out;
      }
      body button {
        padding: 16px 32px;
        background-color: #5e0ba7;
        color: rgba(255, 255, 255, 0.95);
        border: none;
        font-family: "Roboto Mono", menlo, monospace;
        margin: 16px;
        display: block;
      }
      button:hover {
        background: #ac5af4;
        cursor: pointer;
      }
      .closed-dialog {
        opacity: 0;
        display: none;
      }
      body.-closed .closed-dialog {
        opacity: 1;
        display: flex;
      }
      #start {
        display: none;
      }
      #start.-delay {
        display: flex;
      }
    </style>
    <script nonce="{{ nonce }}">
      // Leaving the page stops its app, which may be reserving seats: ask first, while it runs.
      function confirmLeave(event) {
        if (document.body.classList.contains("-closed")) {
          return;
        }
        event.preventDefault();
        event.returnValue = "";
      }
      window.addEventListener("beforeunload", confirmLeave);
      function restart() {
        window.removeEventListener("beforeunload", confirmLeave);
        const params = new URLSearchParams(window.location.search);
        params.delete("delay");
        window.location.href = window.location.pathname + "?" + params.toString();
      }
      document.addEventListener("DOMContentLoaded", () => {
        for (const button of document.querySelectorAll("button[data-restart]")) {
          button.addEventListener("click", restart);
        }
      });
    </script>
  </head>
  <body>
    <div class="dialog-container intro-dialog">
      <div class="shade"></div>
      <div class="intro">
        <div>{{ application.name }}</div>
        <button type="button" id="start" data-restart>Start</button>
      </div>
    </div>
    <div class="dialog-container closed-dialog">
      <div class="shade"></div>
      <div class="intro">
        <div class="message">Session ended.</div>
        <button type="button" data-restart>Restart</button>
      </div>
    </div>
    <div
      id="terminal"
      class="textual-terminal"
      data-session-websocket-url="{{ app_websocket_url }}"
      data-font-size="{{ font_size }}"
    ></div>
  </body>
</html>
"""


def validate_event_id(event_id: str) -> str:
    """Return ``event_id`` if it's a plausible event ID (and so a harmless argument), else raise."""
    if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
        raise ValueError(
            f"Invalid event ID {event_id!r}: use 1-128 letters, digits, '.', '_' or '-'."
        )
    return event_id


def build_argv(event_id: str) -> list[str]:
    """The app process each browser tab gets. It's run directly, never through a shell."""
    validate_event_id(event_id)
    return [sys.executable, "-m", "reinvent_planner.tui", event_id]


def bind_socket(port: int | None = None) -> socket.socket:
    """A listening TCP socket on 127.0.0.1 (a random free port unless ``port`` is given).

    It's bound and listening before the server starts, so there's no window in which another
    process could take the port, and a browser that connects early just waits in the backlog.
    """
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"Invalid port {port}: use 1-65535.")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Otherwise another process could bind the same port there (as in auth.py).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((HOST, port or 0))
        sock.listen(128)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class _DropBadRequests(logging.Filter):
    """Drops aiohttp's tracebacks for malformed requests, which any local process can send to
    flood the terminal, but keeps everything else, such as real bugs in our handlers."""

    def __init__(self) -> None:
        super().__init__()
        self._servers = 0

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        return not isinstance(error, HttpProcessingError)

    def attach(self) -> None:
        self._servers += 1
        logging.getLogger("aiohttp.server").addFilter(self)  # a no-op if already there

    def detach(self) -> None:
        self._servers -= 1
        if self._servers <= 0:
            self._servers = 0
            logging.getLogger("aiohttp.server").removeFilter(self)


_BAD_REQUESTS = _DropBadRequests()


class TrackedAppService(AppService):
    """textual-serve's AppService, but running ``argv`` directly and remembering the process.

    Upstream runs a command string through create_subprocess_shell: on Windows that's cmd.exe,
    which mangles POSIX quoting and leaves the tracked process a cmd.exe parent, so stopping it
    would orphan the app. Running argv with create_subprocess_exec avoids both everywhere, and
    means the process we stop on shutdown is the app itself.
    """

    process: Process | None = None

    def __init__(self, command: str, *, argv: Sequence[str], **kwargs) -> None:
        super().__init__(command, **kwargs)
        self.argv = list(argv)

    async def _open_app_process(self, width: int = 80, height: int = 24) -> Process:
        # As upstream 1.1.3's _open_app_process, but exec instead of shell.
        environment = self._build_environment(width=width, height=height)
        self._process = process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **_own_process_group(),
        )
        self._stdin = process.stdin  # never None: stdin=PIPE
        self.process = process
        _LIVE_CHILDREN[process.pid] = process
        return process

    async def set_terminal_size(self, width: int, height: int) -> None:
        # The browser's resize message is untrusted JSON: pass the app only sane numbers.
        await super().set_terminal_size(*_terminal_size(width, height))


def _own_process_group() -> dict[str, object]:
    """create_subprocess_exec options that keep a terminal's Ctrl+C from reaching the app.

    The server handles Ctrl+C by stopping each app in turn (see stop_app_service), rather than
    every app being interrupted at once, wherever it happens to be.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# Every app process started and not yet known to have exited, so none can outlive the server
# (see kill_leftover_children): the orderly stop can be cut short by a second Ctrl+C.
_LIVE_CHILDREN: dict[int, Process] = {}
_WAIT_NOTICE_SHOWN: list[bool] = []


def kill_leftover_children() -> None:
    """Synchronously end any app process still running: SIGTERM (TerminateProcess on Windows),
    then SIGKILL whatever is still alive a second later. Safe to call more than once. Only
    processes not yet known to have exited are signalled, so a reused pid is never hit."""
    live = [p for p in _LIVE_CHILDREN.values() if p.returncode is None]
    _LIVE_CHILDREN.clear()
    for process in live:
        with contextlib.suppress(OSError):
            os.kill(process.pid, signal.SIGTERM)
    if sys.platform == "win32" or not live:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(_alive(p.pid) for p in live):
        time.sleep(0.05)
    for process in live:
        if _alive(process.pid):
            with contextlib.suppress(OSError):
                os.kill(process.pid, signal.SIGKILL)


def _alive(pid: int) -> bool:
    """Whether our child `pid` is still running (reaping it if it has just exited)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:  # already reaped by asyncio's watcher: it has exited
        return False
    return done == 0


atexit.register(kill_leftover_children)


async def stop_app_service(service: AppService, event_id: str | None = None) -> None:
    """Ask the app to quit; if it hasn't within STOP_TIMEOUT, terminate it, then kill it.

    textual-serve's own ``stop()`` waits forever for the app to exit, so a hung app would
    otherwise outlive Ctrl+C. But a reservation run in progress (it holds the reservation lock)
    is first given up to RUN_GRACE_SECONDS to finish: stopping it mid-run would leave its
    backups untried. A second Ctrl+C stops the server at once.
    """
    if event_id is not None and await asyncio.to_thread(launch.run_in_progress, event_id):
        if not _WAIT_NOTICE_SHOWN:  # once, however many sessions are open
            _WAIT_NOTICE_SHOWN.append(True)
            print(
                "A reservation run is in progress; waiting for it to finish "
                "(press Ctrl+C again to stop anyway)…",
                flush=True,
            )
        deadline = asyncio.get_running_loop().time() + RUN_GRACE_SECONDS
        while asyncio.get_running_loop().time() < deadline and await asyncio.to_thread(
            launch.run_in_progress, event_id
        ):
            await asyncio.sleep(0.5)
    # A timeout or a broken pipe: either way, fall through and make sure the process is gone.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(service.stop(), STOP_TIMEOUT)
    process = getattr(service, "process", None)
    if process is None or process.returncode is not None:
        if process is not None:
            _LIVE_CHILDREN.pop(process.pid, None)
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
    _LIVE_CHILDREN.pop(process.pid, None)


class SecureServer(Server):
    """textual-serve's Server, with per-run auth, Host/Origin checks and security headers."""

    app_service_class: type[AppService] = TrackedAppService

    def __init__(self, argv: Sequence[str], *, port: int, open_browser: bool = False) -> None:
        # Upstream's command string is only a label now; TrackedAppService runs argv itself.
        super().__init__(shlex.join(argv), host=HOST, port=port, title=TITLE)
        self.argv = list(argv)
        self.open_browser = open_browser
        self.origin = f"http://{HOST}:{port}"
        self.authority = f"{HOST}:{port}"
        self.cookie_name = f"{COOKIE_PREFIX}{port}"
        self._token = secrets.token_urlsafe(32)
        # A different random value for the cookie, so the cookie is useless in any other run.
        self._session = secrets.token_urlsafe(32)
        self._token_used = False
        self._active_sessions = 0
        self._services: set[AppService] = set()
        self._stopping: dict[AppService, asyncio.Future[None]] = {}
        self._claimed_downloads: set[str] = set()  # download keys already fetched once

    @property
    def url(self) -> str:
        """The one URL that carries the token; printed once and opened in the browser."""
        return f"{self.origin}/?{TOKEN_PARAM}={self._token}"

    async def _make_app(self) -> web.Application:
        app = await super()._make_app()
        aiohttp_jinja2.setup(
            app, loader=jinja2.DictLoader({"app_index.html": INDEX_TEMPLATE}), autoescape=True
        )
        app.middlewares.append(self._guard)
        app.on_response_prepare.append(self._add_security_headers)
        _BAD_REQUESTS.attach()
        app.on_cleanup.append(self._on_cleanup)
        return app

    @web.middleware
    async def _guard(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        hosts = request.headers.getall(hdrs.HOST, [])
        if hosts != [self.authority]:
            raise web.HTTPForbidden(text="Forbidden.")
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
        is_websocket = (
            request.path == "/ws" or "websocket" in request.headers.get(hdrs.UPGRADE, "").lower()
        )
        if is_websocket and request.headers.getall(hdrs.ORIGIN, []) != [self.origin]:
            raise web.HTTPForbidden(text="Forbidden.")

        signed_in = _same(request.cookies.get(self.cookie_name, ""), self._session)
        if request.path == "/" and TOKEN_PARAM in request.query:
            # Only a real visit may use the link: not `curl -I`, link unfurlers or hover previews.
            if request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET"])
            if self._token_used:
                # The link works once. Reopening it in the browser that used it is fine.
                if signed_in:
                    raise web.HTTPFound("/")
                raise web.HTTPForbidden(text=LINK_USED)
            if not _same(request.query.get(TOKEN_PARAM, ""), self._token):
                raise web.HTTPForbidden(text="Forbidden.")
            self._token_used = True
            # Swap the token for a cookie and drop it from the address bar and history.
            response = web.HTTPFound("/")
            response.set_cookie(
                self.cookie_name, self._session, path="/", httponly=True, samesite="Strict"
            )
            raise response

        if not signed_in:
            raise web.HTTPForbidden(text="Forbidden. Open the link printed by `rip ui`.")
        if _is_static_directory(request):
            raise web.HTTPNotFound()

        request[_NONCE_KEY] = secrets.token_urlsafe(16)
        return await handler(request)

    async def _add_security_headers(
        self, request: web.Request, response: web.StreamResponse
    ) -> None:
        nonce = request.get(_NONCE_KEY)
        script_src = f"'self' 'nonce-{nonce}'" if nonce else "'self'"
        headers = response.headers
        headers["Content-Security-Policy"] = "; ".join(
            [
                "default-src 'none'",
                f"script-src {script_src}",
                # textual.js injects <style> elements, and xterm sets inline styles.
                "style-src 'self' 'unsafe-inline'",
                "font-src 'self'",
                "img-src 'self' data:",
                f"connect-src 'self' ws://{self.authority}",
                "base-uri 'none'",
                "form-action 'none'",
                "frame-ancestors 'none'",
            ]
        )
        headers["X-Frame-Options"] = "DENY"
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["Cache-Control"] = "no-store"
        for name in [h for h in headers if h.lower().startswith("access-control-")]:
            del headers[name]

    async def handle_index(self, request: web.Request) -> web.Response:
        context = {
            "font_size": _to_int(request.query.get("fontsize"), 16, FONT_SIZE_RANGE),
            "app_websocket_url": f"ws://{self.authority}/ws",
            "config": {"static": {"url": f"{self.origin}/static/"}},
            "application": {"name": self.title},
            "nonce": request[_NONCE_KEY],
        }
        return aiohttp_jinja2.render_template("app_index.html", request, context)

    async def handle_websocket(self, request: web.Request) -> web.StreamResponse:
        """textual-serve's handler, plus a session cap and app processes we can stop."""
        # Each WebSocket starts an app subprocess; don't let stuck tabs pile them up.
        if self._active_sessions >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable(
                text=f"Too many open tabs (at most {MAX_SESSIONS}). Close one and retry."
            )
        self._active_sessions += 1
        try:
            websocket = web.WebSocketResponse(heartbeat=15)
            width, height = _terminal_size(request.query.get("width"), request.query.get("height"))
            await websocket.prepare(request)
            service = self.app_service_class(
                self.command,
                argv=self.argv,
                write_bytes=websocket.send_bytes,
                write_str=websocket.send_str,
                close=websocket.close,
                download_manager=self.download_manager,
                debug=False,
            )
            self._services.add(service)
            try:
                await service.start(width, height)
                await self._process_messages(websocket, service)
            except asyncio.CancelledError:
                # Don't wait aiohttp's default 10 s for a hung tab's close frame on the way out.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(websocket.close(), 1.0)
                raise
            except Exception:
                log.exception("The app session failed.")
            finally:
                await self._stop(service)
                self._services.discard(service)
                self._stopping.pop(service, None)
            return websocket
        finally:
            self._active_sessions -= 1

    async def handle_download(self, request: web.Request) -> web.StreamResponse:
        """textual-serve 1.1.3's download handler, with a safe Content-Disposition and one
        fetch per download.

        Upstream puts the file name in the header raw, and serves every request for a key
        from the same queue of chunks, so two fetches at once split the file between them (a
        truncated 200 and a 500). Here the first fetch claims the key; any other, at the same
        time or later, gets 409.
        """
        key = request.match_info["key"]
        if key in self._claimed_downloads:
            raise web.HTTPConflict(text="This download was already fetched.")
        try:
            download_meta = await self.download_manager.get_download_metadata(key)
        except KeyError:
            raise web.HTTPNotFound(text="Download not found.") from None

        content_type = download_meta.mime_type
        if download_meta.encoding:
            content_type += f"; charset={download_meta.encoding}"
        disposition = "inline" if download_meta.open_method == "browser" else "attachment"
        headers = {
            "Content-Type": content_type,
            "Content-Disposition": content_disposition(disposition, download_meta.file_name),
        }
        if request.method == "HEAD":  # headers only: never start (or use up) the download
            return web.Response(headers=headers)

        self._claimed_downloads.add(key)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        try:
            async for chunk in self.download_manager.download(key):
                await response.write(chunk)
            await response.write_eof()
        except ConnectionResetError:
            # The browser went away mid-download. The key stays used (what's left of the file
            # would be a fragment); exporting again gives a new one. No traceback in the terminal.
            pass
        return response

    async def on_shutdown(self, app: web.Application) -> None:
        """Stop every app subprocess before exiting, so Ctrl+C leaves none behind."""
        await asyncio.gather(*(self._stop(service) for service in list(self._services)))

    async def _on_cleanup(self, app: web.Application) -> None:
        _BAD_REQUESTS.detach()
        if hasattr(signal, "SIGHUP"):
            asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)

    async def _stop(self, service: AppService) -> None:
        # One stop per service, shared by the shutdown hook and the WebSocket handler, and
        # shielded so a cancelled handler can't abandon a half-stopped app.
        stopping = self._stopping.get(service)
        if stopping is None:
            stopping = self._stopping[service] = asyncio.ensure_future(
                stop_app_service(service, self.argv[-1])
            )
        await asyncio.shield(stopping)

    async def on_startup(self, app: web.Application) -> None:
        if hasattr(signal, "SIGHUP"):
            # Closing the terminal hangs up the server but not the apps (they have their own
            # sessions): shut down as for Ctrl+C, so they're stopped rather than orphaned.
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, _graceful_exit)
        self.console.print(
            f"{TITLE} is running at:\n\n  {self.url}\n\nPress Ctrl+C to stop.",
            markup=False,
            highlight=False,
        )
        if self.open_browser:
            await asyncio.get_running_loop().run_in_executor(None, webbrowser.open, self.url)

    def run(self, sock: socket.socket) -> None:
        """Serve on ``sock`` until Ctrl+C."""
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        try:
            web.run_app(
                self._make_app(),
                sock=sock,
                access_log=None,  # the access log would record ?token=...
                print=None,
                shutdown_timeout=SHUTDOWN_TIMEOUT,
            )
        finally:
            # A second Ctrl+C cancels the orderly stops; the apps run in their own sessions, so
            # nothing else would ever end them.
            kill_leftover_children()


def serve(event_id: str, *, open_browser: bool = True, port: int | None = None) -> None:
    """Serve the planner for ``event_id`` at a tokenized 127.0.0.1 URL until Ctrl+C.

    On Linux, opening the browser puts the URL on xdg-open's (and perhaps the browser's) command
    line, which other local users can read in /proc. The token works only once and the browser
    uses it at once, so the window is short; to avoid it, pass ``open_browser=False``
    (``--no-browser``) and paste the printed URL.
    """
    argv = build_argv(event_id)
    sock = bind_socket(port)
    server = SecureServer(argv, port=sock.getsockname()[1], open_browser=open_browser)
    server.run(sock)


def _is_static_directory(request: web.Request) -> bool:
    """True for a /static/ URL naming a directory (or escaping it): no directory listings."""
    resource = getattr(request.match_info.route, "resource", None)
    if not isinstance(resource, web.StaticResource):
        return False
    directory = Path(resource.get_info()["directory"]).resolve()
    target = (directory / request.match_info.get("filename", "")).resolve()
    return not target.is_relative_to(directory) or target.is_dir()


def content_disposition(disposition: str, file_name: str) -> str:
    """A Content-Disposition header that no file name can break: an ASCII-only fallback
    (anything but letters, digits, '.', '_' and '-' becomes '_'), quoted, plus the real name
    percent-encoded as UTF-8 (RFC 6266 / RFC 5987)."""
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", file_name) or "download"
    # Control characters and path separators have no place in a file name, encoded or not.
    printable = re.sub(r"[\x00-\x1f\x7f/\\]", "_", file_name)
    encoded = urllib.parse.quote(printable, safe="") or "download"
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def _to_int(value: object, default: int, bounds: tuple[int, int]) -> int:
    """``value`` as an int clamped to ``bounds``, or ``default`` if it isn't a number."""
    try:
        number = int(value)  # a str from the query string, or anything at all from JSON
    except (TypeError, ValueError, OverflowError):
        return default
    low, high = bounds
    return min(max(number, low), high)


def _terminal_size(width: object, height: object) -> tuple[int, int]:
    return _to_int(width, 80, WIDTH_RANGE), _to_int(height, 24, HEIGHT_RANGE)


def _graceful_exit() -> None:
    raise web.GracefulExit()
