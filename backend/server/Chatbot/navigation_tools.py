import re
from typing import Literal

from langchain_core.tools import BaseTool, tool
from typing_extensions import TypedDict

from Utils.route_types import BuildingFloorFiles
from Utils.route_utilary import building_floor_files
from Utils.svg_manipulator import main as render_route
from Utils.svg_manipulator import multi_building_nav as render_multi_building_route

Building = Literal["AB-01", "Lab-Complex"]
Preference = Literal["Lift", "Stairs"]


class RouteArtifact(TypedDict):
    # Map links shown by the frontend; not sent to the model
    files: BuildingFloorFiles


def extract_room_numbers(text: str) -> list[int]:
    """Up to two 3-digit room numbers; a 4-digit "2xxx" (e.g. spoken "2405") counts as the second."""
    result = [int(match) for match in re.findall(r"\b\d{3}\b", text)[:2]]

    match_4_digit = re.search(r"\b2(\d{3})\b", text)
    if match_4_digit:
        result.insert(1, int(match_4_digit.group(1)))

    return result


def route_within_building(
    start: str, end: str, building: Building, preference: Preference | None = None
) -> tuple[str, RouteArtifact | None]:
    start, end = start.strip(), end.strip()
    if not start or not end:
        return "A start and an end location are both required.", None
    if start == end:
        return "The start and end locations are the same.", None

    route = render_route(start, end, building, preference)
    if route["complexity"] == "failed":
        return (
            f"No route found from {start} to {end} in {building}: {route['error']}",
            None,
        )

    files = building_floor_files(building, route)
    floors = (
        f"floor {route['path'][0]}"
        if route["complexity"] == "simple"
        else f"floors {route['path'][0][0]} to {route['path'][1][0]} via lift/stairs"
    )
    summary = (
        f"Route found from {start} to {end} in {building} ({floors}). "
        "The map is shown to the user."
    )
    if route["complexity"] == "complex" and "note" in route:
        summary += f" {route['note']}"
    return summary, {"files": files}


def route_between_buildings(
    start: str, start_building: Building, end: str, end_building: Building
) -> tuple[str, RouteArtifact | None]:
    route = render_multi_building_route(start_building, start, end_building, end)
    if route["complexity"] == "failed":
        return f"No route found from {start} to {end}: {route['error']}", None

    start_files = building_floor_files(start_building, route["path"][start_building])
    end_files = building_floor_files(end_building, route["path"][end_building])
    # Two map panels: where you start, and where you arrive
    files: BuildingFloorFiles = {}
    if "start_floor" in start_files:
        files["start_floor"] = start_files["start_floor"]
    arrival = end_files.get("end_floor") or end_files.get("start_floor")
    if arrival:
        files["end_floor"] = arrival

    summary = (
        f"Route found from {start} ({start_building}) to {end} ({end_building}). "
        "The maps are shown to the user."
    )
    return summary, {"files": files}


def create_navigation_tools() -> list[BaseTool]:
    @tool(response_format="content_and_artifact", parse_docstring=True)
    def find_route(
        start: str,
        end: str,
        building: Building = "AB-01",
        preference: Preference | None = None,
    ) -> tuple[str, RouteArtifact | None]:
        """Find a walking route between two places in the same building and show it on the map.

        A place is a room number or label ("301", "T504"), a teacher's cabin ("G-12")
        or name, or a toilet request: "male toilet", "female toilet" or "toilet"
        routes to the nearest one.

        Args:
            start: Start place, e.g. "301", "T504", "G-12" or a teacher's name.
            end: Destination place, or "male toilet" / "female toilet" / "toilet".
            building: Building code; "AB-01" unless the user names another building.
            preference: "Lift" or "Stairs" when the user states a preference.
        """
        return route_within_building(start, end, building, preference)

    @tool(response_format="content_and_artifact", parse_docstring=True)
    def find_route_between_buildings(
        start: str, start_building: Building, end: str, end_building: Building
    ) -> tuple[str, RouteArtifact | None]:
        """Find a route from a room in one building to a room in another building.

        Args:
            start: Start room number in start_building.
            start_building: Building the user starts in.
            end: Destination room number in end_building.
            end_building: Building the user is going to.
        """
        return route_between_buildings(start, start_building, end, end_building)

    return [find_route, find_route_between_buildings]
