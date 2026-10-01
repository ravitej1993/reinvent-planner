"""A calendar feed that updates itself: your plan as an .ics file in a secret GitHub Gist.

Google Calendar, Apple Calendar and Outlook can subscribe to a URL and re-fetch it, which a
file on your laptop can't offer. The feed is published with the GitHub CLI (`gh`) you're already
signed into, so this tool never handles a GitHub token: it runs `gh api` without a shell and
passes the content on stdin.

Privacy: a secret gist is unlisted and its URL is unguessable, but anyone who has the URL can
read it, including its revision history (every earlier version). By default the feed holds only
your reserved and favorite sessions; personal time and your private ranking are left out unless
you ask for them. Narrowing what's shared starts a fresh gist and deletes the old one, history
and all; `rip calendar unpublish` deletes it outright.

How fast subscribers see changes is up to each calendar app: Apple Calendar and Outlook can
refresh hourly; Google Calendar refreshes subscribed calendars on its own schedule, typically
every 12-24 hours.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .auth import config_dir
from .models import clean_text

GH_TIMEOUT_SECONDS = 60
_GIST_ID = re.compile(r"^[0-9a-f]{20,64}$")
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,100}\.ics$")
_HASH = re.compile(r"^([0-9a-f]{64})?$")
# The HTTP status in gh's error line: "gh: Not Found (HTTP 404)", or "HTTP 502: Bad Gateway
# (https://...)" from its HTTP client. Anchored, so an ID in a URL can't pass for a status.
_GH_STATUS = re.compile(r"^(?:gh: )?HTTP (\d{3})\b|\(HTTP (\d{3})\)$")
# Lines that change on every build without changing what a subscriber sees.
_VOLATILE = ("DTSTAMP", "LAST-MODIFIED", "SEQUENCE")


class FeedError(Exception):
    """Publishing the feed failed; the message says what to do.

    `status` is the HTTP status GitHub answered, when gh reported one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FeedGone(FeedError):
    """The feed's gist is deleted, or not writable by the account `gh` is signed into now."""


@dataclass
class Feed:
    gist_id: str
    owner: str
    filename: str
    include_personal: bool = False
    include_ranked: bool = False
    auto: bool = True
    # Old gists ("owner/id") that couldn't be deleted yet; retried on the next publish/unpublish.
    pending_delete: list[str] = field(default_factory=list)
    # The feed itself was unpublished, but old gists still await deletion.
    deleted: bool = False
    # Hash of the content last published successfully (see content_hash), so updates happen
    # exactly when what subscribers would see changes, and retry after a failure.
    published_hash: str = ""
    # When an automatic update last failed (epoch seconds), to avoid repeating the warning.
    last_failure: float = 0.0

    @property
    def url(self) -> str:
        """The always-latest raw URL (no revision in it), for calendar subscriptions."""
        return f"https://gist.githubusercontent.com/{self.owner}/{self.gist_id}/raw/{self.filename}"

    @property
    def webcal_url(self) -> str:
        return "webcal://" + self.url.removeprefix("https://")


def _feed_path(event_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", event_id)
    return config_dir() / "feeds" / f"{safe}.json"


def load(event_id: str) -> Feed | None:
    path = _feed_path(event_id)
    if not path.exists():
        return None
    try:
        feed = Feed(**json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError) as exc:
        raise FeedError(f"{path} is corrupt. Delete it and publish again.") from exc
    pending_ok = all(
        _LOGIN.match(p.partition("/")[0]) and _GIST_ID.match(p.partition("/")[2])
        for p in feed.pending_delete
    )
    if not (
        _GIST_ID.match(feed.gist_id)
        and _LOGIN.match(feed.owner)
        and _FILENAME.match(feed.filename)
        and _HASH.match(feed.published_hash)
        and pending_ok
    ):
        raise FeedError(f"{path} doesn't look like a feed this tool wrote. Delete it.")
    return feed


def save(event_id: str, feed: Feed) -> None:
    """Owner-only: the gist ID is effectively a read password for your feed."""
    path = _feed_path(event_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".feed-", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(feed), fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def forget(event_id: str) -> None:
    _feed_path(event_id).unlink(missing_ok=True)


def _find_gh() -> str | None:
    """Where `gh` is, never from the current directory (Windows searches it before PATH, so a
    gh.exe dropped in a downloaded folder would run instead)."""
    if sys.platform == "win32":
        # Honored by shutil.which on Python 3.12+; the check below covers 3.11.
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"  # noqa: SIM112 - its real name
    gh = shutil.which("gh")
    if gh is not None and sys.platform == "win32":
        if Path(gh).resolve().parent == Path.cwd().resolve():
            raise FeedError(
                "Found gh in the current directory, not in an installed location; refusing to "
                "run it. Run rip from a different directory."
            )
    return gh


def _gh_status(stderr: str) -> int | None:
    for line in stderr.splitlines():
        if match := _GH_STATUS.search(line.strip()):
            return int(match.group(1) or match.group(2))
    return None


def _gh(method: str, endpoint: str, body: dict | None = None) -> dict:
    """Call the GitHub API through the user's `gh` CLI."""
    gh = _find_gh()
    if gh is None:
        raise FeedError(
            "The GitHub CLI (gh) isn't installed. Install it (https://cli.github.com), run "
            "`gh auth login`, then try again."
        )
    # Pinned to github.com: an Enterprise default host would put the feed on a server its admins
    # can read, and the subscription URL (gist.githubusercontent.com) wouldn't work anyway.
    args = [
        gh,
        "api",
        "--hostname",
        "github.com",
        "--method",
        method,
        endpoint,
        "-H",
        "Accept: application/vnd.github+json",
    ]
    # Never let gh prompt: without a body its stdin would otherwise be the terminal, which the
    # interactive app holds in raw mode.
    stdin: dict = {"input": json.dumps(body)} if body is not None else {"stdin": subprocess.DEVNULL}
    if body is not None:
        args += ["--input", "-"]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            args,
            **stdin,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise FeedError("GitHub didn't answer in time. Try again.") from exc
    if proc.returncode != 0:
        detail = clean_text(proc.stderr or proc.stdout).strip().splitlines()
        message = detail[-1] if detail else f"gh exited with {proc.returncode}"
        status = _gh_status(clean_text(proc.stderr or ""))
        if "auth login" in message or status == 401:
            message += " (run `gh auth login`)"
        raise FeedError(f"GitHub refused the request: {message}", status)
    try:
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError as exc:
        raise FeedError("GitHub returned something unexpected.") from exc


def content_hash(ics: bytes) -> str:
    """A hash of what subscribers would see, ignoring per-build timestamps and sequence."""
    lines = [
        line
        for line in ics.decode("utf-8").splitlines()
        if line.split(":", 1)[0].split(";", 1)[0].upper() not in _VOLATILE
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _not_found(exc: FeedError) -> bool:
    return exc.status == 404


def update(feed: Feed, ics: bytes) -> None:
    """Replace the feed's content in place (same URL). Never creates anything.

    GitHub answers 404 both for a deleted gist and for one the current `gh` account can't
    write, so that becomes FeedGone: the caller decides, with the user, what to do.
    """
    try:
        _gh(
            "PATCH", f"/gists/{feed.gist_id}", {"files": {feed.filename: {"content": ics.decode()}}}
        )
    except FeedError as exc:
        if _not_found(exc):
            raise FeedGone(
                "Your feed's gist is gone, or the account `gh` is signed into can't update it. "
                "Run `rip calendar publish` to set it up again."
            ) from exc
        raise


def create(event_id: str, ics: bytes, *, description: str, settings: Feed | None = None) -> Feed:
    """Create a new secret gist for the feed. Only call after the user has confirmed."""
    filename = f"{re.sub(r'[^A-Za-z0-9._-]', '-', event_id)}.ics"
    created = _gh(
        "POST",
        "/gists",
        {
            "public": False,
            "description": description,
            "files": {filename: {"content": ics.decode()}},
        },
    )
    gist_id, owner = str(created.get("id", "")), str((created.get("owner") or {}).get("login", ""))
    if not (_GIST_ID.match(gist_id) and _LOGIN.match(owner)):
        raise FeedError("GitHub created the gist but returned an unexpected response.")
    feed = Feed(gist_id=gist_id, owner=owner, filename=filename)
    if settings is not None:
        feed.include_personal = settings.include_personal
        feed.include_ranked = settings.include_ranked
        feed.auto = settings.auto
        feed.pending_delete = list(settings.pending_delete)  # never drop an undeleted old gist
    return feed


def claim(event_id: str, feed: Feed, previous: Feed | None) -> Feed:
    """Save a newly created feed, unless another rip process saved a different one meanwhile:
    then delete ours (so no orphaned copy of the schedule is left behind) and keep theirs."""
    current = load(event_id)
    if current is not None and current.gist_id not in (
        feed.gist_id,
        getattr(previous, "gist_id", None),
    ):
        unpublish(feed)
        # Keep track of anything we were already due to delete.
        current.pending_delete = list(
            dict.fromkeys([*current.pending_delete, *feed.pending_delete])
        )
        save(event_id, current)
        return current
    save(event_id, feed)
    return feed


def delete_gist(owner: str, gist_id: str) -> None:
    """Delete a gist and its revision history.

    GitHub answers 404 both for "already deleted" and for "exists, but isn't yours", so the
    signed-in account is checked first: a 404 only counts as done for the gist's own owner.
    """
    login = github_login()
    if login != owner:
        raise FeedError(
            f"That gist is on the GitHub account {owner}, but `gh` is signed into {login}. "
            f"Sign `gh` into {owner} (`gh auth switch`) and try again."
        )
    try:
        _gh("DELETE", f"/gists/{gist_id}")
    except FeedError as exc:
        if not _not_found(exc):
            raise  # already gone is fine


def unpublish(feed: Feed) -> None:
    delete_gist(feed.owner, feed.gist_id)


def retry_pending(feed: Feed, only: set[str] | None = None) -> list[str]:
    """Try again to delete old gists left over from earlier runs; returns what remains.

    `only` limits the retry to entries that were already pending when the command started, so
    a delete that just failed isn't silently retried in the same breath.
    """
    remaining = []
    for entry in feed.pending_delete:
        if only is not None and entry not in only:
            remaining.append(entry)
            continue
        owner, _, gist_id = entry.partition("/")
        try:
            delete_gist(owner, gist_id)
        except FeedError:
            remaining.append(entry)
    feed.pending_delete = remaining
    return remaining


def github_login() -> str:
    """The account `gh` is signed into, to show before publishing."""
    login = str(_gh("GET", "/user").get("login", ""))
    if not _LOGIN.match(login):
        raise FeedError("Couldn't read your GitHub account from `gh`.")
    return login
