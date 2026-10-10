import sqlite3

from typing_extensions import TypedDict

from Utils.api_types import ErrorBody, MessageBody


class EventRecord(TypedDict):
    id: str
    title: str
    date: str
    time: str
    location: str
    capacity: int
    description: str | None
    created_at: str
    user_id: int


class EventWithCount(EventRecord):
    registered: int


class Attendee(TypedDict):
    id: str
    attendee_name: str
    attendee_email: str
    registration_date: str


class RegistrationBody(TypedDict):
    message: str
    event: EventWithCount | None


EventResult = tuple[EventRecord | ErrorBody, int]
EventWithCountResult = tuple[EventWithCount | ErrorBody, int]
MessageResult = tuple[MessageBody | ErrorBody, int]
RegistrationResult = tuple[RegistrationBody | ErrorBody, int]


def event_from_row(row: sqlite3.Row) -> EventRecord:
    return {
        "id": row["id"],
        "title": row["title"],
        "date": row["date"],
        "time": row["time"],
        "location": row["location"],
        "capacity": row["capacity"],
        "description": row["description"],
        "created_at": row["created_at"],
        "user_id": row["user_id"],
    }


def event_with_count_from_row(row: sqlite3.Row) -> EventWithCount:
    return {**event_from_row(row), "registered": row["registered"]}


def attendee_from_row(row: sqlite3.Row) -> Attendee:
    return {
        "id": row["id"],
        "attendee_name": row["attendee_name"],
        "attendee_email": row["attendee_email"],
        "registration_date": row["registration_date"],
    }
