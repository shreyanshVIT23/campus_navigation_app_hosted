"""Caches keyed by file version, the in-memory route-map store, and concurrent routes.

Every cache refreshes itself when its file changes; route maps are drawn in memory
and served by route id (no output SVG files); routes run in a worker thread so
several can be served at once, each seeing its own map.
"""

import asyncio
import contextlib
import inspect
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import networkx as nx
import pytest
from fastapi.testclient import TestClient
from lxml import etree as ET
from map_fixtures import Anchor, Connection, build_map_db

from Utils import (
    db_access,
    db_shortest_path_maker,
    file_version,
    graph_converter,
    place_lookup,
    profile_routes,
    route_maps,
    route_utilary,
    svg_manipulator,
    timing,
)
from Utils.building_handling import get_building_paths
from Utils.loader import env_variables
from Utils.route_check import MapData

SVG_NS = {"svg": "http://www.w3.org/2000/svg"}

# Floor 1: 101 -1- 102 -1- 103
ANCHORS: list[Anchor] = [
    (1, 0.0, 0.0, 1, "101", None),
    (2, 1.0, 0.0, 1, "102", None),
    (3, 2.0, 0.0, 1, "103", None),
]
CONNECTIONS: list[Connection] = [(1, 2, 1.0), (2, 3, 1.0)]


@pytest.fixture
def map_db(tmp_path):
    return build_map_db(tmp_path / "building.db", ANCHORS, CONNECTIONS)


@pytest.fixture(autouse=True)
def fresh_stats():
    timing.reset_cache_stats()
    yield
    timing.reset_cache_stats()


def write_row(db_path: str, sql: str):
    """A committed write that keeps the file's modified time (as quick writes can)."""
    stat = os.stat(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute(sql)
        conn.commit()
    os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


# ---- File versions ----


def test_version_changes_after_any_committed_write(map_db):
    before = file_version.read_version(Path(map_db))
    write_row(
        map_db, "UPDATE anchor_point_description SET description = '104' WHERE id = 3"
    )
    assert file_version.read_version(Path(map_db)) != before


def test_versions_are_read_once_per_scope(map_db, monkeypatch):
    reads = []
    real = file_version.read_version
    monkeypatch.setattr(
        file_version, "read_version", lambda p: reads.append(p) or real(p)
    )
    with file_version.consistent_versions():
        for _ in range(5):
            file_version.file_version(map_db)
        with file_version.consistent_versions():  # nested: same scope
            file_version.file_version(map_db)
    assert len(reads) == 1
    file_version.file_version(map_db)
    file_version.file_version(map_db)
    assert len(reads) == 3  # outside a scope every call reads


def test_scopes_in_different_threads_are_separate(map_db, monkeypatch):
    reads = []
    real = file_version.read_version
    monkeypatch.setattr(
        file_version, "read_version", lambda p: reads.append(p) or real(p)
    )

    def work():
        with file_version.consistent_versions():
            file_version.file_version(map_db)
            file_version.file_version(map_db)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(reads) == 3


# ---- cached_by_file ----


@pytest.fixture
def counted_query(map_db):
    calls = []

    @file_version.cached_by_file("demo", maxsize=2)
    def labels(db_path, floor):
        calls.append(floor)
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT d.description FROM anchor_point_description d JOIN "
                    "anchor_point_coordinates a ON a.id = d.anchor_point_id "
                    "WHERE a.floor = ? ORDER BY d.id",
                    (floor,),
                )
            ]

    return labels, calls


def test_second_call_is_a_cache_hit(map_db, counted_query):
    labels, calls = counted_query
    assert labels(map_db, 1) == ["101", "102", "103"]
    assert labels(map_db, 1) == ["101", "102", "103"]
    assert calls == [1]
    assert timing.cache_stats()["demo"] == {"hits": 1, "misses": 1}


def test_cache_refreshes_when_the_file_changes(map_db, counted_query):
    labels, calls = counted_query
    labels(map_db, 1)
    write_row(
        map_db, "UPDATE anchor_point_description SET description = '104' WHERE id = 3"
    )
    assert labels(map_db, 1) == ["101", "102", "104"]
    assert calls == [1, 1]


def test_callers_cannot_change_the_cached_value(map_db, counted_query):
    labels, _ = counted_query
    labels(map_db, 1).append("999")
    assert labels(map_db, 1) == ["101", "102", "103"]


def test_least_recently_used_entries_are_dropped(map_db, counted_query):
    labels, calls = counted_query
    labels(map_db, 1)
    labels(map_db, 2)
    labels(map_db, 3)  # maxsize 2: floor 1 is dropped
    labels(map_db, 1)
    assert calls == [1, 2, 3, 1]


def test_none_and_errors_are_not_cached(map_db):
    calls = []

    @file_version.cached_by_file("flaky")
    def flaky(db_path, value):
        calls.append(value)
        if value == "boom":
            raise sqlite3.OperationalError("locked")

    for _ in range(2):
        assert flaky(map_db, "none") is None
        with pytest.raises(sqlite3.OperationalError):
            flaky(map_db, "boom")
    assert calls == ["none", "boom", "none", "boom"]


# ---- Real map queries: cached results equal the uncached SQL ----

LABELS = [
    "101",
    "105",
    "10",
    "T015",
    "t015",
    " 105 ",
    "Lift",
    "Stairs",
    "G",
    "B",
    "W",
    "Store",
    "zzz",
    "202",
    "T202",
    "Entry",
]


@pytest.mark.parametrize("building", ["AB-01", "Lab-Complex"])
def test_cached_queries_match_the_sql(building):
    db_path, _ = get_building_paths(building)
    checks = []
    for label in LABELS:
        checks += [
            (db_access.has_exact_label, (db_path, label)),
            (db_access.find_places_anywhere, (db_path, label)),
        ]
        checks += [(db_access.find_places, (db_path, label, f)) for f in range(6)]
    for floor in range(6):
        checks += [
            (db_access.get_connectors, (db_path, floor)),
            (db_access.get_coordinates, (db_path, "Lift %", floor)),
            (db_access.get_coordinates, (db_path, "Stairs %", floor)),
        ]
    checks += [
        (db_access.get_coordinates, (db_path, "%Entry%", 0)),
        (db_access.find_described, (db_path, place_lookup.TOILET_MARKS)),
    ]
    for _, spot in db_access.get_connectors(db_path, 1)[:3]:
        checks.append((db_access.get_description, (db_path, spot, 1)))

    for function, args in checks:
        uncached = inspect.unwrap(function)
        assert function(*args) == uncached(*args), (function.__name__, args)
        assert function(*args) == uncached(*args), (function.__name__, args)


# ---- Floor graphs ----


def test_graph_is_built_once_and_frozen(map_db):
    first = graph_converter.create_graph_from_db(map_db, 1)
    assert graph_converter.create_graph_from_db(map_db, 1) is first
    assert nx.is_frozen(first)
    with pytest.raises(nx.NetworkXError):
        first.add_edge(1, 3)


def test_graph_refreshes_when_the_map_changes(map_db):
    graph_converter.create_graph_from_db(map_db, 1)
    write_row(
        map_db,
        "INSERT INTO connections (point_a_id, point_b_id, distance) VALUES (1, 3, 1.5)",
    )
    assert graph_converter.create_graph_from_db(map_db, 1).has_edge(1, 3)


def test_one_route_reads_each_file_version_once(monkeypatch):
    db_path, _ = get_building_paths("AB-01")
    reads = []
    real = file_version.read_version
    monkeypatch.setattr(
        file_version, "read_version", lambda p: reads.append(p) or real(p)
    )
    route = db_shortest_path_maker.main(db_path, "105", "201")
    assert route["complexity"] == "complex"
    # The map database and the teacher database, once each
    assert len(reads) == len(set(reads)) <= 2


# ---- SVG drawing: a cached template, drawn in memory ----

FLOOR_SVG = """<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">
  <g id="Path" class="cls-1">
    <polyline class="cls-1" points="0 0 10 0 20 0"/>
    <polyline class="cls-1" points="0 0 0 10"/>
  </g>
</svg>
"""


@pytest.fixture
def floor_file(tmp_path):
    path = tmp_path / "floor 1 copy path.svg"
    path.write_text(FLOOR_SVG, encoding="utf-8")
    return path


def visible_segments(svg: bytes) -> set:
    root = ET.fromstring(svg)
    return {
        (line.get("x1"), line.get("y1"), line.get("x2"), line.get("y2"))
        for line in root.xpath("//svg:g[@id='Path']//svg:line", namespaces=SVG_NS)
        if line.get("visibility") == "visible"
    }


def test_drawing_writes_no_files(floor_file, tmp_path):
    before = sorted(tmp_path.iterdir())
    svg = svg_manipulator.render_floor(str(floor_file), [(0.0, 0.0), (10.0, 0.0)])
    assert svg.startswith(b"<?xml")
    assert sorted(tmp_path.iterdir()) == before


def test_template_is_reused_until_the_floor_map_changes(floor_file):
    svg_manipulator.render_floor(str(floor_file), [(0.0, 0.0), (10.0, 0.0)])
    svg_manipulator.render_floor(str(floor_file), [(0.0, 0.0), (0.0, 10.0)])
    assert timing.cache_stats()["svg_template"] == {"hits": 1, "misses": 1}

    stat = floor_file.stat()
    floor_file.write_text(
        FLOOR_SVG.replace("0 0 0 10", "0 0 0 20 0 30"), encoding="utf-8"
    )
    os.utime(floor_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    svg = svg_manipulator.render_floor(str(floor_file), [(0.0, 0.0), (0.0, 20.0)])
    assert visible_segments(svg) == {("0.0", "0.0", "0.0", "20.0")}


def test_each_drawing_starts_from_the_clean_template(floor_file):
    svg_manipulator.render_floor(str(floor_file), [(0.0, 0.0), (10.0, 0.0)])
    svg = svg_manipulator.render_floor(str(floor_file), [(0.0, 0.0), (0.0, 10.0)])
    assert visible_segments(svg) == {("0.0", "0.0", "0.0", "10.0")}
    assert len(ET.fromstring(svg).xpath("//svg:marker", namespaces=SVG_NS)) == 1


def test_drawing_in_many_threads_at_once(floor_file):
    routes = [[(0.0, 0.0), (10.0, 0.0)], [(0.0, 0.0), (0.0, 10.0)]] * 8
    results: list[bytes | None] = [None] * len(routes)

    def draw(i):
        results[i] = svg_manipulator.render_floor(str(floor_file), routes[i])

    threads = [threading.Thread(target=draw, args=(i,)) for i in range(len(routes))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for points, svg in zip(routes, results, strict=True):
        assert svg is not None
        (x1, y1), (x2, y2) = points
        assert visible_segments(svg) == {(str(x1), str(y1), str(x2), str(y2))}


@pytest.mark.parametrize(
    ("start", "end"), [("101", "105"), ("105", "201"), ("301", "male toilet")]
)
def test_real_routes_are_drawn_correctly(start, end):
    db_path, floor_path = get_building_paths("AB-01")
    route = db_shortest_path_maker.main(db_path, start, end)
    assert route["complexity"] != "failed"
    legs = [route["path"]] if route["complexity"] == "simple" else route["path"]
    maps = svg_manipulator.render_route(floor_path, route)
    assert [floor for floor, _ in maps] == [floor for floor, _ in legs]
    for (_, points), (_, svg) in zip(legs, maps, strict=True):
        assert MapData.drawn_problems(svg, points) == []


# ---- In-memory route maps ----


def test_route_maps_round_trip():
    store = route_maps.RouteMapStore()
    route_id = store.save({("AB-01", 1): b"<svg/>", ("AB-01", 2): b"<svg>2</svg>"})
    assert route_maps.is_route_id(route_id)
    assert store.get(route_id, "AB-01", 2) == b"<svg>2</svg>"
    assert store.get(route_id, "AB-01", 3) is None
    assert store.get("0" * 32, "AB-01", 1) is None


@pytest.mark.parametrize("bad", ["", "abc", "../../etc/passwd", "G" * 32, "0" * 33])
def test_malformed_route_ids_are_refused(bad):
    assert not route_maps.is_route_id(bad)


def test_route_maps_expire():
    now = [1000.0]
    store = route_maps.RouteMapStore(ttl_seconds=600, clock=lambda: now[0])
    route_id = store.save({("AB-01", 1): b"<svg/>"})
    now[0] += 599
    assert store.get(route_id, "AB-01", 1) == b"<svg/>"
    now[0] += 2
    assert store.get(route_id, "AB-01", 1) is None


def test_oldest_route_maps_go_first_when_memory_is_full():
    store = route_maps.RouteMapStore(max_bytes=10)
    first = store.save({("AB-01", 1): b"12345"})
    second = store.save({("AB-01", 1): b"12345"})
    third = store.save({("AB-01", 1): b"12345"})
    assert store.get(first, "AB-01", 1) is None
    assert store.get(second, "AB-01", 1) == b"12345"
    assert store.get(third, "AB-01", 1) == b"12345"


# ---- API ----


@pytest.fixture
def api():
    from app import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def route_request(start: str, end: str, building: str | None = "AB-01"):
    return {"start": start, "end": end, "building": building, "preference": None}


def test_cross_floor_maps_are_served_by_route_id(api):
    response = api.post("/process_path", json=route_request("105", "201"))
    assert response.status_code == 200
    db_path, _ = get_building_paths("AB-01")
    legs = svg_manipulator.route_legs(
        db_shortest_path_maker.main(db_path, "105", "201")
    )
    for url, (_, points) in zip(response.json()["files"].values(), legs, strict=True):
        assert "&route=" in url
        svg = api.get(url)
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert MapData.drawn_problems(svg.content, points) == []


def test_same_floor_map_comes_back_directly(api):
    response = api.post("/process_path", json=route_request("101", "105"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content.startswith(b"<?xml")


def test_routes_write_no_files(api):
    data_dir = Path(env_variables["db_path"]).parent
    before = {p for p in data_dir.rglob("*") if p.is_file()}
    api.post("/process_path", json=route_request("105", "201"))
    api.post("/process_path", json=route_request("101", "105"))
    after = {p for p in data_dir.rglob("*") if p.is_file()}
    assert {p for p in after - before if p.suffix == ".svg"} == set()


def test_missing_building_is_refused(api):
    response = api.post(
        "/process_path", json=route_request("101", "105", building=None)
    )
    assert response.status_code == 400
    assert "building" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"floor": "1", "building": "AB-01"}, 400),
        ({"floor": "1", "building": "AB-01", "route": "../../x"}, 400),
        ({"floor": "1", "building": "AB-01", "route": "f" * 32}, 404),
        ({"floor": "one", "building": "AB-01", "route": "f" * 32}, 400),
    ],
)
def test_map_requests_need_a_valid_route_id(api, params, status):
    response = api.get("/load_shortest_path_svg", params=params)
    assert response.status_code == status


def test_expired_map_says_to_search_again(api):
    response = api.get(
        "/load_shortest_path_svg",
        params={"floor": "1", "building": "AB-01", "route": "f" * 32},
    )
    assert "search for the route again" in response.json()["detail"]


def test_concurrent_routes_each_get_their_own_map():
    from app import app

    # Different floor-1 -> floor-2 routes: before, they all shared two map files
    db_path, _ = get_building_paths("AB-01")
    expected = {}
    for start, end in profile_routes.route_pairs(6):
        route = db_shortest_path_maker.main(db_path, start, end)
        assert route["complexity"] == "complex"
        expected[(start, end)] = route["path"]

    async def fire():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post("/process_path", json=route_request(s, e))
                    for s, e in expected
                )
            )
            maps = []
            for response in responses:
                assert response.status_code == 200
                urls = response.json()["files"].values()
                maps.append([(await client.get(url)).content for url in urls])
            return maps

    for legs, svgs in zip(expected.values(), asyncio.run(fire()), strict=True):
        for (_, points), svg in zip(legs, svgs, strict=True):
            assert MapData.drawn_problems(svg, points) == []


def test_a_slow_route_does_not_block_other_requests(monkeypatch):
    from app import app

    def slow_route(start, end, building, preference):
        time.sleep(0.5)
        return {
            "path": None,
            "total_weight": None,
            "complexity": "failed",
            "error": "slow",
            "cause": "input",
        }

    monkeypatch.setattr(route_utilary, "make_svg", slow_route)

    async def fire():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            slow = asyncio.create_task(
                client.post("/process_path", json=route_request("101", "105"))
            )
            await asyncio.sleep(0.05)
            started = time.perf_counter()
            fast = await client.get(
                "/load_svg", params={"floor": "1", "building": "AB-01"}
            )
            fast_seconds = time.perf_counter() - started
            return fast, fast_seconds, await slow

    fast, fast_seconds, slow = asyncio.run(fire())
    assert fast.status_code == 200
    assert slow.status_code == 400
    assert fast_seconds < 0.3, "the fast request waited for the slow route"


def test_template_hides_and_unstyles_walkways_once(floor_file):
    template = svg_manipulator.floor_template(str(floor_file))
    root = ET.fromstring(template.svg)
    group = root.find(".//svg:g[@id='Path']", namespaces=SVG_NS)
    assert group is not None
    lines = group.xpath(".//svg:line", namespaces=SVG_NS)
    assert [line.get("visibility") for line in lines] == ["hidden"] * len(lines)
    assert all("class" not in element.attrib for element in group.iter())
    # Each walkway segment points at its line, in document order
    assert template.lines_by_segment == {
        ((0.0, 0.0), (10.0, 0.0)): [0],
        ((10.0, 0.0), (20.0, 0.0)): [1],
        ((0.0, 0.0), (0.0, 10.0)): [2],
    }
