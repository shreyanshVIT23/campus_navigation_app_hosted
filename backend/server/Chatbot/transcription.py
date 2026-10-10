from typing import BinaryIO

import speech_recognition as sr
from speech_recognition.recognizers import google as google_recognizer

# Browsers record webm/ogg; Gemini understands these directly. speech_recognition
# (the offline fallback) can only read PCM WAV.
WAV_MIME_TYPES = {"audio/wav", "audio/x-wav", "audio/wave"}

# Audio formats Gemini accepts inline (https://ai.google.dev/gemini-api/docs/audio)
GEMINI_AUDIO_MIME_TYPES = WAV_MIME_TYPES | {
    "audio/mp3",
    "audio/mpeg",
    "audio/aiff",
    "audio/aac",
    "audio/ogg",
    "audio/flac",
    "audio/m4a",
    "audio/opus",
    "audio/webm",
}

TRANSCRIBE_PROMPT = (
    "Transcribe this speech exactly. Output only the spoken words, with no "
    "commentary. If there is no intelligible speech, output nothing."
)


def normalize_mime_type(content_type: str | None) -> str:
    # "audio/webm;codecs=opus" -> "audio/webm"
    return (content_type or "").split(";")[0].strip().lower()


def recognize_wav(audio: str | BinaryIO) -> str:
    """Speech-to-text with Google Speech Recognition; returns "Error: ..." on failure."""
    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(audio) as source:
            audio_data = recognizer.record(source)
        transcript = google_recognizer.recognize_legacy(recognizer, audio_data)
        # With show_all/with_confidence left False, the recognizer returns a str.
        if not isinstance(transcript, str):
            return "Error: Unexpected response from speech recognition service."
        return transcript
    except sr.UnknownValueError:
        return "Error: Speech in the audio file could not be understood."
    except sr.RequestError as e:
        return f"Error: Could not connect to Google Speech Recognition service: {e}"
    except (ValueError, OSError, EOFError) as e:
        # Unreadable / non-PCM audio files
        return f"Unexpected error: {e}"
