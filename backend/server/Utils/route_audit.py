"""Audit every place a user can route between, and check each answer.

Read-only. For each building it runs the routing engine (no SVG drawing) for:
- every ordered pair of room labels (each room as start and as end),
- every room to and from the nearest male, female and any toilet,
- every teacher, by cabin number and by name, to and from a reference room,
and checks each result with Utils.route_check: the path must be real (walkways,
lengths, a matching lift/stairs) and exactly as short as an independent solve.
It also lists problems in the map data itself.

Usage (from backend/server):
    uv run python -m Utils.route_audit [--preference none|Stairs|Lift|all]
                                       [--building AB-01|Lab-Complex] [--out report.json]
"""

import argparse
import contextlib
import io
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from typing_extensions import TypedDict

from . import db_shortest_path_maker, place_lookup, svg_manipulator
from .building_handling import get_building_paths
from .loader import env_variables
from .route_check import Anchor, MapData, same_length
from .route_types import BuildingRoute

BUILDINGS = ("AB-01", "Lab-Complex")
# Single letters mark toilets (B/G), window galleries or washrooms (W) and
# teacher rooms (T); "Empty" is not a place
NOT_ROOMS = {"", "Unique anchor point", "T", "B", "G", "W", "Empty"}
TOILET_REQUESTS: dict[place_lookup.ToiletKind, str] = {
    "male": "male toilet",
    "female": "female toilet",
    "any": "toilet",
}


class FailureGroup(TypedDict):
    count: int
    examples: list[str]


class AuditReport(TypedDict):
    building: str
    preference: str | None
    checks: int
    ok: int
    expected_failures: dict[str, FailureGroup]
    problems: dict[str, FailureGroup]
    data_problems: list[str]
    seconds: float


class Check(TypedDict):
    kind: Literal["room", "toilet", "teacher"]
    name: str
    start: str
    end: str
    starts: list[Anchor]
    ends: list[Anchor]
    # Shortest possible length per the independent solve (inf: no route exists)
    expected: float


def room_labels(data: MapData) -> list[str]:
    """Labels a user would type for a room: descriptions, or the extra info of
    toilet and teacher-room anchors ("T202(A)", "B120"); lifts/stairs excluded."""
    labels: set[str] = set()
    for anchor_id, description in data.descriptions.items():
        if anchor_id not in data.points or description.startswith(("Lift", "Stairs")):
            continue
        if description not in NOT_ROOMS:
            labels.add(description)
        elif data.extra_info[anchor_id] and re.search(
            r"\d", data.extra_info[anchor_id]
        ):
            labels.add(data.extra_info[anchor_id])
    return sorted(labels)


def room_checks(data: MapData, preference: str | None) -> Iterator[Check]:
    labels = room_labels(data)
    anchors = {label: data.anchors_of(label) for label in labels}
    for start in labels:
        for end in labels:
            if start == end:
                continue
            yield {
                "kind": "room",
                "name": f"{start} -> {end}",
                "start": start,
                "end": end,
                "starts": anchors[start],
                "ends": anchors[end],
                "expected": data.shortest_length(
                    anchors[start], anchors[end], preference
                ),
            }


def toilet_checks(data: MapData, preference: str | None) -> Iterator[Check]:
    for room in room_labels(data):
        room_anchors = data.anchors_of(room)
        for kind, request in TOILET_REQUESTS.items():
            toilets = data.toilet_anchors(kind)
            room_floors = {floor for floor, _ in room_anchors}
            options = [t for t in toilets if t[0] in room_floors] or toilets
            yield {
                "kind": "toilet",
                "name": f"{room} -> {request}",
                "start": room,
                "end": request,
                "starts": room_anchors,
                "ends": options,
                "expected": data.toilet_length(room_anchors, kind, preference, True),
            }
            yield {
                "kind": "toilet",
                "name": f"{request} -> {room}",
                "start": request,
                "end": room,
                "starts": options,
                "ends": room_anchors,
                "expected": data.toilet_length(room_anchors, kind, preference, False),
            }


def map_label(data: MapData, room: str) -> str:
    """`room`, or the map's own label for it, as the engine resolves it."""
    alias = place_lookup.room_alias(room)
    return alias if alias and not data.anchors_of(room) else room


def teacher_checks(
    data: MapData, teachers: list[place_lookup.Teacher], preference: str | None
) -> Iterator[Check]:
    labels = room_labels(data)
    if not labels:
        return
    reference = labels[0]
    reference_anchors = data.anchors_of(reference)
    for teacher in teachers:
        room = map_label(data, teacher.room_no or "")
        room_anchors = data.anchors_of(room)
        if not room_anchors:
            continue  # the teacher's room is not in this building
        typed = [teacher.cabin_no] if teacher.cabin_no else []
        rooms_for_name = place_lookup.rooms_for_name(teachers, teacher.name)
        # A name shared by teachers in different rooms is reported as ambiguous
        if len(rooms_for_name) == 1:
            typed.append(teacher.name)
        for text in typed:
            if room == reference:
                continue
            yield {
                "kind": "teacher",
                "name": f"{text} [{room}] -> {reference}",
                "start": text,
                "end": reference,
                "starts": room_anchors,
                "ends": reference_anchors,
                "expected": data.shortest_length(
                    room_anchors, reference_anchors, preference
                ),
            }
            yield {
                "kind": "teacher",
                "name": f"{reference} -> {text} [{room}]",
                "start": reference,
                "end": text,
                "starts": reference_anchors,
                "ends": room_anchors,
                "expected": data.shortest_length(
                    reference_anchors, room_anchors, preference
                ),
            }


def judge(
    data: MapData, check: Check, route: BuildingRoute, preference: str | None
) -> tuple[bool, str | None]:
    """(is it a problem, reason) for one engine answer; (False, None) when right."""
    expected = check["expected"]
    if not check["starts"] or not check["ends"]:
        return True, "CHECKER: no anchor has this exact label"
    same_place = set(check["starts"]) & set(check["ends"])

    if route["complexity"] == "failed":
        # Group messages that differ only by room names or numbers
        reason = re.sub(r"'[^']*'|\d+", "#", route["error"])
        if same_place:
            return False, f"same place: {reason}"
        if math.isinf(expected):
            return False, f"no route exists in the map: {reason}"
        return True, f"MISSED ROUTE: {reason}"

    if same_place:
        return True, "ROUTED A PLACE TO ITSELF"
    problems = data.route_problems(route, check["starts"], check["ends"], preference)
    if problems:
        return True, "INVALID PATH: " + re.sub(r"\d+(\.\d+)?", "#", problems[0])
    length = route["total_weight"]
    if same_length(length, expected):
        return False, None
    if length > expected:
        return True, "NOT SHORTEST"
    return True, "SHORTER THAN THE INDEPENDENT SOLVE (checker or path bug)"


def drawn_reason(floor_path, route: BuildingRoute) -> str | None:
    """Why a successful route is not shown properly on its floor maps, or None."""
    legs = svg_manipulator.route_legs(route)
    try:
        maps = svg_manipulator.render_route(floor_path, route)
    except (IndexError, ValueError, OSError) as e:
        return f"NOT SHOWN: drawing failed ({type(e).__name__})"
    for (_, points), (_, svg) in zip(legs, maps, strict=True):
        problems = MapData.drawn_problems(svg, points)
        if problems:
            return "NOT SHOWN: " + re.sub(r"\d+", "#", problems[0])
    return None


def audit(
    building: str,
    preference: str | None,
    teachers: list[place_lookup.Teacher],
    render: bool = False,
) -> AuditReport:
    """Run every check; with `render`, also draw (in memory) and verify the maps
    of toilet and teacher routes."""
    started = time.monotonic()
    db_path, floor_path = get_building_paths(building)
    data = MapData(db_path)
    problems: dict[str, list[str]] = defaultdict(list)
    expected_failures: dict[str, list[str]] = defaultdict(list)
    total = 0

    checks = [
        *room_checks(data, preference),
        *toilet_checks(data, preference),
        *teacher_checks(data, teachers, preference),
    ]
    for check in checks:
        total += 1
        # The engine prints debug lines; keep the report readable
        with contextlib.redirect_stdout(io.StringIO()):
            route = db_shortest_path_maker.main(
                db_path, check["start"], check["end"], preference
            )
        is_problem, reason = judge(data, check, route, preference)
        if reason is None and render and check["kind"] != "room":
            is_problem, reason = True, drawn_reason(floor_path, route)
        if reason is None:
            continue
        if is_problem:
            name = check["name"]
            if reason == "NOT SHORTEST" and route["complexity"] != "failed":
                name += f" (+{route['total_weight'] - check['expected']:.1f})"
            problems[reason].append(name)
        else:
            expected_failures[reason].append(check["name"])

    def grouped(found: dict[str, list[str]]) -> dict[str, FailureGroup]:
        return {
            reason: {"count": len(names), "examples": names[:8]}
            for reason, names in sorted(found.items(), key=lambda kv: -len(kv[1]))
        }

    return {
        "building": building,
        "preference": preference,
        "checks": total,
        "ok": total - sum(len(names) for names in problems.values()),
        "expected_failures": grouped(expected_failures),
        "problems": grouped(problems),
        "data_problems": data.data_problems(),
        "seconds": round(time.monotonic() - started, 1),
    }


def print_report(report: AuditReport) -> None:
    print(f"\n=== {report['building']} (preference={report['preference']}) ===")
    print(
        f"{report['checks']} checks, {report['ok']} correct,"
        f" {report['checks'] - report['ok']} problems, {report['seconds']}s"
    )
    for title, groups in (
        ("PROBLEMS", report["problems"]),
        ("correctly refused", report["expected_failures"]),
    ):
        if groups:
            print(f"  {title}:")
        for reason, info in groups.items():
            print(f"    {info['count']:6d}  {reason}")
            print(f"            e.g. {', '.join(info['examples'][:4])}")
    if report["data_problems"]:
        print(f"  map data problems ({len(report['data_problems'])}):")
        for problem in report["data_problems"]:
            print(f"    - {problem}")


def main():
    parser = argparse.ArgumentParser(
        description="Route and check every place in each building (read-only)."
    )
    parser.add_argument(
        "--preference", choices=["none", "Stairs", "Lift", "all"], default="none"
    )
    parser.add_argument("--building", choices=BUILDINGS, default=None)
    parser.add_argument("--out", type=Path, default=None, help="Write a JSON report")
    parser.add_argument(
        "--render",
        action="store_true",
        help="Also draw toilet and teacher routes (in memory) and verify the maps",
    )
    args = parser.parse_args()

    preferences = {
        "none": [None],
        "Stairs": ["Stairs"],
        "Lift": ["Lift"],
        "all": [None, "Stairs", "Lift"],
    }[args.preference]
    buildings = [args.building] if args.building else list(BUILDINGS)
    teachers = place_lookup.load_teachers(env_variables["teacher_des"])

    reports = []
    for building in buildings:
        for preference in preferences:
            report = audit(building, preference, teachers, args.render)
            print_report(report)
            reports.append(report)

    print("\nTeachers whose room is in no building map:")
    maps = [MapData(get_building_paths(building)[0]) for building in BUILDINGS]
    in_a_map = {
        label
        for data in maps
        for label in (*data.descriptions.values(), *data.extra_info.values())
    }
    missing = sorted(
        {
            (t.room_no, t.name)
            for t in teachers
            if t.room_no not in in_a_map
            and place_lookup.room_alias(t.room_no or "") not in in_a_map
        }
    )
    for room, name in missing:
        print(f"    - {name}: room {room}")

    if args.out:
        args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"\nFull report: {args.out}")


if __name__ == "__main__":
    main()
