"""Builds small map databases with the same schema as the real floor-map DBs."""

import sqlite3
from pathlib import Path

# id, x, y, floor, description, extra_info
Anchor = tuple[int, float, float, int, str, str | None]
# point_a_id, point_b_id, distance
Connection = tuple[int, int, float]


def build_map_db(
    db_path: Path, anchors: list[Anchor], connections: list[Connection]
) -> str:
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
        for anchor_id, x, y, floor, description, extra_info in anchors:
            conn.execute(
                "INSERT INTO anchor_point_coordinates VALUES (?, ?, ?, ?)",
                (anchor_id, x, y, floor),
            )
            conn.execute(
                "INSERT INTO anchor_point_description "
                "(anchor_point_id, name, description, extra_info) VALUES (?, ?, ?, ?)",
                (anchor_id, f"Anchor {anchor_id}", description, extra_info),
            )
        conn.executemany(
            "INSERT INTO connections (point_a_id, point_b_id, distance) VALUES (?, ?, ?)",
            connections,
        )
        conn.commit()
    return str(db_path)
