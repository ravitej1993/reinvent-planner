"""The More tab: calendar feed, exports, travel-time corrections and sign-out, driven headless.

GitHub is a FakeGitHub (never the real `gh`), the Events API an httpx.MockTransport, and the
token store a file in the test's own config directory (see conftest.isolated_dirs).
"""

import csv
import inspect
import io
import json
import threading
import uuid

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session, mock_client
from icalendar import Calendar
from test_calendar_feed import FakeGitHub
from textual import events, on
from textual.app import App
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from reinvent_planner import calendar_feed, cli, services
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import Auth, FileTokenStore, config_dir
from reinvent_planner.catalog import Catalog
from reinvent_planner.models import Schedule
from reinvent_planner.planner import corrections_path, save_corrections
from reinvent_planner.tui import more_pane
from reinvent_planner.tui.app import ConfirmScreen
from reinvent_planner.tui.more_pane import (
    CorrectionScreen,
    MorePane,
    PublishScreen,
    SignOutScreen,
)

FORMULA_TITLE = "=cmd|' /C calc'!A0"
MARKUP_TITLE = "[bold red]Not markup[/] [link=x]y[/link] ["
MARKUP_VENUE = "[bold red]Venetian[/] ["
MARKUP_OTHER = "Wynn [link=x]y[/link]"


class RealDeliveryApp(App):
    """Just the pane, with what PlannerApp offers it: refresh_views, the Launch tab's state,
    and Textual's delivery events handed to the pane. Files really are delivered (see the
    `downloads` fixture, which keeps them in the test's own directory)."""

    presentation = False

    def __init__(self) -> None:
        super().__init__()
        services.use_event(EVENT_ID)
        self.refreshed = 0
        self.running = False
        self.imminent = False

    def compose(self):
        yield MorePane()

    def refresh_views(self) -> None:
        self.refreshed += 1

    def reservation_running(self) -> bool:
        return self.running

    def launch_imminent(self) -> bool:
        return self.imminent

    @on(events.DeliveryComplete)
    def export_done(self, event: events.DeliveryComplete) -> None:
        self.query_one(MorePane).export_delivered(event)

    @on(events.DeliveryFailed)
    def export_broke(self, event: events.DeliveryFailed) -> None:
        self.query_one(MorePane).export_failed(event)


class MoreApp(RealDeliveryApp):
    """RealDeliveryApp, but files are only recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.delivered: list[tuple[str, dict]] = []

    def deliver_text(self, path_or_file, **kwargs):  # never write to ~/Downloads in a test
        self.delivered.append((path_or_file.read(), kwargs))
        return f"key{len(self.delivered)}"


@pytest.fixture
def seeded():
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(
            event,
            [
                make_session("res100", time="08:00", title="Reserved talk"),
                make_session("fav200", time="10:00", title=FORMULA_TITLE),
                make_session("rank300", time="13:00", title=MARKUP_TITLE),
                make_session("other400", time="15:00", title="Not on my list"),
            ],
        )
        cat.set_rank(EVENT_ID, "rank300", 1)
        cat.save_schedule(EVENT_ID, Schedule(reserved=["res100"], favorites=["fav200"]))


@pytest.fixture
def api(monkeypatch):
    """A signed-in user and a fake Events API that serves their schedule."""
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

    def handler(request):
        if request.url.path.endswith("/schedule"):
            return httpx.Response(200, json={"schedule": schedule})
        return httpx.Response(404)

    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(handler), sleep=lambda s: None
        ),
    )
    return schedule


@pytest.fixture
def gh(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(calendar_feed, "_gh", github)
    return github


@pytest.fixture
def travel_table():
    """A two-venue travel table whose venue names look like markup."""
    path = config_dir() / "venues" / f"{EVENT_ID}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[aliases]",
                'venetian = ["venetian"]',
                'wynn = ["wynn"]',
                "[names]",
                f"venetian = {json.dumps(MARKUP_VENUE)}",
                f"wynn = {json.dumps(MARKUP_OTHER)}",
                "[minutes]",
                '"venetian|wynn" = 20',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def text_of(app, selector: str) -> str:
    return str(app.query_one(selector, Static).render())


def notices(app) -> str:
    return " ".join(str(n.message) for n in app._notifications)


async def press(app, pilot, selector: str) -> None:
    app.screen.query_one(selector, Button).press()
    await pilot.pause()


async def settle(app, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def until(pilot, condition, what: str = "condition") -> None:
    for _ in range(400):
        if condition():
            return
        await pilot.pause(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def dialog_text(app) -> str:
    return " ".join(str(w.render()) for w in app.screen.query("Label, Static"))


def summaries(ics: str) -> list[str]:
    return sorted(str(e["summary"]) for e in Calendar.from_ical(ics).walk("VEVENT"))


# -- calendar feed -------------------------------------------------------------------------


async def test_publish_asks_then_publishes_with_the_chosen_options(seeded, api, gh):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        assert "Not published" in text_of(app, "#feed-status")
        await press(app, pilot, "#feed-publish")
        assert isinstance(app.screen, PublishScreen)
        assert app.screen.query_one("#publish-auto", Checkbox).value is True  # the default
        app.screen.query_one("#publish-personal", Checkbox).value = True
        app.screen.query_one("#publish-auto", Checkbox).value = False
        await pilot.pause()
        await press(app, pilot, "#publish-continue")
        await settle(app, pilot)  # which account gh is on, to name it
        assert isinstance(app.screen, ConfirmScreen)
        details = dialog_text(app)
        assert "secret GitHub Gist" in details and "gh CLI" in details
        assert "GitHub account @octocat-example" in details
        assert "personal time" in details and gh.gists == {}  # nothing sent yet
        assert [c[:2] for c in gh.calls] == [("GET", "/user")]
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        feed = calendar_feed.load(EVENT_ID)
        assert feed is not None and feed.include_personal and not feed.include_ranked
        assert not feed.auto
        assert "Secret interview" in gh.content() and MARKUP_TITLE not in gh.content()
        status = text_of(app, "#feed-status")
        assert "Published" in status and feed.url in status and "Auto-update: off" in status
        assert "personal time" in status and "ranked picks" not in status
        await press(app, pilot, "#feed-show-link")
        assert feed.webcal_url in text_of(app, "#feed-link")


async def test_declining_publish_does_nothing(seeded, api, gh):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#feed-publish")
        await press(app, pilot, "#publish-continue")
        await settle(app, pilot)
        await press(app, pilot, "#no")
        await settle(app, pilot)
        assert "Not published" in text_of(app, "#feed-status")
    assert gh.gists == {} and [c[:2] for c in gh.calls] == [("GET", "/user")]  # only a read
    assert calendar_feed.load(EVENT_ID) is None


async def test_unpublish_needs_a_confirmation(seeded, api, gh):
    services.use_event(EVENT_ID)
    assert services.run(cli.calendar_publish, yes=True).ok
    assert len(gh.gists) == 1
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#feed-unpublish")
        assert isinstance(app.screen, ConfirmScreen)
        await press(app, pilot, "#no")
        await settle(app, pilot)
        assert len(gh.gists) == 1 and calendar_feed.load(EVENT_ID) is not None
        await press(app, pilot, "#feed-unpublish")
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        assert "Not published" in text_of(app, "#feed-status")
    assert gh.gists == {} and calendar_feed.load(EVENT_ID) is None


async def test_missing_gh_is_explained(seeded, api, monkeypatch):
    # The real wrapper, but gh isn't on PATH: nothing is ever run.
    monkeypatch.setattr(calendar_feed.shutil, "which", lambda name: None)
    ran = []
    monkeypatch.setattr(calendar_feed.subprocess, "run", lambda *a, **kw: ran.append(a))
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#feed-publish")
        await press(app, pilot, "#publish-continue")
        await settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen)  # no account to name: no question
        assert "isn't installed" in notices(app) and "gh auth login" in notices(app)
        assert "gh auth login" in text_of(app, "#feed-note")
    assert ran == [] and calendar_feed.load(EVENT_ID) is None


# -- exports -------------------------------------------------------------------------------


async def test_csv_download_is_delivered_and_formula_safe(seeded):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#export-plan-csv")
        await settle(app, pilot)
        await press(app, pilot, "#export-all-csv")
        await settle(app, pilot)
    (plan, plan_args), (catalog, catalog_args) = app.delivered
    assert plan_args["save_filename"] == f"{EVENT_ID}-plan.csv"
    assert catalog_args["save_filename"] == f"{EVENT_ID}-catalog.csv"
    assert plan_args["mime_type"] == "text/csv" and plan_args["encoding"] == "utf-8"
    assert plan.startswith("﻿")  # like the CLI's files, for Excel
    rows = list(csv.DictReader(io.StringIO(plan.removeprefix("﻿"))))
    titles = {r["Code"]: r["Title"] for r in rows}
    assert titles == {
        "RES100": "Reserved talk",
        "FAV200": "'" + FORMULA_TITLE,
        "RANK300": MARKUP_TITLE,
    }
    assert len(list(csv.DictReader(io.StringIO(catalog.removeprefix("﻿"))))) == 4


async def test_ics_download_is_a_valid_calendar(seeded):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#export-ics")
        await settle(app, pilot)
    ((text, args),) = app.delivered
    assert args["save_filename"] == f"{EVENT_ID}-plan.ics" and args["mime_type"] == "text/calendar"
    assert summaries(text) == [
        "FAV200 – " + FORMULA_TITLE,
        "RANK300 – " + MARKUP_TITLE,
        "RES100 – Reserved talk",
    ]


async def test_export_without_a_catalog_explains(tmp_path):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#export-plan-csv")
        await settle(app, pilot)
        assert "rip sync" in notices(app)
    assert app.delivered == []


# -- travel-time corrections --------------------------------------------------------------


async def test_setting_a_correction_writes_the_corrections_file(travel_table):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        assert app.query_one("#travel-reset-all", Button).disabled
        await press(app, pilot, "#travel-add")
        assert isinstance(app.screen, CorrectionScreen)
        app.screen.query_one("#correction-first", Select).value = "venetian"
        app.screen.query_one("#correction-second", Select).value = "wynn"
        app.screen.query_one("#correction-minutes", Input).value = "121"
        await press(app, pilot, "#correction-save")
        assert isinstance(app.screen, CorrectionScreen)  # out of range: still asking
        app.screen.query_one("#correction-minutes", Input).value = "35"
        await press(app, pilot, "#correction-save")
        await settle(app, pilot)
        table = app.query_one("#travel-table", DataTable)
        assert table.row_count == 1
        between, mine, figure = (str(c) for c in table.get_row_at(0))
        assert between == f"{MARKUP_VENUE} ↔ {MARKUP_OTHER}"  # literal, never markup
        assert (mine, figure) == ("35 min", "20 min")
    text = corrections_path(EVENT_ID).read_text(encoding="utf-8")
    assert '"venetian|wynn" = 35' in text


async def test_reset_all_asks_first(travel_table):
    save_corrections(EVENT_ID, {"venetian|wynn": 30})
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#travel-reset-all")
        assert isinstance(app.screen, ConfirmScreen)
        await press(app, pilot, "#no")
        await settle(app, pilot)
        assert corrections_path(EVENT_ID).exists()
        await press(app, pilot, "#travel-reset-all")
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        assert app.query_one("#travel-table", DataTable).row_count == 0
    assert not corrections_path(EVENT_ID).exists()


async def test_reset_selected_asks_first(travel_table):
    save_corrections(EVENT_ID, {"venetian|wynn": 30})
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#travel-reset")
        assert isinstance(app.screen, ConfirmScreen)
        question = str(app.screen.query("Label").first().render())
        assert MARKUP_VENUE in question  # literal
        await press(app, pilot, "#yes")
        await settle(app, pilot)
    assert not corrections_path(EVENT_ID).exists()


async def test_markup_venue_names_stay_literal_in_the_dialog(travel_table):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#travel-add")
        select = app.screen.query_one("#correction-first", Select)
        labels = [str(prompt) for prompt, _value in select._options if _value != Select.NULL]
        assert MARKUP_VENUE in labels and MARKUP_OTHER in labels


# -- account -------------------------------------------------------------------------------


@pytest.fixture
def signed_in(monkeypatch):
    FileTokenStore().save(fresh_tokens())
    revoked = []

    def revoke(request):
        revoked.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(cli, "_auth", lambda: Auth(FileTokenStore(), http=mock_client(revoke)))
    return revoked


async def test_sign_out_asks_then_deletes_the_tokens(signed_in):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await settle(app, pilot)  # the sign-in is read in a worker
        assert "attendee@example.com" in text_of(app, "#account-status")
        await press(app, pilot, "#sign-out")
        assert isinstance(app.screen, SignOutScreen)
        await press(app, pilot, "#sign-out-no")
        await settle(app, pilot)
        assert FileTokenStore().load() is not None and signed_in == []
        await press(app, pilot, "#sign-out")
        assert app.screen.query_one("#sign-out-builder-id", Checkbox).value is False
        await press(app, pilot, "#sign-out-yes")
        await settle(app, pilot)
        assert "Not signed in" in text_of(app, "#account-status")
        assert app.refreshed == 1
    assert FileTokenStore().load() is None
    assert len(signed_in) == 1  # the refresh token was revoked


async def test_presentation_mode_hides_the_email(signed_in, seeded, api, gh):
    services.use_event(EVENT_ID)
    assert services.run(cli.calendar_publish, yes=True).ok
    app = MoreApp()
    app.presentation = True
    async with app.run_test(size=(160, 60)) as pilot:
        await settle(app, pilot)
        account = text_of(app, "#account-status")
        assert "Signed in" in account and "attendee@example.com" not in account
        feed = calendar_feed.load(EVENT_ID)
        assert feed.url not in text_of(app, "#feed-status")  # the link is shown on request


# -- the account the feed goes to (M1) ------------------------------------------------------


async def open_publish_confirm(app, pilot) -> None:
    await press(app, pilot, "#feed-publish")
    await press(app, pilot, "#publish-continue")
    await settle(app, pilot)
    assert isinstance(app.screen, ConfirmScreen)


async def test_a_switched_gh_account_is_spelled_out_before_publishing(seeded, api, gh):
    services.use_event(EVENT_ID)
    assert services.run(cli.calendar_publish, yes=True).ok
    (old_id,) = gh.gists
    gh.login = "work-account"
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await open_publish_confirm(app, pilot)
        details = dialog_text(app)
        assert "different GitHub account" in details
        assert "gh is now signed in as @work-account, not @octocat-example" in details
        assert "a NEW secret gist will be created on @work-account" in details
        assert "the old one on @octocat-example stays online" in details
        await press(app, pilot, "#yes")
        await settle(app, pilot)
    feed = calendar_feed.load(EVENT_ID)
    assert feed.owner == "work-account" and feed.gist_id != old_id
    assert old_id in gh.gists and f"octocat-example/{old_id}" in feed.pending_delete


async def test_an_update_names_the_account(seeded, api, gh):
    services.use_event(EVENT_ID)
    assert services.run(cli.calendar_publish, yes=True).ok
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await open_publish_confirm(app, pilot)
        details = dialog_text(app)
        assert "Update your calendar feed?" in details
        assert "GitHub account @octocat-example" in details and "different" not in details


async def test_an_account_switch_after_the_confirmation_is_refused(seeded, api, gh):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await open_publish_confirm(app, pilot)
        assert "@octocat-example" in dialog_text(app)
        gh.login = "someone-else"  # `gh auth switch` while the question was open
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        assert "now signed into someone-else, not octocat-example" in notices(app)
    assert gh.gists == {} and calendar_feed.load(EVENT_ID) is None


async def test_a_long_gh_error_is_shown_whole(seeded, api, gh, monkeypatch):
    detail = " ".join(f"word{i}" for i in range(900))  # about 5000 characters
    assert len(detail) > 5000

    def refuse_creating(method, endpoint, body=None):
        if method == "POST":
            raise calendar_feed.FeedError(f"GitHub refused the request: {detail}", 422)
        return gh(method, endpoint, body)

    monkeypatch.setattr(calendar_feed, "_gh", refuse_creating)
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await open_publish_confirm(app, pilot)
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        (error,) = [str(n.message) for n in app._notifications if n.severity == "error"]
        assert error == f"GitHub refused the request: {detail}"
        assert "GitHub refused the request:" in text_of(app, "#feed-note")
    assert calendar_feed.load(EVENT_ID) is None


# -- presentation mode (M2) ----------------------------------------------------------------


async def test_presentation_mode_hides_gist_ids_after_publishing(seeded, api, gh):
    app = MoreApp()
    app.presentation = True
    async with app.run_test(size=(160, 60)) as pilot:
        await open_publish_confirm(app, pilot)
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        (gist_id,) = gh.gists
        shown = " ".join([notices(app), text_of(app, "#feed-status"), text_of(app, "#feed-note")])
        assert "published" in notices(app).lower()
        assert gist_id not in shown and "gist.github" not in shown
        assert "(hidden)" in text_of(app, "#feed-note")
        assert not app.query_one("#feed-link", Static).display  # only on request
        await press(app, pilot, "#feed-show-link")
        assert gist_id in text_of(app, "#feed-link")  # asked for, so shown


async def test_presentation_mode_hides_pending_deletes(seeded, gh):
    gist_id = uuid.uuid4().hex
    calendar_feed.save(
        EVENT_ID,
        calendar_feed.Feed(
            gist_id=uuid.uuid4().hex,
            owner="octocat-example",
            filename=f"{EVENT_ID}.ics",
            deleted=True,
            pending_delete=[f"octocat-example/{gist_id}"],
        ),
    )
    app = MoreApp()
    app.presentation = True
    async with app.run_test(size=(160, 60)) as pilot:
        assert "1 old gist(s)" in text_of(app, "#feed-status")
        await press(app, pilot, "#feed-unpublish")
        assert isinstance(app.screen, ConfirmScreen)
        details = dialog_text(app)
        assert gist_id not in details and "(hidden)" in details
        await press(app, pilot, "#no")
        app.presentation = False
        await press(app, pilot, "#feed-unpublish")
        assert f"https://gist.github.com/octocat-example/{gist_id}" in dialog_text(app)


async def test_a_link_shown_before_presentation_mode_is_hidden_again(seeded, api, gh):
    services.use_event(EVENT_ID)
    assert services.run(cli.calendar_publish, yes=True).ok
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#feed-show-link")
        assert app.query_one("#feed-link", Static).display
        app.presentation = True
        app.query_one(MorePane).reload()
        await pilot.pause()
        assert not app.query_one("#feed-link", Static).display


def test_redact_gists_hides_links_and_ids():
    gist_id = "0123456789abcdef0123456789abcdef"
    text = (
        f"Subscribe: https://gist.githubusercontent.com/me/{gist_id}/raw/x.ics\n"
        f"webcal://gist.githubusercontent.com/me/{gist_id}/raw/x.ics and "
        f"https://gist.github.com/me/{gist_id} or me/{gist_id}."
    )
    redacted = more_pane.redact_gists(text)
    assert gist_id not in redacted and "gist.github" not in redacted
    assert redacted.count("(hidden)") == 4 and redacted.startswith("Subscribe: (hidden)")


# -- refusals while the Launch tab is busy (L8, L9) -----------------------------------------


async def test_nothing_changes_while_a_reservation_run_is_going(
    seeded, travel_table, gh, signed_in
):
    save_corrections(EVENT_ID, {"venetian|wynn": 30})
    app = MoreApp()
    app.running = True
    async with app.run_test(size=(160, 60)) as pilot:
        await settle(app, pilot)
        for button in ("#feed-publish", "#sign-out", "#travel-add"):
            await press(app, pilot, button)
            assert app.screen is app.screen_stack[0], button  # no dialog opened
        assert "reservation run is in progress" in notices(app)
        # A run that starts while a question is open: the answer changes nothing either.
        app.running = False
        await press(app, pilot, "#travel-reset-all")
        assert isinstance(app.screen, ConfirmScreen)
        app.running = True
        await press(app, pilot, "#yes")
        await settle(app, pilot)
        await press(app, pilot, "#travel-reset")
        await press(app, pilot, "#yes")
        await settle(app, pilot)
    assert corrections_path(EVENT_ID).exists() and gh.calls == []
    assert FileTokenStore().load() is not None and signed_in == []


async def test_github_and_sign_out_wait_while_go_is_imminent(seeded, api, gh, signed_in):
    services.use_event(EVENT_ID)
    assert services.run(cli.calendar_publish, yes=True).ok
    calls = len(gh.calls)
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await settle(app, pilot)
        app.imminent = True
        for button in ("#feed-publish", "#feed-unpublish", "#sign-out"):
            await press(app, pilot, button)
            assert app.screen is app.screen_stack[0], button
        assert "GO is about to light" in notices(app)
        # GO gets close while the question is open: refused on the answer too.
        app.imminent = False
        await press(app, pilot, "#feed-unpublish")
        app.imminent = True
        await press(app, pilot, "#yes")
        await settle(app, pilot)
    assert len(gh.calls) == calls and calendar_feed.load(EVENT_ID) is not None
    assert FileTokenStore().load() is not None


# -- exports: delivery (L5, L6) --------------------------------------------------------------


async def test_an_export_is_reported_saved_only_once_delivered(seeded, tmp_path):
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#export-ics")
        await settle(app, pilot)
        ((_text, args),) = app.delivered
        assert args["name"] == f"rip-export:{EVENT_ID}-plan.ics"
        assert "saving it to your Downloads folder" in notices(app)
        assert "saved:" not in notices(app)
        path = tmp_path / f"{EVENT_ID}-plan.ics"
        app.post_message(events.DeliveryComplete(key="key1", path=path, name=args["name"]))
        await pilot.pause()
        assert f"{EVENT_ID}-plan.ics saved: {path}" in notices(app)
        # Someone else's delivery (e.g. a screenshot) isn't ours to report.
        pane = app.query_one(MorePane)
        assert not pane.export_delivered(events.DeliveryComplete(key="x", name="screenshot"))
        await press(app, pilot, "#export-plan-csv")
        await settle(app, pilot)
        app.post_message(
            events.DeliveryFailed(
                key="key2", exception=PermissionError("disk full"), name=app.delivered[1][1]["name"]
            )
        )
        await pilot.pause()
        errors = [str(n.message) for n in app._notifications if n.severity == "error"]
        assert errors == [f"Couldn't save {EVENT_ID}-plan.csv: disk full"]


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """Where Textual saves a delivered file in a terminal: the test's directory, never
    ~/Downloads."""
    import textual.app

    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(textual.app, "user_downloads_path", lambda: folder)
    return folder


async def test_a_real_delivery_saves_the_exact_bytes(seeded, downloads):
    app = RealDeliveryApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await press(app, pilot, "#export-ics")
        await settle(app, pilot)
        await until(pilot, lambda: "saved:" in notices(app), "the ICS delivery")
        await press(app, pilot, "#export-plan-csv")
        await settle(app, pilot)
        await until(pilot, lambda: notices(app).count("saved:") == 2, "the CSV delivery")
    ics = (downloads / f"{EVENT_ID}-plan.ics").read_bytes()
    assert b"BEGIN:VCALENDAR\r\n" in ics and b"\r\r\n" not in ics  # CRLF, as RFC 5545 wants
    assert summaries(ics.decode("utf-8"))[0].startswith("FAV200")
    plan = (downloads / f"{EVENT_ID}-plan.csv").read_bytes()
    assert plan.startswith("﻿".encode()) and b"\r\n" in plan and b"\r\r\n" not in plan


def test_delivery_text_lets_windows_text_mode_add_the_carriage_returns(monkeypatch):
    crlf = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    monkeypatch.setattr(more_pane.sys, "platform", "darwin")
    assert more_pane.delivery_text(crlf, web=False) == crlf  # macOS/Linux: written as is
    monkeypatch.setattr(more_pane.sys, "platform", "win32")
    # Windows terminal: text mode turns each "\n" back into "\r\n" (never "\r\r\n").
    assert more_pane.delivery_text(crlf, web=False) == "BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    assert more_pane.delivery_text(crlf, web=True) == crlf  # the web driver encodes as is


# -- sign-out and the Builder ID session (L10, L11) ------------------------------------------


async def test_the_sign_in_is_never_read_on_the_ui_thread(signed_in, monkeypatch):
    threads = []
    real = services.signed_in_as
    gate = threading.Event()  # holds the first read, so "Checking…" is seen on any machine

    def spy():
        threads.append(threading.current_thread() is threading.main_thread())
        gate.wait(5)
        return real()

    monkeypatch.setattr(services, "signed_in_as", spy)
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        assert "Checking" in text_of(app, "#account-status")
        gate.set()
        await settle(app, pilot)
        app.query_one(MorePane).reload()
        await settle(app, pilot)
        assert "attendee@example.com" in text_of(app, "#account-status")
    assert len(threads) >= 2 and not any(threads)


@pytest.fixture
def builder_id(monkeypatch):
    """Auth.end_builder_id_session without a browser or loopback port."""
    seen: dict = {"release": threading.Event()}

    def end_session(self, *, announce, timeout, **extra):  # no `cancel`, like auth today
        cancel = extra.get("cancel")
        seen.update(timeout=timeout, cancel=cancel)
        announce("https://builder-id.example/logout?port=1")
        while not seen["release"].wait(0.01):
            if cancel is not None and cancel.is_set():
                raise more_pane.AuthError("Sign-in cancelled.")

    monkeypatch.setattr(more_pane.Auth, "end_builder_id_session", end_session)
    return seen


async def sign_out_with_builder_id(app, pilot) -> None:
    await settle(app, pilot)
    await press(app, pilot, "#sign-out")
    app.screen.query_one("#sign-out-builder-id", Checkbox).value = True
    await pilot.pause()
    await press(app, pilot, "#sign-out-yes")
    await until(pilot, lambda: "Opening https://builder-id.example" in notices(app), "the URL")


async def test_builder_id_sign_out_shows_the_url_and_blocks_nothing(
    signed_in, builder_id, travel_table
):
    save_corrections(EVENT_ID, {"venetian|wynn": 30})
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await sign_out_with_builder_id(app, pilot)
        assert builder_id["timeout"] == more_pane.BUILDER_ID_WAIT_SECONDS
        assert FileTokenStore().load() is None and len(signed_in) == 1  # tokens first
        assert "waiting for the browser" in text_of(app, "#account-status")
        # While the browser step waits, the rest of the tab still works.
        await press(app, pilot, "#travel-reset-all")
        await press(app, pilot, "#yes")
        await until(pilot, lambda: not corrections_path(EVENT_ID).exists(), "the reset")
        assert "Still working" not in notices(app)
        builder_id["release"].set()
        await settle(app, pilot)
        assert "Builder ID browser session has ended" in notices(app)
        assert "waiting" not in text_of(app, "#account-status")


async def test_builder_id_wait_can_be_stopped_when_auth_supports_it(
    signed_in, builder_id, monkeypatch
):
    monkeypatch.setattr(more_pane, "builder_id_cancellable", lambda: True)
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await sign_out_with_builder_id(app, pilot)
        stop = app.query_one("#builder-id-cancel", Button)
        assert stop.display and builder_id["cancel"] is not None
        stop.press()
        await settle(app, pilot)
        assert "Stopped waiting for the browser" in notices(app)
        assert not stop.display


async def test_no_stop_button_while_auth_cannot_stop_waiting(signed_in, builder_id):
    assert not more_pane.builder_id_cancellable()
    app = MoreApp()
    async with app.run_test(size=(160, 60)) as pilot:
        await sign_out_with_builder_id(app, pilot)
        assert not app.query_one("#builder-id-cancel", Button).display
        assert builder_id["cancel"] is None
        builder_id["release"].set()
        await settle(app, pilot)


def test_no_method_shadows_a_textual_internal():
    """Same rule as tests/test_tui.py: our own names mustn't replace Textual's."""
    from textual.containers import VerticalScroll
    from textual.screen import ModalScreen

    allowed = {"compose", "on_mount", "reload"}
    for cls, base in (
        (MorePane, VerticalScroll),
        (PublishScreen, ModalScreen),
        (CorrectionScreen, ModalScreen),
        (SignOutScreen, ModalScreen),
    ):
        own = {
            n
            for n, v in vars(cls).items()
            if not n.startswith("__") and inspect.isfunction(v) and n not in allowed
        }
        base_names = {n for klass in base.__mro__ for n in vars(klass)}
        instance_attrs = set(base.__init__.__code__.co_names)
        assert not own & (base_names | instance_attrs), (cls.__name__, own & base_names)
    assert "reload" not in {n for klass in VerticalScroll.__mro__ for n in vars(klass)}
