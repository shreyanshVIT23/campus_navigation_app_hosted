"""Tests run against temporary copies of the data they write, never the real files."""

import contextlib
import sqlite3
from pathlib import Path

import pytest

from Utils.loader import env_variables

SERVER_DIR = Path(__file__).resolve().parent.parent
WRITABLE = (
    "db_path",
    "event_db",
    "user_db_path",
    "teacher_des",
    "audio_path",
)
READ_ONLY = ("floor_map", "image_assets", "teacher_data")


def teacher_rows(db_path: Path) -> list[tuple]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        return conn.execute("SELECT * FROM teachers").fetchall()


def teacher_count(db_path: Path) -> int:
    return len(teacher_rows(db_path))


@pytest.mark.parametrize("key", WRITABLE)
def test_writable_data_is_a_temporary_copy(key):
    assert not env_variables[key].is_relative_to(SERVER_DIR), env_variables[key]


@pytest.mark.parametrize("key", READ_ONLY)
def test_read_only_inputs_come_from_the_repo(key):
    assert env_variables[key].is_relative_to(SERVER_DIR), env_variables[key]


@pytest.mark.parametrize("database", ["ab01-01.db", "lab.db"])
def test_map_databases_are_copied(database):
    assert (env_variables["db_path"] / database).is_file()


def test_copies_hold_the_real_data():
    # Earlier tests may have added rows to the copy, never removed any
    real = SERVER_DIR / "DB" / env_variables["teacher_des"].name
    assert set(teacher_rows(real)) <= set(teacher_rows(env_variables["teacher_des"]))


def test_adding_a_teacher_leaves_the_real_database_alone(client, auth_headers):
    real = SERVER_DIR / "DB" / env_variables["teacher_des"].name
    real_before = teacher_count(real)
    copy_before = teacher_count(env_variables["teacher_des"])

    response = client.post(
        "/teachers",
        json={
            "name": "Dr. Isolation Check",
            "cabin_no": "Z-999",
            "room_no": "101",
            "phone_number": "0000000000",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert teacher_count(env_variables["teacher_des"]) == copy_before + 1
    assert teacher_count(real) == real_before
