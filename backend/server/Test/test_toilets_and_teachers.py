"""Routes to the nearest toilet of a kind, and to teachers by cabin or name.

The synthetic map (weights on the edges):

Floor 1:  G -5- 101 -10- B105 -20- W -5- 103
                 |  \\
                 5   3
                 |    \\
               T110  Stairs 1

Floor 2:  201 -3- Stairs 1        201 -4- G205

B = boys' toilet, G = girls' toilet; W is a window gallery (not a washroom),
except the listed washroom rooms such as 309(A) (boys) and 325 (girls).
T110 is a teacher room.
"""

import sqlite3

import pytest
from map_fixtures import Anchor, Connection, build_map_db

from Utils import db_shortest_path_maker

ANCHORS: list[Anchor] = [
    (1, -5.0, 0.0, 1, "G", "G101"),
    (2, 0.0, 0.0, 1, "101", None),
    (3, 10.0, 0.0, 1, "B", "B105"),
    (4, 30.0, 0.0, 1, "W", None),
    (5, 30.0, 5.0, 1, "103", None),
    (6, 0.0, 5.0, 1, "T", "T110"),
    (7, 0.0, -3.0, 1, "Stairs 1", None),
    (8, 0.0, 10.0, 2, "201", None),
    (9, 0.0, 7.0, 2, "Stairs 1", None),
    (10, 4.0, 10.0, 2, "G", "G205"),
]
CONNECTIONS: list[Connection] = [
    (1, 2, 5.0),
    (2, 3, 10.0),
    (3, 4, 20.0),
    (4, 5, 5.0),
    (2, 6, 5.0),
    (2, 7, 3.0),
    (8, 9, 3.0),
    (8, 10, 4.0),
]
TEACHERS = [
    ("dr. asha rao", "G-01", "T110"),
    ("dr. ravi rao", "G-02", "T110"),
    ("dr. meena iyer", "A-101", "103"),
    ("dr. kiran iyer", "G-205", "201"),
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


# ---- Toilets ----


@pytest.mark.parametrize("request_text", ["female toilet", "Ladies washroom", "ladies"])
def test_nearest_female_toilet_on_the_same_floor(route, request_text):
    result = route("101", request_text)
    assert result["complexity"] == "simple"
    assert result["path"] == (1, [(0.0, 0.0), (-5.0, 0.0)])
    assert result["total_weight"] == 5.0


@pytest.mark.parametrize(
    ("request_text", "toilet", "length"),
    [
        ("female toilet", (-5.0, 0.0), 40.0),  # G at the far end
        ("toilet", (10.0, 0.0), 25.0),  # B105, nearer than G
    ],
)
def test_a_window_gallery_is_not_a_toilet(route, request_text, toilet, length):
    # The W next to 103 (5 away) is a window gallery, never a toilet
    result = route("103", request_text)
    assert result["complexity"] == "simple"
    assert result["path"][1][-1] == toilet
    assert result["total_weight"] == length


@pytest.fixture
def floor_3_map(tmp_path):
    # Floor 3 of AB-01: washrooms are W-labelled rooms; one W is a window gallery
    anchors: list[Anchor] = [
        (1, 0.0, 0.0, 3, "301", None),
        (2, 1.0, 0.0, 3, "W", None),  # window gallery, the nearest W
        (3, 5.0, 0.0, 3, "W", "309(A)"),  # boys
        (4, 7.0, 0.0, 3, "W", "309(B)"),  # boys
        (5, 0.0, 8.0, 3, "W", "325"),  # girls
    ]
    connections: list[Connection] = [(1, 2, 1.0), (1, 3, 5.0), (3, 4, 2.0), (1, 5, 8.0)]
    return build_map_db(tmp_path / "floor-3.db", anchors, connections)


@pytest.mark.parametrize(
    ("request_text", "toilet", "length"),
    [
        ("male toilet", (5.0, 0.0), 5.0),  # 309(A)
        ("female toilet", (0.0, 8.0), 8.0),  # 325
        ("toilet", (5.0, 0.0), 5.0),  # nearest washroom, not the gallery at 1
    ],
)
def test_w_washroom_rooms_have_their_own_gender(
    floor_3_map, tmp_path, request_text, toilet, length
):
    result = db_shortest_path_maker.main(
        floor_3_map, "301", request_text, None, str(tmp_path / "no-teachers.db")
    )
    assert result["complexity"] == "simple"
    assert result["path"][1][-1] == toilet
    assert result["total_weight"] == length


@pytest.mark.parametrize("request_text", ["male toilet", "gents", "Men's washroom"])
def test_nearest_male_toilet(route, request_text):
    result = route("101", request_text)
    assert result["complexity"] == "simple"
    assert result["path"] == (1, [(0.0, 0.0), (10.0, 0.0)])
    assert result["total_weight"] == 10.0


def test_any_toilet_is_the_nearest_of_either_kind(route):
    result = route("101", "toilet")
    assert result["path"] == (1, [(0.0, 0.0), (-5.0, 0.0)])


def test_toilet_on_the_same_floor_is_preferred(route):
    result = route("201", "female toilet")
    assert result["complexity"] == "simple"
    assert result["path"] == (2, [(0.0, 10.0), (4.0, 10.0)])


def test_no_toilet_of_that_kind_on_the_floor_uses_another_floor(route):
    # Floor 2 has no male toilet: 201 -> Stairs 1 (3), Stairs 1 -> 101 -> B105 (13)
    result = route("201", "male toilet")
    assert result["complexity"] == "complex"
    assert result["path"] == [
        (2, [(0.0, 10.0), (0.0, 7.0)]),
        (1, [(0.0, -3.0), (0.0, 0.0), (10.0, 0.0)]),
    ]
    assert result["total_weight"] == 16.0


def test_route_from_a_toilet_to_a_room(route):
    result = route("male toilet", "201")
    assert result["complexity"] == "complex"
    assert result["path"] == [
        (1, [(10.0, 0.0), (0.0, 0.0), (0.0, -3.0)]),
        (2, [(0.0, 7.0), (0.0, 10.0)]),
    ]
    assert result["total_weight"] == 16.0


def test_a_toilet_asked_for_its_own_kind(route):
    result = route("B105", "male toilet")
    assert result["complexity"] == "failed"
    assert "'B105' is itself a male toilet" in result["error"]


def test_a_toilet_can_route_to_the_other_kind(route):
    result = route("B105", "female toilet")
    assert result["complexity"] == "simple"
    assert result["total_weight"] == 15.0


def test_two_toilet_requests_are_refused(route):
    result = route("female toilet", "male toilet")
    assert result["complexity"] == "failed"
    assert "Choose a room as the start or the end" in result["error"]


def test_toilet_and_lift_requests_are_refused(route):
    result = route("toilet", "Lift")
    assert result["complexity"] == "failed"


# ---- Teachers ----


@pytest.mark.parametrize("cabin", ["G-01", "G1", "g-001", "G01"])
def test_cabin_number_routes_to_the_teachers_room(route, cabin):
    by_cabin = route(cabin, "101")
    by_room = route("T110", "101")
    assert by_cabin["complexity"] == "simple"
    assert by_cabin["path"] == by_room["path"]


def test_exact_map_label_beats_a_cabin_number(route):
    # "G205" is the toilet on floor 2; "G-205" is dr. kiran iyer's cabin in 201
    toilet = route("G205", "201")
    assert toilet["path"] == (2, [(4.0, 10.0), (0.0, 10.0)])
    cabin = route("G-205", "101")
    assert cabin["complexity"] == "complex"
    assert cabin["path"][0][1][0] == (0.0, 10.0)


def test_teacher_name_routes_to_their_room(route):
    result = route("rao", "101")
    assert result["path"] == (1, [(0.0, 5.0), (0.0, 0.0)])


def test_teacher_name_matching_several_rooms_is_ambiguous(route):
    result = route("iyer", "101")
    assert result["complexity"] == "failed"
    assert "matches teachers in several rooms" in result["error"]
    assert "dr. meena iyer (103)" in result["error"]


def test_unknown_cabin_is_reported(route):
    result = route("G-05", "101")
    assert result["complexity"] == "failed"
    assert "'G-05' not found" in result["error"]


def test_two_cabins_in_the_same_room(route):
    result = route("G-01", "G-02")
    assert result["complexity"] == "failed"
    assert "'G-01' and 'G-02' are the same place" in result["error"]


def test_teacher_to_the_nearest_toilet(route):
    result = route("G-01", "toilet")
    # T110 -> 101 (5) -> G (5)
    assert result["total_weight"] == 10.0


# ---- Input handling ----


@pytest.mark.parametrize(("start", "end"), [(" 101 ", "103"), ("101", "  103")])
def test_whitespace_around_labels_is_ignored(route, start, end):
    assert route(start, end)["complexity"] == "simple"


def test_labels_are_case_insensitive(route):
    assert route("t110", "101")["path"] == route("T110", "101")["path"]


@pytest.mark.parametrize(("start", "end"), [("", "101"), ("101", "   ")])
def test_empty_locations_are_refused(route, start, end):
    result = route(start, end)
    assert result["complexity"] == "failed"
    assert "both required" in result["error"]


@pytest.mark.parametrize("preference", ["lift", "LIFT", "STAIRS", "stairs"])
def test_preference_is_case_insensitive(route, preference):
    # Only stairs connect the floors: "lift" falls back to them and says so
    result = route("201", "101", preference)
    assert result["complexity"] == "complex"
    if preference.lower() == "lift":
        assert "uses the stairs" in result.get("note", "")
    else:
        assert "note" not in result


def test_lowercase_stairs_request_finds_the_nearest_stairs(route):
    result = route("101", "stairs")
    assert result["path"] == (1, [(0.0, 0.0), (0.0, -3.0)])
