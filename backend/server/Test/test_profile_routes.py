"""The benchmark tool (Utils.profile_routes) measures every scenario on temporary
copies and never touches the real data."""

from pathlib import Path

import pytest

from Utils import profile_routes
from Utils.test_copies import use_copies

SERVER_DIR = Path(__file__).resolve().parent.parent


def snapshot(folder: Path) -> dict[str, int]:
    return {
        str(path): path.stat().st_mtime_ns
        for path in folder.rglob("*")
        if path.is_file()
    }


@pytest.fixture(scope="module")
def report():
    before = snapshot(SERVER_DIR / "DB")
    result = profile_routes.run(repeat=1, micro=True, concurrent=4, profile=True)
    after = snapshot(SERVER_DIR / "DB")
    assert after == before, "the benchmark wrote to the real data"
    return result


def test_every_scenario_is_timed(report):
    names = {scenario["name"] for scenario in report["scenarios"]}
    assert names == {scenario.name for scenario in profile_routes.SCENARIOS}
    for scenario in report["scenarios"]:
        assert scenario["status"] == scenario["expected_status"], scenario
        assert scenario["median_ms"] > 0
        assert scenario["p90_ms"] >= scenario["median_ms"]


def test_scenarios_report_their_stages(report):
    cross_floor = next(s for s in report["scenarios"] if s["name"] == "cross floor")
    for stage in ("route", "graph", "dijkstra", "svg"):
        assert stage in cross_floor["stages_ms"], cross_floor["stages_ms"]


def test_every_micro_benchmark_is_measured(report):
    names = {bench["name"] for bench in report["micro"]}
    for expected in (
        "graph build (floor 5)",
        "node lookup: full scan",
        "node lookup: dict",
        "db.has_exact_label",
        "db.find_places",
        "db.find_places_anywhere",
        "db.get_connectors",
        "teachers",
        "translate_place",
        "resolve_candidates",
        "svg: draw route on floor 5",
        "svg: parse a file written earlier",
        "svg: parse a file just written",
        "engine only: cross floor",
    ):
        assert expected in names, expected
    for bench in report["micro"]:
        assert bench["median_ms"] >= 0
        assert bench["runs"] >= 1


def test_profile_lists_top_functions_and_shares(report):
    profile = report["profile"]
    assert len(profile["top_cumulative"]) > 0
    assert len(profile["top_own"]) > 0
    shares = profile["share_of_own_time"]
    assert set(shares) == set(profile_routes.BUCKETS) | {"other"}
    assert sum(shares.values()) == pytest.approx(1.0, abs=0.01)


def test_concurrency_checks_each_map(report):
    concurrency = report["concurrency"]
    assert len(concurrency["requests"]) == 4
    for request in concurrency["requests"]:
        assert request["status"] == 200
        assert isinstance(request["own_route"], bool)
    assert concurrency["total_ms"] > 0


def test_compare_reports_speedups():
    before = {
        "scenarios": [{"name": "a", "median_ms": 100.0}],
        "micro": [{"name": "graph", "median_ms": 10.0}],
    }
    after = {
        "scenarios": [{"name": "a", "median_ms": 25.0}],
        "micro": [{"name": "graph", "median_ms": 0.5}],
    }
    rows = profile_routes.compare(before, after)
    assert rows == [
        {
            "kind": "scenario",
            "name": "a",
            "before_ms": 100.0,
            "after_ms": 25.0,
            "speedup": 4.0,
        },
        {
            "kind": "micro",
            "name": "graph",
            "before_ms": 10.0,
            "after_ms": 0.5,
            "speedup": 20.0,
        },
    ]


def test_copies_must_be_made_before_the_config_loads(tmp_path):
    # Utils.loader is already imported in this test process
    with pytest.raises(RuntimeError, match="before Utils.loader"):
        use_copies(tmp_path)


def test_latency_counts_from_the_moment_all_requests_were_sent(report):
    concurrency = report["concurrency"]
    # The last answer arrives at the end of the batch, so its latency is the total
    assert concurrency["latency_max_ms"] == pytest.approx(
        concurrency["total_ms"], rel=0.2
    )


def test_compare_follows_a_renamed_benchmark():
    before = {"micro": [{"name": "svg: modify_svg (floor 5)", "median_ms": 28.0}]}
    after = {"micro": [{"name": "svg: draw route on floor 5", "median_ms": 7.0}]}
    assert profile_routes.compare(before, after) == [
        {
            "kind": "micro",
            "name": "svg: draw route on floor 5",
            "before_ms": 28.0,
            "after_ms": 7.0,
            "speedup": 4.0,
        }
    ]


def test_chat_scenario_reports_its_stages(report):
    chat = next(s for s in report["scenarios"] if s["name"].startswith("chatbot"))
    assert "chat.freshness" in chat["stages_ms"], chat["stages_ms"]


def test_profile_is_broken_down_by_scenario(report):
    by_scenario = report["profile"]["by_scenario"]
    assert set(by_scenario) == {s.name for s in profile_routes.SCENARIOS}
    for rows in by_scenario.values():
        assert rows and rows[0]["own_ms"] >= rows[-1]["own_ms"]
