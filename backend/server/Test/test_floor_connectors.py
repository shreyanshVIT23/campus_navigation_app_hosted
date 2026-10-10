import sqlite3

import pytest

from Utils import db_shortest_path_maker

# id, x, y, floor, description
ANCHORS = [
    # Floor 2: room 201 is right next to "Stairs 5", further from "Stairs 1"
    (1, 0.0, 0.0, 2, "201"),
    (2, 1.0, 0.0, 2, "Stairs 5"),
    (3, 10.0, 0.0, 2, "Stairs 1"),
    (4, 20.0, 0.0, 2, "Lift 1"),
    # Floor 1: no "Stairs 5"; "Stairs 1" exists; plus an unnumbered "Stairs"
    (5, 10.0, 5.0, 1, "Stairs 1"),
    (6, 0.0, 5.0, 1, "105"),
    (7, 5.0, 5.0, 1, "Stairs"),
    (8, 20.0, 5.0, 1, "Lift 1"),
]
CONNECTIONS = [
    (1, 2, 1.0),
    (1, 3, 10.0),
    (3, 4, 10.0),
    (5, 6, 10.0),
    (7, 6, 1.0),
    (8, 5, 10.0),
]


@pytest.fixture
def graph_db(tmp_path):
    db_path = tmp_path / "building.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE anchor_point_coordinates "
            "(id INTEGER PRIMARY KEY, x_coordinate REAL, y_coordinate REAL, floor INTEGER)"
        )
        conn.execute(
            "CREATE TABLE anchor_point_description (id INTEGER PRIMARY KEY, "
            "anchor_point_id INTEGER, name TEXT, description TEXT, extra_info TEXT)"
        )
        conn.execute(
            "CREATE TABLE connections (id INTEGER PRIMARY KEY, point_a_id INTEGER, "
            "point_b_id INTEGER, distance REAL)"
        )
        for anchor_id, x, y, floor, description in ANCHORS:
            conn.execute(
                "INSERT INTO anchor_point_coordinates VALUES (?, ?, ?, ?)",
                (anchor_id, x, y, floor),
            )
            conn.execute(
                "INSERT INTO anchor_point_description "
                "(anchor_point_id, name, description) VALUES (?, ?, ?)",
                (anchor_id, f"Anchor {anchor_id}", description),
            )
        conn.executemany(
            "INSERT INTO connections (point_a_id, point_b_id, distance) VALUES (?, ?, ?)",
            CONNECTIONS,
        )
        conn.commit()
    return str(db_path)


def test_uses_stairs_that_exist_on_both_floors(graph_db):
    route = db_shortest_path_maker.main(graph_db, "201", "105", "Stairs")
    assert route["complexity"] == "complex"
    (floor_a, leg_a), (floor_b, leg_b) = route["path"]
    assert (floor_a, floor_b) == (2, 1)
    # Via "Stairs 1" (the nearer "Stairs 5" does not exist on floor 1)
    assert leg_a == [(0.0, 0.0), (10.0, 0.0)]
    assert leg_b == [(10.0, 5.0), (0.0, 5.0)]
    assert route["total_weight"] == 20.0


def test_no_preference_picks_the_shortest_total_walk(graph_db):
    route = db_shortest_path_maker.main(graph_db, "201", "105")
    assert route["complexity"] == "complex"
    # Stairs 1: 10 + 10 = 20; Lift 1: 20 + 20 = 40
    assert route["total_weight"] == 20.0


def test_lift_preference_uses_the_shared_lift(graph_db):
    route = db_shortest_path_maker.main(graph_db, "201", "105", "Lift")
    assert route["complexity"] == "complex"
    (_, leg_a), (_, leg_b) = route["path"]
    assert leg_a[-1] == (20.0, 0.0)
    assert leg_b[0] == (20.0, 5.0)


def test_shared_connectors_are_matched_by_exact_name(graph_db):
    connectors = db_shortest_path_maker.shared_connectors(graph_db, 2, 1, "Stairs")
    # "Stairs 5" is missing on floor 1, and the unnumbered "Stairs" cannot be paired
    assert [name for name, _, _ in connectors] == ["Stairs 1"]


def test_stairs_preference_falls_back_to_the_lift_when_no_stairs_link(graph_db):
    with sqlite3.connect(graph_db) as conn:
        conn.execute(
            "UPDATE anchor_point_description SET description = 'Stairs 9' "
            "WHERE anchor_point_id = 5"
        )
        conn.commit()
    route = db_shortest_path_maker.main(graph_db, "201", "105", "Stairs")
    assert route["complexity"] == "complex"
    assert route["path"][0][1][-1] == (20.0, 0.0)  # Lift 1
    assert route.get("note") == (
        "No stairs connect floor 2 and floor 1, so this route uses the lift."
    )


def test_clear_error_when_nothing_links_the_floors(graph_db):
    with sqlite3.connect(graph_db) as conn:
        conn.execute(
            "UPDATE anchor_point_description SET description = description || ' x' "
            "WHERE anchor_point_id IN (5, 8)"
        )
        conn.commit()
    route = db_shortest_path_maker.main(graph_db, "201", "105", "Stairs")
    assert route["complexity"] == "failed"
    assert "No lifts or stairs connect floor 2 and floor 1" in route["error"]
