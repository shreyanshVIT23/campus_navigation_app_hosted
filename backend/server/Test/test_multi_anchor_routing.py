"""Rooms with several anchors (doors, toilet sections) and routes with no walkway.

The synthetic map (weights on the edges):

Floor 1:  105 -2- 106a -10- 106b -2- 101 -8- 120 -20- 120A
           |                 |        |
          15                 3       10
           |                 |        |
          107b            Stairs 1   107a          130 (no walkway)

Floor 2:  T202(A) -5- Stairs 1 -25- T202(B)
"""

import pytest
from map_fixtures import Anchor, Connection, build_map_db

from Utils import db_shortest_path_maker

ANCHORS: list[Anchor] = [
    (1, -2.0, 0.0, 1, "105", None),
    (2, 0.0, 0.0, 1, "106", None),
    (3, 10.0, 0.0, 1, "106", None),
    (4, 12.0, 0.0, 1, "101", None),
    (5, 20.0, 0.0, 1, "120", None),
    (6, 40.0, 0.0, 1, "120A", None),
    (7, 10.0, 3.0, 1, "Stairs 1", None),
    (8, 12.0, 10.0, 1, "107", None),
    (9, -2.0, 10.0, 1, "107", None),
    (10, 90.0, 90.0, 1, "130", None),
    (11, 0.0, 10.0, 2, "T", "T202(A)"),
    (12, 5.0, 10.0, 2, "Stairs 1", None),
    (13, 30.0, 10.0, 2, "T", "T202(B)"),
]
CONNECTIONS: list[Connection] = [
    (1, 2, 2.0),
    (2, 3, 10.0),
    (3, 4, 2.0),
    (4, 5, 8.0),
    (5, 6, 20.0),
    (3, 7, 3.0),
    (4, 8, 10.0),
    (1, 9, 15.0),
    (11, 12, 5.0),
    (12, 13, 25.0),
]


@pytest.fixture
def map_db(tmp_path):
    return build_map_db(tmp_path / "building.db", ANCHORS, CONNECTIONS)


# ---- Rooms with several doors ----


def test_route_to_a_room_with_two_doors_uses_the_nearer_door(map_db):
    route = db_shortest_path_maker.main(map_db, "101", "106")
    assert route["complexity"] == "simple"
    assert route["path"] == (1, [(12.0, 0.0), (10.0, 0.0)])
    assert route["total_weight"] == 2.0


def test_route_from_a_room_with_two_doors_uses_the_nearer_door(map_db):
    route = db_shortest_path_maker.main(map_db, "106", "105")
    assert route["complexity"] == "simple"
    assert route["path"] == (1, [(0.0, 0.0), (-2.0, 0.0)])
    assert route["total_weight"] == 2.0


def test_both_rooms_with_two_doors_use_the_closest_pair(map_db):
    # 106b -> 101 -> 107a = 12; via 106a -> 105 -> 107b = 17
    route = db_shortest_path_maker.main(map_db, "106", "107")
    assert route["complexity"] == "simple"
    assert route["path"] == (1, [(10.0, 0.0), (12.0, 0.0), (12.0, 10.0)])
    assert route["total_weight"] == 12.0


def test_two_door_room_on_another_floor(map_db):
    # T202(A) -> Stairs 1 = 5, then Stairs 1 -> 106b = 3
    route = db_shortest_path_maker.main(map_db, "202", "106", "Stairs")
    assert route["complexity"] == "complex"
    assert route["path"] == [
        (2, [(0.0, 10.0), (5.0, 10.0)]),
        (1, [(10.0, 3.0), (10.0, 0.0)]),
    ]
    assert route["total_weight"] == 8.0


def test_two_door_room_to_the_nearest_stairs(map_db):
    route = db_shortest_path_maker.main(map_db, "106", "Stairs")
    assert route["complexity"] == "simple"
    assert route["path"] == (1, [(10.0, 0.0), (10.0, 3.0)])
    assert route["total_weight"] == 3.0


# ---- Toilet-only labels ----


def test_toilet_only_label_resolves_to_the_nearest_section(map_db):
    route = db_shortest_path_maker.main(map_db, "Stairs", "202")
    assert route["complexity"] == "simple"
    assert route["path"] == (2, [(5.0, 10.0), (0.0, 10.0)])
    assert route["total_weight"] == 5.0


def test_resolve_candidates_keeps_every_section_of_a_toilet(map_db):
    floor, spots = db_shortest_path_maker.resolve_candidates(map_db, "202", 2)
    assert floor == 2
    assert sorted(spots) == [(0.0, 10.0), (30.0, 10.0)]


# ---- Label matching ----


def test_exact_label_beats_a_longer_label_with_the_same_prefix(map_db):
    # "120" must not also pick "120A" further down the corridor
    floor, spots = db_shortest_path_maker.resolve_candidates(map_db, "120", 1)
    assert (floor, spots) == (1, [(20.0, 0.0)])


def test_a_prefix_of_several_rooms_is_reported_as_ambiguous(map_db):
    route = db_shortest_path_maker.main(map_db, "10", "120")
    assert route["complexity"] == "failed"
    assert "'10' matches several places" in route["error"]


def test_unknown_room_is_reported(map_db):
    route = db_shortest_path_maker.main(map_db, "199", "101")
    assert route["complexity"] == "failed"
    assert "'199' not found" in route["error"]


# ---- No walkway ----


def test_room_with_no_walkway_fails_instead_of_an_empty_route(map_db):
    route = db_shortest_path_maker.main(map_db, "101", "130")
    assert route["complexity"] == "failed"
    assert "No walkway connects '101' and '130' on floor 1" in route["error"]


def test_shortest_between_returns_no_path_for_unreachable_targets(map_db):
    graph = db_shortest_path_maker.graph_converter.create_graph_from_db(map_db, 1)
    path, weight = db_shortest_path_maker.shortest_between(graph, [4], [10])
    assert path == []
    assert weight == float("inf")
