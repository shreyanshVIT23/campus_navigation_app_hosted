"""Propose lift/stairs names so that one name means one shaft on every floor.

Read-only. Each floor map is drawn in its own frame (floor 1 of AB-01 is drawn
upside-down), so positions cannot be compared directly. Rooms that sit above
each other share their last two digits (105 on floor 1, 205 on floor 2); they
give an affine fit from one floor's frame onto the reference floor's. Each lift
or stairs is then named after the nearest one of the same kind on the reference
floor. The output is a table to review before encoding it in
Utils/fix_connector_labels.py.

Usage (from backend/server):
    uv run python -m Utils.align_connectors [--building AB-01] [--reference 2]
"""

import argparse
import contextlib
import math
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

import numpy as np

from .building_handling import get_building_paths

ROOM_NUMBER = re.compile(r"\d{3}")
CONNECTOR_KINDS = ("Lift", "Stairs")
# A proposal further than this (map units) from any same-kind connector on the
# reference floor has no counterpart there
FAR = 25.0


class Fit(NamedTuple):
    # [x, y, 1] @ matrix -> [x', y'] in the reference floor's frame
    matrix: np.ndarray
    rooms_used: int
    rotation_degrees: float
    mirrored: bool
    median_error: float
    max_error: float

    def apply(self, x: float, y: float) -> tuple[float, float]:
        mapped = np.array([x, y, 1.0]) @ self.matrix
        return float(mapped[0]), float(mapped[1])


class Proposal(NamedTuple):
    floor: int
    x: float
    y: float
    current: str
    proposed: str
    distance: float


def floor_anchors(db_path, floor: int) -> list[tuple[str, float, float]]:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        return conn.execute(
            """
            SELECT TRIM(apd.description), apc.x_coordinate, apc.y_coordinate
            FROM anchor_point_coordinates apc
            JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
            WHERE apc.floor = ?
            """,
            (floor,),
        ).fetchall()


def room_centres(db_path, floor: int) -> dict[str, tuple[float, float]]:
    """Last two digits of each room number on the floor -> mean door position."""
    doors: dict[str, list[tuple[float, float]]] = {}
    for description, x, y in floor_anchors(db_path, floor):
        if ROOM_NUMBER.fullmatch(description) and description[0] == str(floor):
            doors.setdefault(description[1:], []).append((x, y))
    return {
        key: (
            sum(x for x, _ in spots) / len(spots),
            sum(y for _, y in spots) / len(spots),
        )
        for key, spots in doors.items()
    }


def fit_floor(db_path, floor: int, reference: int) -> Fit:
    source, target = room_centres(db_path, floor), room_centres(db_path, reference)
    shared = sorted(source.keys() & target.keys())
    if len(shared) < 3:
        raise ValueError(
            f"Need at least 3 rooms shared by floor {floor} and floor {reference};"
            f" found {len(shared)}."
        )
    a = np.array([[*source[key], 1.0] for key in shared])
    b = np.array([target[key] for key in shared])
    matrix, *_ = np.linalg.lstsq(a, b, rcond=None)
    errors = np.hypot(*(a @ matrix - b).T)
    linear = matrix[:2].T  # [[dx'/dx, dx'/dy], [dy'/dx, dy'/dy]]
    return Fit(
        matrix=matrix,
        rooms_used=len(shared),
        rotation_degrees=math.degrees(math.atan2(linear[1, 0], linear[0, 0])),
        mirrored=bool(np.linalg.det(linear) < 0),
        median_error=float(np.median(errors)),
        max_error=float(errors.max()),
    )


def connectors(db_path, floor: int) -> list[tuple[str, float, float]]:
    return [
        (name, x, y)
        for name, x, y in floor_anchors(db_path, floor)
        if name.startswith(CONNECTOR_KINDS)
    ]


def propose_names(db_path, floor: int, reference: int) -> list[Proposal]:
    """For each lift/stairs on `floor`, the nearest same-kind one on `reference`."""
    fit = fit_floor(db_path, floor, reference)
    on_reference = connectors(db_path, reference)
    proposals = []
    for name, x, y in sorted(connectors(db_path, floor)):
        kind = name.split()[0]
        mapped = fit.apply(x, y)
        candidates = [
            (math.dist(mapped, (rx, ry)), ref_name)
            for ref_name, rx, ry in on_reference
            if ref_name.split()[0] == kind
        ]
        if not candidates:
            continue
        distance, proposed = min(candidates)
        proposals.append(Proposal(floor, x, y, name, proposed, distance))
    return proposals


def main():
    parser = argparse.ArgumentParser(
        description="Propose consistent lift/stairs names per floor (read-only)."
    )
    parser.add_argument("--building", default="AB-01")
    parser.add_argument("--reference", type=int, default=2)
    args = parser.parse_args()

    db_path = get_building_paths(args.building)[0]
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        floors = [
            floor
            for (floor,) in conn.execute(
                "SELECT DISTINCT floor FROM anchor_point_coordinates ORDER BY floor"
            )
        ]

    for floor in floors:
        if floor == args.reference:
            continue
        try:
            fit = fit_floor(db_path, floor, args.reference)
        except ValueError as e:
            print(f"\nfloor {floor}: {e}")
            continue
        print(
            f"\nfloor {floor} -> floor {args.reference}: {fit.rooms_used} shared rooms,"
            f" rotation {fit.rotation_degrees:.0f} deg"
            f"{', MIRRORED' if fit.mirrored else ''},"
            f" fit error median {fit.median_error:.1f} / max {fit.max_error:.1f}"
        )
        proposals = propose_names(db_path, floor, args.reference)
        taken: dict[str, int] = {}
        for p in proposals:
            taken[p.proposed] = taken.get(p.proposed, 0) + 1
        for p in proposals:
            notes = []
            if p.distance > FAR:
                notes.append("NO COUNTERPART on the reference floor")
            if taken[p.proposed] > 1:
                notes.append("name proposed twice")
            change = "keep" if p.current == p.proposed else f"-> {p.proposed}"
            print(
                f"  ({p.x:6.1f}, {p.y:6.1f})  {p.current:9} {change:13}"
                f" off by {p.distance:5.1f}  {'; '.join(notes)}"
            )


if __name__ == "__main__":
    main()
