"""Per-request stage timings: the timed() helper, the request middleware (log line
and Server-Timing header), logging setup, and no print() on the request path."""

import ast
import asyncio
import logging
import logging.handlers
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

from Utils import log_setup, timing

SERVER_DIR = Path(__file__).resolve().parent.parent
METRIC = r"[\w.-]+(;desc=\"[^\"]*\")?(;dur=\d+(\.\d+)?)?"
SERVER_TIMING = re.compile(rf"^{METRIC}(, {METRIC})*$")


@pytest.fixture
def clock(monkeypatch):
    """A fake perf_counter that returns the given seconds in order."""

    def use(*seconds: float):
        ticks = iter(seconds)
        monkeypatch.setattr(timing, "clock", lambda: next(ticks))

    return use


@pytest.fixture(autouse=True)
def fresh_cache_stats():
    timing.reset_cache_stats()
    yield
    timing.reset_cache_stats()


# ---- timed() ----


def test_timed_block_records_the_stage(clock):
    clock(1.0, 1.25)
    with timing.collect() as timings, timing.timed("graph"):
        pass
    assert timings.stages == {"graph": 250.0}
    assert timings.calls == {"graph": 1}


def test_timed_decorator_adds_up_every_call(clock):
    @timing.timed("lookup")
    def lookup(value):
        return value * 2

    clock(0.0, 0.010, 1.0, 1.030)
    with timing.collect() as timings:
        assert lookup(2) == 4
        assert lookup(3) == 6
    assert timings.stages == {"lookup": pytest.approx(40.0)}
    assert timings.calls == {"lookup": 2}


def test_nested_stages_are_measured_separately(clock):
    clock(0.0, 1.0, 3.0, 6.0)
    with (
        timing.collect() as timings,
        timing.timed("route"),
        timing.timed("graph"),
    ):
        pass
    assert timings.stages == {"route": 6000.0, "graph": 2000.0}


def test_a_failing_stage_is_still_recorded(clock):
    clock(0.0, 0.5)
    with (
        timing.collect() as timings,
        pytest.raises(ValueError),
        timing.timed("route"),
    ):
        raise ValueError("bad input")
    assert timings.stages == {"route": 500.0}


def test_timed_outside_a_request_only_logs(clock, caplog):
    clock(0.0, 0.002)
    with caplog.at_level(logging.DEBUG, logger="Utils.timing"), timing.timed("graph"):
        pass
    assert timing.current() is None
    assert "stage=graph ms=2.00" in caplog.text


def test_cache_events_are_counted_per_request_and_overall():
    with timing.collect() as timings:
        timing.cache_event("graph", hit=False)
        timing.cache_event("graph", hit=True)
        timing.cache_event("graph", hit=True)
        timing.cache_event("svg", hit=False)
    assert timings.cache == {"graph:hit": 2, "graph:miss": 1, "svg:miss": 1}
    assert timing.cache_stats() == {
        "graph": {"hits": 2, "misses": 1},
        "svg": {"hits": 0, "misses": 1},
    }


def test_server_timing_header_format():
    timings = timing.RequestTimings()
    timings.add("graph", 12.3456)
    timings.add("svg.write", 3.0)
    timings.cache["graph:hit"] = 1
    header = timing.server_timing_header(timings, total_ms=20.0)
    assert SERVER_TIMING.match(header), header
    assert header.startswith("total;dur=20.00")
    assert "graph;dur=12.35" in header
    assert 'cache;desc="graph:hit=1"' in header


# ---- Request middleware ----


def demo_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(timing.TimingMiddleware)

    @app.get("/stage/{name}")
    async def stage(name: str, wait: float = 0.0):
        with timing.timed(name):
            await asyncio.sleep(wait)
        return {"stage": name}

    @app.get("/thread")
    async def in_thread():
        def work():
            with timing.timed("worker"):
                timing.cache_event("graph", hit=True)
            return "done"

        return {"result": await run_in_threadpool(work)}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    return app


def test_middleware_logs_one_summary_line(caplog):
    with caplog.at_level(logging.INFO, logger="Utils.timing"):
        response = TestClient(demo_app()).get("/stage/graph")
    assert response.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "Utils.timing"]
    assert len(lines) == 1
    assert re.match(r"GET /stage/graph 200 \d+\.\dms graph=\d+\.\d", lines[0])


def test_middleware_sets_the_server_timing_header():
    response = TestClient(demo_app()).get("/stage/graph")
    header = response.headers["server-timing"]
    assert SERVER_TIMING.match(header), header
    assert header.startswith("total;dur=")
    assert "graph;dur=" in header


def test_stages_in_a_worker_thread_reach_the_request():
    response = TestClient(demo_app()).get("/thread")
    header = response.headers["server-timing"]
    assert "worker;dur=" in header
    assert 'cache;desc="graph:hit=1"' in header


def test_concurrent_requests_keep_their_own_timings():
    async def both():
        transport = httpx.ASGITransport(app=demo_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # The slow request is still running while the fast one finishes
            return await asyncio.gather(
                client.get("/stage/slow", params={"wait": 0.05}),
                client.get("/stage/fast"),
            )

    slow, fast = asyncio.run(both())
    assert "slow;dur=" in slow.headers["server-timing"]
    assert "fast" not in slow.headers["server-timing"]
    assert "fast;dur=" in fast.headers["server-timing"]
    assert "slow" not in fast.headers["server-timing"]


def test_a_crashing_request_is_logged_as_500(caplog):
    client = TestClient(demo_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="Utils.timing"):
        assert client.get("/boom").status_code == 500
    assert any("GET /boom 500" in r.getMessage() for r in caplog.records)


def test_the_app_reports_route_stages():
    from app import app

    response = TestClient(app).post(
        "/process_path",
        json={"start": "105", "end": "201", "building": "AB-01", "preference": None},
    )
    assert response.status_code == 200
    header = response.headers["server-timing"]
    for stage in ("route", "translate", "resolve", "graph", "dijkstra", "svg"):
        assert f"{stage};dur=" in header, header


# ---- Logging setup ----


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    yield root
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_log_level_is_respected(restore_root_logger):
    log_setup.configure_logging("debug", None)
    assert restore_root_logger.level == logging.DEBUG
    log_setup.configure_logging("WARNING", None)
    assert restore_root_logger.level == logging.WARNING


def test_unknown_log_level_is_refused(restore_root_logger):
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        log_setup.configure_logging("loud", None)


def test_log_file_rotates(restore_root_logger, tmp_path):
    log_file = tmp_path / "logs" / "backend.log"
    log_setup.configure_logging("INFO", log_file)
    rotating = [
        h
        for h in restore_root_logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 1024 * 1024
    assert rotating[0].backupCount == 5
    logging.getLogger("Utils.test").info("written to the file")
    rotating[0].flush()
    assert "written to the file" in log_file.read_text(encoding="utf-8")


# ---- No print() on the request path ----

REQUEST_PATH_MODULES = [
    "app.py",
    "Utils/db_access.py",
    "Utils/db_shortest_path_maker.py",
    "Utils/graph_converter.py",
    "Utils/place_lookup.py",
    "Utils/route_utilary.py",
    "Utils/svg_manipulator.py",
    "Utils/timing.py",
    "Chatbot/chat_bot.py",
    "Chatbot/audio_to_text.py",
    "Chatbot/navigation_tools.py",
    "Chatbot/teacher_tools.py",
]
# Developer helpers that print a report and are never called by a request
DEVELOPER_HELPERS = {"example", "check"}


def prints_on_request_path(source: str) -> list[int]:
    lines: list[int] = []

    def visit(node: ast.AST, skipped: bool):
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            skipped = True
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in DEVELOPER_HELPERS
        ):
            skipped = True
        if (
            not skipped
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, skipped)

    visit(ast.parse(source), False)
    return lines


def test_the_print_finder_skips_main_blocks_and_helpers():
    source = (
        "def route():\n    print('x')\n"
        "def example():\n    print('y')\n"
        "if __name__ == '__main__':\n    print('z')\n"
    )
    assert prints_on_request_path(source) == [2]


@pytest.mark.parametrize("module", REQUEST_PATH_MODULES)
def test_no_print_on_the_request_path(module):
    source = (SERVER_DIR / module).read_text(encoding="utf-8")
    assert prints_on_request_path(source) == [], f"use a logger in {module}"
