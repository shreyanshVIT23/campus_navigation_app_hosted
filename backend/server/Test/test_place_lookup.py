"""Toilet requests, cabin numbers and teacher names (Utils.place_lookup)."""

import os
import sqlite3

import pytest

from Utils import place_lookup
from Utils.place_lookup import CabinKey, Teacher
from Utils.route_utilary import get_room_no_by_cabin

TEACHERS = [
    Teacher("dr. asha rao", "G-01", "T110"),
    Teacher("dr. ravi rao", "G-02", "T110"),
    Teacher("dr. meena iyer", "A-101", "101"),
    Teacher("dr. kiran iyer", "G-205", "201"),
]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("female toilet", "female"),
        ("Ladies washroom", "female"),
        ("ladies", "female"),
        ("Women's restroom", "female"),
        ("girls toilet", "female"),
        ("male toilet", "male"),
        ("Gents", "male"),
        ("Men's washroom", "male"),
        ("boys bathroom", "male"),
        ("toilet", "any"),
        ("nearest washroom", "any"),
        ("WC", "any"),
    ],
)
def test_toilet_requests(text, kind):
    assert place_lookup.toilet_kind(text) == kind


@pytest.mark.parametrize(
    "text", ["101", "T202(A)", "G223", "Dr. Raman", "man", "Store", "G-12", "Lift"]
)
def test_rooms_and_names_are_not_toilet_requests(text):
    assert place_lookup.toilet_kind(text) is None


@pytest.mark.parametrize(
    ("description", "extra_info", "gender"),
    [
        ("B", "B120", "male"),
        ("B", "B121", "male"),
        ("G", "G108-A", "female"),
        ("G", "G108-B", "female"),
        ("W", "309(A)", "male"),
        ("W", "309(B)", "male"),
        ("W", "325", "female"),
        ("W", None, None),  # window gallery
        ("W", "", None),
        ("T", "T110", None),  # teacher room
        ("101", None, None),
    ],
)
def test_toilet_gender_of_map_labels(description, extra_info, gender):
    assert place_lookup.toilet_gender(description, extra_info) == gender


@pytest.mark.parametrize("text", ["G-01", "G1", "g-001", " G 1 ", "G01"])
def test_cabin_numbers_in_any_spelling(text):
    assert place_lookup.cabin_key(text) == CabinKey("G", 1)


@pytest.mark.parametrize("text", ["101", "AB-1", "G-1234", "T202(A)", "", "G-"])
def test_not_cabin_numbers(text):
    assert place_lookup.cabin_key(text) is None


def test_only_dashed_cabins_are_always_cabins():
    assert place_lookup.is_dashed_cabin("G-205")
    assert not place_lookup.is_dashed_cabin("G205")


def test_room_for_cabin():
    assert place_lookup.room_for_cabin(TEACHERS, "G1") == "T110"
    assert place_lookup.room_for_cabin(TEACHERS, "a-101") == "101"
    assert place_lookup.room_for_cabin(TEACHERS, "G-05") is None
    assert place_lookup.room_for_cabin(TEACHERS, "101") is None


def test_rooms_for_name_groups_teachers_by_room():
    assert place_lookup.rooms_for_name(TEACHERS, "rao") == {
        "T110": ["dr. asha rao", "dr. ravi rao"]
    }
    assert place_lookup.rooms_for_name(TEACHERS, "Kiran Iyer") == {
        "201": ["dr. kiran iyer"]
    }
    assert set(place_lookup.rooms_for_name(TEACHERS, "iyer")) == {"101", "201"}
    # Too short to mean anyone
    assert place_lookup.rooms_for_name(TEACHERS, "ra") == {}


@pytest.fixture
def teacher_db(tmp_path):
    db_path = tmp_path / "teachers.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE teachers (id INTEGER PRIMARY KEY, name TEXT, cabin_no TEXT,"
            " room_no TEXT, phone_number TEXT)"
        )
        conn.executemany(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES (?, ?, ?)",
            [*TEACHERS, ("dr. no room", "G-09", None)],
        )
        conn.commit()
    return str(db_path)


def test_load_teachers_skips_teachers_without_a_room(teacher_db):
    assert place_lookup.load_teachers(teacher_db) == TEACHERS


def test_load_teachers_without_a_database(tmp_path):
    assert place_lookup.load_teachers(tmp_path / "missing.db") == []
    assert place_lookup.load_teachers(None) == []


def test_load_teachers_sees_new_rows(teacher_db):
    assert len(place_lookup.load_teachers(teacher_db)) == 4
    with sqlite3.connect(teacher_db) as conn:
        conn.execute(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES ('new', 'G-03', '101')"
        )
        conn.commit()
    assert len(place_lookup.load_teachers(teacher_db)) == 5


def test_load_teachers_sees_a_write_that_keeps_the_modified_time(teacher_db):
    # Windows can give two quick writes the same modified time
    assert len(place_lookup.load_teachers(teacher_db)) == 4
    stat = os.stat(teacher_db)
    with sqlite3.connect(teacher_db) as conn:
        conn.execute("UPDATE teachers SET room_no = '201' WHERE cabin_no = 'G-01'")
        conn.commit()
    os.utime(teacher_db, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert os.stat(teacher_db).st_size == stat.st_size
    teachers = place_lookup.load_teachers(teacher_db)
    assert ("G-01", "201") in {(t.cabin_no, t.room_no) for t in teachers}


@pytest.mark.parametrize(
    ("typed", "room"), [("G-01", "T110"), ("G1", "T110"), ("g-001", "T110")]
)
def test_custom_process_cabin_lookup_accepts_any_spelling(teacher_db, typed, room):
    assert get_room_no_by_cabin(typed, teacher_db) == room


def test_custom_process_cabin_lookup_unknown_cabin(teacher_db):
    assert get_room_no_by_cabin("Z-9", teacher_db) is None
