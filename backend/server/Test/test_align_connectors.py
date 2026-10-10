"""Fitting one floor map onto another to name lifts/stairs consistently."""

import math

import pytest
from map_fixtures import Anchor, build_map_db

from Utils import align_connectors

# Floor 2 is the reference. Floor 1 is the same building drawn rotated 180°
# about (50, 50) and shifted by (+7, -3): x' = 107 - x, y' = 97 - y.
ROOMS = {"05": (10.0, 20.0), "06": (80.0, 15.0), "07": (60.0, 90.0), "08": (20.0, 70.0)}
FLOOR_2_CONNECTORS = {
    "Stairs 1": (5.0, 95.0),  # bottom-left
    "Stairs 2": (5.0, 5.0),  # top-left
    "Lift 1": (95.0, 95.0),
}


def rotated(point: tuple[float, float]) -> tuple[float, float]:
    return (107.0 - point[0], 97.0 - point[1])


@pytest.fixture
def map_db(tmp_path):
    anchors: list[Anchor] = []
    next_id = 1
    for suffix, point in ROOMS.items():
        anchors.append((next_id, *point, 2, f"2{suffix}", None))
        anchors.append((next_id + 1, *rotated(point), 1, f"1{suffix}", None))
        next_id += 2
    # Floor 1 calls its connectors by other (wrong) names
    floor_1_names = {"Stairs 1": "Stairs 9", "Stairs 2": "Stairs", "Lift 1": "Lift"}
    for name, point in FLOOR_2_CONNECTORS.items():
        anchors.append((next_id, *point, 2, name, None))
        anchors.append((next_id + 1, *rotated(point), 1, floor_1_names[name], None))
        next_id += 2
    return build_map_db(tmp_path / "building.db", anchors, [])


def test_fit_finds_the_rotation(map_db):
    fit = align_connectors.fit_floor(map_db, floor=1, reference=2)
    assert fit.rooms_used == 4
    assert math.isclose(abs(fit.rotation_degrees), 180.0, abs_tol=0.5)
    assert fit.median_error < 1e-6


def test_connectors_get_the_reference_floor_names(map_db):
    proposals = align_connectors.propose_names(map_db, floor=1, reference=2)
    by_current = {p.current: p.proposed for p in proposals}
    assert by_current == {
        "Stairs 9": "Stairs 1",
        "Stairs": "Stairs 2",
        "Lift": "Lift 1",
    }
    assert all(p.distance < 1e-6 for p in proposals)


def test_a_lift_is_never_matched_to_stairs(map_db):
    proposals = align_connectors.propose_names(map_db, floor=1, reference=2)
    for proposal in proposals:
        assert proposal.proposed.split()[0] == proposal.current.split()[0]


def test_too_few_shared_rooms(tmp_path):
    anchors: list[Anchor] = [
        (1, 0.0, 0.0, 1, "105", None),
        (2, 1.0, 1.0, 2, "205", None),
    ]
    db_path = build_map_db(tmp_path / "tiny.db", anchors, [])
    with pytest.raises(ValueError, match="at least 3 rooms"):
        align_connectors.fit_floor(db_path, floor=1, reference=2)
