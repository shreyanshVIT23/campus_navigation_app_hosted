"""What a typed place means besides a map label: a toilet request or a teacher.

Toilets on the map: B = boys' (male), G = girls' (female). W is a window gallery,
except a few W-labelled rooms that are washrooms (WASHROOM_ROOMS). Teachers live
in the teacher database with a cabin number (e.g. "G-12", unrelated to the G
toilets) and the map room that holds the cabin (e.g. "T022").
"""

import contextlib
import functools
import re
import sqlite3
from pathlib import Path
from typing import Literal, NamedTuple

ToiletKind = Literal["male", "female", "any"]
Gender = Literal["male", "female"]

# Map descriptions that mark a toilet by themselves
TOILET_DESCRIPTIONS: dict[str, Gender] = {"B": "male", "G": "female"}
# W normally marks a window gallery; these W-labelled rooms are washrooms
# (AB-01 floor 3, confirmed by the user)
WASHROOM_ROOMS: dict[str, Gender] = {
    "309(A)": "male",
    "309(B)": "male",
    "325": "female",
}
# Every map description that can mark a toilet
TOILET_MARKS = (*TOILET_DESCRIPTIONS, "W")
# Teacher rooms the map labels differently (confirmed by the user); used only
# when the map has no label for the teacher's room itself
ROOM_ALIASES = {"T110": "110"}


def room_alias(room: str) -> str | None:
    """The map's label for a room it doesn't know by this name, or None."""
    return ROOM_ALIASES.get(room.strip().upper())


def toilet_gender(description: str, extra_info: str | None) -> Gender | None:
    """Whether a map anchor is a male or female toilet, or not a toilet (None)."""
    description = description.strip()
    if description in TOILET_DESCRIPTIONS:
        return TOILET_DESCRIPTIONS[description]
    if description == "W":
        return WASHROOM_ROOMS.get((extra_info or "").strip())
    return None


def serves(kind: ToiletKind, gender: Gender | None) -> bool:
    """Whether a toilet of `gender` answers a request for `kind`."""
    return gender is not None and kind in ("any", gender)


TOILET_WORDS = {
    "toilet",
    "toilets",
    "washroom",
    "washrooms",
    "restroom",
    "restrooms",
    "bathroom",
    "bathrooms",
    "lavatory",
    "wc",
    "loo",
}
FEMALE_WORDS = {
    "female",
    "females",
    "girl",
    "girls",
    "lady",
    "ladies",
    "woman",
    "women",
}
MALE_WORDS = {"male", "males", "boy", "boys", "gent", "gents", "man", "men"}

# "G-12", "G12", "g 012": one letter, an optional dash, up to three digits
CABIN_PATTERN = re.compile(r"([A-Za-z])\s*(-?)\s*(\d{1,3})")


def toilet_kind(text: str) -> ToiletKind | None:
    """The toilet asked for ("female toilet", "gents", "washroom"), or None."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    female, male = bool(words & FEMALE_WORDS), bool(words & MALE_WORDS)
    asks_for_toilet = bool(words & TOILET_WORDS) or (
        len(words) == 1 and words <= {"gents", "ladies"}
    )
    if not asks_for_toilet:
        return None
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return "any"


class CabinKey(NamedTuple):
    letter: str
    number: int


def cabin_key(text: str) -> CabinKey | None:
    """A cabin number in any common spelling ("G-01", "G1", "g-001"), or None."""
    match = CABIN_PATTERN.fullmatch(text.strip())
    if match is None:
        return None
    return CabinKey(match.group(1).upper(), int(match.group(3)))


def is_dashed_cabin(text: str) -> bool:
    """ "G-12" is always a cabin; "G12" may also be part of a map label."""
    match = CABIN_PATTERN.fullmatch(text.strip())
    return match is not None and match.group(2) == "-"


class Teacher(NamedTuple):
    name: str
    cabin_no: str | None
    room_no: str | None


def load_teachers(teacher_db: str | Path | None) -> list[Teacher]:
    """Every teacher with a room; empty when there is no teacher database."""
    if teacher_db is None or not Path(teacher_db).is_file():
        return []
    path = Path(teacher_db).resolve()
    # Re-read only when the file changes (teachers can be added at runtime)
    return list(_read_teachers(path, _file_version(path)))


def _file_version(path: Path) -> tuple[int, int, bytes]:
    """Changes whenever the database is written.

    The modified time alone can repeat for quick writes (Windows clock ticks), so
    the SQLite header's file change counter (offset 24) is included: rollback-
    journal databases increment it on every committed write.
    https://www.sqlite.org/fileformat.html#file_change_counter
    """
    stat = path.stat()
    with path.open("rb") as file:
        header = file.read(28)
    return stat.st_mtime_ns, stat.st_size, header[24:28]


@functools.lru_cache(maxsize=8)
def _read_teachers(path: Path, _version: tuple[int, int, bytes]) -> tuple[Teacher, ...]:
    uri = f"file:{path.as_posix()}?mode=ro"
    # sqlite3.Error propagates to the routing engine, which reports it
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT TRIM(name), TRIM(cabin_no), TRIM(room_no) FROM teachers"
            " WHERE room_no IS NOT NULL AND TRIM(room_no) != ''"
        ).fetchall()
    return tuple(Teacher(name, cabin_no, room_no) for name, cabin_no, room_no in rows)


def room_for_cabin(teachers: list[Teacher], text: str) -> str | None:
    """The map room holding the cabin `text`, or None if no teacher has it."""
    key = cabin_key(text)
    if key is None:
        return None
    rooms = {
        teacher.room_no
        for teacher in teachers
        if teacher.cabin_no and cabin_key(teacher.cabin_no) == key and teacher.room_no
    }
    # A cabin belongs to one room; if the data disagrees, let the caller report it
    return rooms.pop() if len(rooms) == 1 else None


def rooms_for_name(teachers: list[Teacher], text: str) -> dict[str, list[str]]:
    """Map room -> teacher names, for teachers whose name contains `text`."""
    needle = " ".join(re.findall(r"[a-z]+", text.lower()))
    if len(needle) < 3:
        return {}
    rooms: dict[str, list[str]] = {}
    for teacher in teachers:
        name = " ".join(re.findall(r"[a-z]+", teacher.name.lower()))
        if needle in name and teacher.room_no:
            names = rooms.setdefault(teacher.room_no, [])
            if teacher.name not in names:
                names.append(teacher.name)
    return rooms
