import asyncio
import io
import os
import re
import sqlite3
from pathlib import Path

import nltk
import spacy
from fastapi import HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from rapidfuzz import fuzz, process
from typing_extensions import TypedDict
from werkzeug.datastructures import FileStorage

from Chatbot.__init__ import env_variables, process_data
from Chatbot.navigation_tools import extract_room_numbers
from Chatbot.transcription import recognize_wav
from Utils.api_types import ErrorBody
from Utils.route_types import PathFilesResponse

# Keys (including the space in "Phone Number") match the /search_teacher response
TeacherMatch = TypedDict(
    "TeacherMatch",
    {
        "Matched_Name": str,
        "Cabin": str | None,
        "Room_No": str | None,
        "Phone Number": str | None,
    },
)

PathProcessResult = tuple[PathFilesResponse | FileResponse | ErrorBody, int]

# NLTK data used here: word_tokenize -> punkt_tab, stopwords.words -> stopwords
NLTK_RESOURCES = {
    "tokenizers/punkt_tab": "punkt_tab",
    "corpora/stopwords": "stopwords",
}


def ensure_nltk_data(download_dir: str | None = None):
    for resource_path, package in NLTK_RESOURCES.items():
        try:
            nltk.data.find(resource_path)
        except LookupError:
            nltk.download(
                package, download_dir=download_dir, quiet=True, raise_on_error=True
            )


if os.getenv("DOCKER_ENV") or os.path.exists("/.dockerenv"):
    nltk_data_dir = "./nltk_data"
    nltk.data.path = [nltk_data_dir]
    ensure_nltk_data(nltk_data_dir)
else:
    ensure_nltk_data()


class AudioProcessor:
    def __init__(self):
        self.nlp = spacy.load("en_core_web_sm")

    def audio_to_text(self, file_path: str) -> str:
        if not Path(file_path).is_file():
            return "Error: The specified audio file was not found."
        return recognize_wav(file_path)

    @staticmethod
    def extract_3_digit_numbers(text: str) -> list[int]:
        return extract_room_numbers(text)

    @staticmethod
    def preprocess_query(query):
        stop_words = set(stopwords.words("english"))
        words = word_tokenize(query)
        keywords = [word for word in words if word.lower() not in stop_words]
        return " ".join(keywords)

    def extract_names(self, text):
        doc = self.nlp(text)
        names = [ent.text for ent in doc.ents if ent.label_ == "PERSON"]
        return "".join(names)

    def get_teacher_details_with_preprocessing(
        self, teacher_name, db_path=env_variables["teacher_des"]
    ) -> TeacherMatch | ErrorBody:
        conn = None
        try:
            teacher_name = re.sub(r"[^\w\s]", "", teacher_name)
            preprocessed_name_scypy = self.extract_names(teacher_name).lower()
            if not preprocessed_name_scypy or preprocessed_name_scypy in "kevin cabin":
                preprocessed_name_scypy = teacher_name.lower()
            preprocess_name_nltk = self.preprocess_query(teacher_name).lower()
            common_chars = "".join(
                [
                    char
                    for char in preprocessed_name_scypy
                    if char in preprocess_name_nltk
                ]
            )
            preprocessed_name = "".join(common_chars)

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                query = "SELECT name, cabin_no, room_no, phone_number FROM teachers"
                cursor.execute(query)
                records = cursor.fetchall()

            if not records:
                return {"error": "No data found in the database."}

            names = [row[0] for row in records]

            _, score, index = process.extractOne(
                preprocessed_name, names, scorer=fuzz.token_set_ratio
            )

            if score < 60:
                return {"error": "No close match found in the database."}

            matched_row = records[index]
            return {
                "Matched_Name": matched_row[0].title(),
                "Cabin": matched_row[1],
                "Room_No": matched_row[2],
                "Phone Number": matched_row[3],
            }
        except sqlite3.Error as e:
            return {"error": f"Database error: {e}"}
        finally:
            if conn:
                conn.close()

    @staticmethod
    def open_audio_as_filestorage(file_path):
        resolved_path = Path(__file__).parent / file_path
        try:
            file_content = resolved_path.read_bytes()
        except OSError as e:
            print(f"Error opening file: {e}")
            return None
        print(resolved_path.name)
        return FileStorage(io.BytesIO(file_content))

    async def process(
        self,
        audio_file: UploadFile | None = None,
        audio_loc: str | None = None,
        text: str | None = None,
        db_path: str | Path = env_variables["teacher_des"],
        audio_path: str | Path = env_variables["audio_path"],
        building: str = "AB-01",
    ) -> ErrorBody | PathProcessResult:
        resolved_db_path = Path(db_path).resolve()
        script_dir = Path(__file__).parent
        temp_audio_dir = audio_path

        if text:
            query = text
        else:
            if audio_loc:
                audio_path_resolved = Path(audio_loc)
                if not audio_path_resolved.is_absolute():
                    audio_path_resolved = (script_dir / audio_path_resolved).resolve()
            else:
                if not audio_file:
                    raise ValueError(
                        "Either audio file or text input must be provided."
                    )
                audio_path_resolved = Path(temp_audio_dir) / (
                    audio_file.filename or "uploaded_audio.wav"
                )
                audio_path_resolved.parent.mkdir(parents=True, exist_ok=True)

                audio_bytes = await audio_file.read()
                await run_in_threadpool(audio_path_resolved.write_bytes, audio_bytes)
            if not audio_path_resolved.exists():
                raise FileNotFoundError(f"Audio file not found: {audio_path_resolved}")
            query = self.audio_to_text(str(audio_path_resolved))
            print(query)
            if "Error" in query:
                return {"error": query}

        room_numbers = self.extract_3_digit_numbers(query)
        if len(room_numbers) == 0:
            return {"error": "Use at least 1 room number."}
        elif len(room_numbers) == 1:
            teacher = self.get_teacher_details_with_preprocessing(
                query, resolved_db_path
            )
            print("start: ", teacher)
            if "error" in teacher:
                return teacher
            if not teacher["Room_No"]:
                return {"error": "No room number recorded for the matched teacher."}
            start = teacher["Room_No"]
            end = str(room_numbers[0])
        else:
            start, end = map(str, room_numbers[:2])

        try:
            return await process_data(
                {"start": start, "end": end, "preference": None, "building": building}
            )
        except HTTPException as e:
            error: ErrorBody = {"error": f"An unexpected error occurred: {e.detail}"}
            return error, e.status_code


if __name__ == "__main__":
    processor = AudioProcessor()
    print(asyncio.run(processor.process(text="Path from 301 to 303")))
