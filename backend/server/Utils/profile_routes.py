"""Benchmark and profile the backend. Read-only: everything runs on temporary
copies of the data (Utils.test_copies), never on the real DB/.

- Scenarios: real HTTP requests (FastAPI TestClient); median/p90 wall time and
  the per-stage times from each response's Server-Timing header.
- --profile: cProfile of the same work. TestClient runs the app in another
  thread, which cProfile can't see, so the route functions are called directly.
- --micro: isolated timings of each suspected bottleneck.
- --concurrent K: K different routes fired at once; total time, latencies, and
  whether each returned map shows that request's own route.
- --compare BEFORE AFTER: speedup per scenario and micro-benchmark.

Usage (from backend/server):
    uv run --no-sync python -m Utils.profile_routes --repeat 20 --micro --profile
        --concurrent 10 --out ../.claude/notes/scripts/profile_before.json
    uv run --no-sync python -m Utils.profile_routes --compare BEFORE.json AFTER.json
"""

import argparse
import asyncio
import cProfile
import json
import platform
import shutil
import statistics
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import parse_qs, urlsplit

from .test_copies import use_copies

Report = dict[str, Any]


class Scenario(NamedTuple):
    name: str
    kind: Literal["route", "multi", "chat"]
    # route: start, end, building, preference ("" = none); multi: building 1,
    # start, building 2, end; chat: the question
    args: tuple[str, ...]
    expected_status: int = 200


SCENARIOS = [
    Scenario("same floor", "route", ("101", "105", "AB-01", "")),
    Scenario("cross floor", "route", ("105", "201", "AB-01", "")),
    Scenario("cross floor, stairs", "route", ("105", "201", "AB-01", "Stairs")),
    Scenario("cross floor, lift", "route", ("105", "201", "AB-01", "Lift")),
    Scenario("teacher name", "route", ("vishnuvarthanan", "105", "AB-01", "")),
    Scenario("teacher cabin", "route", ("G-03", "201", "AB-01", "")),
    Scenario("nearest toilet", "route", ("301", "female toilet", "AB-01", "")),
    Scenario("ambiguous input", "route", ("10", "201", "AB-01", ""), 400),
    Scenario("multi building", "multi", ("AB-01", "101", "Lab-Complex", "201")),
    Scenario("chatbot (offline fallback)", "chat", ("who is vishnuvarthanan",)),
]

# Own-time buckets for the profile; anything else is "other"
BUCKETS: dict[str, tuple[str, ...]] = {
    "sqlite": ("sqlite3", "db_access.py"),
    "networkx": ("networkx",),
    "svg (lxml)": ("lxml", "svg_manipulator.py"),
    "routing engine": (
        "db_shortest_path_maker.py",
        "graph_converter.py",
        "place_lookup.py",
    ),
    "web framework": ("starlette", "fastapi", "anyio", "httpx", "pydantic", "h11"),
}


def summary(times_ms: list[float]) -> dict[str, float]:
    ordered = sorted(times_ms)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return {"median_ms": statistics.median(ordered), "p90_ms": p90}


def parse_server_timing(header: str) -> dict[str, float]:
    stages: dict[str, float] = {}
    for metric in filter(None, (part.strip() for part in header.split(","))):
        name, *params = metric.split(";")
        for param in params:
            if param.startswith("dur="):
                stages[name] = float(param[4:])
    return stages


def bench(name: str, fn: Callable[[], object], runs: int, **extra: Any) -> Report:
    fn()  # warm-up: imports, first-time file reads
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
    return {"name": name, "runs": runs, **summary(times), **extra}


# ---- Scenarios ----


def request_for(scenario: Scenario) -> tuple[str, dict[str, Any]]:
    if scenario.kind == "route":
        start, end, building, preference = scenario.args
        return "/process_path", {
            "start": start,
            "end": end,
            "building": building,
            "preference": preference or None,
        }
    building_1, start, building_2, end = scenario.args
    return "/multi_building_process_path", {
        "start": start,
        "end": end,
        "building_name_1": building_1,
        "building_name_2": building_2,
    }


def direct_call(scenario: Scenario, offline_bot) -> Callable[[], object]:
    """The same work as the request, in this thread (for cProfile)."""
    from . import svg_manipulator

    if scenario.kind == "route":
        start, end, building, preference = scenario.args
        return lambda: svg_manipulator.main(start, end, building, preference or None)
    if scenario.kind == "multi":
        building_1, start, building_2, end = scenario.args
        return lambda: svg_manipulator.multi_building_nav(
            building_1, start, building_2, end
        )
    return lambda: offline_chat(offline_bot, scenario.args[0])


def offline_chat(bot, question: str):
    # Measure the work, not the response cache
    bot.response_cache.clear()
    return bot.chat(question)


def run_scenarios(client, offline_bot, repeat: int) -> list[Report]:
    from . import timing

    results = []
    for scenario in SCENARIOS:
        times: list[float] = []
        stages: dict[str, list[float]] = {}
        status = 200
        for attempt in range(repeat + 1):  # attempt 0 warms up
            start = time.perf_counter()
            if scenario.kind == "chat":
                # Called directly (no Gemini, no network), so collect stages here
                with timing.collect() as chat_timings:
                    offline_chat(offline_bot, scenario.args[0])
                stage_ms: dict[str, float] = dict(chat_timings.stages)
            else:
                path, body = request_for(scenario)
                response = client.post(path, json=body)
                status = response.status_code
                stage_ms = parse_server_timing(
                    response.headers.get("server-timing", "")
                )
            elapsed = (time.perf_counter() - start) * 1000
            if attempt == 0:
                continue
            times.append(elapsed)
            for stage, ms in stage_ms.items():
                stages.setdefault(stage, []).append(ms)
        results.append(
            {
                "name": scenario.name,
                "status": status,
                "expected_status": scenario.expected_status,
                "runs": repeat,
                **summary(times),
                "stages_ms": {s: statistics.median(v) for s, v in stages.items()},
            }
        )
    return results


# ---- cProfile ----


def bucket_of(file_name: str, function: str) -> str:
    where = f"{file_name} {function}".replace("\\", "/")
    for bucket, markers in BUCKETS.items():
        if any(marker in where for marker in markers):
            return bucket
    return "other"


def profile_rows(calls: list[Callable[[], object]], repeat: int) -> list[Report]:
    """cProfile `repeat` rounds of `calls`: one row per function, own time first."""
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(repeat):
        for call in calls:
            call()
    profiler.disable()

    rows = []
    # One entry per function; times in seconds with the default timer. Built-ins
    # (e.g. sqlite3.Cursor.execute) have a description string instead of code.
    for entry in profiler.getstats():
        if isinstance(entry.code, str):
            file_name, line, function = "~", 0, entry.code
        else:
            code = entry.code
            file_name, line, function = (
                code.co_filename,
                code.co_firstlineno,
                code.co_name,
            )
        rows.append(
            {
                "function": f"{Path(file_name).name}:{line}({function})",
                "bucket": bucket_of(file_name, function),
                "calls": entry.callcount,
                "own_ms": entry.inlinetime * 1000,
                "cumulative_ms": entry.totaltime * 1000,
            }
        )
    return sorted(rows, key=lambda r: -r["own_ms"])


def run_profile(offline_bot, repeat: int, top: int = 25) -> Report:
    calls = {s.name: direct_call(s, offline_bot) for s in SCENARIOS}
    for call in calls.values():
        call()  # warm-up outside the profile

    rows = profile_rows(list(calls.values()), repeat)
    shares = dict.fromkeys([*BUCKETS, "other"], 0.0)
    for row in rows:
        shares[row["bucket"]] += row["own_ms"]
    total_own = sum(shares.values()) or 1.0
    return {
        "runs": repeat,
        "total_own_ms": total_own,
        "top_cumulative": sorted(rows, key=lambda r: -r["cumulative_ms"])[:top],
        "top_own": rows[:top],
        "share_of_own_time": {b: own / total_own for b, own in shares.items()},
        # Where each scenario's own time goes (e.g. to explain one slow request)
        "by_scenario": {
            name: profile_rows([call], repeat)[:10] for name, call in calls.items()
        },
    }


# ---- Micro-benchmarks ----


def run_micro(repeat: int) -> list[Report]:
    from lxml import etree as ET

    from . import (
        db_access,
        db_shortest_path_maker,
        graph_converter,
        place_lookup,
        svg_manipulator,
    )
    from .building_handling import get_building_paths
    from .loader import env_variables

    runs = max(10, repeat * 5)
    db_path, floor_path = get_building_paths("AB-01")
    teacher_db = env_variables["teacher_des"]
    teachers = place_lookup.load_teachers(teacher_db)
    results: list[Report] = []

    for floor in range(6):
        results.append(
            bench(
                f"graph build (floor {floor})",
                lambda floor=floor: graph_converter.create_graph_from_db(
                    db_path, floor
                ),
                runs,
            )
        )

    # Node lookups for every lift/stairs on floor 5: today's full scan vs a dict
    graph = graph_converter.create_graph_from_db(db_path, 5)
    spots = [coords for _, coords in db_access.get_connectors(db_path, 5)]
    index = {(d["x"], d["y"], d["floor"]): n for n, d in graph.nodes(data=True)}
    results.append(
        bench(
            "node lookup: full scan",
            lambda: db_shortest_path_maker.nodes_at(graph, spots, 5),
            runs,
            items=len(spots),
            graph_nodes=graph.number_of_nodes(),
        )
    )
    results.append(
        bench(
            "node lookup: dict",
            lambda: [index.get((x, y, 5)) for x, y in spots],
            runs,
            items=len(spots),
        )
    )

    lookups: list[tuple[str, Callable[[], object]]] = [
        ("db.has_exact_label", lambda: db_access.has_exact_label(db_path, "105")),
        ("db.find_places", lambda: db_access.find_places(db_path, "105", 1)),
        (
            "db.find_places_anywhere",
            lambda: db_access.find_places_anywhere(db_path, "105"),
        ),
        ("db.get_coordinates", lambda: db_access.get_coordinates(db_path, "Lift %", 1)),
        ("db.get_connectors", lambda: db_access.get_connectors(db_path, 1)),
        (
            "db.find_described",
            lambda: db_access.find_described(db_path, place_lookup.TOILET_MARKS),
        ),
        ("teachers", lambda: place_lookup.load_teachers(teacher_db)),
        (
            "translate_place",
            lambda: db_shortest_path_maker.translate_place(
                db_path, "vishnuvarthanan", teachers
            ),
        ),
        (
            "resolve_candidates",
            lambda: db_shortest_path_maker.resolve_candidates(db_path, "105", 1),
        ),
        (
            "engine only: same floor",
            lambda: db_shortest_path_maker.main(db_path, "101", "105"),
        ),
        (
            "engine only: cross floor",
            lambda: db_shortest_path_maker.main(db_path, "105", "201"),
        ),
    ]
    results += [bench(name, fn, runs) for name, fn in lookups]

    # SVG work on the largest floor map (floor 5)
    route = db_shortest_path_maker.main(db_path, "501", "515")
    if route["complexity"] != "simple":
        raise RuntimeError(f"benchmark route 501 -> 515 failed: {route}")
    points = route["path"][1]
    source = svg_manipulator.floor_svg(5, str(floor_path))
    source_bytes = Path(source).read_bytes()
    with tempfile.TemporaryDirectory() as out_dir:
        out_file = str(Path(out_dir) / "floor 5.svg")
        tree = ET.parse(source)
        results += [
            bench("svg: parse a file written earlier", lambda: ET.parse(source), runs),
            parse_fresh_file(Path(out_dir), source_bytes, runs),
            bench(
                "svg: write floor 5",
                lambda: tree.write(out_file, xml_declaration=True, encoding="utf-8"),
                runs,
            ),
            bench(
                "svg: draw route on floor 5",
                lambda: svg_manipulator.render_floor(source, points),
                runs,
                source_bytes=len(source_bytes),
            ),
        ]
    return results


def parse_fresh_file(folder: Path, content: bytes, runs: int) -> Report:
    """Parse a file right after writing it (only the parse is timed)."""
    from lxml import etree as ET

    times = []
    for run in range(runs + 1):
        fresh = folder / f"fresh-{run}.svg"
        fresh.write_bytes(content)
        start = time.perf_counter()
        ET.parse(str(fresh))
        if run:  # run 0 warms up
            times.append((time.perf_counter() - start) * 1000)
    return {"name": "svg: parse a file just written", "runs": runs, **summary(times)}


# ---- Concurrency ----


def drawn_segments(svg_text: str) -> set:
    from lxml import etree as ET

    from .svg_manipulator import NS, point_key

    root = ET.fromstring(svg_text.encode("utf-8"))
    path_group = root.find(".//svg:g[@id='Path']", namespaces=NS)
    if path_group is None:
        return set()
    return {
        (
            point_key(line.get("x1"), line.get("y1")),
            point_key(line.get("x2"), line.get("y2")),
        )
        for line in path_group.xpath(".//svg:line", namespaces=NS)
        if line.get("visibility") == "visible" and line.get("stroke") == "red"
    }


def route_pairs(count: int) -> list[tuple[str, str]]:
    """`count` different floor-1 -> floor-2 routes that exist (they share map files)."""
    import sqlite3
    from contextlib import closing

    from . import db_shortest_path_maker
    from .building_handling import get_building_paths

    db_path = get_building_paths("AB-01")[0]
    query = (
        "SELECT DISTINCT TRIM(d.description) FROM anchor_point_description d"
        " JOIN anchor_point_coordinates a ON a.id = d.anchor_point_id"
        " WHERE a.floor = ? AND TRIM(d.description) GLOB ? ORDER BY 1"
    )
    with closing(
        sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    ) as conn:
        first = [row[0] for row in conn.execute(query, (1, "1[0-9][0-9]"))]
        second = [row[0] for row in conn.execute(query, (2, "2[0-9][0-9]"))]
    pairs = []
    for start, end in zip(first, second):
        if db_shortest_path_maker.main(db_path, start, end)["complexity"] == "complex":
            pairs.append((start, end))
        if len(pairs) == count:
            return pairs
    raise RuntimeError(f"only {len(pairs)} floor-1 -> floor-2 routes found")


def run_concurrency(app, count: int) -> Report:
    import httpx

    from . import db_shortest_path_maker, svg_manipulator
    from .building_handling import get_building_paths

    db_path = get_building_paths("AB-01")[0]
    pairs = route_pairs(count)
    expected = {}
    for start, end in pairs:
        route = db_shortest_path_maker.main(db_path, start, end)
        if route["complexity"] == "complex":
            expected[(start, end)] = {
                floor: svg_manipulator.route_segments(points)
                for floor, points in route["path"]
            }

    async def fire():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://bench"
        ) as client:

            async def one(start: str, end: str):
                response = await client.post(
                    "/process_path",
                    json={
                        "start": start,
                        "end": end,
                        "building": "AB-01",
                        "preference": None,
                    },
                )
                # Latency as a user sees it: from the moment all were sent
                return response, (time.perf_counter() - began) * 1000

            began = time.perf_counter()
            answers = await asyncio.gather(*(one(s, e) for s, e in pairs))
            total_ms = (time.perf_counter() - began) * 1000

            # Fetch each request's maps only after all of them are done, as a
            # browser would after its own response
            requests = []
            for (start, end), (response, latency) in zip(pairs, answers):
                own = response.status_code == 200
                if own:
                    for url in response.json()["files"].values():
                        floor = int(parse_qs(urlsplit(url).query)["floor"][0])
                        drawn = drawn_segments((await client.get(url)).text)
                        own = (
                            own
                            and bool(drawn)
                            and drawn <= expected[(start, end)][floor]
                        )
                requests.append(
                    {
                        "route": f"{start} -> {end}",
                        "status": response.status_code,
                        "latency_ms": latency,
                        "own_route": own,
                    }
                )
            return total_ms, requests

    total_ms, requests = asyncio.run(fire())
    latencies = [r["latency_ms"] for r in requests]
    return {
        "requests": requests,
        "total_ms": total_ms,
        "requests_per_second": len(requests) / (total_ms / 1000),
        "latency_median_ms": statistics.median(latencies),
        "latency_max_ms": max(latencies),
        "own_route_count": sum(r["own_route"] for r in requests),
    }


# ---- Whole run ----


def run(repeat: int, micro: bool, concurrent: int, profile: bool) -> Report:
    """Benchmark with the environment already pointed at data copies."""
    from fastapi.testclient import TestClient

    from app import app
    from Chatbot.chat_bot import Chatbot

    from . import timing

    timing.reset_cache_stats()
    offline_bot = Chatbot(llm=None, api_key=None)
    report: Report = {
        "meta": {
            "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "repeat": repeat,
        }
    }
    with TestClient(app) as client:
        report["scenarios"] = run_scenarios(client, offline_bot, repeat)
    if profile:
        report["profile"] = run_profile(offline_bot, repeat)
    if micro:
        report["micro"] = run_micro(repeat)
    if concurrent:
        report["concurrency"] = run_concurrency(app, concurrent)
    report["cache_stats"] = timing.cache_stats()
    return report


# Benchmarks renamed when the code changed: new name -> name in older reports
RENAMED = {
    "svg: draw route on floor 5": "svg: modify_svg (floor 5)",
    "svg: parse a file written earlier": "svg: parse floor 5 source",
}


def compare(before: Report, after: Report) -> list[dict[str, Any]]:
    rows = []
    for kind, key in (("scenario", "scenarios"), ("micro", "micro")):
        old = {row["name"]: row["median_ms"] for row in before.get(key, [])}
        for row in after.get(key, []):
            name = row["name"]
            before_ms = old.get(name, old.get(RENAMED.get(name, "")))
            if before_ms is not None:
                rows.append(
                    {
                        "kind": kind,
                        "name": name,
                        "before_ms": before_ms,
                        "after_ms": row["median_ms"],
                        "speedup": round(before_ms / max(row["median_ms"], 1e-9), 2),
                    }
                )
    return rows


def print_report(report: Report):
    print("\n=== Scenarios (HTTP, median / p90 ms) ===")
    for row in report["scenarios"]:
        stages = ", ".join(
            f"{s} {ms:.1f}" for s, ms in row["stages_ms"].items() if s != "total"
        )
        print(
            f"{row['name']:<28} {row['status']} {row['median_ms']:8.1f} "
            f"{row['p90_ms']:8.1f}   {stages}"
        )
    if "profile" in report:
        profile = report["profile"]
        print(
            f"\n=== Share of own time ({profile['total_own_ms']:.0f} ms profiled) ==="
        )
        for bucket, share in sorted(
            profile["share_of_own_time"].items(), key=lambda kv: -kv[1]
        ):
            print(f"{bucket:<16} {share:6.1%}")
        print("\n=== Top functions by own time ===")
        for row in profile["top_own"][:15]:
            print(f"{row['own_ms']:9.1f} ms {row['calls']:>8} calls  {row['function']}")
        for name, rows in profile["by_scenario"].items():
            print(f"\n--- {name}: top functions by own time ---")
            for row in rows[:5]:
                print(
                    f"{row['own_ms']:9.1f} ms {row['calls']:>8} calls  {row['function']}"
                )
    if "micro" in report:
        print("\n=== Micro-benchmarks (median / p90 ms) ===")
        for row in report["micro"]:
            print(f"{row['name']:<28} {row['median_ms']:9.3f} {row['p90_ms']:9.3f}")
    if "concurrency" in report:
        c = report["concurrency"]
        print(
            f"\n=== {len(c['requests'])} routes at once ===\n"
            f"total {c['total_ms']:.0f} ms, {c['requests_per_second']:.1f} req/s, "
            f"latency median {c['latency_median_ms']:.0f} ms, max {c['latency_max_ms']:.0f} ms, "
            f"own map shown for {c['own_route_count']}/{len(c['requests'])}"
        )
    if report.get("cache_stats"):
        print(f"\ncache hits/misses: {report['cache_stats']}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark and profile the backend.")
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--micro", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--concurrent", type=int, default=0)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()

    if args.compare:
        before, after = (
            json.loads(p.read_text(encoding="utf-8")) for p in args.compare
        )
        for row in compare(before, after):
            print(
                f"{row['kind']:<9} {row['name']:<30} {row['before_ms']:9.2f} -> "
                f"{row['after_ms']:9.2f} ms  x{row['speedup']}"
            )
        return

    data_dir = Path(tempfile.mkdtemp(prefix="campus-nav-bench-"))
    try:
        use_copies(data_dir)
        report = run(args.repeat, args.micro, args.concurrent, args.profile)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
    print_report(report)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport: {args.out}")


if __name__ == "__main__":
    main()
