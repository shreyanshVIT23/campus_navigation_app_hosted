"""The independent route checker, and the engine checked against it.

The randomized test builds many small two-floor maps (rooms with several doors,
paired and unpaired lifts/stairs, walkways of real lengths) and requires the
engine to return a valid path exactly as short as the checker's own solve, for
every ordered pair of rooms and every preference.
"""

import itertools
import math
import random

import pytest
from map_fixtures import Anchor, Connection, build_map_db

from Utils import db_shortest_path_maker, route_audit
from Utils.route_check import MapData, same_length
from Utils.route_types import SimpleRoute

# ---- A hand-made map ----

# Floor 1: 101 -4- Stairs 1 ; 101 -3- 102 ; Floor 2: Stairs 1 -5- 201
ANCHORS: list[Anchor] = [
    (1, 0.0, 0.0, 1, "101", None),
    (2, 4.0, 0.0, 1, "Stairs 1", None),
    (3, 0.0, 3.0, 1, "102", None),
    (4, 4.0, 10.0, 2, "Stairs 1", None),
    (5, 4.0, 15.0, 2, "201", None),
]
CONNECTIONS: list[Connection] = [(1, 2, 4.0), (1, 3, 3.0), (4, 5, 5.0)]


@pytest.fixture
def small_map(tmp_path):
    return build_map_db(tmp_path / "small.db", ANCHORS, CONNECTIONS)


def no_teachers(tmp_path):
    return str(tmp_path / "no-teachers.db")


def test_engine_route_passes_the_checker(small_map, tmp_path):
    data = MapData(small_map)
    route = db_shortest_path_maker.main(
        small_map, "102", "201", None, no_teachers(tmp_path)
    )
    starts, ends = data.anchors_of("102"), data.anchors_of("201")
    assert data.route_problems(route, starts, ends, None) == []
    assert route["total_weight"] == data.shortest_length(starts, ends, None) == 12.0


def test_checker_catches_a_jump_between_unconnected_points(small_map):
    data = MapData(small_map)
    route: SimpleRoute = {
        "path": (1, [(0.0, 3.0), (4.0, 0.0)]),
        "total_weight": 5.0,
        "complexity": "simple",
        "error": None,
    }
    problems = data.route_problems(
        route, data.anchors_of("102"), data.anchors_of("Stairs 1"), None
    )
    assert any("no walkway" in problem for problem in problems)


def test_checker_catches_a_wrong_length(small_map):
    data = MapData(small_map)
    route: SimpleRoute = {
        "path": (1, [(0.0, 3.0), (0.0, 0.0)]),
        "total_weight": 2.0,
        "complexity": "simple",
        "error": None,
    }
    problems = data.route_problems(
        route, data.anchors_of("102"), data.anchors_of("101"), None
    )
    assert problems == ["reported length 2.000 but the path walks 3.000"]


def test_checker_catches_a_route_that_ends_elsewhere(small_map):
    data = MapData(small_map)
    route: SimpleRoute = {
        "path": (1, [(0.0, 3.0), (0.0, 0.0)]),
        "total_weight": 3.0,
        "complexity": "simple",
        "error": None,
    }
    problems = data.route_problems(
        route, data.anchors_of("102"), data.anchors_of("Stairs 1"), None
    )
    assert problems == ["does not end at the end room"]


def test_checker_catches_a_floor_change_against_the_preference(tmp_path):
    # Both kinds link the floors, so a stairs route is wrong when the lift was asked for
    anchors: list[Anchor] = [
        *ANCHORS,
        (6, 9.0, 0.0, 1, "Lift 1", None),
        (7, 9.0, 10.0, 2, "Lift 1", None),
    ]
    connections: list[Connection] = [*CONNECTIONS, (1, 6, 9.0), (7, 5, 7.0)]
    db_path = build_map_db(tmp_path / "with-lift.db", anchors, connections)
    data = MapData(db_path)
    route = db_shortest_path_maker.main(
        db_path, "101", "201", "Stairs", no_teachers(tmp_path)
    )
    problems = data.route_problems(
        route, data.anchors_of("101"), data.anchors_of("201"), "Lift"
    )
    assert problems == ["uses 'Stairs 1' against preference 'Lift'"]


def test_checker_falls_back_like_the_engine_when_no_lift_links(small_map):
    # Only stairs link the floors: the lift preference is met by the stairs
    data = MapData(small_map)
    starts, ends = data.anchors_of("101"), data.anchors_of("201")
    assert data.shortest_length(starts, ends, "Lift") == 9.0


def test_checker_says_no_route_when_floors_are_not_linked(tmp_path):
    anchors: list[Anchor] = [
        (1, 0.0, 0.0, 1, "101", None),
        (2, 0.0, 10.0, 2, "201", None),
    ]
    data = MapData(build_map_db(tmp_path / "unlinked.db", anchors, []))
    assert math.isinf(
        data.shortest_length(data.anchors_of("101"), data.anchors_of("201"), None)
    )


def test_map_data_problems_are_reported(tmp_path):
    anchors: list[Anchor] = [
        (1, 0.0, 0.0, 1, "101", None),
        (2, 3.0, 4.0, 1, "102", None),
        (3, 9.0, 9.0, 1, "103", None),  # no walkway
        (4, 0.0, 0.0, 2, "201", None),
        (5, 1.0, 0.0, 1, "Stairs", None),
        (6, 2.0, 0.0, 1, "Stairs", None),  # same name twice on a floor
        (7, 5.0, 5.0, 2, "Lift 7", None),  # exists on no other floor
    ]
    connections: list[Connection] = [
        (1, 2, 9.0),  # anchors are 5 apart
        (1, 4, 1.0),  # joins two floors
        (1, 5, 0.0),  # zero length
        (4, 7, 7.07),
    ]
    data = MapData(build_map_db(tmp_path / "bad.db", anchors, connections))
    problems = data.data_problems()
    assert "connection 1-2 is 9.0 long but the anchors are 5.00 apart" in problems
    assert "connection 1-4 joins two floors" in problems
    assert "connection 1-5 has distance 0.0" in problems
    assert "floor 1: '103' (anchor 3) is cut off from the main walkway" in problems
    assert (
        "floor 1: 'Stairs' is used more than once, so it cannot link floors" in problems
    )
    assert "floor 2: 'Lift 7' exists on no other floor" in problems


# ---- Randomized maps: the engine must always give the shortest valid path ----


def random_map(seed: int) -> tuple[list[Anchor], list[Connection]]:
    rng = random.Random(seed)
    anchors: list[Anchor] = []
    connections: list[Connection] = []
    next_id = itertools.count(1)
    for floor in (1, 2):
        # Distinct grid points so every anchor has unique coordinates
        points = rng.sample([(x, y) for x in range(10) for y in range(10)], 14)
        ids = []
        for x, y in points:
            anchor_id = next(next_id)
            ids.append(anchor_id)
            anchors.append(
                (anchor_id, float(x), float(y), floor, "Unique anchor point", None)
            )
        # A random spanning tree plus a few extra walkways (cycles)
        for i in range(1, len(ids)):
            j = rng.randrange(i)
            connections.append((ids[i], ids[j], 0.0))
        for _ in range(5):
            a, b = rng.sample(ids, 2)
            connections.append((a, b, 0.0))

        labels = [f"{floor}0{n}" for n in range(1, 5)]
        labels.append(labels[0])  # a room with two doors
        labels += ["Stairs 1", "Stairs 2", "Lift 1"]
        if floor == 2:
            labels.append("Stairs 3")  # exists on floor 2 only
        rng.shuffle(ids)
        for anchor_id, label in zip(ids, labels, strict=False):
            index = next(i for i, a in enumerate(anchors) if a[0] == anchor_id)
            anchor = anchors[index]
            anchors[index] = (anchor[0], anchor[1], anchor[2], anchor[3], label, None)

    by_id = {a[0]: a for a in anchors}
    connections = [
        (a, b, round(math.dist(by_id[a][1:3], by_id[b][1:3]), 3))
        for a, b, _ in connections
        if a != b
    ]
    return anchors, connections


@pytest.mark.parametrize("seed", range(25))
def test_engine_matches_the_independent_solve_on_random_maps(tmp_path, seed):
    anchors, connections = random_map(seed)
    db_path = build_map_db(tmp_path / f"random-{seed}.db", anchors, connections)
    data = MapData(db_path)
    rooms = sorted({a[4] for a in anchors if a[4][0].isdigit()})
    teachers = no_teachers(tmp_path)

    for preference in (None, "Stairs", "Lift"):
        for start, end in itertools.permutations(rooms, 2):
            route = db_shortest_path_maker.main(
                db_path, start, end, preference, teachers
            )
            starts, ends = data.anchors_of(start), data.anchors_of(end)
            expected = data.shortest_length(starts, ends, preference)
            name = f"seed {seed}: {start} -> {end} ({preference})"
            if math.isinf(expected):
                assert route["complexity"] == "failed", name
                continue
            assert route["complexity"] != "failed", f"{name}: {route['error']}"
            assert data.route_problems(route, starts, ends, preference) == [], name
            assert same_length(route["total_weight"], expected), (
                f"{name}: {route['total_weight']} != {expected}"
            )


def test_audit_judges_engine_answers(small_map, tmp_path):
    data = MapData(small_map)
    checks = list(route_audit.room_checks(data, None))
    assert {check["name"] for check in checks} >= {"101 -> 201", "201 -> 102"}
    for check in checks:
        route = db_shortest_path_maker.main(
            small_map, check["start"], check["end"], None, no_teachers(tmp_path)
        )
        assert route_audit.judge(data, check, route, None) == (False, None), check[
            "name"
        ]
