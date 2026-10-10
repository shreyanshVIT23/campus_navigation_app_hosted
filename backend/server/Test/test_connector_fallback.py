"""A lift/stairs preference falls back to the other kind when it cannot be met.

Lab-Complex, for example, has no lifts: asking for the lift between floors
must still give a route, by stairs, and say so.
"""

import pytest
from map_fixtures import Anchor, Connection, build_map_db

from Utils import db_shortest_path_maker
from Utils.route_check import MapData, same_length


def two_floor_map(tmp_path, connectors: list[str]) -> str:
    """101 on floor 1 and 201 on floor 2, linked only by the given connectors."""
    anchors: list[Anchor] = [
        (1, 0.0, 0.0, 1, "101", None),
        (2, 0.0, 10.0, 2, "201", None),
    ]
    connections: list[Connection] = []
    for index, name in enumerate(connectors):
        low, high = 10 + 2 * index, 11 + 2 * index
        x = 5.0 * (index + 1)
        anchors += [(low, x, 0.0, 1, name, None), (high, x, 10.0, 2, name, None)]
        connections += [(1, low, x), (2, high, x)]
    return build_map_db(tmp_path / "two-floors.db", anchors, connections)


def route(db_path, preference, tmp_path):
    no_teachers = str(tmp_path / "no-teachers.db")
    return db_shortest_path_maker.main(db_path, "101", "201", preference, no_teachers)


def test_lift_preference_without_lifts_uses_the_stairs(tmp_path):
    db_path = two_floor_map(tmp_path, ["Stairs 1"])
    result = route(db_path, "Lift", tmp_path)
    assert result["complexity"] == "complex"
    assert result["path"][0][1][-1] == (5.0, 0.0)
    assert result.get("note") == (
        "No lift connects floor 1 and floor 2, so this route uses the stairs."
    )


def test_stairs_preference_without_stairs_uses_the_lift(tmp_path):
    db_path = two_floor_map(tmp_path, ["Lift 1"])
    result = route(db_path, "Stairs", tmp_path)
    assert result["complexity"] == "complex"
    assert result.get("note") == (
        "No stairs connect floor 1 and floor 2, so this route uses the lift."
    )


def test_preference_that_can_be_met_has_no_note(tmp_path):
    # The stairs are nearer, but the lift was asked for and exists
    db_path = two_floor_map(tmp_path, ["Stairs 1", "Lift 1"])
    result = route(db_path, "Lift", tmp_path)
    assert result["complexity"] == "complex"
    assert result["path"][0][1][-1] == (10.0, 0.0)
    assert "note" not in result


def test_no_preference_has_no_note(tmp_path):
    db_path = two_floor_map(tmp_path, ["Stairs 1"])
    assert "note" not in route(db_path, None, tmp_path)


@pytest.mark.parametrize("preference", [None, "Lift", "Stairs"])
def test_floors_with_no_lift_or_stairs_at_all(tmp_path, preference):
    result = route(two_floor_map(tmp_path, []), preference, tmp_path)
    assert result["complexity"] == "failed"
    assert "No lifts or stairs connect floor 1 and floor 2" in result["error"]


@pytest.mark.parametrize("preference", ["Lift", "Stairs"])
def test_checker_agrees_with_the_fallback(tmp_path, preference):
    other = {"Lift": "Stairs 1", "Stairs": "Lift 1"}[preference]
    db_path = two_floor_map(tmp_path, [other])
    data = MapData(db_path)
    result = route(db_path, preference, tmp_path)
    assert result["complexity"] == "complex", result["error"]
    starts, ends = data.anchors_of("101"), data.anchors_of("201")
    assert data.route_problems(result, starts, ends, preference) == []
    assert same_length(
        result["total_weight"], data.shortest_length(starts, ends, preference)
    )


def test_api_returns_the_note_for_lab_complex_lift(client):
    # Lab-Complex has stairs only
    response = client.post(
        "/process_path",
        json={
            "start": "101",
            "end": "201",
            "preference": "Lift",
            "building": "Lab-Complex",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["files"]) == {"start_floor", "end_floor"}
    assert body["note"] == (
        "No lift connects floor 1 and floor 2, so this route uses the stairs."
    )


def test_api_has_no_note_when_the_preference_is_met(client):
    response = client.post(
        "/process_path",
        json={
            "start": "101",
            "end": "201",
            "preference": "Stairs",
            "building": "Lab-Complex",
        },
    )
    assert response.status_code == 200
    assert "note" not in response.json()
