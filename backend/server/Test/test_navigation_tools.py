import pytest

from Chatbot import navigation_tools
from Chatbot.navigation_tools import (
    create_navigation_tools,
    extract_room_numbers,
    route_between_buildings,
    route_within_building,
)
from Utils.route_types import (
    ComplexRoute,
    FailedRoute,
    MultiBuildingRoute,
    SimpleRoute,
)

SIMPLE: SimpleRoute = {
    "path": (3, [(0.0, 0.0), (1.0, 1.0)]),
    "total_weight": 2.0,
    "complexity": "simple",
    "error": None,
}
COMPLEX: ComplexRoute = {
    "path": [(3, [(0.0, 0.0)]), (4, [(1.0, 1.0)])],
    "total_weight": 9.0,
    "complexity": "complex",
    "error": None,
}
FAILED: FailedRoute = {
    "path": None,
    "total_weight": None,
    "complexity": "failed",
    "error": "Start location '999' not found on this building's map.",
    "cause": "input",
}


@pytest.fixture
def engine_calls(monkeypatch):
    calls = []

    def fake_route(start, end, building, preference=None):
        calls.append((start, end, building, preference))
        return {"301": SIMPLE, "302": COMPLEX}.get(start, FAILED)

    monkeypatch.setattr(navigation_tools, "render_route", fake_route)
    return calls


def leg(building: str, floor: int, route_id: str = "") -> dict:
    url = f"/load_shortest_path_svg?floor={floor}&building={building}"
    return {
        "building": building,
        "floor": floor,
        "url": url + (f"&route={route_id}" if route_id else ""),
    }


def test_simple_route_has_start_floor_only(engine_calls):
    summary, artifact = route_within_building("301", "305", "AB-01")
    assert artifact == {
        "files": {
            "start_floor": "/load_shortest_path_svg?floor=3&building=AB-01",
            "legs": [leg("AB-01", 3)],
        }
    }
    assert "301" in summary and "305" in summary
    assert engine_calls == [("301", "305", "AB-01", None)]


def test_complex_route_has_both_floors(engine_calls):
    _, artifact = route_within_building("302", "405", "AB-01", "Lift")
    assert artifact == {
        "files": {
            "start_floor": "/load_shortest_path_svg?floor=3&building=AB-01",
            "end_floor": "/load_shortest_path_svg?floor=4&building=AB-01",
            "legs": [leg("AB-01", 3), leg("AB-01", 4)],
        }
    }
    assert engine_calls[-1][3] == "Lift"


def test_fallback_note_reaches_the_summary(monkeypatch):
    with_note: ComplexRoute = {
        **COMPLEX,
        "note": "No lift connects floor 3 and floor 4, so this route uses the stairs.",
    }
    monkeypatch.setattr(
        navigation_tools, "render_route", lambda *_args, **_kwargs: with_note
    )
    summary, _ = route_within_building("302", "405", "AB-01", "Lift")
    assert summary.endswith("so this route uses the stairs.")


def test_find_route_describes_toilet_requests():
    find_route = create_navigation_tools()[0]
    assert "female toilet" in find_route.description


def test_failed_route_returns_error_and_no_files(engine_calls):
    summary, artifact = route_within_building("999", "301", "AB-01")
    assert artifact is None
    assert "not found" in summary


def test_same_start_and_end_does_not_call_engine(engine_calls):
    summary, artifact = route_within_building("301", "301", "AB-01")
    assert artifact is None
    assert engine_calls == []
    assert "same" in summary.lower()


def test_route_between_buildings(monkeypatch):
    route: MultiBuildingRoute = {
        "path": {"AB-01": SIMPLE, "Lab-Complex": COMPLEX},
        "total_weight": 11.0,
        "complexity": "multi-building",
        "error": None,
    }
    monkeypatch.setattr(
        navigation_tools, "render_multi_building_route", lambda *args: route
    )
    _, artifact = route_between_buildings("301", "AB-01", "405", "Lab-Complex")
    assert artifact == {
        "files": {
            "start_floor": "/load_shortest_path_svg?floor=3&building=AB-01",
            "end_floor": "/load_shortest_path_svg?floor=4&building=Lab-Complex",
            "legs": [leg("AB-01", 3), leg("Lab-Complex", 3), leg("Lab-Complex", 4)],
        }
    }


def test_route_between_buildings_shows_the_exit_and_entrance_floors(monkeypatch):
    # AB-01 202 (floor 2) down to the exit on floor 0, then the Lab Complex
    # entrance on floor 0 up to 202 on floor 2: four maps, in walking order
    leaving: ComplexRoute = {
        "path": [(2, [(0.0, 0.0)]), (0, [(1.0, 1.0)])],
        "total_weight": 5.0,
        "complexity": "complex",
        "error": None,
        "map_id": "a" * 32,
    }
    arriving: ComplexRoute = {
        "path": [(0, [(2.0, 2.0)]), (2, [(3.0, 3.0)])],
        "total_weight": 6.0,
        "complexity": "complex",
        "error": None,
        "map_id": "a" * 32,
    }
    route: MultiBuildingRoute = {
        "path": {"AB-01": leaving, "Lab-Complex": arriving},
        "total_weight": 11.0,
        "complexity": "multi-building",
        "error": None,
    }
    monkeypatch.setattr(
        navigation_tools, "render_multi_building_route", lambda *args: route
    )
    _, artifact = route_between_buildings("202", "AB-01", "202", "Lab-Complex")
    assert artifact is not None
    assert artifact["files"].get("legs") == [
        leg("AB-01", 2, "a" * 32),
        leg("AB-01", 0, "a" * 32),
        leg("Lab-Complex", 0, "a" * 32),
        leg("Lab-Complex", 2, "a" * 32),
    ]
    # The two-map fields stay for older pages: where you start, where you arrive
    assert artifact["files"].get("start_floor") == leg("AB-01", 2, "a" * 32)["url"]
    assert artifact["files"].get("end_floor") == leg("Lab-Complex", 2, "a" * 32)["url"]


def test_find_route_tool_returns_content_and_artifact(engine_calls):
    tools = {tool.name: tool for tool in create_navigation_tools()}
    assert set(tools) == {"find_route", "find_route_between_buildings"}
    message = tools["find_route"].invoke(
        {
            "type": "tool_call",
            "name": "find_route",
            "args": {"start": "301", "end": "305"},
            "id": "call_1",
        }
    )
    assert message.artifact == {
        "files": {
            "start_floor": "/load_shortest_path_svg?floor=3&building=AB-01",
            "legs": [leg("AB-01", 3)],
        }
    }
    assert "301" in message.content


@pytest.mark.parametrize(
    "text, expected",
    [
        ("how do i go from 301 to 405", [301, 405]),
        ("path from room 301", [301]),
        ("no rooms here", []),
        ("from 301 to 2405", [301, 405]),
    ],
)
def test_extract_room_numbers(text, expected):
    assert extract_room_numbers(text) == expected
