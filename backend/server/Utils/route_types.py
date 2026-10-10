from typing import Literal

from typing_extensions import NotRequired, TypedDict

Coordinates = tuple[float, float]
# (floor number, points along the path on that floor)
FloorPath = tuple[int, list[Coordinates]]


class SimpleRoute(TypedDict):
    path: FloorPath
    total_weight: float
    complexity: Literal["simple"]
    error: None
    # Set once the route's maps are drawn: the id they are served under
    map_id: NotRequired[str]


class ComplexRoute(TypedDict):
    # [start floor leg, end floor leg] joined by a lift or stairs
    path: list[FloorPath]
    total_weight: float
    complexity: Literal["complex"]
    error: None
    # Set when the lift/stairs preference could not be met and the other was used
    note: NotRequired[str]
    # Set once the route's maps are drawn: the id they are served under
    map_id: NotRequired[str]


# Why a route failed: "input" = what was typed can't be used, "route" = the map
# has no way between valid places, "internal" = something broke
FailureCause = Literal["input", "route", "internal"]


class FailedRoute(TypedDict):
    path: None
    total_weight: None
    # A literal tag (not None) so type checkers can narrow on "complexity"
    complexity: Literal["failed"]
    error: str
    cause: FailureCause


BuildingRoute = SimpleRoute | ComplexRoute | FailedRoute


class MultiBuildingRoute(TypedDict):
    # building name -> route inside that building
    path: dict[str, BuildingRoute]
    total_weight: float
    complexity: Literal["multi-building"]
    error: None


# ---- Route requests ----


class PathRequest(TypedDict):
    start: str
    end: str
    preference: str | None
    building: str | None


# Keys with spaces match the frontend payload for /multi_building_process_path
MultiBuildingRequest = TypedDict(
    "MultiBuildingRequest",
    {
        "Start Location": str,
        "End Location": str,
        "building_name_1": str,
        "building_name_2": str,
    },
)


# ---- API response bodies built from routes ----


class PathFloorFiles(TypedDict):
    start_floor: str
    end_floor: str


class PathFilesResponse(TypedDict):
    files: PathFloorFiles
    # e.g. "No lift connects floor 1 and floor 2, so this route uses the stairs."
    note: NotRequired[str]


class MapLeg(TypedDict):
    building: str
    floor: int
    url: str


class BuildingFloorFiles(TypedDict, total=False):
    start_floor: str
    end_floor: str
    # Every floor map of the route in walking order (chat only); a route between
    # buildings also has the exit and entrance floors, which the two fields above skip
    legs: list[MapLeg]


class MultiBuildingFilesResponse(TypedDict):
    files: dict[str, BuildingFloorFiles]
