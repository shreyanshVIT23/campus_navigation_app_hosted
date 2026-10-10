from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .event_types import EventWithCount, event_with_count_from_row
from .helpers import (
    cancel_registration,
    get_db_connection,
    get_event_by_id,
    register_for_event,
)

events_router = APIRouter(tags=["events"])


@events_router.get("")
@events_router.get("/")
async def list_events() -> list[EventWithCount]:

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT events.*, COUNT(registrations.id) as registered
            FROM events
            LEFT JOIN registrations ON events.id = registrations.event_id
            GROUP BY events.id
            """
        )
        events = [event_with_count_from_row(row) for row in cursor.fetchall()]
    return events


@events_router.get("/user-registrations")
async def get_user_registrations(email: str) -> list[str]:
    if not email:
        return []
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT event_id FROM registrations WHERE attendee_email = ?", (email,)
        )
        return [row["event_id"] for row in cursor.fetchall()]


@events_router.get("/{event_id}")
async def get_event_route(event_id: str) -> EventWithCount:

    event = get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


async def read_json_object(request: Request) -> dict[str, Any]:
    # Invalid JSON or a non-object body is treated as an empty payload.
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@events_router.post("/{event_id}/register")
async def register_event_route(event_id: str, request: Request):

    data = await read_json_object(request)
    result, status_code = register_for_event(event_id, data)
    return JSONResponse(content=result, status_code=status_code)


@events_router.post("/{event_id}/cancel-registration")
async def cancel_registration_route(event_id: str, request: Request):

    data = await read_json_object(request)

    email = data.get("email") or data.get("attendee_email")
    if not email:
        raise HTTPException(status_code=400, detail="Attendee email is required")

    result, status_code = cancel_registration(event_id, email)
    return JSONResponse(content=result, status_code=status_code)
