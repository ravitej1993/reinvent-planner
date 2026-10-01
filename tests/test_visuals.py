"""The Strip map, the day timeline, the launch board and the rip-neon theme. Every session here
is made up."""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import ZONE, make_session
from rich.cells import cell_len
from rich.text import Text
from textual.app import App

from reinvent_planner.planner import Item, TravelTimes, item_from_session
from reinvent_planner.reserve import Choice, Event
from reinvent_planner.tui import themes
from reinvent_planner.tui.themes import PALETTE
from reinvent_planner.tui.visuals import render_launch_strip, render_strip_map, render_timeline

Z = ZoneInfo(ZONE)
WIDTHS = [20, 40, 60, 71, 72, 80, 100, 119, 120, 160]


@pytest.fixture
def travel() -> TravelTimes:
    return TravelTimes.load("reinvent2026")  # the bundled table (conftest isolates corrections)


def item(session_id, kind="favorite", **fields) -> Item:
    return item_from_session(make_session(session_id, **fields), Z, kind)


@pytest.fixture
def day() -> list[Item]:
    """Venetian 9:00 ✓ → Forum 10:30 ★ (an easy walk) → MGM 12:00 ★ (too tight)."""
    return [
        item("a", "reserved", venue="Venetian", time="09:00", length="60"),
        item("b", venue="Caesars Forum", time="10:30", length="60"),
        item("c", venue="MGM Grand", time="12:00", length="60"),
    ]


def lines_of(text: Text) -> list[Text]:
    return text.split("\n")


def styles_at(text: Text, index: int) -> list[str]:
    return [str(span.style) for span in text.spans if span.start <= index < span.end]


def line_with(text: Text, needle: str) -> Text:
    return next(line for line in lines_of(text) if needle in line.plain)


# --- the Strip map ---


def test_narrow_map_snapshot(day, travel):
    assert render_strip_map(day, travel, width=60, zone=Z).plain == (
        "▌STRIP · Tue 1 Dec · 3 stops · ⚠ 1 tight/overlap\n"
        " N\n"
        " ┃\n"
        " ●  Venetian  1\n"
        " ┃\n"
        " ●  Caesars Forum  2 ⚠\n"
        " ┃\n"
        " ●  MGM Grand  3 ⚠\n"
        " ┃\n"
        "\n"
        " 1 09:00  ✓  Venetian A Session a\n"
        "    ↓ 12′ walk (0.9 km / 0.6 mi) · 30′ free ✓\n"
        " 2 10:30  ★  Caesars Forum B Session b\n"
        "    ↓ 27′ walk (2.0 km) · 30′ free ⚠ TIGHT, needs ~40′ · or…\n"  # miles dropped first
        " 3 12:00  ★  MGM Grand C Session c"
    )


def test_walks_show_kilometres_and_miles(day, travel):
    text = render_strip_map(day, travel, width=100, zone=Z).plain
    assert "↓ 12′ walk (0.9 km / 0.6 mi) · 30′ free ✓" in text
    assert "↓ 27′ walk (2.0 km / 1.2 mi) · 30′ free ⚠ TIGHT, needs ~40′ · or monorail 24′" in text


def test_miles_are_dropped_first_when_a_leg_would_not_fit(day, travel):
    text = render_strip_map(day, travel, width=72, zone=Z).plain
    assert "↓ 12′ walk (0.9 km / 0.6 mi) · 30′ free ✓" in text  # short enough to keep them
    assert "↓ 27′ walk (2.0 km) · 30′ free ⚠ TIGHT, needs ~40′ · or monorail 24′" in text
    side = render_strip_map(day, travel, width=120, zone=Z).plain  # the itinerary column is 62
    assert "(2.0 km) · 30′ free ⚠ TIGHT" in side and "1.2 mi" not in side


def test_distances_use_the_shared_planner_helper(day, travel, monkeypatch):
    monkeypatch.setattr(
        "reinvent_planner.tui.visuals.distance_parts", lambda meters: (f"{meters}M", "SHARED")
    )
    assert "12′ walk (894M / SHARED)" in render_strip_map(day, travel, width=100, zone=Z).plain


def test_tight_leg_is_red_and_an_easy_one_is_not(day, travel):
    text = render_strip_map(day, travel, width=100, zone=Z)
    tight = line_with(text, "TIGHT")
    easy = line_with(text, "12′ walk")
    assert PALETTE["tight"] in styles_at(tight, tight.plain.index("TIGHT"))
    assert "⚠" in tight.plain  # not colour alone
    assert PALETTE["tight"] not in styles_at(easy, easy.plain.index("walk"))
    assert "✓" in easy.plain


def test_tight_legs_come_from_planner_find_issues(day, travel, monkeypatch):
    """The map reuses planner's tight-transfer logic: with no issues, nothing is tight."""
    monkeypatch.setattr("reinvent_planner.tui.visuals.find_issues", lambda items, travel: [])
    assert "TIGHT" not in render_strip_map(day, travel, width=100, zone=Z).plain


def test_wide_map_puts_venues_where_they_are(day, travel):
    text = render_strip_map(day, travel, width=100, zone=Z)
    rows = text.plain.split("\n")

    def where(name):
        row = next(i for i, line in enumerate(rows) if name in line and "║" in line)
        return row, rows[row].index(name), rows[row].index("║")

    venetian, forum, mgm = where("Venetian 1"), where("Caesars Forum 2"), where("MGM Grand 3")
    wynn, palace = where("○ Wynn"), where("Caesars Palace")
    assert wynn[0] < venetian[0] < forum[0] < mgm[0]  # north to south
    assert forum[1] > forum[2]  # Caesars Forum: east of the Strip
    assert palace[1] < palace[2]  # Caesars Palace: west of it
    assert "◍ Sphere" in text.plain
    assert "Wynn/Encore" not in text.plain  # a grouping key, not on the route


def test_side_by_side_from_120_columns(day, travel):
    text = render_strip_map(day, travel, width=130, zone=Z)
    assert any("║" in line and "09:00" in line for line in text.plain.split("\n"))
    stacked = render_strip_map(day, travel, width=100, zone=Z)
    assert not any("║" in line and "09:00" in line for line in stacked.plain.split("\n"))


@pytest.mark.parametrize("width", WIDTHS)
def test_every_line_fits_the_width(day, travel, width):
    long = item("d", venue="Venetian", time="15:00", title="Word " * 60)
    for text in (
        render_strip_map([*day, long], travel, width=width, zone=Z),
        render_timeline([*day, long], Z, width=width, travel=travel),
    ):
        for line in lines_of(text):
            assert cell_len(line.plain) <= max(width, 20), (width, line.plain)


def test_markup_looking_titles_and_control_characters_stay_literal(travel):
    hostile = item(
        "x",
        venue="Venetian",
        time="09:00",
        title="[bold red]Not markup[/] :smile: \x1b[31mred\x1b[0m\nsecond line",
        abbreviation="[link=https://evil.example]AIM1[/link]",
    )
    text = render_strip_map([hostile], travel, width=160, zone=Z)
    assert "[bold red]Not markup[/] :smile:" in text.plain
    assert "[link=https://evil.example]AIM1[/link]" in text.plain
    assert "\x1b" not in text.plain
    assert not any("second line" in line and "09:00" not in line for line in text.plain.split("\n"))
    assert all("bold red" not in str(span.style) for span in text.spans)
    timeline = render_timeline([hostile], Z, width=160)
    assert "★ [link=https:/" in timeline.plain  # literal, cut to the block's hour


def test_highlight_picks_out_the_stop(day, travel):
    text = render_strip_map(day, travel, width=100, zone=Z, highlight="b")
    line = line_with(text, "Session b")
    assert PALETTE["highlight"] in styles_at(line, line.plain.index("Session b"))
    other = line_with(text, "Session a")
    assert PALETTE["highlight"] not in styles_at(other, other.plain.index("Session a"))


def test_ranks_show_as_glyphs(day, travel):
    text = render_strip_map(day, travel, width=100, zone=Z, ranks={"c": 2})
    assert " #2 " in line_with(text, "Session c").plain
    assert " ✓ " in line_with(text, "Session a").plain


def test_output_is_deterministic(day, travel):
    shuffled = day[:]
    random.Random(7).shuffle(shuffled)  # noqa: S311 (a fixed shuffle, not security)
    ranks = {"c": 2, "b": 1}
    reordered = dict(reversed(list(ranks.items())))
    for width in (60, 100, 130):
        first = render_strip_map(day, travel, width=width, zone=Z, ranks=ranks)
        again = render_strip_map(shuffled, travel, width=width, zone=Z, ranks=reordered)
        assert (first.plain, first.spans) == (again.plain, again.spans)
    first = render_timeline(day, Z, width=80, ranks=ranks, travel=travel)
    again = render_timeline(shuffled, Z, width=80, ranks=reordered, travel=travel)
    assert (first.plain, first.spans) == (again.plain, again.spans)


def test_unknown_venue_is_listed_off_the_map(day, travel):
    hotel = item("h", venue="Hotel Room", time="14:00")
    text = render_strip_map([*day, hotel], travel, width=100, zone=Z)
    assert "off map: Hotel Room" in text.plain


def test_personal_time_without_a_venue(travel):
    start = datetime(2026, 12, 1, 18, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 in Las Vegas
    lunch = Item(
        key="personal:1",
        code="PERSONAL",
        title="Coffee",
        start=start,
        end=start.replace(hour=19),
        venue=None,
        room=None,
        kinds={"personal"},
    )
    first = item("a", "reserved", venue="Venetian", time="09:00", length="60")
    text = render_strip_map([first, lunch], travel, width=80, zone=Z)
    assert " 2 10:00  ◆ " in text.plain  # shown in local time
    assert "no venue" in text.plain


def test_other_event_falls_back_to_an_alphabetical_list():
    table = TravelTimes({}, {}, same=5, unknown=30)
    items = [
        item("a", venue="Hall B", time="09:00"),
        item("b", venue="Hall A", time="11:00"),
    ]
    text = render_strip_map(items, table, width=120, zone=Z)
    assert "║" not in text.plain
    rows = text.plain.split("\n")
    assert rows.index(" ●  Hall A  2") < rows.index(" ●  Hall B  1")


def test_places_order_the_list_by_latitude_and_draw_a_map():
    table = TravelTimes({"alpha": ["alpha hall"], "zulu": ["zulu hall"]}, {}, same=5, unknown=30)
    items = [
        item("a", venue="Alpha Hall", time="09:00"),
        item("z", venue="Zulu Hall", time="11:00"),
    ]
    places = {"alpha": (36.10, -115.17), "zulu": (36.13, -115.16)}  # Zulu is further north
    narrow = render_strip_map(items, table, width=60, zone=Z, places=places).plain.split("\n")
    assert narrow.index(" ●  zulu  2") < narrow.index(" ●  alpha  1")
    alphabetical = render_strip_map(items, table, width=60, zone=Z).plain.split("\n")
    assert alphabetical.index(" ●  alpha  1") < alphabetical.index(" ●  zulu  2")
    wide = render_strip_map(items, table, width=100, zone=Z, places=places).plain
    assert "║" not in wide  # not the Strip, so no road
    assert wide.index("● zulu 2") < wide.index("● alpha 1")


def test_empty_day(travel):
    assert "No timed sessions this day." in render_strip_map([], travel, width=80).plain
    assert "No timed sessions this day." in render_timeline([], Z, width=80).plain


# --- the day timeline ---


def test_timeline_snapshot(day, travel):
    assert render_timeline(day, Z, width=60, travel=travel).plain == (
        "08   09   10    11   12    13   14    15   16    17   18\n"
        "┬────┬────┬─────┬────┬─────┬────┬─────┬────┬─────┬────┬────┐\n"
        "     ✓ A  ░░░★ B   ⚠░★ C   \n"  # C's fill runs to 13:00
        "✓res #Nrank ★fav ◆own ░walk ⚠tight"
    )


def fill_columns(line: Text, style: str) -> set[int]:
    return {
        i for span in line.spans if str(span.style) == style for i in range(span.start, span.end)
    }


def test_last_block_on_a_lane_keeps_its_fill_to_its_end():
    """The browser-pass repro: a block last on its lane lost its fill after its label."""
    first = item("a", abbreviation="DMO301", venue="Venetian", time="10:00", length="120")
    last = item("b", abbreviation="DMO410", venue="Venetian", time="16:30", length="120")
    alone = item("c", abbreviation="DMO220", venue="Venetian", time="13:30", length="60")
    also = item(
        "d", "reserved", abbreviation="DMO999", venue="Venetian", time="13:00", length="120"
    )
    text = render_timeline([first, alone, also, last], Z, width=140, start_hour=8, end_hour=19)
    lanes = lines_of(text)[2:4]
    per_minute = 140 / (11 * 60)

    def columns(start, end):
        return set(range(int((start - 480) * per_minute), int((end - 480) * per_minute)))

    favorite = fill_columns(lanes[0], PALETTE["favorite"])
    assert columns(600, 720) <= favorite  # DMO301 10:00-12:00, followed by more
    assert columns(990, 1110) <= favorite  # DMO410 16:30-18:30, last on lane 1
    assert "DMO220" in lanes[1].plain
    assert columns(810, 870) <= fill_columns(lanes[1], PALETTE["favorite"])  # 13:30-14:30
    assert max(favorite) <= (1110 - 480) * per_minute  # and no further than its end


def test_timeline_stacks_overlaps_in_lanes(day, travel):
    clash = item("d", "reserved", venue="MGM Grand", time="12:30", length="60")
    lanes = lines_of(render_timeline([*day, clash], Z, width=110, ranks={"c": 3}))[2:4]
    assert "#3 C" in lanes[0].plain
    assert "✓ D" in lanes[1].plain


def test_timeline_travel_gap_only_between_different_venues(travel):
    same = [
        item("a", venue="Venetian", time="09:00", length="60"),
        item("b", venue="Venetian", time="11:00", length="60"),
    ]
    assert "░" not in render_timeline(same, Z, width=110, travel=travel).plain.split("\n")[2]
    moved = [same[0], item("b", venue="Caesars Forum", time="11:00", length="60")]
    lane = lines_of(render_timeline(moved, Z, width=110, travel=travel))[2]
    assert "░" in lane.plain
    assert "⚠" not in lane.plain  # an hour is plenty for Venetian → Forum


def test_timeline_tight_gap_is_red_with_a_glyph(day, travel):
    lane = lines_of(render_timeline(day, Z, width=110, travel=travel))[2]
    index = lane.plain.index("⚠")
    assert PALETTE["tight"] in styles_at(lane, index)
    easy = lane.plain.index("░")  # the first gap (Venetian → Forum) is fine
    assert PALETTE["tight"] not in styles_at(lane, easy)


def test_timeline_without_travel_compares_venue_names(day):
    lane = lines_of(render_timeline(day, Z, width=110))[2]
    assert "░" in lane.plain and "⚠" not in lane.plain


def test_timeline_marks_now_and_items_before_the_window(day):
    early = item("e", venue="Venetian", time="07:00", length="120")
    now = datetime(2026, 12, 1, 10, 0, tzinfo=Z)
    text = render_timeline([early, *day], Z, width=110, now=now, start_hour=8)
    assert "▼" in lines_of(text)[1].plain
    assert "‹★ E" in text.plain
    tomorrow = datetime(2026, 12, 2, 10, 0, tzinfo=Z)
    assert "▼" not in render_timeline(day, Z, width=110, now=tomorrow).plain


def test_timeline_hour_labels_thin_out_when_narrow(day):
    narrow = lines_of(render_timeline(day, Z, width=20))[0].plain
    assert "08" in narrow and "09" not in narrow
    wide = lines_of(render_timeline(day, Z, width=110))[0].plain
    assert all(f"{h:02d}" in wide for h in range(8, 19))


def test_timeline_shows_early_and_late_reserved_sessions():
    """The review repro: nothing reserved may vanish off the default window."""
    early = item(
        "early", "reserved", abbreviation="EARLY1", venue="Venetian", time="07:00", length="45"
    )
    party = item(
        "party", "reserved", abbreviation="PARTY", venue="MGM Grand", time="19:30", length="210"
    )
    text = render_timeline([early, party], Z, width=120)
    lanes = "\n".join(line.plain for line in lines_of(text)[2:-1])
    assert "✓ EAR" in lanes and "✓ PARTY" in lanes  # 45 minutes: cut to its own width
    assert "‹" not in text.plain and "›" not in text.plain
    labels = lines_of(text)[0].plain
    assert labels.startswith("07") and "22" in labels


def test_timeline_auto_fit_bounds(day):
    ticks = lambda items: lines_of(render_timeline(items, Z, width=200))[0].plain.split()  # noqa: E731
    assert ticks(day)[0] == "08" and ticks(day)[-1] in ("18", "19")  # at least 08-19
    dawn = item("d", venue="Venetian", time="05:15", length="30")
    assert ticks([dawn, *day])[0] == "05"
    late = item("l", venue="Venetian", time="21:00", length="75")  # ends 22:15
    assert ticks([*day, late])[-1] in ("22", "23")
    explicit = lines_of(render_timeline([dawn, *day], Z, width=200, start_hour=9))[0].plain
    assert explicit.split()[0] == "09"


def test_timeline_clips_at_midnight_with_a_marker():
    party = item(
        "p", "reserved", abbreviation="PARTY", venue="MGM Grand", time="22:00", length="180"
    )  # to 01:00 the next day
    text = render_timeline([party], Z, width=100)
    lane = lines_of(text)[2]
    assert lane.plain.rstrip().endswith("›")
    assert cell_len(lane.plain) <= 100
    assert "earlier/later" in lines_of(text)[-1].plain


def test_timeline_legend_mentions_edges_only_when_used(day):
    assert "earlier/later" not in render_timeline(day, Z, width=120).plain
    clipped = render_timeline(day, Z, width=120, end_hour=12)
    assert "›" in clipped.plain
    assert "earlier/later" in lines_of(clipped)[-1].plain


def test_timeline_lists_what_explicit_hours_leave_out():
    early = item(
        "e", "reserved", abbreviation="EARLY1", venue="Venetian", time="07:00", length="45"
    )
    mid = item("m", venue="Venetian", time="10:00", length="60")
    party = item(
        "p", "reserved", abbreviation="PARTY", venue="MGM Grand", time="19:30", length="60"
    )
    text = render_timeline([early, mid, party], Z, width=120, start_hour=8, end_hour=19)
    outside = line_with(text, "before 08:00")
    assert outside.plain == "⚠ before 08:00: EARLY1 07:00 · after 19:00: PARTY 19:30"
    assert PALETTE["warning"] in styles_at(outside, 0)
    assert "░" not in lines_of(text)[2].plain[70:]  # no travel bar running off the edge


@pytest.mark.parametrize("width", WIDTHS)
def test_timeline_edge_cases_fit_the_width(width):
    items = [
        item("e", "reserved", abbreviation="EARLY1", venue="Venetian", time="02:00", length="45"),
        item("p", "reserved", abbreviation="PARTY", venue="MGM Grand", time="23:00", length="240"),
    ]
    for hours in ({}, {"start_hour": 8, "end_hour": 19}):
        text = render_timeline(items, Z, width=width, **hours)
        assert all(cell_len(line.plain) <= max(width, 12) for line in lines_of(text))


@pytest.mark.parametrize("hours", [(10, 10), (19, 8), (-1, 5), (8, 25)])
def test_timeline_rejects_bad_hours(day, hours):
    with pytest.raises(ValueError):
        render_timeline(day, Z, width=80, start_hour=hours[0], end_hour=hours[1])


# --- the theme ---


def test_register_adds_rip_neon_without_switching():
    app = App()
    before = app.theme
    theme = themes.register(app)
    assert theme.name == "rip-neon"
    assert "rip-neon" in app.available_themes
    assert app.theme == before


@pytest.mark.parametrize(
    "colour",
    [
        themes.FOREGROUND,
        themes.GREEN,
        themes.CYAN,
        themes.MAGENTA,
        themes.RED,
        themes.AMBER,
        themes.VIOLET,
        themes.MUTED,
    ],
)
def test_colours_are_readable_on_the_background(colour):
    assert themes.contrast_ratio(colour, themes.BACKGROUND) >= 4.5
    assert themes.contrast_ratio(colour, themes.PANEL) >= 4.5


@pytest.mark.parametrize("fill", [themes.GREEN, themes.CYAN, themes.AMBER, themes.VIOLET])
def test_block_text_is_readable_on_its_fill(fill):
    assert themes.contrast_ratio(themes.BACKGROUND, fill) >= 4.5


# --- the launch board ---


def pick(rank, *codes, walk_up=False) -> Choice:
    options = [item(code.lower(), abbreviation=code, time="09:00") for code in codes]
    return Choice(rank=rank, options=options, walk_up=walk_up)


def event(kind, choice=None, option=0, round=1, reason="") -> Event:
    target = choice.options[option] if choice is not None else None
    return Event(round=round, kind=kind, choice=choice, item=target, reason=reason)


@pytest.fixture
def picks() -> list[Choice]:
    return [pick(1, "AIM301"), pick(2, "DEV202", "DEV203"), pick(3, "SEC101")]


def test_launch_board_starts_pending_in_rank_order(picks):
    walk_up = pick(4, "KEY001", walk_up=True)
    text = render_launch_strip([picks[2], walk_up, picks[0], picks[1]], [], width=120)
    assert text.plain == (" · #1 AIM301   · #2 DEV202   · #3 SEC101\n0/3 booked")


def test_launch_board_follows_a_run(picks):
    first, second, third = picks
    events = [
        event("sending", first),
        event("booked", first),
        event("sending", second),
        event("refused", second),
        event("sending", second, option=1, round=2),
        event("sending", third),
        event("refused", third),
    ]
    board = render_launch_strip(picks, events, width=120).plain
    assert "✓ #1 AIM301" in board
    assert "→ #2 b:DEV203" in board
    assert "✗ #3 unfilled" in board
    assert board.endswith("1/3 booked · ✗ 1 unfilled")

    after = render_launch_strip(picks, [*events, event("booked", second, option=1)], width=120)
    assert "✓ #2 b:DEV203" in after.plain
    assert after.plain.endswith("2/3 booked · 1 on backup · ✗ 1 unfilled")


def test_refused_with_a_backup_left_says_next(picks):
    board = render_launch_strip(picks, [event("refused", picks[1])], width=120).plain
    assert "✗ #2 DEV202 → next" in board


def test_retrying_and_stopped(picks):
    events = [event("sending", picks[0]), event("retrying", picks[0])]
    stopped = [*events, event("stopped", reason="cancelled")]
    board = render_launch_strip(picks, stopped, width=120)
    assert "… #1" in board.plain
    assert board.plain.endswith("0/3 booked · ■ stopped (cancelled)")
    pending = line_with(board, "· #2")
    assert "dim" in " ".join(styles_at(pending, pending.plain.index("#2")))
    live = render_launch_strip(picks, events, width=120)
    line = line_with(live, "· #2")
    assert "dim" not in " ".join(styles_at(line, line.plain.index("#2")))


def test_launch_board_colours_have_glyphs(picks):
    events = [event("booked", picks[0]), event("refused", picks[2])]
    board = render_launch_strip(picks, events, width=120)
    line = lines_of(board)[0]
    assert PALETTE["reserved"] in styles_at(line, line.plain.index("✓"))
    assert PALETTE["tight"] in styles_at(line, line.plain.index("✗"))


@pytest.mark.parametrize("width", [12, 30, 45, 80])
def test_launch_board_wraps_to_the_width(width):
    many = [pick(rank, f"ABC{rank:03d}") for rank in range(1, 13)]
    board = render_launch_strip(many, [], width=width)
    rows = board.plain.split("\n")
    assert all(cell_len(row) <= max(width, 12) for row in rows)
    assert sum(row.count("#") for row in rows[:-1]) == 12  # every pick shown
    if width < 80:
        assert len(rows) > 2


def test_launch_board_codes_stay_literal():
    odd = pick(1, "[red]X[/red]")
    events = [event("booked", odd)]
    board = render_launch_strip([odd], events, width=80)
    assert "✓ #1 [red]X[/red]" in board.plain
    assert all(" red" not in f" {span.style}" for span in board.spans)


def test_events_for_unknown_picks_are_ignored(picks):
    stranger = pick(9, "ZZZ999")
    board = render_launch_strip(picks, [event("booked", stranger)], width=120)
    assert "ZZZ999" not in board.plain
    assert board.plain.endswith("0/3 booked")


def test_no_picks():
    assert render_launch_strip([], [], width=80).plain == "No picks to reserve."


def test_launch_board_shows_a_pick_that_may_be_reserved():
    from reinvent_planner.reserve import Choice, Event
    from reinvent_planner.tui.visuals import render_launch_strip

    p1 = item_from_session(make_session("p1"), Z, "ranked")
    b1 = item_from_session(make_session("b1"), Z, "ranked")
    choice = Choice(1, [p1, b1])
    board = render_launch_strip(
        [choice],
        [Event(1, "sending", choice, p1), Event(2, "in_doubt", choice, p1, "no confirmation")],
        width=80,
    ).plain
    assert "? #1 P1 check" in board and "may be reserved" in board and "0/1 booked" in board
