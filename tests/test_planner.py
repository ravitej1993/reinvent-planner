from zoneinfo import ZoneInfo

from conftest import make_session

from reinvent_planner.auth import config_dir
from reinvent_planner.models import PersonalTime
from reinvent_planner.planner import (
    TravelTimes,
    build_checklist,
    find_issues,
    group_by_day,
    item_from_personal_time,
    item_from_session,
    merge_items,
)

LA = ZoneInfo("America/Los_Angeles")

TRAVEL = TravelTimes.from_toml(
    """
same_venue_minutes = 5
unknown_minutes = 30
[aliases]
venetian = ["venetian", "palazzo"]
mgm = ["mgm grand"]
caesars_forum = ["caesars forum"]
caesars_palace = ["caesars palace"]
[minutes]
"venetian|mgm" = 35
"""
)


def item(session_id, kind="favorite", **kwargs):
    return item_from_session(make_session(session_id, **kwargs), LA, kind)


def kinds(issues):
    return [(i.kind, i.first.key, i.second.key) for i in issues]


def test_overlap_detected():
    issues = find_issues([item("a", time="10:00"), item("b", time="10:30")], TRAVEL)
    assert kinds(issues) == [("overlap", "a", "b")]


def test_back_to_back_same_venue_is_fine():
    issues = find_issues([item("a", time="10:00"), item("b", time="11:05")], TRAVEL)
    assert issues == []


def test_tight_transfer_between_far_venues():
    a = item("a", time="10:00", venue="The Venetian")
    b = item("b", time="11:15", venue="MGM Grand")
    (issue,) = find_issues([a, b], TRAVEL)
    assert (issue.kind, issue.gap_minutes, issue.needed_minutes, issue.estimate_known) == (
        "tight",
        15,
        35,
        True,
    )
    assert "needs ~35" in issue.describe()


def test_unknown_venues_use_a_flagged_guess():
    a = item("a", time="10:00", venue="Somewhere")
    b = item("b", time="11:10", venue="Elsewhere")
    (issue,) = find_issues([a, b], TRAVEL)
    assert issue.needed_minutes == 30 and not issue.estimate_known
    assert "rough guess" in issue.describe()


def test_only_the_next_session_counts_as_a_transfer():
    a = item("a", time="10:00", venue="Venetian")
    b = item("b", time="11:40", venue="Venetian")
    c = item("c", time="12:00", venue="MGM Grand", length="30")  # far, but b comes first
    assert kinds(find_issues([a, b, c], TRAVEL)) == [("overlap", "b", "c")]


def test_all_day_and_untimed_items_are_ignored():
    all_day = item("x", isAllDaySession=True)
    untimed = item("y", time="TBA")
    assert find_issues([all_day, untimed, item("a")], TRAVEL) == []


def test_personal_time_without_location_skips_travel_check():
    pt = item_from_personal_time(
        PersonalTime(
            personal_time_id="p1",
            start_date_time="2026-12-01T19:05:00",  # 11:05 in Las Vegas
            end_date_time="2026-12-01T20:00:00",
            title="Lunch",
            description="Team lunch",
        )
    )
    assert find_issues([item("a", time="10:00", venue="MGM Grand"), pt], TRAVEL) == []


def test_alias_longest_match_wins():
    assert TRAVEL.venue_key("Caesars Forum, Level 1") == "caesars_forum"
    assert TRAVEL.venue_key("Caesars Palace") == "caesars_palace"
    assert TRAVEL.venue_key("The Palazzo") == "venetian"
    assert TRAVEL.venue_key(None) is None


def test_merge_combines_kinds():
    merged = merge_items([item("a", "reserved"), item("a", "favorite"), item("b")])
    assert [(i.key, i.kinds) for i in merged] == [
        ("a", {"reserved", "favorite"}),
        ("b", {"favorite"}),
    ]


def test_group_by_day():
    days = group_by_day([item("b", date="2026-12-02"), item("a"), item("u", time="TBA")], LA)
    assert [str(d) for d in days] == ["2026-12-01", "2026-12-02", "None"]


def test_checklist_keeps_picks_in_rank_order():
    fixed = [item("res", "reserved", time="08:00")]
    p1 = item("p1", "ranked", time="10:00")
    p2 = item("p2", "ranked", time="10:30")  # clashes with p1
    p3 = item("p3", "ranked", time="13:00")
    b1 = item("b1", "backup", time="10:00", venue="MGM Grand")  # would clash with p3? no
    entries = build_checklist(
        [(2, p2, []), (1, p1, [b1]), (3, p3, []), (4, fixed[0], [])], fixed, TRAVEL
    )
    assert [(e.rank, e.item.key, e.status) for e in entries] == [
        (1, "p1", "reserve"),
        (2, "p2", "clash"),
        (3, "p3", "reserve"),
        (4, "res", "reserved"),
    ]
    assert [c.key for c in entries[1].clashes_with] == ["p1"]
    # A backup is checked against everything kept except its own primary.
    assert entries[0].backups[0].clashes_with == []


def test_checklist_flags_backups_that_clash_with_other_picks():
    p1 = item("p1", "ranked", time="10:00")
    p2 = item("p2", "ranked", time="14:00")
    backup = item("b", "backup", time="14:00")  # overlaps p2, kept earlier? p2 comes after
    entries = build_checklist([(1, p2, []), (2, p1, [backup])], [], TRAVEL)
    assert [c.key for c in entries[1].backups[0].clashes_with] == ["p2"]


def test_checklist_keeps_each_picks_own_backups_when_ranks_tie():
    p1 = item("p1", "ranked", time="09:00")
    p2 = item("p2", "ranked", time="13:00")
    b1 = item("b1", "backup", time="09:00")
    b2 = item("b2", "backup", time="13:00")
    entries = build_checklist([(1, p1, [b1]), (1, p2, [b2])], [], TRAVEL)
    backups = {e.item.key: [b.item.key for b in e.backups] for e in entries}
    assert backups == {"p1": ["b1"], "p2": ["b2"]}


def test_checklist_marks_unscheduled():
    (entry,) = build_checklist([(1, item("t", "ranked", time="TBA"), [])], [], TRAVEL)
    assert entry.status == "unscheduled"


def test_bundled_reinvent_travel_table_loads():
    travel = TravelTimes.load("reinvent2026")
    assert travel.minutes("Venetian", "MGM Grand") == (45, True)  # measured 32 min walk + buffer
    assert travel.minutes("MGM Grand", "Venetian") == (45, True)
    assert travel.minutes("Wynn", "Encore") == (20, True)
    assert travel.minutes("Venetian", None) is None


def test_user_override_wins():
    path = config_dir() / "venues" / "reinvent2026.toml"
    path.parent.mkdir(parents=True)
    path.write_text('same_venue_minutes = 9\n[aliases]\nv = ["venetian"]\n', encoding="utf-8")
    assert TravelTimes.load("reinvent2026").minutes("Venetian", "Venetian") == (9, True)


def test_unknown_event_uses_defaults():
    assert TravelTimes.load("someotherevent").minutes("A", "B") == (30, False)


def test_backups_are_checked_against_lower_picks_too():
    r1 = item("r1", "ranked", time="09:00")
    backup = item("bk", "backup", time="13:00")
    r2 = item("r2", "ranked", time="13:00")
    entries = build_checklist([(1, r1, [backup]), (2, r2, [])], [], TRAVEL)
    assert [c.key for c in entries[0].backups[0].clashes_with] == ["r2"]


def test_reserved_backup_is_marked_not_self_clashing():
    r1 = item("r1", "ranked", time="09:00")
    backup = item("bk", "reserved", time="09:00")
    entries = build_checklist([(1, r1, [backup])], [backup], TRAVEL)
    status = entries[0].backups[0]
    assert status.reserved and [c.key for c in status.clashes_with] == []


def test_only_overlaps_block_travel_never_does():
    from reinvent_planner.planner import blocks

    a = item("a", time="10:00", venue="Venetian")
    far = item("f", time="11:10", venue="MGM Grand")  # 10 min for a 45-min trip
    assert blocks(a, far, TRAVEL) is None
    assert find_issues([a, far], TRAVEL)[0].kind == "tight"  # still warned about
    assert blocks(a, item("o", time="10:30"), TRAVEL).kind == "overlap"


# -- venues that live in `room` (re:Invent 2026: Wynn/Encore, Caesars Palace) -----------

REINVENT = TravelTimes.load("reinvent2026")


def room_item(session_id, room, time="10:00", venue=None):
    return item(session_id, time=time, venue=venue, room=room)


def test_room_supplies_the_venue_when_venue_is_empty():
    wynn = room_item("w", "Wynn/Encore | Level 1 | Latour 1")
    mgm = item("m", time="11:10", venue="MGM Grand", room="Level 3 | Room 1")
    (issue,) = find_issues([wynn, mgm], REINVENT)
    assert (issue.kind, issue.needed_minutes, issue.estimate_known) == ("tight", 45, True)
    assert "Wynn/Encore to MGM Grand" in issue.describe()


def test_wynn_and_encore_rooms_resolve_to_their_building():
    latour = room_item("w", "Wynn/Encore | Convention Promenade | Latour 5")
    cristal = room_item("c", "Wynn/Encore | Upper Convention Promenade | Cristal 2")
    chopin = room_item("e", "Wynn/Encore | Level 1 | Chopin 4", time="11:10")
    ballroom = room_item("b", "Wynn/Encore | Level 1 | Encore Ballroom 3")
    key = lambda i: REINVENT.place_key(i.venue, i.room)  # noqa: E731
    assert (key(latour), key(cristal), key(chopin), key(ballroom)) == (
        "wynn",
        "wynn",
        "encore",
        "encore",
    )
    (issue,) = find_issues([latour, chopin], REINVENT)  # 10 min between buildings, needs 20
    assert (issue.kind, issue.needed_minutes) == ("tight", 20)
    assert REINVENT.place_key(None, "Wynn/Encore | Somewhere new") == "wynn_encore"
    palace = room_item("p", "Caesars Palace | Promenade South | Octavius 4")
    assert key(palace) == "caesars_palace"  # "Promenade" only refines Wynn/Encore rooms


def test_rooms_without_a_known_venue_are_not_treated_as_venues():
    a = room_item("a", "Vision Hall")
    b = room_item("b", "West Wing", time="11:05")
    assert find_issues([a, b], REINVENT) == []
    assert find_issues([a, b], TRAVEL) == []


def test_bundled_table_is_measured_and_consistent():
    """Every planning figure follows the documented formula from its measured walk."""
    import math
    import tomllib
    from importlib import resources

    data = tomllib.loads(
        (resources.files("reinvent_planner") / "data" / "reinvent2026.toml").read_text()
    )
    assert set(data["minutes"]) == set(data["walking"])
    assert set(data["monorail"]) <= set(data["walking"])
    for pair, walk in data["walking"].items():
        expected = min(45, 5 * math.ceil((walk["minutes"] + 10) / 5))  # walking-based
        assert data["minutes"][pair] == expected, pair
        assert 300 < walk["meters"] < 5000, pair
    for pair, rail in data["monorail"].items():
        assert rail["minutes"] == rail["walking"] + 5 + rail["ride"], pair  # exact invariant
        assert rail["minutes"] <= data["walking"][pair]["minutes"] + 10, pair  # only useful ones
    assert set(data["names"]) == set(data["aliases"])


def test_travel_times_exposes_names_walks_and_order():
    travel = TravelTimes.load("reinvent2026")
    assert travel.venue_keys()[0] == "venetian"
    assert travel.name("encore") == "Encore"
    assert travel.name("wynn_encore") == "Wynn/Encore"
    assert travel.planned("mgm", "venetian") == travel.planned("venetian", "mgm") == 45
    assert travel.planned("mgm", "mgm") == 5
    meters, minutes = travel.walking("venetian", "caesars_forum")
    assert 500 < meters < 1500 and minutes < 20
    assert travel.walking("venetian", "nowhere") is None


def test_checklist_carries_travel_warnings_but_keeps_the_pick():
    far = TravelTimes.from_toml(
        '[aliases]\nven = ["venetian"]\nmgm = ["mgm grand"]\n[minutes]\n"ven|mgm" = 45\n'
    )
    p1 = item("p1", "ranked", time="10:00", venue="Venetian")
    p2 = item("p2", "ranked", time="11:10", venue="MGM Grand")
    entries = build_checklist([(1, p1, []), (2, p2, [])], [], far)
    assert [e.status for e in entries] == ["reserve", "reserve"]
    assert [w.kind for w in entries[1].travel_warnings] == ["tight"]


def test_invalid_override_is_a_clear_error():
    import pytest

    from reinvent_planner.planner import TravelTableError

    path = config_dir() / "venues" / "reinvent2026.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[walking]\n"a|b" = { minutes = 20 }\n', encoding="utf-8")
    with pytest.raises(TravelTableError, match="invalid"):
        TravelTimes.load("reinvent2026")
    path.write_text("not = [valid", encoding="utf-8")
    with pytest.raises(TravelTableError):
        TravelTimes.load("reinvent2026")


def test_monorail_is_an_oriented_off_peak_tip():
    travel = TravelTimes.load("reinvent2026")
    there = travel.monorail("caesars_forum", "mgm")
    back = travel.monorail("mgm", "caesars_forum")
    assert there.route == "Harrah's/The LINQ → MGM Grand"
    assert back.route == "MGM Grand → Harrah's/The LINQ"  # shown in the direction travelled
    assert there.beats(27) and not travel.monorail("wynn", "mgm").beats(44)  # a tie isn't a tip
    assert travel.planned("caesars_forum", "mgm") == 40  # allowances stay walking-based
    assert travel.monorail("venetian", "wynn") is None  # pointless routes aren't listed


def test_override_numbers_must_be_whole():
    import pytest

    for bad in ("3.9", "true"):
        with pytest.raises(ValueError):
            TravelTimes.from_toml(f'[minutes]\n"a|b" = {bad}\n')
    assert TravelTimes.from_toml('[minutes]\n"a|b" = 30.0\n').planned("a", "b") == 30


def test_corrections_layer_over_the_table():
    from reinvent_planner.planner import corrections_path, save_corrections

    save_corrections("reinvent2026", {"venetian|wynn": 25})
    travel = TravelTimes.load("reinvent2026")
    assert travel.planned("wynn", "venetian") == 25
    assert frozenset(("venetian", "wynn")) in travel.corrected
    assert travel.planned("venetian", "mgm") == 45  # everything else untouched
    corrections_path("reinvent2026").write_text('[minutes]\n"venetian|wynn" = 500\n')
    import pytest

    from reinvent_planner.planner import TravelTableError

    with pytest.raises(TravelTableError, match="between 1 and 180"):
        TravelTimes.load("reinvent2026")


def test_shuttle_and_connection_notes():
    travel = TravelTimes.load("reinvent2026")
    assert travel.shuttle("venetian", "mgm") is True
    assert travel.shuttle("venetian", "wynn") is False
    assert travel.shuttle("caesars_forum", "venetian") is False
    assert "bridge" in travel.note("wynn", "venetian")
    assert "Expo hours" in travel.note("caesars_forum", "venetian")
    assert TravelTimes({}, {}, same=5, unknown=30).shuttle("a", "b") is None  # no info


def test_resolve_accepts_names_keys_and_aliases():
    travel = TravelTimes.load("reinvent2026")
    assert travel.resolve("Venetian") == "venetian"
    assert travel.resolve("wynn") == "wynn"
    assert travel.resolve("Encore") == "encore"
    assert travel.resolve("MGM Grand") == "mgm"
    assert travel.resolve("caesars forum") == "caesars_forum"
    assert travel.resolve("Luxor") is None


def test_bundled_table_structure():
    """Keys in TOML belong to the table header above them; catch misplaced ones."""
    import tomllib
    from importlib import resources

    data = tomllib.loads(
        (resources.files("reinvent_planner") / "data" / "reinvent2026.toml").read_text()
    )
    venues = set(data["aliases"])
    for parent, buildings in data["room_aliases"].items():
        assert parent in venues and set(buildings) <= venues, (parent, set(buildings))
    for pair in [*data["no_shuttle"], *data["notes"], *data["minutes"]]:
        assert set(pair.split("|")) <= venues, pair


def test_corrections_file_is_valid_toml_whatever_the_keys():
    import tomllib

    from reinvent_planner.planner import corrections_path, save_corrections

    save_corrections("odd", {'a"b|c\\d': 10})
    assert tomllib.loads(corrections_path("odd").read_text())["minutes"] == {'a"b|c\\d': 10}


def test_override_venue_keys_must_be_simple():
    import pytest

    with pytest.raises(ValueError, match="venue keys"):
        TravelTimes.from_toml('[aliases]\n"a|b" = ["x"]\n')
