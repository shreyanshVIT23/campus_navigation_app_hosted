"""Routes on the real AB-01 map that worked at commit 21d7a6b must keep working.

Lengths were measured with that commit's code and database. A route may get
shorter (better connector pairing) but never longer or fail. Read-only: the
routing engine does not write SVGs or touch the database.
"""

import pytest

from Utils import db_shortest_path_maker
from Utils.building_handling import get_building_paths

# (start, end, preference) -> route length at 21d7a6b
HEAD_ROUTES = {
    ("101", "105", None): 234.3,
    ("101", "105", "Stairs"): 234.3,
    ("101", "105", "Lift"): 234.3,
    ("105", "201", None): 800.7,
    ("105", "201", "Stairs"): 800.7,
    ("105", "201", "Lift"): 961.6,
    ("301", "405", None): 300.5,
    ("301", "405", "Stairs"): 300.5,
    ("301", "405", "Lift"): 373.2,
    ("201", "305", None): 255.8,
    ("201", "305", "Stairs"): 255.8,
    ("201", "305", "Lift"): 364.7,
    ("101", "301", None): 938.3,
    ("101", "301", "Stairs"): 938.3,
    ("101", "301", "Lift"): 1136.3,
    ("501", "001", None): 235.2,
    ("501", "001", "Stairs"): 235.2,
    ("501", "001", "Lift"): 408.3,
    ("201", "105", "Lift"): 1208.9,
    ("502", "101", "Lift"): 1063.9,
}

# Broken at 21d7a6b (rooms with two doors, toilet-only labels); must work now
FIXED_ROUTES = [
    ("202", "106", None),
    ("201", "106", None),
    ("302", "106", "Stairs"),
    ("021", "105", None),
    ("201", "105", None),
    ("502", "101", "Stairs"),
]


@pytest.fixture(scope="module")
def ab01_db():
    return get_building_paths("AB-01")[0]


@pytest.mark.parametrize(("start", "end", "preference"), list(HEAD_ROUTES))
def test_routes_that_worked_at_head_still_work(ab01_db, start, end, preference):
    route = db_shortest_path_maker.main(ab01_db, start, end, preference)
    assert route["complexity"] != "failed", route["error"]
    head_length = HEAD_ROUTES[(start, end, preference)]
    # HEAD lengths are rounded to 0.1
    assert route["total_weight"] <= head_length + 0.05


def test_same_floor_route_is_unchanged(ab01_db):
    route = db_shortest_path_maker.main(ab01_db, "101", "105")
    assert route["complexity"] == "simple"
    assert route["total_weight"] == pytest.approx(234.3, abs=0.05)


@pytest.mark.parametrize(("start", "end", "preference"), FIXED_ROUTES)
def test_routes_broken_at_head_now_work(ab01_db, start, end, preference):
    route = db_shortest_path_maker.main(ab01_db, start, end, preference)
    assert route["complexity"] != "failed", route["error"]
    legs = [route["path"]] if route["complexity"] == "simple" else route["path"]
    assert all(points for _, points in legs)
    assert route["total_weight"] < float("inf")
