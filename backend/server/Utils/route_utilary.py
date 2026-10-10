import os
import sqlite3
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse

from . import place_lookup
from .api_types import MessageBody
from .building_handling import get_building_paths
from .db_access import NewTeacher, TeacherRecord, add_teacher_to_db, get_teacher_data
from .loader import env_variables
from .route_types import (
    BuildingFloorFiles,
    BuildingRoute,
    FailedRoute,
    MultiBuildingFilesResponse,
    MultiBuildingRequest,
    PathFilesResponse,
    PathRequest,
)
from .svg_manipulator import (
    floor_svg as floor_svg_location,
)
from .svg_manipulator import (
    main as make_svg,
)
from .svg_manipulator import (
    multi_building_nav as make_svg_multi,
)
from .svg_manipulator import (
    output as output_svg_location,
)


class InvalidInputError(Exception):
    pass


# input: what was typed can't be used; route: the map has no way between valid
# places; internal: something broke on the server
STATUS_FOR_CAUSE = {"input": 400, "route": 422, "internal": 500}


def route_failure(result: FailedRoute) -> HTTPException:
    return HTTPException(
        status_code=STATUS_FOR_CAUSE[result["cause"]], detail=result["error"]
    )


def shortest_path_svg_url(floor: int, building: str | None) -> str:
    return f"/load_shortest_path_svg?floor={floor}&building={building}"


async def process_path_logic(
    data: PathRequest,
) -> tuple[PathFilesResponse | FileResponse, int]:
    start = data["start"]
    end = data["end"]
    building = data["building"]

    if not start or not end:
        raise HTTPException(status_code=400, detail="Start and end points are required")
    if start == end:
        raise HTTPException(
            status_code=400, detail="Start and end points cannot be the same"
        )

    result = make_svg(start, end, building, data["preference"])
    _, _, output_path = get_building_paths(building_name=building)

    if result["complexity"] == "failed":
        raise route_failure(result)

    if result["complexity"] == "simple":
        floor_no, _ = result["path"]
        output_svg = output_svg_location(floor_no, output_path)
        return FileResponse(output_svg, media_type="image/svg+xml"), 200

    (start_floor, _), (end_floor, _) = result["path"]
    response: PathFilesResponse = {
        "files": {
            "start_floor": shortest_path_svg_url(start_floor, building),
            "end_floor": shortest_path_svg_url(end_floor, building),
        }
    }
    if "note" in result:
        response["note"] = result["note"]
    return response, 200


def building_floor_files(
    building_name: str, building_route: BuildingRoute
) -> BuildingFloorFiles:
    if building_route["complexity"] == "simple":
        floor_no, _ = building_route["path"]
        return {"start_floor": shortest_path_svg_url(floor_no, building_name)}

    if building_route["complexity"] == "complex":
        (start_floor, _), (end_floor, _) = building_route["path"]
        return {
            "start_floor": shortest_path_svg_url(start_floor, building_name),
            "end_floor": shortest_path_svg_url(end_floor, building_name),
        }

    return {}


async def process_multi_building(
    data: MultiBuildingRequest,
) -> tuple[MultiBuildingFilesResponse, int]:
    building_name_1 = data["building_name_1"]
    building_1_start = data["Start Location"]
    building_name_2 = data["building_name_2"]
    building_2_end = data["End Location"]

    if not building_1_start or not building_2_end:
        raise HTTPException(status_code=400, detail="Start and end points are required")
    if building_1_start == building_2_end and building_name_1 == building_name_2:
        raise HTTPException(
            status_code=400, detail="Start and end points cannot be the same"
        )

    result = make_svg_multi(
        building_name_1, building_1_start, building_name_2, building_2_end
    )

    if result["complexity"] == "failed":
        raise route_failure(result)

    return {
        "files": {
            building_name: building_floor_files(building_name, building_route)
            for building_name, building_route in result["path"].items()
        }
    }, 200


async def prepare_custom_data(data: dict[str, Any]) -> PathRequest:
    custom_type = data.get("type")
    custom_start = data.get("start")
    custom_end = data.get("end")
    preference = data.get("preference")
    building_name = data.get("building")

    if not custom_type or not custom_start or not custom_end:
        raise HTTPException(
            status_code=400, detail="Missing required fields: type, start, or end"
        )

    if custom_type.lower() == "teacher cabin":
        try:
            # Cabins are listed in the teacher database, not the building's map
            teacher_db = str(env_variables["teacher_des"])
            start_room = get_room_no_by_cabin(custom_start, teacher_db) or custom_start
            end_room = get_room_no_by_cabin(custom_end, teacher_db) or custom_end

            return {
                "start": start_room,
                "end": end_room,
                "preference": preference,
                "building": building_name,
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "start": custom_start,
        "end": custom_end,
        "preference": preference,
        "building": building_name,
    }


async def load_svg_logic(floor: str | None, building: str | None) -> FileResponse:
    if not floor or not building:
        raise HTTPException(
            status_code=400, detail="Floor/Building parameter is required"
        )

    _, floor_map, _ = get_building_paths(building)

    svg_path = floor_svg_location(floor, floor_map)
    if not os.path.exists(svg_path):
        raise HTTPException(
            status_code=404, detail=f"SVG file for floor {floor} not found"
        )

    return FileResponse(svg_path, media_type="image/svg+xml")


async def load_shortest_path_svg_logic(
    floor: str | None, building: str | None
) -> FileResponse:
    if not floor:
        raise HTTPException(status_code=400, detail="Floor parameter is required")

    _, _, output = get_building_paths(building)
    svg_path = output_svg_location(floor, output)
    if not os.path.exists(svg_path):
        raise HTTPException(
            status_code=404, detail=f"SVG file for floor {floor} not found"
        )

    return FileResponse(svg_path, media_type="image/svg+xml")


async def add_teacher(data: NewTeacher) -> MessageBody:
    try:
        add_teacher_to_db(str(env_variables["teacher_des"]), data)
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to add teacher: Database error: {e}"
        ) from e
    return {"message": "Teacher added successfully"}


async def retrieve_teachers(
    name: str | None = None,
    cabin_no: str | None = None,
    room_no: str | None = None,
) -> list[TeacherRecord]:
    try:
        teachers = get_teacher_data(
            str(env_variables["teacher_des"]), name, cabin_no, room_no
        )
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve teachers: Database error: {e}"
        ) from e

    if not teachers:
        raise HTTPException(status_code=404, detail="No teacher found")
    return teachers


def get_room_no_by_cabin(cabin_no: str, db_path: str) -> str | None:
    """The map room of a teacher's cabin, in any spelling ("G-01", "G1", "g-001")."""
    try:
        teachers = place_lookup.load_teachers(db_path)
    except sqlite3.Error as e:
        print(f"An error occurred while accessing the database: {e}")
        return None
    return place_lookup.room_for_cabin(teachers, cabin_no)
