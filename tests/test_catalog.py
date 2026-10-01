import os

import pytest
from conftest import EVENT_ID, make_event, make_session

from reinvent_planner.catalog import Catalog, CatalogError, SearchFilters
from reinvent_planner.models import Schedule

SESSIONS = [
    make_session("aim301", title="Building agents with Amazon Bedrock", type="Workshop"),
    make_session(
        "cmp201",
        title="Graviton cost savings",
        level="200 - Intermediate",
        venue="MGM Grand",
        time="13:00",
        topics=["Compute"],
        services=["Amazon EC2"],
        seatAvailability="unavailable",
    ),
    make_session(
        "sec401",
        title="Zero trust deep dive",
        level="400 - Expert",
        date="2026-12-02",
        isReservable=False,
        seatAvailability=None,
        topics=["Security"],
        services=[],
        roles=["Security Engineer"],
        abstract="Identity-centric controls.",
    ),
]


def search(catalog, **kwargs):
    return [s.session_id for s in catalog.search(EVENT_ID, SearchFilters(**kwargs))]


@pytest.fixture
def synced(catalog, event):
    catalog.save_event(event)
    catalog.replace_sessions(event, SESSIONS)
    return catalog


def test_first_sync_reports_no_changes(catalog, event):
    result = catalog.replace_sessions(event, SESSIONS)
    assert result.total == 3
    assert (result.added, result.removed, result.changed) == ([], [], [])


def test_resync_reports_added_removed_and_changed(synced, event):
    moved = make_session(
        "aim301", title="Building agents with Amazon Bedrock", type="Workshop", time="15:00"
    )
    result = synced.replace_sessions(event, [moved, SESSIONS[1], make_session("new100")])
    assert [s.session_id for s in result.added] == ["new100"]
    assert result.removed == [("SEC401", "Zero trust deep dive")]
    assert [(s.session_id, fields) for s, fields in result.changed] == [
        ("aim301", ["start_utc", "end_utc"])
    ]
    assert synced.session_count(EVENT_ID) == 3


def test_full_text_search_matches_title_abstract_and_tags(synced):
    assert search(synced, query="bedrock") == ["aim301"]
    assert search(synced, query="identity") == ["sec401"]  # abstract
    assert search(synced, query="grav") == ["cmp201"]  # prefix
    assert search(synced, query="ec2") == ["cmp201"]  # service tag


def test_search_survives_fts_syntax_in_queries(synced):
    assert search(synced, query='AND "OR* ( NEAR') == []
    assert search(synced, query="") == ["aim301", "cmp201", "sec401"]


def test_filters(synced):
    assert search(synced, types=["workshop"]) == ["aim301"]
    assert search(synced, level="4") == ["sec401"]
    assert search(synced, venue="mgm") == ["cmp201"]
    assert search(synced, topics=["secur"]) == ["sec401"]
    assert search(synced, services=["bedrock"]) == ["aim301"]
    assert search(synced, roles=["engineer"]) == ["sec401"]
    assert search(synced, reservable_only=True) == ["aim301", "cmp201"]
    assert search(synced, available_only=True) == ["aim301"]
    assert search(synced, days=["tue"]) == ["aim301", "cmp201"]
    assert search(synced, days=["2026-12-02"]) == ["sec401"]
    assert search(synced, limit=1) == ["aim301"]


def test_search_without_fts_falls_back(synced):
    synced.has_fts = False
    assert search(synced, query="graviton") == ["cmp201"]


def test_resolve(synced):
    assert synced.resolve(EVENT_ID, "Aim301").session_id == "aim301"
    assert synced.resolve(EVENT_ID, "cmp201").code == "CMP201"
    with pytest.raises(CatalogError, match="rip search"):
        synced.resolve(EVENT_ID, "nope")


def test_resolve_before_sync_says_to_sync(catalog):
    with pytest.raises(CatalogError, match="rip sync"):
        catalog.resolve(EVENT_ID, "aim301")


def test_resolve_ambiguous_code(catalog, event):
    catalog.replace_sessions(
        event, [make_session("x1", abbreviation="DUP1"), make_session("x2", abbreviation="DUP1")]
    )
    with pytest.raises(CatalogError, match="several"):
        catalog.resolve(EVENT_ID, "dup1")


def test_display_zone_prefers_session_timezones(catalog):
    event = make_event(timezone="Europe/Ulyanovsk")
    catalog.save_event(event)
    catalog.replace_sessions(event, [make_session("a"), make_session("b")])
    assert catalog.zone(EVENT_ID).key == "America/Los_Angeles"


def test_display_zone_falls_back_to_event(catalog, event):
    catalog.save_event(event)
    catalog.replace_sessions(event, [make_session("a", sessionTime={"date": "2026-12-01"})])
    assert catalog.zone(EVENT_ID).key == "America/Los_Angeles"


def test_schedule_cache(catalog):
    assert catalog.load_schedule(EVENT_ID) is None
    catalog.save_schedule(EVENT_ID, Schedule(reserved=["a"], favorites=["b"]))
    schedule, fetched_at = catalog.load_schedule(EVENT_ID)
    assert schedule.reserved == ["a"] and fetched_at


def test_ranks_and_backups(synced):
    synced.set_rank(EVENT_ID, "aim301", 1)
    synced.add_backup(EVENT_ID, "cmp201", "aim301")
    items = synced.plan_items(EVENT_ID)
    assert [(i.session_id, i.rank, i.backup_for) for i in items] == [
        ("aim301", 1, None),
        ("cmp201", None, "aim301"),
    ]
    with pytest.raises(CatalogError):
        synced.add_backup(EVENT_ID, "aim301", "aim301")
    assert synced.remove_plan_item(EVENT_ID, "aim301")  # removes its backups too
    assert synced.plan_items(EVENT_ID) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_database_is_owner_only(tmp_path):
    Catalog(tmp_path / "c.sqlite3").close()
    assert ((tmp_path / "c.sqlite3").stat().st_mode & 0o777) == 0o600


def test_sync_refuses_to_wipe_the_catalog(synced, event):
    with pytest.raises(CatalogError, match="--force"):
        synced.replace_sessions(event, [])
    with pytest.raises(CatalogError):
        synced.replace_sessions(event, SESSIONS[:1])  # 1 of 3 is under half
    assert synced.session_count(EVENT_ID) == 3
    synced.replace_sessions(event, SESSIONS[:1], force=True)
    assert synced.session_count(EVENT_ID) == 1


def test_venue_filter_and_venues_use_room_when_venue_is_empty(catalog, event):
    catalog.replace_sessions(
        event,
        [
            make_session("w1", venue=None, room="Wynn/Encore | Level 1 | Latour 1"),
            make_session("m1", venue="MGM Grand", room="Level 3 | Room 1"),
            make_session("x1", venue="MGM Grand", room="Wynn/Encore themed room"),
        ],
    )
    assert search(catalog, venue="wynn") == ["w1"]  # not x1: it has a real venue
    assert catalog.venues(EVENT_ID) == ["MGM Grand", "Wynn/Encore"]


def test_feature_industry_laptop_and_id_filters(catalog, event):
    catalog.replace_sessions(
        event,
        [
            make_session(
                "w1", type="Workshop", features=["Hands-on"], industries=["Financial Services"]
            ),
            make_session("c1", type="Chalk talk", features=["Discussion"]),
            make_session("b1", type="Builders' session", features=["Hands-on"]),
            make_session("l1", type="Lab", features=["Self-paced"]),
            make_session("e1", type="Exam prep"),
        ],
    )
    assert search(catalog, features=["hands-on"]) == ["b1", "w1"]
    assert search(catalog, industries=["financial"]) == ["w1"]
    assert search(catalog, laptop_required=True) == ["b1", "e1", "w1"]  # labs only "may" need one
    assert search(catalog, only_ids=frozenset({"c1", "l1"})) == ["c1", "l1"]
    assert search(catalog, only_ids=frozenset()) == []
    assert search(catalog, only_ids=frozenset({"w1"}), features=["discussion"]) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_existing_database_is_tightened_to_owner_only(tmp_path):
    path = tmp_path / "c.sqlite3"
    Catalog(path).close()
    path.chmod(0o644)
    Catalog(path).close()
    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_symlinked_database_is_refused(tmp_path):
    victim = tmp_path / "victim.sqlite3"
    Catalog(victim).close()
    victim.chmod(0o644)
    link = tmp_path / "c.sqlite3"
    link.symlink_to(victim)
    with pytest.raises(CatalogError, match="symbolic link"):
        Catalog(link)
    assert (victim.stat().st_mode & 0o777) == 0o644


def test_waits_for_another_writer_instead_of_failing(tmp_path):
    with Catalog(tmp_path / "c.sqlite3") as cat:
        assert cat.db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
