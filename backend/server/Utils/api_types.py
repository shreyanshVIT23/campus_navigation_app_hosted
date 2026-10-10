from typing_extensions import TypedDict


class ErrorBody(TypedDict):
    error: str


class MessageBody(TypedDict):
    message: str
