from pathlib import Path
from typing import Annotated

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import NotRequired, TypedDict

from Chatbot.audio_to_text import AudioProcessor
from Chatbot.chat_bot import Chatbot, ChatResult
from Chatbot.transcription import GEMINI_AUDIO_MIME_TYPES, normalize_mime_type
from Events_Management.event_management import user_events_router
from Events_Management.event_view import events_router
from Events_Management.user_management import users_router
from Utils.loader import env_variables
from Utils.route_types import BuildingFloorFiles
from Utils.route_utilary import (
    add_teacher,
    get_room_no_by_cabin,
    load_shortest_path_svg_logic,
    load_svg_logic,
    process_multi_building,
    process_path_logic,
    retrieve_teachers,
)

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router=events_router, prefix="/events")
app.include_router(router=users_router, prefix="/user")
app.include_router(router=user_events_router)

app.mount(
    "/server/Assets",
    StaticFiles(directory=env_variables["image_assets"]),
    name="assets",
)
app.mount(
    "/Assets",
    StaticFiles(directory=env_variables["image_assets"]),
    name="assets",
)

chatbot = Chatbot()

# Voice questions are short; Gemini's inline limit is 20 MB per request
MAX_AUDIO_BYTES = 10 * 1024 * 1024

UNEXPECTED_ERROR_PREFIXES = {
    "/custom_process": "An error occurred",
    "/chat": "An error occurred",
    "/chat/audio": "An error occurred",
    "/reload_knowledge": "Failed to reload knowledge base",
}


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    prefix = UNEXPECTED_ERROR_PREFIXES.get(
        request.url.path, "An unexpected error occurred"
    )
    return JSONResponse(status_code=500, content={"detail": f"{prefix}: {exc}"})


class PathData(BaseModel):
    start: str
    end: str
    preference: str | None = None
    building: str | None = None


class CustomPathData(BaseModel):
    type: str
    start: str
    end: str
    preference: str | None = None
    building: str | None = None


class MultiBuildingPathData(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    start: str = Field(alias="Start Location")
    end: str = Field(alias="End Location")
    building_name_1: str
    building_name_2: str


class TeacherData(BaseModel):
    name: str
    cabin_no: str
    room_no: str
    phone_number: str | None = None


class UserRegister(BaseModel):
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class ChatMessage(BaseModel):
    message: str


class ChatResponse(TypedDict):
    response: str
    status: str
    # Map links when the answer is a route (same shape as /process_path "files")
    files: NotRequired[BuildingFloorFiles]
    # What was heard, for /chat/audio
    transcript: NotRequired[str]


def chat_response(result: ChatResult) -> ChatResponse:
    body: ChatResponse = {"response": result["response"], "status": "success"}
    if "files" in result:
        body["files"] = result["files"]
    if "transcript" in result:
        body["transcript"] = result["transcript"]
    return body


@app.get("/load_svg")
async def load_svg(floor: str | None = None, building: str | None = None):
    return await load_svg_logic(floor, building)


@app.get("/load_shortest_path_svg")
async def load_shortest_path_svg(floor: str | None = None, building: str | None = None):
    return await load_shortest_path_svg_logic(floor, building)


# process_path_logic / process_multi_building raise HTTPException on failure.
@app.post("/process_path")
async def process_path(data: PathData):
    response, _ = await process_path_logic(
        {
            "start": data.start,
            "end": data.end,
            "preference": data.preference,
            "building": data.building,
        }
    )
    return response


@app.post("/multi_building_process_path")
async def multi_building_process_path(data: MultiBuildingPathData):
    response, _ = await process_multi_building(
        {
            "Start Location": data.start,
            "End Location": data.end,
            "building_name_1": data.building_name_1,
            "building_name_2": data.building_name_2,
        }
    )
    return response


@app.post("/custom_process")
async def custom_process(data: CustomPathData):
    custom_type = data.type
    custom_start = data.start
    custom_end = data.end

    if not custom_type or not custom_start or not custom_end:
        raise HTTPException(
            status_code=400, detail="Missing required fields: type, start, or end"
        )

    if custom_type.lower() != "teacher cabin":
        raise HTTPException(status_code=400, detail=f"Unsupported type: {custom_type}")

    db_path = str(env_variables["teacher_des"])

    try:
        start_room = get_room_no_by_cabin(custom_start, db_path) or custom_start
        end_room = get_room_no_by_cabin(custom_end, db_path) or custom_end
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    response, _ = await process_path_logic(
        {
            "start": start_room,
            "end": end_room,
            "preference": data.preference,
            "building": data.building,
        }
    )
    return response


@app.get("/teachers")
async def get_teachers(
    name: str | None = None, cabin_no: str | None = None, room_no: str | None = None
):
    return await retrieve_teachers(name, cabin_no, room_no)


@app.post("/teachers", status_code=201)
async def create_teacher(data: TeacherData):
    return await add_teacher(
        {
            "name": data.name,
            "cabin_no": data.cabin_no,
            "room_no": data.room_no,
            "phone_number": data.phone_number,
        }
    )


@app.post("/upload")
async def upload_audio(
    text: Annotated[str | None, Form()] = None,
    audio_file: Annotated[UploadFile | None, File()] = None,
):
    audio_dir = env_variables.get("audio_path")
    if not audio_dir:
        raise HTTPException(
            status_code=500, detail="AUDIO_DIR environment variable not set"
        )

    audio_dir_path = Path(audio_dir)
    audio_dir_path.mkdir(parents=True, exist_ok=True)

    audio_processor = AudioProcessor()

    if text:
        response = await audio_processor.process(text=text)
    elif audio_file:
        response = await audio_processor.process(audio_file=audio_file)
    else:
        raise HTTPException(
            status_code=400, detail="Either audio file or text input must be provided"
        )

    if isinstance(response, tuple) and len(response) == 2:
        result, status_code = response

        # If result is a FileResponse, return it directly
        if isinstance(result, FileResponse):
            return result

        # Otherwise, return as JSON
        headers = {
            "message": (
                "Text processed successfully"
                if text
                else "Audio file uploaded successfully"
            )
        }
        return JSONResponse(content=result, status_code=status_code, headers=headers)
    elif isinstance(response, dict):
        status_code = 400 if "error" in response else 200
        return JSONResponse(content=response, status_code=status_code)

    return JSONResponse(
        content={"error": "Invalid response format from processor"}, status_code=400
    )


@app.get("/search_teacher")
async def search_teacher(
    teacher_name: Annotated[str, Query(description="Name of the teacher to search")],
):
    if not teacher_name:
        raise HTTPException(status_code=400, detail="Teacher name is required.")

    audio_processor = AudioProcessor()
    result = audio_processor.get_teacher_details_with_preprocessing(teacher_name)

    return JSONResponse(content=result)


# The agent makes blocking Gemini/SQLite calls, so it runs in the threadpool.
@app.post("/chat")
async def chat(message: ChatMessage) -> ChatResponse:
    result = await run_in_threadpool(chatbot.chat, message.message)
    return chat_response(result)


@app.post("/chat/audio")
async def chat_audio(audio_file: Annotated[UploadFile, File()]) -> ChatResponse:
    mime_type = normalize_mime_type(audio_file.content_type)
    if mime_type not in GEMINI_AUDIO_MIME_TYPES:
        raise HTTPException(
            status_code=415, detail=f"Unsupported audio type: {mime_type or 'unknown'}"
        )

    audio_bytes = await audio_file.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file is too large.")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio file is empty.")

    result = await run_in_threadpool(chatbot.chat_audio, audio_bytes, mime_type)
    return chat_response(result)


@app.post("/reload_knowledge")
async def reload_knowledge():
    chatbot.reload_knowledge_base()
    return {"message": "Knowledge base reloaded successfully", "status": "success"}


if __name__ == "__main__":
    import uvicorn

    # Port 8000 matches the frontend's default NEXT_PUBLIC_API_BASE_URL
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
