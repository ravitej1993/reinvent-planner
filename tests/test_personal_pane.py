"""The Personal time tab, driven headless against a fake API (no terminal, no real schedule)."""

import inspect

import httpx
import pytest
from conftest import EVENT_ID, fresh_tokens, make_event, make_session
from test_personal_time import FakePersonalTimeServer
from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from reinvent_planner import cli, services
from reinvent_planner.api import EventsClient
from reinvent_planner.auth import FileTokenStore
from reinvent_planner.catalog import Catalog
from reinvent_planner.models import Schedule
from reinvent_planner.tui.app import ConfirmScreen
from reinvent_planner.tui.personal_pane import PersonalPane, PersonalTimeForm

LUNCH = {
    "personalTimeId": "lunch0001abcdef",
    "startDateTime": "2026-12-01T20:00:00",  # Tue Dec 1, 12:00-13:00 in Las Vegas
    "endDateTime": "2026-12-01T21:00:00",
    "title": "Lunch",
    "description": "Lunch",
    "location": "Encore",
}


class PaneApp(App):
    def compose(self) -> ComposeResult:
        yield PersonalPane()


@pytest.fixture
def server(monkeypatch):
    """The catalog (with a session that clashes with a noon lunch) and a fake API."""
    event = make_event()
    with Catalog() as cat:
        cat.save_event(event)
        cat.replace_sessions(event, [make_session("aim301", time="12:30")])
    services.use_event(EVENT_ID)
    fake = FakePersonalTimeServer()
    fake.reserved = ["aim301"]
    real = EventsClient
    monkeypatch.setattr(
        cli,
        "EventsClient",
        lambda auth=None, **kw: real(
            auth, transport=httpx.MockTransport(fake), sleep=lambda s: None
        ),
    )
    return fake


@pytest.fixture
def signed_in(server):
    FileTokenStore().save(fresh_tokens())
    return server


def seed(server, *entries):
    """Entries on the (fake) real schedule and in the cached copy the table reads."""
    server.entries.extend(dict(e) for e in entries)
    with Catalog() as cat:
        cat.save_schedule(
            EVENT_ID,
            Schedule.model_validate(
                {"reserved": server.reserved, "favorites": [], "personalTime": list(entries)}
            ),
        )


async def open_form(app, pilot, button="#personal-add"):
    app.query_one(button, Button).press()
    for _ in range(3):  # pushed, then composed, then its fields filled (slow on CI)
        await pilot.pause()
    assert isinstance(app.screen, PersonalTimeForm)
    return app.screen


def fill(form, **values):
    for name, value in values.items():
        if name == "day":
            form.query_one("#pt-day", Select).value = value
        else:
            form.query_one(f"#pt-{name}", Input).value = value


async def save(app, pilot):
    app.screen.query_one("#pt-save", Button).press()
    for _ in range(3):  # the form closes, then the confirmation opens and is composed
        await pilot.pause()


async def answer(app, pilot, yes: bool):
    assert isinstance(app.screen, ConfirmScreen)
    await pilot.pause()  # composed (its buttons exist) before pressing one
    app.screen.query_one("#yes" if yes else "#no", Button).press()
    for _ in range(3):  # the press, then the worker it starts, then that worker's callback
        await pilot.pause()
        await app.workers.wait_for_complete()
    # Shown only when a test fails: what the app told the user (e.g. an error after the write).
    print("app messages:", [str(n.message) for n in app._notifications])


def question(app) -> str:
    return " ".join(str(w.render()) for w in app.screen.query(Label))


def details(app) -> str:
    return " ".join(str(w.render()) for w in app.screen.query(Static))


def rows(app) -> list[list[str]]:
    table = app.query_one("#personal-table", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def notices(app) -> str:
    return " ".join(str(n.message) for n in app._notifications)


async def test_add_asks_first_then_posts_the_times_in_utc(signed_in):
    seed(signed_in)  # the cached schedule holds a seat at AIM301 (12:30)
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        form = await open_form(app, pilot)
        fill(form, title="Team lunch", day="2026-12-01", start="12:00", end="13:00")
        fill(form, where="Wynn buffet")
        await save(app, pilot)
        assert "Add 'Team lunch' on Tue Dec 1, 12:00–13:00 to your re:Invent schedule?" in (
            question(app)
        )
        assert "AIM301" in details(app) and "[yellow]" not in details(app)  # the clash, first
        assert signed_in.writes == []  # nothing sent before the answer
        await answer(app, pilot, yes=True)
        ((method, path, body),) = signed_in.writes
        assert method == "POST" and path.endswith("/personal-time")
        assert (body["startDateTime"], body["endDateTime"]) == (
            "2026-12-01T20:00:00",
            "2026-12-01T21:00:00",
        )
        assert body["location"] == "Wynn buffet" and body["description"] == "Team lunch"
        assert rows(app) == [["1", "Tue Dec 1", "12:00–13:00", "Team lunch", "Wynn buffet"]]
        assert "Added" in notices(app)


async def test_declining_or_cancelling_changes_nothing(signed_in):
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        form = await open_form(app, pilot)
        fill(form, title="Lunch", day="2026-12-01", start="12:00", end="13:00")
        await save(app, pilot)
        await answer(app, pilot, yes=False)
        form = await open_form(app, pilot)
        form.query_one("#pt-cancel", Button).press()
        await pilot.pause()
        assert not isinstance(app.screen, PersonalTimeForm | ConfirmScreen)
        assert rows(app) == []
    assert signed_in.writes == [] and signed_in.entries == []


async def test_edit_prefills_the_form_and_sends_the_entry_id(signed_in):
    seed(signed_in, LUNCH)
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        assert rows(app) == [["1", "Tue Dec 1", "12:00–13:00", "Lunch", "Encore"]]
        form = await open_form(app, pilot, "#personal-edit")
        values = {n: form.query_one(f"#pt-{n}", Input).value for n in ("title", "start", "end")}
        assert values == {"title": "Lunch", "start": "12:00", "end": "13:00"}
        assert form.query_one("#pt-day", Select).value == "2026-12-01"
        assert form.query_one("#pt-where", Input).value == "Encore"
        assert form.query_one("#pt-note", Input).value == ""  # the defaulted description
        fill(form, end="13:30", where="")
        await save(app, pilot)
        assert "Change 'Lunch' (Tue Dec 1 12:00–13:00) to 'Lunch' on Tue Dec 1, 12:00–13:30?" in (
            question(app)
        )
        await answer(app, pilot, yes=True)
        ((method, path, body),) = signed_in.writes
        assert method == "PUT" and path.endswith(f"/personal-time/{LUNCH['personalTimeId']}")
        assert (body["startDateTime"], body["endDateTime"]) == (
            "2026-12-01T20:00:00",
            "2026-12-01T21:30:00",
        )
        assert "location" not in body  # cleared
        assert rows(app) == [["1", "Tue Dec 1", "12:00–13:30", "Lunch", ""]]


async def test_remove_asks_then_deletes_by_id(signed_in):
    breakfast = {
        **LUNCH,
        "personalTimeId": "bfast0002abcdef",
        "startDateTime": "2026-12-01T15:00:00",
        "endDateTime": "2026-12-01T15:30:00",
        "title": "Breakfast",
        "description": "Breakfast",
    }
    seed(signed_in, LUNCH, breakfast)
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        assert [r[3] for r in rows(app)] == ["Breakfast", "Lunch"]  # `rip time ls` order
        app.query_one("#personal-table", DataTable).move_cursor(row=1)
        app.query_one("#personal-remove", Button).press()
        await pilot.pause()
        assert "Remove 'Lunch' (Tue Dec 1 12:00–13:00)" in question(app)
        await answer(app, pilot, yes=True)
        ((method, path, _body),) = signed_in.writes
        assert method == "DELETE" and path.endswith(f"/personal-time/{LUNCH['personalTimeId']}")
        assert [r[3] for r in rows(app)] == ["Breakfast"]
    assert [e["title"] for e in signed_in.entries] == ["Breakfast"]


@pytest.mark.parametrize(
    ("title", "start", "end", "message"),
    [
        ("", "12:00", "13:00", "Give it a title"),
        ("   ", "12:00", "13:00", "Give it a title"),
        ("Lunch", "10", "13:00", "ambiguous"),
        ("Lunch", "12:00", "noon", "isn't a time"),
        ("Lunch", "12:00", "12:00", "end must differ from the start"),
        ("Lunch", "12:00", "12:07", "multiple of 5"),
    ],
)
async def test_bad_input_is_refused_in_the_form(signed_in, title, start, end, message):
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        form = await open_form(app, pilot)
        fill(form, title=title, day="2026-12-01", start=start, end=end)
        await save(app, pilot)
        assert app.screen is form  # still open, saying why
        assert message in str(form.query_one("#pt-error", Static).render())
    assert signed_in.writes == []


async def test_titles_are_shown_literally_never_as_markup(signed_in):
    seed(signed_in, {**LUNCH, "title": "[bold]x[/]", "location": "[link=y]z[/link] ["})
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        assert rows(app)[0][3:] == ["[bold]x[/]", "[link=y]z[/link] ["]
        app.query_one("#personal-remove", Button).press()
        await pilot.pause()
        assert "Remove '[bold]x[/]'" in question(app)
        await answer(app, pilot, yes=False)
        form = await open_form(app, pilot, "#personal-edit")
        assert form.query_one("#pt-title", Input).value == "[bold]x[/]"


async def test_signed_out_writes_explain_instead_of_crashing(server):
    seed(server, LUNCH)
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        form = await open_form(app, pilot)
        fill(form, title="Lunch 2", day="2026-12-02", start="12:00", end="13:00")
        await save(app, pilot)
        await answer(app, pilot, yes=True)
        assert "not signed in" in notices(app).lower()
        app.query_one("#personal-remove", Button).press()
        await pilot.pause()
        await answer(app, pilot, yes=True)
        assert rows(app)[0][3] == "Lunch"  # still listed from the cache
    assert server.writes == []


async def test_refresh_reads_the_real_schedule(signed_in):
    signed_in.entries.append(dict(LUNCH))  # on the real schedule, not yet in the cache
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        assert rows(app) == []
        app.query_one("#personal-refresh", Button).press()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [r[3] for r in rows(app)] == ["Lunch"]
    assert signed_in.writes == []
    with Catalog() as cat:
        assert [pt.title for pt in cat.load_schedule(EVENT_ID)[0].personal_time] == ["Lunch"]


async def test_reload_picks_up_changes_to_the_cache(signed_in):
    app = PaneApp()
    async with app.run_test(size=(140, 50)) as pilot:
        assert rows(app) == []
        seed(signed_in, LUNCH)
        app.query_one(PersonalPane).reload()
        await pilot.pause()
        assert [r[3] for r in rows(app)] == ["Lunch"]


def test_no_method_shadows_a_textual_internal():
    from textual.containers import Vertical
    from textual.screen import ModalScreen

    for cls, base in ((PersonalPane, Vertical), (PersonalTimeForm, ModalScreen)):
        own = {
            n
            for n, v in vars(cls).items()
            if n.startswith("_") and not n.startswith("__") and inspect.isfunction(v)
        }
        base_names = {n for klass in base.__mro__ for n in vars(klass)}
        instance_attrs = set(base.__init__.__code__.co_names)
        assert not own & (base_names | instance_attrs), (cls.__name__, own & base_names)


async def test_an_overnight_block_can_be_added_like_the_cli_allows():
    """Review part A, L4: 23:00-01:00 was refused (so overnight blocks couldn't even be edited)."""
    from datetime import date

    from reinvent_planner.tui.personal_pane import BlockForm

    form = BlockForm(
        title="Late", day=date(2026, 12, 1), start="23:00", end="01:00", where="", note=""
    )
    assert "ends the next day" in form.when
