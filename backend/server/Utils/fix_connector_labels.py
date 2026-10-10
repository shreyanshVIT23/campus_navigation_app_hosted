"""Data fix: name AB-01's lifts/stairs after the same shafts on other floors.

Floors are linked by lift/stairs name ("Stairs 1" on floor 2 is "Stairs 1" on
floor 1). The floor-1 map is drawn upside-down compared with floors 0, 2, 4 and 5
(Utils.align_connectors measures a 173 deg rotation from the shared room
numbers), so its corners were named as if it were not:

  floor-1 position     committed    -> physical corner   new name
  top-right stairs     "Stairs"        bottom-left       Stairs 1
  bottom-right stairs  "Stairs"        top-left          Stairs 2
  top-left stairs      "Stairs 2"      bottom-right      Stairs 3
  bottom-left stairs   "Stairs 1"      top-right         Stairs 4
  top-middle stairs    "Stairs"        bottom-middle     Stairs 5
  top-right lift       "Lift"          bottom-left       Lift 1
  top-left lift        "Lift 2"        bottom-right      Lift 3
  bottom-left lift     "Lift 1"        top-right         (none: see below)

An earlier version of this fix named the right-hand stairs "Stairs 4" (top-right)
and "Stairs 3" (bottom-right); those labels are accepted as current too.

Floor 3 is drawn rotated ~90 deg and its names already match.

AB-01 has only Lift 1/2/3 on every floor. The extra lift drawn at floor 1's
bottom-left and floor 3's bottom-right is a drawing mistake (confirmed by the
user), so those anchors lose their label: they stay as plain walkway points.

Usage (from backend/server):
    uv run python -m Utils.fix_connector_labels            # dry run
    uv run python -m Utils.fix_connector_labels --apply    # write the changes
"""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

from typing_extensions import TypedDict

from .building_handling import get_building_paths

# Coordinates come from the floor-map SVG and are stored with 1 decimal place
COORDINATE_TOLERANCE = 0.05


class LabelFix(NamedTuple):
    floor: int
    x: float
    y: float
    # Labels the anchor may carry now; anything else is left alone
    current: tuple[str, ...]
    # None removes the label; the point stays on the walkway
    new: str | None


class FixReport(TypedDict):
    fix: str
    status: str


AB01_FIXES = [
    LabelFix(1, 504.4, 101.1, ("Stairs", "Stairs 4"), "Stairs 1"),
    LabelFix(1, 506.3, 485.3, ("Stairs", "Stairs 3"), "Stairs 2"),
    LabelFix(1, 99.9, 107.3, ("Stairs 2",), "Stairs 3"),
    LabelFix(1, 97.8, 490.7, ("Stairs 1",), "Stairs 4"),
    LabelFix(1, 342.9, 147.5, ("Stairs",), "Stairs 5"),
    LabelFix(1, 560.1, 149.5, ("Lift",), "Lift 1"),
    LabelFix(1, 27.0, 147.5, ("Lift 2",), "Lift 3"),
    LabelFix(1, 33.4, 434.1, ("Lift 1", "Lift 4"), None),
    LabelFix(3, 460.0, 393.6, ("Lift",), None),
]


def apply_label_fixes(
    db_path: str | Path, fixes: list[LabelFix], apply: bool = False
) -> list[FixReport]:
    """Rename or remove labels that are still as expected; never touch anything else."""
    report: list[FixReport] = []
    with closing(sqlite3.connect(db_path)) as conn:
        for fix in fixes:
            accepted = " or ".join(map(repr, fix.current))
            target = "(no label)" if fix.new is None else repr(fix.new)
            label = f"floor {fix.floor} ({fix.x}, {fix.y}): {accepted} -> {target}"
            # A point without a label has no description row (apd.id is NULL)
            row = conn.execute(
                """
                SELECT apd.id, TRIM(apd.description)
                FROM anchor_point_coordinates apc
                LEFT JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
                WHERE apc.floor = ?
                  AND ABS(apc.x_coordinate - ?) < ?
                  AND ABS(apc.y_coordinate - ?) < ?
                """,
                (fix.floor, fix.x, COORDINATE_TOLERANCE, fix.y, COORDINATE_TOLERANCE),
            ).fetchone()

            if row is None:
                status = "not found"
            elif row[0] is None:
                status = "already done" if fix.new is None else "not found"
            elif row[1] == fix.new:
                status = "already done"
            elif row[1] not in fix.current:
                status = f"skipped: found {row[1]!r}"
            elif fix.new is None:
                status = "removed" if apply else "would remove"
                if apply:
                    conn.execute(
                        "DELETE FROM anchor_point_description WHERE id = ?", (row[0],)
                    )
            else:
                status = "renamed" if apply else "would rename"
                if apply:
                    conn.execute(
                        "UPDATE anchor_point_description SET description = ?"
                        " WHERE id = ?",
                        (fix.new, row[0]),
                    )
            report.append({"fix": label, "status": status})

        if apply:
            conn.commit()
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Name AB-01's lifts/stairs consistently (dry run by default)."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Map database to fix (default: the configured AB-01 database)",
    )
    parser.add_argument("--apply", action="store_true", help="Write the changes")
    args = parser.parse_args()

    db_path = args.db or get_building_paths("AB-01")[0]
    for entry in apply_label_fixes(db_path, AB01_FIXES, apply=args.apply):
        print(f"{entry['status']:>14}  {entry['fix']}")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write the changes.")


if __name__ == "__main__":
    main()
