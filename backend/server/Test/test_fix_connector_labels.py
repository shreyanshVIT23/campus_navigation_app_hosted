import sqlite3
from collections import Counter

import pytest
from map_fixtures import Anchor, Connection, build_map_db

from Utils.fix_connector_labels import AB01_FIXES, LabelFix, apply_label_fixes

# AB-01 floor 1 lifts/stairs as committed (21d7a6b); the floor is drawn upside-down.
# The floor-1 bottom-left and floor-3 bottom-right lifts are drawing mistakes (user,
# 2026-10-10): AB-01 has only Lift 1/2/3 on every floor.
COMMITTED: list[Anchor] = [
    (1, 504.4, 101.1, 1, "Stairs", None),  # top-right
    (2, 506.3, 485.3, 1, "Stairs", None),  # bottom-right
    (3, 99.9, 107.3, 1, "Stairs 2", None),  # top-left
    (4, 97.8, 490.7, 1, "Stairs 1", None),  # bottom-left
    (5, 342.9, 147.5, 1, "Stairs", None),  # top-middle
    (6, 560.1, 149.5, 1, "Lift", None),  # top-right
    (7, 27.0, 147.5, 1, "Lift 2", None),  # top-left
    (8, 33.4, 434.1, 1, "Lift 1", None),  # bottom-left
    (9, 300.0, 300.0, 1, "105", None),  # not a connector: never touched
    (10, 460.0, 393.6, 3, "Lift", None),  # floor 3 bottom-right
]
# The fake lifts stay on the walkway
WALKWAYS: list[Connection] = [(8, 9, 270.0), (10, 9, 180.0)]
# Anchors whose label is removed; their points become plain walkway nodes
REMOVED = {8, 10}
# Physically consistent with floors 0/2/4/5 (Stairs 1 = BL ... Lift 3 = BR)
EXPECTED = {
    1: "Stairs 1",
    2: "Stairs 2",
    3: "Stairs 3",
    4: "Stairs 4",
    5: "Stairs 5",
    6: "Lift 1",
    7: "Lift 3",
    9: "105",
}


@pytest.fixture
def map_db(tmp_path):
    return build_map_db(tmp_path / "ab01-01.db", COMMITTED, WALKWAYS)


@pytest.fixture
def earlier_fix_db(tmp_path):
    # The live DB after the first fix named the right-hand stairs 4 (TR) and 3 (BR)
    anchors: list[Anchor] = [
        (*anchor[:4], {1: "Stairs 4", 2: "Stairs 3"}.get(anchor[0], anchor[4]), None)
        for anchor in COMMITTED
    ]
    return build_map_db(tmp_path / "ab01-01-live.db", anchors, WALKWAYS)


@pytest.fixture
def relabelled_db(tmp_path):
    # The live DB after the relabel: every name final, the fake floor-1 lift "Lift 4"
    final = {**EXPECTED, 8: "Lift 4", 10: "Lift"}
    anchors: list[Anchor] = [
        (*anchor[:4], final[anchor[0]], None) for anchor in COMMITTED
    ]
    return build_map_db(tmp_path / "ab01-01-relabelled.db", anchors, WALKWAYS)


def descriptions(db_path):
    with sqlite3.connect(db_path) as conn:
        return dict(
            conn.execute(
                "SELECT anchor_point_id, description FROM anchor_point_description"
            ).fetchall()
        )


def points(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            row[0] for row in conn.execute("SELECT id FROM anchor_point_coordinates")
        }


def walkways(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT point_a_id, point_b_id, distance FROM connections"
        ).fetchall()


def test_dry_run_reports_but_does_not_write(map_db):
    report = apply_label_fixes(map_db, AB01_FIXES, apply=False)
    assert {entry["status"] for entry in report} == {"would rename", "would remove"}
    assert descriptions(map_db)[1] == "Stairs"
    assert descriptions(map_db)[8] == "Lift 1"


def test_apply_relabels_floor_1_to_match_the_other_floors(map_db):
    report = apply_label_fixes(map_db, AB01_FIXES, apply=True)
    assert {entry["status"] for entry in report} == {"renamed", "removed"}
    assert descriptions(map_db) == EXPECTED


def test_fake_lifts_become_plain_walkway_points(map_db):
    apply_label_fixes(map_db, AB01_FIXES, apply=True)
    assert REMOVED.isdisjoint(descriptions(map_db))
    assert REMOVED <= points(map_db)
    assert walkways(map_db) == WALKWAYS


def test_ab01_keeps_only_lifts_1_to_3(map_db):
    apply_label_fixes(map_db, AB01_FIXES, apply=True)
    lifts = {name for name in descriptions(map_db).values() if name.startswith("Lift")}
    assert lifts == {"Lift 1", "Lift 3"}  # floor 1's Lift 2 is not in this fixture


def test_apply_after_the_relabel_removes_only_the_fake_lifts(relabelled_db):
    report = apply_label_fixes(relabelled_db, AB01_FIXES, apply=True)
    statuses = Counter(entry["status"] for entry in report)
    assert statuses == {"already done": len(AB01_FIXES) - 2, "removed": 2}
    assert descriptions(relabelled_db) == EXPECTED


def test_apply_also_works_after_the_earlier_fix(earlier_fix_db):
    apply_label_fixes(earlier_fix_db, AB01_FIXES, apply=True)
    assert descriptions(earlier_fix_db) == EXPECTED


def test_every_connector_name_is_unique_after_the_fix(map_db):
    apply_label_fixes(map_db, AB01_FIXES, apply=True)
    names = Counter(
        name
        for name in descriptions(map_db).values()
        if name.startswith(("Lift", "Stairs"))
    )
    assert all(count == 1 for count in names.values()), names


def test_apply_is_idempotent(map_db):
    apply_label_fixes(map_db, AB01_FIXES, apply=True)
    report = apply_label_fixes(map_db, AB01_FIXES, apply=True)
    assert {entry["status"] for entry in report} == {"already done"}
    assert descriptions(map_db) == EXPECTED


def test_unexpected_current_label_is_not_overwritten(map_db):
    with sqlite3.connect(map_db) as conn:
        conn.execute(
            "UPDATE anchor_point_description SET description = 'Lift 9' "
            "WHERE anchor_point_id = 1"
        )
        conn.commit()
    report = apply_label_fixes(map_db, AB01_FIXES, apply=True)
    assert report[0]["status"] == "skipped: found 'Lift 9'"
    assert descriptions(map_db)[1] == "Lift 9"


def test_missing_anchor_is_reported(map_db):
    fix = LabelFix(floor=1, x=1.0, y=1.0, current=("Stairs",), new="Stairs 9")
    report = apply_label_fixes(map_db, [fix], apply=True)
    assert report[0]["status"] == "not found"


def test_removal_at_a_missing_point_is_reported(map_db):
    fix = LabelFix(floor=1, x=1.0, y=1.0, current=("Lift",), new=None)
    report = apply_label_fixes(map_db, [fix], apply=True)
    assert report[0]["status"] == "not found"


def test_removal_never_deletes_an_unexpected_label(map_db):
    fix = LabelFix(floor=1, x=300.0, y=300.0, current=("Lift",), new=None)
    report = apply_label_fixes(map_db, [fix], apply=True)
    assert report[0]["status"] == "skipped: found '105'"
    assert descriptions(map_db)[9] == "105"
