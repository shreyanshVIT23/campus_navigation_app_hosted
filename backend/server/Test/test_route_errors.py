"""Failed routes say what is wrong, and the API answers with the matching status.

input    -> 400: what was typed can't be used (unknown, ambiguous, same place, ...)
route    -> 422: the places are fine but the map has no way between them
internal -> 500: something broke (e.g. a damaged database)

The synthetic map (weights on the edges):

Floor 1:  101 -1- 102 -1- 103 -1- ... -1- 112   (rooms 101-112 in a row)
          Stairs 1 -1- 101
          130 (no walkway)
Floor 2:  201 -1- Stairs 1
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from map_fixtures import Anchor, Connection, build_map_db

from app import app
from Utils import db_shortest_path_maker, route_utilary
from Utils.route_types import FailedRoute, MultiBuildingRoute, SimpleRoute

ROOMS = [f"1{n:02d}" for n in range(1, 13)]
ANCHORS: list[Anchor] = [
    *((i, float(i), 0.0, 1, room, None) for i, room in enumerate(ROOMS, start=1)),
    (20, 0.0, 0.0, 1, "Stairs 1", None),
    (21, 50.0, 50.0, 1, "130", None),
    (30, 0.0, 10.0, 2, "Stairs 1", None),
    (31, 1.0, 10.0, 2, "201", None),
]
CONNECTIONS: list[Connection] = [
    *((i, i + 1, 1.0) for i in range(1, len(ROOMS))),
    (20, 1, 1.0),
    (30, 31, 1.0),
]
TEACHERS = [
    ("dr. anil jain", "G-01", "101"),
    ("dr. sunita jain", "G-02", "105"),
]


@pytest.fixture
def map_db(tmp_path):
    return build_map_db(tmp_path / "building.db", ANCHORS, CONNECTIONS)


@pytest.fixture
def teacher_db(tmp_path):
    db_path = tmp_path / "teachers.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE teachers (id INTEGER PRIMARY KEY, name TEXT, cabin_no TEXT,"
            " room_no TEXT, phone_number TEXT)"
        )
        conn.executemany(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES (?, ?, ?)", TEACHERS
        )
        conn.commit()
    return str(db_path)


@pytest.fixture
def route(map_db, teacher_db):
    def run(start, end, preference=None):
        return db_shortest_path_maker.main(map_db, start, end, preference, teacher_db)

    return run


def failure(result) -> FailedRoute:
    assert result["complexity"] == "failed", result
    return result


# ---- What the person typed ----


def test_unknown_place_says_what_can_be_typed(route):
    result = failure(route("zzz", "101"))
    assert result["cause"] == "input"
    assert "Start location 'zzz' not found" in result["error"]
    assert "room number" in result["error"]
    assert "teacher's name" in result["error"]
    assert "cabin number" in result["error"]


def test_unknown_end_names_the_end(route):
    result = failure(route("101", "zzz"))
    assert "End location 'zzz' not found" in result["error"]


def test_partial_room_number_lists_the_matches_and_asks_for_the_full_number(route):
    result = failure(route("10", "201"))
    assert result["cause"] == "input"
    assert "'10' matches several places: 101, 102," in result["error"]
    assert "full room number" in result["error"]


def test_a_long_list_of_matches_is_shortened(route):
    # "1" matches all twelve 1xx rooms and 130 on floor 1
    result = failure(route("1", "201"))
    assert "and 3 more" in result["error"]
    assert "112" not in result["error"]


def test_teacher_name_in_several_rooms_asks_for_the_full_name(route):
    result = failure(route("jain", "201"))
    assert result["cause"] == "input"
    assert "dr. anil jain (101)" in result["error"]
    assert "dr. sunita jain (105)" in result["error"]
    assert "full name or cabin number" in result["error"]


def test_same_place_asks_for_a_different_start_or_end(route):
    result = failure(route("G-01", "101"))
    assert result["cause"] == "input"
    assert "'G-01' and '101' are the same place" in result["error"]
    assert "different start or end" in result["error"]


def test_two_general_places_explain_why(route):
    result = failure(route("toilet", "Lift"))
    assert result["cause"] == "input"
    assert "'toilet' and 'Lift' are both general places" in result["error"]
    assert "Choose a room as the start or the end" in result["error"]


def test_missing_lift_on_the_floor_is_named(route):
    result = failure(route("101", "Lift"))
    assert result["cause"] == "input"
    assert "There is no lift on floor 1" in result["error"]


def test_missing_toilets_are_an_input_problem(route):
    result = failure(route("101", "female toilet"))
    assert result["cause"] == "input"
    assert "no female toilets in this building" in result["error"]


@pytest.mark.parametrize(("start", "end"), [("", "101"), ("101", "  ")])
def test_empty_places_are_an_input_problem(route, start, end):
    assert failure(route(start, end))["cause"] == "input"


# ---- The map has no way ----


def test_no_walkway_is_a_route_problem(route):
    result = failure(route("101", "130"))
    assert result["cause"] == "route"
    assert "No walkway connects '101' and '130' on floor 1" in result["error"]


# ---- Something broke ----


def test_a_damaged_database_is_an_internal_problem(tmp_path, teacher_db):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database" * 100)
    result = failure(
        db_shortest_path_maker.main(str(broken), "101", "102", None, teacher_db)
    )
    assert result["cause"] == "internal"


# ---- API status codes ----


@pytest.fixture
def api():
    # raise_server_exceptions=False so 500 responses can be inspected
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def failed(cause, message) -> FailedRoute:
    return {
        "path": None,
        "total_weight": None,
        "complexity": "failed",
        "error": message,
        "cause": cause,
    }


@pytest.mark.parametrize(
    ("cause", "status"), [("input", 400), ("route", 422), ("internal", 500)]
)
def test_process_path_status_follows_the_cause(api, monkeypatch, cause, status):
    monkeypatch.setattr(
        route_utilary, "make_svg", lambda *args: failed(cause, "explained")
    )
    response = api.post(
        "/process_path",
        json={"start": "101", "end": "102", "building": "AB-01", "preference": None},
    )
    assert response.status_code == status
    assert response.json() == {"detail": "explained"}


@pytest.mark.parametrize(
    ("cause", "status"), [("input", 400), ("route", 422), ("internal", 500)]
)
def test_multi_building_status_follows_the_cause(api, monkeypatch, cause, status):
    monkeypatch.setattr(
        route_utilary, "make_svg_multi", lambda *args: failed(cause, "explained")
    )
    response = api.post(
        "/multi_building_process_path",
        json={
            "start": "101",
            "end": "001",
            "building_name_1": "AB-01",
            "building_name_2": "Lab-Complex",
        },
    )
    assert response.status_code == status
    assert response.json() == {"detail": "explained"}


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        ("zzz", "Start location 'zzz' not found"),
        ("10", "'10' matches several places"),
    ],
)
def test_real_map_input_errors_are_400(api, start, expected):
    response = api.post(
        "/process_path",
        json={"start": start, "end": "201", "building": "AB-01", "preference": None},
    )
    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_multi_building_failure_keeps_the_cause_of_the_failed_leg(map_db, monkeypatch):
    leg: FailedRoute = failed("input", "Start location 'zzz' not found")
    ok: SimpleRoute = {
        "path": (0, [(0.0, 0.0)]),
        "total_weight": 0.0,
        "complexity": "simple",
        "error": None,
    }
    monkeypatch.setattr(
        db_shortest_path_maker,
        "get_building_entrance_exit",
        lambda _db, building, _place: ((0.0, 0.0), leg if building == "A" else ok),
    )
    result: MultiBuildingRoute | FailedRoute = (
        db_shortest_path_maker.multi_building_nav(
            map_db, "A", "zzz", map_db, "B", "101"
        )
    )
    assert result == leg
