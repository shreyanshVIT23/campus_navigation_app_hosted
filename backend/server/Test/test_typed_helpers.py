import sqlite3

import pandas as pd
import pytest

from Chatbot import audio_to_text
from Events_Management.event_types import (
    attendee_from_row,
    event_from_row,
    event_with_count_from_row,
)
from Events_Management.helpers import parse_capacity
from Utils.excel_to_db import excel_to_sqlite
from Utils.route_utilary import building_floor_files

# ============================================================
# EVENT ROW CONVERTERS
# ============================================================


@pytest.fixture
def event_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE events (
            id TEXT, title TEXT, date TEXT, time TEXT, location TEXT,
            capacity INTEGER, description TEXT, created_at TEXT, user_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE registrations (
            id TEXT, event_id TEXT, attendee_name TEXT, attendee_email TEXT,
            registration_date TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("e1", "Talk", "2026-10-10", "10:00", "Hall", 30, None, "now", 7),
    )
    conn.execute(
        "INSERT INTO registrations VALUES (?, ?, ?, ?, ?)",
        ("r1", "e1", "Alice", "alice@example.com", "2026-10-09"),
    )
    yield conn
    conn.close()


def test_event_from_row_has_exact_keys(event_rows):
    row = event_rows.execute("SELECT * FROM events").fetchone()
    event = event_from_row(row)
    assert event == {
        "id": "e1",
        "title": "Talk",
        "date": "2026-10-10",
        "time": "10:00",
        "location": "Hall",
        "capacity": 30,
        "description": None,
        "created_at": "now",
        "user_id": 7,
    }


def test_event_with_count_from_row_adds_registered(event_rows):
    row = event_rows.execute(
        """
        SELECT events.*, COUNT(registrations.id) as registered
        FROM events LEFT JOIN registrations ON events.id = registrations.event_id
        GROUP BY events.id
        """
    ).fetchone()
    event = event_with_count_from_row(row)
    assert event["registered"] == 1
    assert event["title"] == "Talk"


def test_attendee_from_row(event_rows):
    row = event_rows.execute(
        "SELECT id, attendee_name, attendee_email, registration_date FROM registrations"
    ).fetchone()
    assert attendee_from_row(row) == {
        "id": "r1",
        "attendee_name": "Alice",
        "attendee_email": "alice@example.com",
        "registration_date": "2026-10-09",
    }


# ============================================================
# CAPACITY PARSING
# ============================================================


@pytest.mark.parametrize(
    "value, expected",
    [(25, 25), (0, 0), ("25", 25), (" 7 ", 7), (-1, None), ("fifty", None)],
)
def test_parse_capacity_values(value, expected):
    assert parse_capacity(value) == expected


@pytest.mark.parametrize("value", [None, True, 2.5, "", "-3"])
def test_parse_capacity_rejects(value):
    assert parse_capacity(value) is None


# ============================================================
# ROUTE -> FLOOR FILES
# ============================================================


def test_building_floor_files_simple():
    files = building_floor_files(
        "AB-01",
        {
            "path": (3, [(0.0, 0.0)]),
            "total_weight": 1.0,
            "complexity": "simple",
            "error": None,
        },
    )
    assert files == {"start_floor": "/load_shortest_path_svg?floor=3&building=AB-01"}


def test_building_floor_files_complex():
    files = building_floor_files(
        "Lab-Complex",
        {
            "path": [(0, [(0.0, 0.0)]), (2, [(1.0, 1.0)])],
            "total_weight": 4.0,
            "complexity": "complex",
            "error": None,
        },
    )
    assert files == {
        "start_floor": "/load_shortest_path_svg?floor=0&building=Lab-Complex",
        "end_floor": "/load_shortest_path_svg?floor=2&building=Lab-Complex",
    }


def test_building_floor_files_carry_the_route_id():
    route_id = "0123456789abcdef0123456789abcdef"
    files = building_floor_files(
        "AB-01",
        {
            "path": [(1, [(0.0, 0.0)]), (2, [(1.0, 1.0)])],
            "total_weight": 4.0,
            "complexity": "complex",
            "error": None,
            "map_id": route_id,
        },
    )
    assert files == {
        "start_floor": f"/load_shortest_path_svg?floor=1&building=AB-01&route={route_id}",
        "end_floor": f"/load_shortest_path_svg?floor=2&building=AB-01&route={route_id}",
    }


def test_building_floor_files_failed_route_is_empty():
    files = building_floor_files(
        "AB-01",
        {
            "path": None,
            "total_weight": None,
            "complexity": "failed",
            "error": "boom",
            "cause": "internal",
        },
    )
    assert files == {}


# ============================================================
# NLTK DATA
# ============================================================


def test_ensure_nltk_data_downloads_only_missing(monkeypatch):
    available = {"corpora/stopwords"}
    downloaded = []

    def fake_find(resource_path):
        if resource_path not in available:
            raise LookupError(resource_path)
        return resource_path

    def fake_download(package, download_dir=None, quiet=False, raise_on_error=False):
        downloaded.append((package, download_dir, quiet, raise_on_error))
        return True

    monkeypatch.setattr(audio_to_text.nltk.data, "find", fake_find)
    monkeypatch.setattr(audio_to_text.nltk, "download", fake_download)

    audio_to_text.ensure_nltk_data("some_dir")
    assert downloaded == [("punkt_tab", "some_dir", True, True)]


def test_nltk_resources_cover_tokenizer_and_stopwords():
    assert set(audio_to_text.NLTK_RESOURCES.values()) == {"punkt_tab", "stopwords"}


# ============================================================
# EXCEL -> SQLITE
# ============================================================


def test_excel_to_sqlite_renames_and_inserts(tmp_path):
    excel_file = tmp_path / "teachers.xlsx"
    db_file = tmp_path / "teachers.db"

    pd.DataFrame(
        {
            "Room No.": ["AB-301A", "bad-room-x"],
            "Cabin No.": ["C-1", "C-2"],
            "Name": ["Ravi Verma", "Nobody"],
            "Mobile No.": ["999", "888"],
            "Status": ["Active", "Active"],
        }
    ).to_excel(excel_file, index=False)

    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE anchor_point_description (extra_info TEXT)")
        conn.execute("INSERT INTO anchor_point_description VALUES ('T301(A)')")
        conn.commit()

    excel_to_sqlite(excel_file, db_file)

    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT name, cabin_no, room_no, phone_number FROM teachers"
        ).fetchall()
    assert rows == [("Ravi Verma", "C-1", "T301(A)", "999")]


def test_excel_to_sqlite_missing_file_is_reported(tmp_path, capsys):
    excel_to_sqlite(tmp_path / "missing.xlsx", tmp_path / "teachers.db")
    assert "Error reading Excel file" in capsys.readouterr().out
