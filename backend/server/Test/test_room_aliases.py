"""Teacher rooms the map knows by another name: T110 is the map's room 110.

The synthetic map (weights on the edges):

Floor 1:  101 -6- 110        (no "T110" on this map)
"""

import sqlite3

import pytest
from map_fixtures import Anchor, Connection, build_map_db

from Utils import db_shortest_path_maker, place_lookup, route_audit
from Utils.route_check import MapData

ANCHORS: list[Anchor] = [
    (1, 0.0, 0.0, 1, "101", None),
    (2, 6.0, 0.0, 1, "110", None),
]
CONNECTIONS: list[Connection] = [(1, 2, 6.0)]
TEACHERS = [("dr. divya haridas", "C-110", "T110")]
FROM_110 = (1, [(6.0, 0.0), (0.0, 0.0)])


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


@pytest.mark.parametrize(
    ("room", "alias"), [("T110", "110"), ("t110", "110"), (" T110 ", "110")]
)
def test_room_alias(room, alias):
    assert place_lookup.room_alias(room) == alias


@pytest.mark.parametrize("room", ["110", "T004", "101", ""])
def test_rooms_without_an_alias(room):
    assert place_lookup.room_alias(room) is None


@pytest.mark.parametrize("start", ["divya", "C-110", "T110", "t110"])
def test_teacher_room_missing_from_the_map_routes_to_its_alias(
    map_db, teacher_db, start
):
    result = db_shortest_path_maker.main(map_db, start, "101", None, teacher_db)
    assert result["complexity"] == "simple", result["error"]
    assert result["path"] == FROM_110
    assert result["total_weight"] == 6.0


def test_alias_is_not_used_when_the_map_has_the_room(tmp_path, teacher_db):
    # A map with its own "T110" T-room keeps routing there
    anchors: list[Anchor] = [*ANCHORS, (3, 0.0, 4.0, 1, "T", "T110")]
    map_db = build_map_db(
        tmp_path / "with-t110.db", anchors, [*CONNECTIONS, (1, 3, 4.0)]
    )
    result = db_shortest_path_maker.main(map_db, "divya", "101", None, teacher_db)
    assert result["path"] == (1, [(0.0, 4.0), (0.0, 0.0)])


def test_audit_checks_the_teacher_at_the_alias_room(map_db):
    data = MapData(map_db)
    teachers = [place_lookup.Teacher(*row) for row in TEACHERS]
    checks = list(route_audit.teacher_checks(data, teachers, None))
    assert checks, "the T110 teacher must be audited"
    assert all(data.anchors_of("110") in (c["starts"], c["ends"]) for c in checks)
