import json
import os
import subprocess
import uuid

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session
from icalendar import Calendar
from typer.testing import CliRunner

from reinvent_planner import calendar_feed, cli
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import FileTokenStore
from reinvent_planner.calendar_feed import Feed, FeedError
from reinvent_planner.catalog import Catalog, SearchFilters


class FakeGitHub:
    """Stands in for `gh api`: records calls, keeps gists in memory."""

    def __init__(self, login="octocat-example"):
        self.login = login
        self.gists: dict[str, dict] = {}
        self.owners: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail_next: str | None = None

    def __call__(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, body))
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            raise FeedError(f"GitHub refused the request: {message}")
        if endpoint == "/user":
            return {"login": self.login}
        if method == "POST" and endpoint == "/gists":
            gist_id = uuid.uuid4().hex
            self.gists[gist_id] = body
            self.owners[gist_id] = self.login
            return {"id": gist_id, "owner": {"login": self.login}}
        gist_id = endpoint.rsplit("/", 1)[-1]
        # Like GitHub: a gist you don't own answers 404, the same as a deleted one.
        if gist_id not in self.gists or self.owners[gist_id] != self.login:
            raise FeedError("GitHub refused the request: gh: Not Found (HTTP 404)", 404)
        if method == "PATCH":
            self.gists[gist_id]["files"].update(body["files"])
            return {"id": gist_id}
        if method == "DELETE":
            del self.gists[gist_id]
            return {}
        raise AssertionError(f"unexpected {method} {endpoint}")

    def content(self):
        gist = list(self.gists.values())[-1]  # the newest gist
        (file,) = gist["files"].values()
        return file["content"]


# -- the gh wrapper -----------------------------------------------------------------


def test_missing_gh_is_explained(monkeypatch):
    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: None)
    with pytest.raises(FeedError, match="isn't installed"):
        calendar_feed._gh("GET", "/user")


def test_gh_runs_without_a_shell_and_sends_the_body_on_stdin(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(calendar_feed.subprocess, "run", fake_run)
    assert calendar_feed._gh("POST", "/gists", {"public": False}) == {"ok": True}
    assert seen["args"][:7] == [
        "/usr/bin/gh",
        "api",
        "--hostname",
        "github.com",  # pinned: never a GitHub Enterprise default host
        "--method",
        "POST",
        "/gists",
    ]
    assert "--input" in seen["args"] and json.loads(seen["input"]) == {"public": False}
    assert "shell" not in seen and seen["timeout"] == calendar_feed.GH_TIMEOUT_SECONDS


def test_gh_never_reads_the_terminal_or_prompts(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout='{"login": "octocat"}', stderr="")

    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(calendar_feed.subprocess, "run", fake_run)
    monkeypatch.setenv("RIP_TEST_MARKER", "kept")
    assert calendar_feed._gh("GET", "/user") == {"login": "octocat"}
    # No body: stdin is /dev/null, not the (raw-mode) terminal the app runs in.
    assert seen["stdin"] is subprocess.DEVNULL and seen.get("input") is None
    assert seen["env"]["GH_PROMPT_DISABLED"] == "1"
    assert seen["env"]["RIP_TEST_MARKER"] == "kept"  # the rest of the environment is passed on
    seen.clear()
    calendar_feed._gh("POST", "/gists", {"public": False})
    assert "stdin" not in seen and json.loads(seen["input"]) == {"public": False}
    assert seen["env"]["GH_PROMPT_DISABLED"] == "1"


def test_gh_failure_suggests_signing_in(monkeypatch):
    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        calendar_feed.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="To get started with GitHub CLI, please run:  gh auth login"
        ),
    )
    with pytest.raises(FeedError, match="gh auth login"):
        calendar_feed._gh("GET", "/user")


def _gh_fails(monkeypatch, stderr, returncode=1):
    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        calendar_feed.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, returncode, stdout="", stderr=stderr),
    )


FEED = Feed(gist_id="abc404def0123456789a", owner="octocat-example", filename="x.ics")


def test_only_a_real_404_status_means_the_gist_is_gone(monkeypatch):
    _gh_fails(monkeypatch, "gh: Not Found (HTTP 404)\n")
    with pytest.raises(calendar_feed.FeedGone):
        calendar_feed.update(FEED, b"BEGIN:VCALENDAR")
    _gh_fails(monkeypatch, "HTTP 404: Not Found (https://api.github.com/gists/abc)\n")
    with pytest.raises(calendar_feed.FeedGone):
        calendar_feed.update(FEED, b"BEGIN:VCALENDAR")


@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 502: Bad Gateway (https://api.github.com/gists/abc404def0123456789a)\n",
        "gh: Server Error (HTTP 500)\nsee https://api.github.com/gists/abc404def (Not Found)\n",
    ],
)
def test_a_404_in_a_url_is_not_mistaken_for_not_found(monkeypatch, stderr):
    _gh_fails(monkeypatch, stderr)
    with pytest.raises(FeedError) as caught:
        calendar_feed.update(FEED, b"BEGIN:VCALENDAR")
    assert not isinstance(caught.value, calendar_feed.FeedGone)
    assert caught.value.status in (500, 502)


def test_gh_errors_lose_control_characters(monkeypatch):
    _gh_fails(monkeypatch, "gh: \x1b]8;;https://evil.example\x07Bad\x1b[2J request (HTTP 422)\n")
    with pytest.raises(FeedError) as caught:
        calendar_feed._gh("GET", "/user")
    assert "\x1b" not in str(caught.value) and "\x07" not in str(caught.value)
    assert str(caught.value).endswith("]8;;https://evil.exampleBad[2J request (HTTP 422)")
    assert caught.value.status == 422


def test_gh_in_the_current_directory_is_refused_on_windows(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setattr(calendar_feed.sys, "platform", "win32")
    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: str(tmp_path / "gh.exe"))
    ran = []
    monkeypatch.setattr(calendar_feed.subprocess, "run", lambda *a, **kw: ran.append(a))
    with pytest.raises(FeedError, match="current directory"):
        calendar_feed._gh("GET", "/user")
    assert ran == []
    assert os.environ["NoDefaultCurrentDirectoryInExePath"] == "1"  # noqa: SIM112


def test_gh_on_path_is_used_on_windows(monkeypatch, tmp_path):
    installed = tmp_path / "bin"
    installed.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setattr(calendar_feed.sys, "platform", "win32")
    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: str(installed / "gh.exe"))
    assert calendar_feed._find_gh() == str(installed / "gh.exe")


# -- publishing ---------------------------------------------------------------------


def test_create_makes_a_secret_gist_and_update_keeps_the_url(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(calendar_feed, "_gh", github)
    feed = calendar_feed.create(EVENT_ID, b"BEGIN:VCALENDAR", description="d")
    method, endpoint, body = github.calls[-1]
    assert (method, endpoint, body["public"]) == ("POST", "/gists", False)
    assert (
        feed.url
        == f"https://gist.githubusercontent.com/octocat-example/{feed.gist_id}/raw/{EVENT_ID}.ics"
    )
    assert feed.webcal_url.startswith("webcal://gist.githubusercontent.com/")
    calendar_feed.update(feed, b"BEGIN:VCALENDAR v2")
    assert github.calls[-1][0] == "PATCH" and github.content() == "BEGIN:VCALENDAR v2"


def test_update_never_creates_when_the_gist_is_gone(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(calendar_feed, "_gh", github)
    feed = calendar_feed.create(EVENT_ID, b"x", description="d")
    github.gists.clear()
    with pytest.raises(calendar_feed.FeedGone):
        calendar_feed.update(feed, b"y")
    assert github.gists == {} and github.calls[-1][0] == "PATCH"  # nothing was created


def test_other_update_errors_are_not_papered_over(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(calendar_feed, "_gh", github)
    feed = calendar_feed.create(EVENT_ID, b"x", description="d")
    github.fail_next = "HTTP 403: rate limited"
    with pytest.raises(FeedError, match="403") as info:
        calendar_feed.update(feed, b"y")
    assert not isinstance(info.value, calendar_feed.FeedGone)
    assert len(github.gists) == 1


def test_claim_keeps_one_gist_when_two_processes_create(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(calendar_feed, "_gh", github)
    theirs = calendar_feed.create(EVENT_ID, b"a", description="d")
    calendar_feed.save(EVENT_ID, theirs)  # the other process saved first
    ours = calendar_feed.create(EVENT_ID, b"b", description="d")
    kept = calendar_feed.claim(EVENT_ID, ours, None)
    assert kept.gist_id == theirs.gist_id and list(github.gists) == [theirs.gist_id]


def test_feed_record_is_owner_only_and_validated(tmp_path):
    feed = Feed(gist_id="a" * 32, owner="octocat-example", filename="x.ics")
    calendar_feed.save(EVENT_ID, feed)
    path = calendar_feed._feed_path(EVENT_ID)
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert calendar_feed.load(EVENT_ID) == feed
    path.write_text(json.dumps({"gist_id": "../../evil", "owner": "x", "filename": "f"}))
    with pytest.raises(FeedError, match="doesn't look like"):
        calendar_feed.load(EVENT_ID)
    path.write_text(json.dumps({"gist_id": "a" * 32, "owner": "x", "filename": "../../etc/passwd"}))
    with pytest.raises(FeedError, match="doesn't look like"):
        calendar_feed.load(EVENT_ID)
    path.write_text("{corrupt")
    with pytest.raises(FeedError, match="corrupt"):
        calendar_feed.load(EVENT_ID)


# -- the CLI ------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def env(monkeypatch):
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("res100", time="08:00", title="Reserved talk"),
                make_session("fav200", time="10:00", title="Favorite talk"),
                make_session("rank300", time="13:00", title="Ranked talk"),
            ],
        )
        cat.set_rank(EVENT_ID, "rank300", 1)
    FileTokenStore().save(fresh_tokens())
    schedule = {
        "reserved": ["res100"],
        "favorites": ["fav200"],
        "personalTime": [
            {
                "personalTimeId": "p1",
                "startDateTime": "2026-12-01T20:00:00",
                "endDateTime": "2026-12-01T21:00:00",
                "title": "Secret interview",
                "description": "Secret interview",
            }
        ],
    }

    def api(request):
        if request.url.path.endswith("/schedule"):
            return httpx.Response(200, json={"schedule": schedule})
        if request.url.path.endswith("/sessions"):
            with Catalog() as cat:
                items = [
                    json.loads(r["data"])
                    for r in cat.db.execute(
                        "SELECT data FROM sessions WHERE event_id = ?", (EVENT_ID,)
                    )
                ]
            return httpx.Response(200, json={"items": items, "totalCount": len(items)})
        if request.url.path.endswith(f"/events/{EVENT_ID}"):
            return httpx.Response(200, json={"event": event.model_dump(by_alias=True)})
        if request.url.path.endswith("/favorites"):
            ids = json.loads(request.content)["sessionIds"]
            schedule["favorites"] += ids
            return httpx.Response(200, json={"result": {"successful": ids, "failed": []}})
        return httpx.Response(404)

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(api), sleep=lambda s: None
        ),
    )
    github = FakeGitHub()
    github.schedule = schedule  # tests edit this to simulate changes made on the website
    monkeypatch.setattr(calendar_feed, "_gh", github)
    return github


def rip(*args, input=None):
    return runner.invoke(cli.app, ["--event", EVENT_ID, *args], input=input, env={"COLUMNS": "200"})


def summaries(ics: str) -> list[str]:
    return sorted(str(e["summary"]) for e in Calendar.from_ical(ics).walk("VEVENT"))


def test_first_publish_asks_and_shares_only_reserved_and_favorites(env):
    result = rip("calendar", "publish", input="y\n")
    assert result.exit_code == 0, result.output
    assert "octocat-example" in result.output and "Anyone with the link" in result.output
    assert summaries(env.content()) == ["FAV200 – Favorite talk", "RES100 – Reserved talk"]
    assert "Secret interview" not in env.content() and "Ranked talk" not in env.content()
    assert "REFRESH-INTERVAL" in env.content()
    assert "gist.githubusercontent.com" in result.output and "webcal://" in result.output


def test_declining_publishes_nothing(env):
    result = rip("calendar", "publish", input="n\n")
    assert result.exit_code == 1 and env.gists == {}


def test_widening_what_is_shared_asks_again(env):
    rip("calendar", "publish", "-y")
    result = rip("calendar", "publish", "--personal", input="n\n")
    assert result.exit_code == 1
    assert "Secret interview" not in env.content()
    rip("calendar", "publish", "--personal", "-y")
    assert "Secret interview" in env.content()
    rip("calendar", "publish")  # settings are remembered, and a plain update doesn't ask
    assert "Secret interview" in env.content()


def test_feed_updates_itself_after_a_schedule_change(env):
    rip("calendar", "publish", "-y")
    result = rip("fav", "add", "rank300", "-y")
    assert result.exit_code == 0, result.output
    assert "Calendar feed updated" in result.output
    assert "RANK300 – Ranked talk" in summaries(env.content())


def test_a_feed_problem_never_fails_the_schedule_change(env):
    rip("calendar", "publish", "-y")
    env.fail_next = "HTTP 502: bad gateway"
    result = rip("fav", "add", "rank300", "-y")
    assert result.exit_code == 0, result.output
    assert "Couldn't update your calendar feed" in result.output


def test_no_auto_leaves_the_feed_alone(env):
    rip("calendar", "publish", "--no-auto", "-y")
    calls = len(env.calls)
    rip("fav", "add", "rank300", "-y")
    assert len(env.calls) == calls


def test_url_and_unpublish(env):
    assert rip("calendar", "url").exit_code == 1
    rip("calendar", "publish", "-y")
    url = rip("calendar", "url").output.strip()
    assert url.startswith("https://gist.githubusercontent.com/octocat-example/")
    result = rip("calendar", "unpublish", "-y")
    assert result.exit_code == 0 and env.gists == {}
    assert calendar_feed.load(EVENT_ID) is None


def test_ranked_status_never_leaks_into_the_feed(env):
    with Catalog() as cat:
        cat.set_rank(EVENT_ID, "fav200", 2)  # a favorite you've also ranked
    rip("calendar", "publish", "-y")
    assert "Ranked" not in env.content() and "RANK300" not in env.content()
    assert "Favorite" in env.content()


def test_sharing_less_starts_a_fresh_gist_and_deletes_the_old_history(env):
    rip("calendar", "publish", "--personal", "-y")
    (old_id,) = env.gists
    result = rip("calendar", "publish", "--no-personal", input="y\n")
    assert result.exit_code == 0, result.output
    assert "new link" in result.output and "old link no longer works" in result.output
    assert old_id not in env.gists and len(env.gists) == 1
    assert "Secret interview" not in env.content()


def test_auto_refresh_never_creates_a_gist_and_pauses_itself(env):
    rip("calendar", "publish", "-y")
    env.gists.clear()  # deleted on GitHub, or gh switched to an account that can't write it
    before = len(env.calls)
    result = rip("fav", "add", "rank300", "-y")
    assert result.exit_code == 0, result.output
    assert "paused" in result.output
    new_calls = env.calls[before:]
    assert env.gists == {} and [c[0] for c in new_calls] == ["PATCH"]  # tried, never created
    assert calendar_feed.load(EVENT_ID).auto is False
    calls = len(env.calls)
    rip("fav", "add", "fav200", "-y")
    assert len(env.calls) == calls  # stays quiet until you publish again


def test_republishing_a_gone_feed_asks_and_names_the_account(env):
    rip("calendar", "publish", "-y")
    env.gists.clear()
    env.login = "work-account"
    result = rip("calendar", "publish", input="y\n")
    assert result.exit_code == 0, result.output
    assert "work-account" in result.output and "octocat-example" in result.output
    assert "gone or not writable" in result.output
    assert len(env.gists) == 1


def test_a_crashing_side_effect_never_fails_the_write(env, monkeypatch):
    rip("calendar", "publish", "-y")

    def boom(*args, **kwargs):
        raise PermissionError("gh is not executable")

    monkeypatch.setattr(calendar_feed, "update", boom)
    result = rip("fav", "add", "rank300", "-y")
    assert result.exit_code == 0, result.output
    assert "Couldn't update your calendar feed" in result.output


def test_unpublish_refuses_when_gh_is_on_another_account(env):
    rip("calendar", "publish", "-y")
    env.login = "work-account"
    result = rip("calendar", "unpublish", "-y")
    assert result.exit_code == 1 and "gh auth switch" in result.output
    assert len(env.gists) == 1 and calendar_feed.load(EVENT_ID) is not None  # nothing forgotten


def test_a_failed_old_gist_delete_is_reported_and_retried(env, monkeypatch):
    rip("calendar", "publish", "--personal", "-y")
    (old_id,) = env.gists
    failures = {"DELETE": 1}

    def flaky(method, endpoint, body=None):
        if failures.get(method):
            failures[method] -= 1
            raise FeedError("GitHub refused the request: HTTP 502: bad gateway")
        return env(method, endpoint, body)

    monkeypatch.setattr(calendar_feed, "_gh", flaky)
    result = rip("calendar", "publish", "--no-personal", "-y")
    assert result.exit_code == 0, result.output
    assert "Couldn't delete the old gist" in result.output and old_id in result.output
    assert "Subscribe with this link" in result.output  # the new link is still shown
    assert calendar_feed.load(EVENT_ID).pending_delete == [f"octocat-example/{old_id}"]
    assert old_id in env.gists  # not silently retried in the same run
    retry = rip("calendar", "publish")
    assert retry.exit_code == 0 and old_id not in env.gists
    assert calendar_feed.load(EVENT_ID).pending_delete == []


def test_widening_under_a_switched_account_asks_once(env):
    rip("calendar", "publish", "-y")
    env.login = "work-account"
    result = rip("calendar", "publish", "--personal", input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Publish?") == 1 and "Share more?" not in result.output
    assert "work-account" in result.output


def test_widening_in_place_says_same_link(env):
    rip("calendar", "publish", "-y")
    result = rip("calendar", "publish", "--personal", input="y\n")
    assert "existing feed (same link)" in result.output and "Share more?" in result.output


def test_pending_deletes_survive_a_second_narrowing(env, monkeypatch):
    rip("calendar", "publish", "--personal", "--ranked", "-y")
    (first,) = env.gists

    def no_deletes(method, endpoint, body=None):
        if method == "DELETE":
            raise FeedError("GitHub refused the request: HTTP 502: bad gateway")
        return env(method, endpoint, body)

    monkeypatch.setattr(calendar_feed, "_gh", no_deletes)
    rip("calendar", "publish", "--no-personal", "-y")
    second = calendar_feed.load(EVENT_ID).gist_id
    rip("calendar", "publish", "--no-ranked", "-y")
    pending = calendar_feed.load(EVENT_ID).pending_delete
    assert f"octocat-example/{first}" in pending and f"octocat-example/{second}" in pending


def test_expected_login_refuses_a_gist_on_another_account(env):
    rip("calendar", "publish", "-y")
    (old_id,) = env.gists
    env.login = "work-account"  # gh switched after the app named octocat-example
    result = rip("calendar", "publish", "-y", "--expected-login", "octocat-example")
    assert result.exit_code == 1 and "now signed into work-account" in result.output
    assert list(env.gists) == [old_id]  # nothing new created on work-account
    assert calendar_feed.load(EVENT_ID).gist_id == old_id
    # The account that was confirmed: allowed, as without the check.
    result = rip("calendar", "publish", "-y", "--expected-login", "work-account")
    assert result.exit_code == 0, result.output
    assert len(env.gists) == 2


def test_old_gist_on_another_account_is_tracked(env):
    rip("calendar", "publish", "-y")
    (old_id,) = env.gists
    env.login = "work-account"
    result = rip("calendar", "publish", input="y\n")
    assert "still online on octocat-example" in result.output and old_id in result.output
    assert f"octocat-example/{old_id}" in calendar_feed.load(EVENT_ID).pending_delete


def test_partial_unpublish_leaves_no_dead_link(env, monkeypatch):
    rip("calendar", "publish", "--personal", "-y")

    def no_deletes(method, endpoint, body=None):
        if method == "DELETE":
            raise FeedError("GitHub refused the request: HTTP 502: bad gateway")
        return env(method, endpoint, body)

    monkeypatch.setattr(calendar_feed, "_gh", no_deletes)
    rip("calendar", "publish", "--no-personal", "-y")  # leaves the first gist pending
    monkeypatch.setattr(calendar_feed, "_gh", env)
    pending_first = calendar_feed.load(EVENT_ID).pending_delete[0]

    def only_main_delete(method, endpoint, body=None):
        if method == "DELETE" and endpoint.endswith(pending_first.split("/")[1]):
            raise FeedError("GitHub refused the request: HTTP 502: bad gateway")
        return env(method, endpoint, body)

    monkeypatch.setattr(calendar_feed, "_gh", only_main_delete)
    result = rip("calendar", "unpublish", "-y")
    assert result.exit_code == 0, result.output and "still needs deleting" in result.output
    assert calendar_feed.load(EVENT_ID).deleted is True
    assert rip("calendar", "url").exit_code == 1  # no dead link
    calls = len(env.calls)
    rip("fav", "add", "rank300", "-y")
    assert not any(c[0] == "PATCH" for c in env.calls[calls:])  # no confusing "gone" warning


def _patches(env, since):
    return [c for c in env.calls[since:] if c[0] == "PATCH"]


def test_a_website_change_reaches_the_feed_on_the_next_schedule_read(env):
    rip("calendar", "publish", "-y")
    env.schedule["reserved"].append("rank300")  # reserved on the website, not through rip
    before = len(env.calls)
    result = rip("schedule")
    assert result.exit_code == 0, result.output
    assert "Calendar feed updated" in result.output
    assert len(_patches(env, before)) == 1
    assert "RANK300 – Ranked talk" in summaries(env.content())


def test_rip_sync_also_picks_up_website_changes(env):
    rip("calendar", "publish", "-y")
    env.schedule["favorites"].append("rank300")
    before = len(env.calls)
    result = rip("sync")
    assert result.exit_code == 0, result.output
    assert len(_patches(env, before)) == 1 and "RANK300" in env.content()


def test_no_change_means_no_new_revision(env):
    rip("calendar", "publish", "-y")
    rip("schedule")  # caches the current schedule
    before = len(env.calls)
    rip("schedule")
    rip("sync")
    assert _patches(env, before) == []


def test_one_change_makes_one_revision(env):
    rip("calendar", "publish", "-y")
    before = len(env.calls)
    rip("fav", "add", "rank300", "-y")
    assert len(_patches(env, before)) == 1  # not one for the pre-read and one for the add
    before = len(env.calls)
    rip("calendar", "publish")
    assert len(_patches(env, before)) == 1  # publish's own update only


def test_rank_changes_update_a_feed_that_shares_ranked_picks(env):
    rip("calendar", "publish", "-y")
    before = len(env.calls)
    rip("rank", "set", "fav200", "2")
    assert _patches(env, before) == []  # ranking isn't shared: nothing to update
    rip("calendar", "publish", "--ranked", "-y")
    before = len(env.calls)
    rip("rank", "set", "res100", "3")
    assert len(_patches(env, before)) == 1
    before = len(env.calls)
    rip("rank", "rm", "res100")
    assert len(_patches(env, before)) == 1


def test_content_hash_ignores_per_build_timestamps():
    a = b"BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\nSEQUENCE:1\r\nSUMMARY:x\r\nEND:VEVENT\r\n"
    b = b"BEGIN:VEVENT\r\nDTSTAMP:20270101T000000Z\r\nSEQUENCE:9\r\nSUMMARY:x\r\nEND:VEVENT\r\n"
    c = b"BEGIN:VEVENT\r\nDTSTAMP:20270101T000000Z\r\nSEQUENCE:9\r\nSUMMARY:y\r\nEND:VEVENT\r\n"
    assert calendar_feed.content_hash(a) == calendar_feed.content_hash(b)
    assert calendar_feed.content_hash(a) != calendar_feed.content_hash(c)


def test_publishing_records_what_it_published(env):
    rip("calendar", "publish", "-y")
    assert calendar_feed.load(EVENT_ID).published_hash
    before = len(env.calls)
    rip("schedule")
    assert _patches(env, before) == []  # nothing new to publish


def test_a_failed_refresh_is_retried_on_the_next_read(env):
    rip("calendar", "publish", "-y")
    env.schedule["reserved"].append("rank300")
    env.fail_next = "HTTP 502: bad gateway"
    first = rip("schedule")
    assert "Couldn't update your calendar feed" in first.output
    assert "RANK300" not in env.content()
    before = len(env.calls)
    rip("schedule")  # same schedule as the failed attempt: still retried
    assert len(_patches(env, before)) == 1 and "RANK300" in env.content()


def test_a_declined_write_does_not_hide_a_website_change(env):
    rip("calendar", "publish", "-y")
    rip("schedule")
    env.schedule["reserved"].append("rank300")  # website change...
    rip("fav", "add", "res100", input="n\n")  # ...seen by a pre-read, then the add is declined
    assert "RANK300" in env.content()  # published anyway, by the read that saw it


def test_a_moved_session_reaches_the_feed_on_sync(env):
    rip("calendar", "publish", "-y")
    with Catalog() as cat:
        moved = make_session("fav200", time="16:00", title="Favorite talk", room="New Room 9")
        rows = [
            s for s in cat.search(EVENT_ID, SearchFilters(limit=None)) if s.session_id != "fav200"
        ]
        cat.replace_sessions(make_event(), [*rows, moved], force=True)
    before = len(env.calls)
    result = rip("sync")
    assert result.exit_code == 0, result.output
    assert len(_patches(env, before)) == 1
    assert "New Room 9" in env.content()


def test_repeated_failures_on_reads_warn_at_most_hourly(env, monkeypatch):
    rip("calendar", "publish", "-y")
    env.schedule["reserved"].append("rank300")

    def always_502(method, endpoint, body=None):
        if method == "PATCH":
            raise FeedError("GitHub refused the request: HTTP 502: bad gateway")
        return env(method, endpoint, body)

    monkeypatch.setattr(calendar_feed, "_gh", always_502)
    assert "Couldn't update" in rip("schedule").output
    assert "Couldn't update" not in rip("schedule").output  # quiet on the next read
    assert "Couldn't update" in rip("fav", "add", "fav200", "-y").output  # writes always say
