import csv
import io
import stat
import sys
from zoneinfo import ZoneInfo

import pytest
from conftest import make_session

from reinvent_planner.export_csv import COLUMNS, rows, safe_cell, to_csv, write_csv
from reinvent_planner.planner import item_from_session

LA = ZoneInfo("America/Los_Angeles")


def parse(text):
    return list(csv.reader(io.StringIO(text)))


def test_titles_with_commas_quotes_and_newlines_round_trip():
    s = make_session("a", title='Agents, "fast"\nand safe')
    table = parse(to_csv(rows([(None, s)], LA)))
    assert table[0] == list(COLUMNS)
    # Commas and quotes round-trip; a line break in a title is already a space (models.py).
    assert table[1][COLUMNS.index("Title")] == 'Agents, "fast" and safe'


def test_formula_like_cells_are_neutralized():
    for dangerous in ('=HYPERLINK("http://x")', "+1", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        assert safe_cell(dangerous) == "'" + dangerous
    assert safe_cell("Normal title") == "Normal title"
    assert safe_cell(None) == ""
    s = make_session("a", title="=cmd|' /C calc'!A0", speakers=[{"name": "@evil"}])
    row = parse(to_csv(rows([(None, s)], LA)))[1]
    assert row[COLUMNS.index("Title")].startswith("'=")
    assert row[COLUMNS.index("Speakers")] == "'@evil"


def test_row_contents():
    s = make_session("a", time="16:00", length="90", venue=None, room="Wynn/Encore | Level 1")
    item = item_from_session(s, LA, "favorite")
    row = dict(zip(COLUMNS, parse(to_csv(rows([(item, s)], LA, {"a": 2})))[1], strict=True))
    assert (row["Day"], row["Date"], row["Start"], row["End"]) == (
        "Tue",
        "2026-12-01",
        "16:00",
        "17:30",
    )
    assert row["Venue"] == "Wynn/Encore"
    assert row["On my list"] == "Favorite" and row["Rank"] == "2"
    assert row["Seats"] == "Available"


def test_untimed_and_all_day():
    tba = parse(to_csv(rows([(None, make_session("t", time="TBA"))], LA)))[1]
    assert tba[COLUMNS.index("Start")] == "TBA"
    all_day = make_session("d", isAllDaySession=True, sessionTime={"date": "2026-12-02"})
    row = parse(to_csv(rows([(None, all_day)], LA)))[1]
    assert row[COLUMNS.index("Start")] == "all day"


def test_formula_hardening_for_trimming_importers():
    for dangerous in (" =1+1", "\u00a0=1", "\u3000@x", "＝1+1", "＋1", "\n=1+1"):
        assert safe_cell(dangerous).startswith("'"), repr(dangerous)
    assert safe_cell("Talk - part 2") == "Talk - part 2"  # only a *leading* sign matters


def test_write_csv_is_atomic_owner_only_and_keeps_the_bom(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    path = tmp_path / "plan.csv"
    try:
        path.symlink_to(victim)
    except OSError:
        pytest.skip("can't create symlinks here")
    write_csv(path, "Title\r\nCafé\r\n")
    assert victim.read_text(encoding="utf-8") == "precious"
    assert not path.is_symlink()
    assert path.read_bytes() == "\ufeffTitle\r\nCafé\r\n".encode()
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
