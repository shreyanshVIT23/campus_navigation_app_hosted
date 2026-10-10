from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .event_types import (
    Attendee,
    EventWithCount,
    attendee_from_row,
    event_with_count_from_row,
)
from .helpers import (
    create_event,
    delete_event,
    get_db_connection,
    get_event_by_id,
    remove_attendee,
    update_event,
)
from .user_auth_jwt import get_current_user

user_events_router = APIRouter(prefix="/user/events", tags=["user events"])


@user_events_router.get("")
@user_events_router.get("/")
async def list_user_events(
    current_user_id: str = Depends(get_current_user),
) -> list[EventWithCount]:

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


@user_events_router.post("")
@user_events_router.post("/")
async def create_user_event(
    request: Request, current_user_id: str = Depends(get_current_user)
):

    data = await request.json()

    data["user_id"] = current_user_id
    result, status_code = create_event(data)
    return JSONResponse(content=result, status_code=status_code)


@user_events_router.get("/{event_id}")
async def get_user_event(
    event_id: str, current_user_id: str = Depends(get_current_user)
) -> EventWithCount:

    event = get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return event


@user_events_router.put("/{event_id}")
async def update_user_event(
    event_id: str, request: Request, current_user_id: str = Depends(get_current_user)
):

    event = get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    data = await request.json()
    result, status_code = update_event(event_id, data)
    return JSONResponse(content=result, status_code=status_code)


@user_events_router.delete("/{event_id}")
async def delete_user_event(
    event_id: str, current_user_id: str = Depends(get_current_user)
):

    event = get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    result, status_code = delete_event(event_id)
    return JSONResponse(content=result, status_code=status_code)


@user_events_router.get("/{event_id}/attendees")
async def get_user_event_attendees(
    event_id: str, current_user_id: str = Depends(get_current_user)
) -> list[Attendee]:

    event = get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, attendee_name, attendee_email, registration_date
            FROM registrations
            WHERE event_id = ?
            """,
            (event_id,),
        )
        attendees = [attendee_from_row(row) for row in cursor.fetchall()]
    return attendees


@user_events_router.delete("/{event_id}/attendees/{attendee_id}")
async def delete_user_event_attendee(
    event_id: str, attendee_id: str, current_user_id: str = Depends(get_current_user)
):

    event = get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if str(event["user_id"]) != current_user_id:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to modify attendees for this event",
        )

    result, status_code = remove_attendee(event_id, attendee_id)
    return JSONResponse(content=result, status_code=status_code)
