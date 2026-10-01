import tomllib

import httpx
import pytest
from conftest import make_event, make_session
from typer.testing import CliRunner

from reinvent_planner import cli, venues_measure
from reinvent_planner.auth import config_dir
from reinvent_planner.catalog import Catalog
from reinvent_planner.planner import TravelTimes
from reinvent_planner.venues_measure import Measurer, allowance, venue_key

EVENT = "summit2026"
PLACES = {
    "Moscone West": ("37.7840", "-122.4012"),
    "Moscone South": ("37.7830", "-122.4005"),
    "Hilton Union Square": ("37.7859", "-122.4104"),
}


class FakeOSM:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "nominatim.openstreetmap.org":
            name = request.url.params["q"].split(",")[0]
            if name in self.missing or name not in PLACES:
                return httpx.Response(200, json=[])
            lat, lon = PLACES[name]
            return httpx.Response(
                200, json=[{"lat": lat, "lon": lon, "display_name": f"{name}, San Francisco"}]
            )
        if request.url.host == "routing.openstreetmap.de":
            return httpx.Response(200, json={"routes": [{"distance": 850.0, "duration": 660.0}]})
        return httpx.Response(404)


@pytest.fixture
def catalog_with(monkeypatch):
    def seed(venues, event_id=EVENT):
        event = make_event(eventId=event_id, address={"city": "San Francisco"})
        with Catalog() as cat:
            cat.save_event(event)
            cat.replace_sessions(
                event,
                [make_session(f"s{i}", venue=v, time=f"{8 + i}:00") for i, v in enumerate(venues)],
            )

    return seed


@pytest.fixture
def osm(monkeypatch):
    fake = FakeOSM()
    real = Measurer
    monkeypatch.setattr(
        venues_measure,
        "Measurer",
        lambda **kw: real(transport=httpx.MockTransport(fake), sleep=lambda s: None),
    )
    return fake


runner = CliRunner()


def rip(*args, event=EVENT, input=None):
    return runner.invoke(cli.app, ["--event", event, *args], input=input, env={"COLUMNS": "200"})


def test_allowance_and_keys():
    assert allowance(11) == 25 and allowance(44) == 45 and allowance(0) == 10
    assert venue_key("Moscone West") == "moscone_west" and venue_key("!!!") == "venue"


def test_measure_builds_a_loadable_table(catalog_with, osm):
    catalog_with(list(PLACES))
    result = rip("venues", "measure", input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "San Francisco" in result.output  # the matched places are shown to check
    path = config_dir() / "venues" / f"{EVENT}.toml"
    data = tomllib.loads(path.read_text())
    assert set(data["names"].values()) == set(PLACES)
    assert len(data["minutes"]) == 3 and set(data["minutes"].values()) == {25}  # 11 min walk
    travel = TravelTimes.load(EVENT)
    assert travel.minutes("Moscone West", "Hilton Union Square") == (25, True)
    user_agents = {r.headers["user-agent"] for r in osm.requests}
    assert all(ua.startswith("reinvent-planner/") for ua in user_agents)
    assert len(osm.requests) == 3 + 3  # 3 lookups + 3 routes


def test_only_venue_names_and_city_are_sent(catalog_with, osm):
    catalog_with(list(PLACES))
    rip("venues", "measure", "-y")
    queries = [r.url.params.get("q") for r in osm.requests if r.url.host.startswith("nominatim")]
    assert sorted(queries) == sorted(f"{name}, San Francisco" for name in PLACES)


def test_declining_sends_nothing(catalog_with, osm):
    catalog_with(list(PLACES))
    result = rip("venues", "measure", input="n\n")
    assert result.exit_code == 1 and osm.requests == []


def test_single_venue_events_need_no_table(catalog_with, osm):
    catalog_with(["Moscone West", "Moscone West"])
    result = rip("venues", "measure", "-y")
    assert result.exit_code == 0 and "nothing to measure" in result.output
    assert osm.requests == []


def test_existing_tables_are_protected(catalog_with, osm):
    catalog_with(["MGM Grand", "Venetian"], event_id="reinvent2026")
    result = rip("venues", "measure", "-y", event="reinvent2026")
    assert result.exit_code == 1 and "--force" in result.output and osm.requests == []


def test_a_venue_osm_cannot_find_saves_nothing(catalog_with, osm):
    osm.missing = {"Hilton Union Square"}
    catalog_with(list(PLACES))
    result = rip("venues", "measure", "-y")
    assert result.exit_code == 1 and "couldn't find: Hilton Union Square" in result.output
    assert not (config_dir() / "venues" / f"{EVENT}.toml").exists()


def test_requests_are_spaced_out():
    sleeps = []
    fake = FakeOSM()
    with Measurer(transport=httpx.MockTransport(fake), sleep=sleeps.append) as measurer:
        measurer.geocode("Moscone West", None)
        measurer.geocode("Moscone South", None)
    assert sleeps and all(s > 0.5 for s in sleeps)


def test_too_many_venues_is_refused():
    with (
        Measurer(transport=httpx.MockTransport(FakeOSM()), sleep=lambda s: None) as measurer,
        pytest.raises(venues_measure.MeasureError, match="more than"),
    ):
        venues_measure.geocode_all(measurer, [f"v{i}" for i in range(13)], None, lambda m: None)


def test_matches_are_confirmed_before_measuring_or_saving(catalog_with, osm):
    catalog_with(list(PLACES))
    result = rip("venues", "measure", input="y\nn\n")  # yes to the lookup, no to the matches
    assert result.exit_code == 1
    assert not any(r.url.host.startswith("routing") for r in osm.requests)  # no walks measured
    assert not (config_dir() / "venues" / f"{EVENT}.toml").exists()


def test_names_with_emoji_and_odd_characters_make_a_valid_table(catalog_with, osm):
    PLACES["Café 🎪 Hall"] = ("37.7850", "-122.4050")
    PLACES['Room "A" \\ B'] = ("37.7851", "-122.4051")
    try:
        catalog_with(["Café 🎪 Hall", 'Room "A" \\ B', "Moscone West"])
        result = rip("venues", "measure", "-y")
        assert result.exit_code == 0, result.output
        travel = TravelTimes.load(EVENT)
        assert "Café 🎪 Hall" in travel.names.values()
    finally:
        PLACES.pop("Café 🎪 Hall")
        PLACES.pop('Room "A" \\ B')


def test_near_duplicate_names_count_once(catalog_with, osm):
    catalog_with(["Moscone West", "moscone west ", "Moscone South"])
    rip("venues", "measure", "-y")
    lookups = [r for r in osm.requests if r.url.host.startswith("nominatim")]
    assert len(lookups) == 2


def test_force_backs_up_your_file_and_reuses_saved_places(catalog_with, osm):
    catalog_with(list(PLACES))
    rip("venues", "measure", "-y")
    path = config_dir() / "venues" / f"{EVENT}.toml"
    path.write_text(path.read_text() + "# my edit\n")
    before = len(osm.requests)
    result = rip("venues", "measure", "--force", "-y")
    assert result.exit_code == 0, result.output
    backups = list(path.parent.glob(f"{EVENT}.toml.*.bak"))
    assert len(backups) == 1 and "# my edit" in backups[0].read_text()
    assert "previous file is at" in " ".join(result.output.split())  # immune to line wraps
    new = osm.requests[before:]
    assert not any(r.url.host.startswith("nominatim") for r in new)  # cached places reused
    rip("venues", "measure", "--force", "--regeocode", "-y")
    assert any(r.url.host.startswith("nominatim") for r in osm.requests[before + len(new) :])


def test_force_over_the_bundled_table_says_what_it_replaces(catalog_with, osm):
    PLACES["MGM Grand"] = ("36.1027", "-115.1694")
    PLACES["Venetian"] = ("36.1219", "-115.1662")
    try:
        catalog_with(["MGM Grand", "Venetian"], event_id="reinvent2026")
        result = rip("venues", "measure", "--force", event="reinvent2026", input="n\n")
        assert "replaces the current table" in result.output and "hand-checked" in result.output
        assert result.exit_code == 1 and osm.requests == []
    finally:
        PLACES.pop("MGM Grand")
        PLACES.pop("Venetian")


def test_sessions_without_a_venue_are_reported(catalog_with, osm):
    catalog_with(["Moscone West", "Moscone South", None])
    result = rip("venues", "measure", "-y")
    assert "1 session(s) list no venue" in result.output


def test_a_far_away_match_is_flagged():
    from reinvent_planner.venues_measure import Place, outliers

    near = [Place(f"n{i}", f"n{i}", 37.78 + i * 0.001, -122.40, "") for i in range(3)]
    far = Place("far", "far", 40.71, -74.00, "New York")
    assert outliers([*near, far]) == {"far"}
    assert outliers(near) == set()


def test_saved_places_with_impossible_coordinates_are_looked_up_again():
    from reinvent_planner.venues_measure import cached_places

    text = (
        "[places]\n"
        'a = { name = "A", lat = nan, lon = 1.0, label = "x" }\n'
        'b = { name = "B", lat = 500.0, lon = 1.0, label = "x" }\n'
        'c = { name = "C", lat = 37.0, lon = -122.0, label = "ok" }\n'
    )
    assert list(cached_places(text)) == ["c"]


def test_two_far_apart_venues_are_flagged():
    from reinvent_planner.venues_measure import Place, outliers

    sf = Place("SF", "sf", 37.78, -122.40, "")
    ny = Place("NY", "ny", 40.71, -74.00, "")
    near = Place("SF2", "sf2", 37.781, -122.401, "")
    assert outliers([sf, ny]) == {"sf", "ny"}
    assert outliers([sf, near]) == set()


def test_a_failed_write_keeps_your_old_table(catalog_with, osm, monkeypatch):
    catalog_with(list(PLACES))
    rip("venues", "measure", "-y")
    path = config_dir() / "venues" / f"{EVENT}.toml"
    original = path.read_text()
    import os

    real_fdopen = os.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        handle.write = lambda text: (_ for _ in ()).throw(OSError("disk full"))
        return handle

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    result = rip("venues", "measure", "--force", "-y")
    assert result.exit_code != 0
    assert path.read_text() == original  # untouched
    assert not list(path.parent.glob(".measure-*"))  # no temp file left behind
    assert not list(path.parent.glob(f"{EVENT}.toml.*.bak"))  # nothing moved aside


def test_long_walks_ask_before_saving(catalog_with, monkeypatch):
    class SlowOSM(FakeOSM):
        def __call__(self, request):
            if request.url.host == "routing.openstreetmap.de":
                return httpx.Response(
                    200, json={"routes": [{"distance": 9000.0, "duration": 4800.0}]}
                )
            return super().__call__(request)

    fake = SlowOSM()
    real = Measurer
    monkeypatch.setattr(
        venues_measure,
        "Measurer",
        lambda **kw: real(transport=httpx.MockTransport(fake), sleep=lambda s: None),
    )
    catalog_with(list(PLACES))
    result = rip("venues", "measure", input="y\ny\nn\n")
    assert "take over an hour" in result.output and result.exit_code == 1
    assert not (config_dir() / "venues" / f"{EVENT}.toml").exists()


@pytest.mark.parametrize(
    ("lat", "lon"),
    [("91", "0"), ("0", "-180.5"), ("nan", "0"), ("0", "inf"), ("-1e999", "0")],
)
def test_geocoder_answers_with_impossible_coordinates_are_ignored(lat, lon):
    def fake(request):
        return httpx.Response(200, json=[{"lat": lat, "lon": lon, "display_name": "Nowhere"}])

    with Measurer(transport=httpx.MockTransport(fake), sleep=lambda s: None) as measurer:
        assert measurer.geocode("Moscone West", None) is None


def test_geocoder_labels_lose_control_characters():
    def fake(request):
        label = "Moscone\x1b]8;;https://evil.example\x07 West\x1b[2J"
        return httpx.Response(200, json=[{"lat": "37.78", "lon": "-122.40", "display_name": label}])

    with Measurer(transport=httpx.MockTransport(fake), sleep=lambda s: None) as measurer:
        place = measurer.geocode("Moscone West", None)
    assert place.label == "Moscone]8;;https://evil.example West[2J"


def test_a_lone_surrogate_in_a_venue_name_still_writes_a_valid_table(tmp_path):
    place = venues_measure.Place("Hall \ud83d A", "hall_a", 37.78, -122.40, "Hall \udc00")
    other = venues_measure.Place("Hall B", "hall_b", 37.79, -122.41, "Hall B")
    text = venues_measure.build_table(EVENT, [place, other], {("hall_a", "hall_b"): (850, 11)})
    path = tmp_path / "table.toml"
    path.write_text(text, encoding="utf-8")  # would raise UnicodeEncodeError on a surrogate
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["names"]["hall_a"] == "Hall � A"
