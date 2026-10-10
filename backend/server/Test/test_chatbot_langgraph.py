import base64
import sqlite3

import httpx
import pytest
from chat_fakes import (
    POOJA_RESEARCH,
    RAVI_BACKGROUND,
    RAVI_BIO,
    ScriptedChatModel,
    answer_from_last_tool,
    build_faculty_db,
    tool_call,
)
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import app as app_module
from Chatbot import chat_bot as chat_bot_module
from Chatbot import navigation_tools
from Chatbot.chat_bot import Chatbot
from Utils import timing
from Utils.route_types import SimpleRoute

EXPERIENCE_QUESTION = "how many years of experience does ravi verma have"
UNKNOWN_QUESTION = "What is the weather on Mars?"
CHAT_SUCCESS_KEYS = {"response", "status"}
GEMINI_DOWN = httpx.ConnectError("Gemini is unreachable")

SIMPLE_ROUTE: SimpleRoute = {
    "path": (3, [(0.0, 0.0), (1.0, 1.0)]),
    "total_weight": 2.0,
    "complexity": "simple",
    "error": None,
}
ROUTE_FILES = {"start_floor": "/load_shortest_path_svg?floor=3&building=AB-01"}


@pytest.fixture
def faculty_db(tmp_path):
    return build_faculty_db(tmp_path / "faculty.db")


@pytest.fixture
def model():
    return ScriptedChatModel()


@pytest.fixture
def bot(faculty_db, model):
    return Chatbot(db_path=faculty_db, llm=model)


@pytest.fixture
def route_engine(monkeypatch):
    calls = []

    def fake_route(start, end, building, preference=None):
        calls.append((start, end, building))
        return SIMPLE_ROUTE

    monkeypatch.setattr(navigation_tools, "render_route", fake_route)
    return calls


@pytest.fixture
def chat_client(client, bot, monkeypatch):
    monkeypatch.setattr(app_module, "chatbot", bot)
    return client


def profile_then_answer(model):
    model.steps.extend(
        [
            tool_call("get_teacher_profile", {"name": "Ravi Verma"}),
            answer_from_last_tool("From the profile: "),
        ]
    )


# ============================================================
# GRAPH STRUCTURE
# ============================================================


def test_graph_has_expected_nodes(bot):
    assert {"transcribe", "agent", "tools", "fallback"} <= set(bot.graph.nodes)


def test_all_tools_are_bound_to_the_model(bot, model):
    assert set(model.bound_tool_names) == {
        "find_teacher",
        "get_teacher_profile",
        "search_faculty_knowledge",
        "find_route",
        "find_route_between_buildings",
    }


# ============================================================
# AGENT <-> TOOLS LOOP
# ============================================================


def test_agent_calls_profile_tool_and_answers_from_it(bot, model):
    profile_then_answer(model)
    result = bot.chat(EXPERIENCE_QUESTION)

    assert "12 years" in result["response"]
    assert "files" not in result
    assert len(model.calls) == 2
    first_call, second_call = model.calls
    assert isinstance(first_call[0], SystemMessage)
    assert isinstance(first_call[-1], HumanMessage)
    assert first_call[-1].content == EXPERIENCE_QUESTION
    tool_messages = [m for m in second_call if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert RAVI_BACKGROUND in str(tool_messages[0].content)


def test_respond_returns_text(bot, model):
    model.steps.append(AIMessage(content="Dr. Ravi Verma is an Associate Professor."))
    assert bot.respond("who is ravi verma") == (
        "Dr. Ravi Verma is an Associate Professor."
    )


def test_answer_text_is_cleaned(bot, model):
    model.steps.append(
        AIMessage(content="Based on the context, **he** has 12 years of experience")
    )
    assert bot.respond(EXPERIENCE_QUESTION) == "He has 12 years of experience."


def test_tool_error_is_reported_to_the_model(bot, model):
    model.steps.extend(
        [
            tool_call("get_teacher_profile", {"wrong_arg": "x"}),
            answer_from_last_tool(),
        ]
    )
    result = bot.chat("who is ravi verma")
    # The invalid call comes back as an error ToolMessage instead of crashing
    assert "error" in result["response"].lower()


def test_route_tool_puts_files_in_result(bot, model, route_engine):
    model.steps.extend(
        [
            tool_call("find_route", {"start": "301", "end": "305"}),
            AIMessage(content="Here is the route from 301 to 305."),
        ]
    )
    result = bot.chat("how do i get from 301 to 305")
    assert result["files"] == ROUTE_FILES
    assert route_engine == [("301", "305", "AB-01")]


def test_route_answers_are_not_cached(bot, model, route_engine):
    model.default = AIMessage(content="Here is the route.")
    for _ in range(2):
        model.steps.append(tool_call("find_route", {"start": "301", "end": "305"}))
        bot.chat("how do i get from 301 to 305")
    # The route is re-rendered each time (shared SVG files may have changed)
    assert len(route_engine) == 2


# ============================================================
# FALLBACK WHEN GEMINI IS UNAVAILABLE
# ============================================================


def test_fallback_answers_from_best_matching_entry(bot, model):
    model.default = GEMINI_DOWN
    assert bot.respond(EXPERIENCE_QUESTION) == RAVI_BACKGROUND
    assert bot.respond("who is ravi verma") == RAVI_BIO


def test_fallback_directory_only_teacher(bot, model):
    model.default = GEMINI_DOWN
    assert bot.respond("where is suresh kumar's cabin") == (
        "Suresh Kumar is a faculty member at VIT Bhopal (Cabin: C-202, Room: T402)."
    )


def test_empty_model_answer_uses_fallback(bot, model):
    model.steps.append(AIMessage(content=""))
    assert bot.respond(EXPERIENCE_QUESTION) == RAVI_BACKGROUND
    # A degraded answer is not cached
    assert len(bot.response_cache) == 0


def test_fallback_topic_question_uses_search(bot, model):
    model.default = GEMINI_DOWN
    assert bot.respond("who works on machine learning") == POOJA_RESEARCH


def test_fallback_unknown_question(bot, model):
    model.default = GEMINI_DOWN
    assert bot.respond(UNKNOWN_QUESTION) in bot.fallback_responses


def test_fallback_navigation_by_room_numbers(bot, model, route_engine):
    model.default = GEMINI_DOWN
    result = bot.chat("how do i go from 301 to 305")
    assert result["files"] == ROUTE_FILES
    assert route_engine == [("301", "305", "AB-01")]


def test_no_api_key_uses_fallback(faculty_db):
    offline_bot = Chatbot(db_path=faculty_db, llm=None, api_key=None)
    assert offline_bot.respond("who is ravi verma") == RAVI_BIO


def test_endless_tool_calls_hit_recursion_limit_and_fall_back(bot, model):
    model.default = tool_call("get_teacher_profile", {"name": "Ravi Verma"})
    assert bot.respond(EXPERIENCE_QUESTION) == RAVI_BACKGROUND


# ============================================================
# CACHING
# ============================================================


def test_response_cache_hit_skips_model(bot, model):
    profile_then_answer(model)
    first = bot.respond(EXPERIENCE_QUESTION)
    second = bot.respond("  HOW many years of experience does Ravi Verma have ")
    assert first == second
    assert len(model.calls) == 2


def test_agent_node_cache_replays_conversation(bot, model):
    profile_then_answer(model)
    first = bot.respond(EXPERIENCE_QUESTION)
    bot.response_cache.clear()
    # No new scripted steps: an uncached agent call would answer "Done."
    second = bot.respond(EXPERIENCE_QUESTION)
    assert second == first
    assert len(model.calls) == 2


def test_degraded_answers_are_not_cached(bot, model):
    model.default = GEMINI_DOWN
    bot.respond(UNKNOWN_QUESTION)
    bot.respond("who is ravi verma")
    assert len(bot.response_cache) == 0


def test_reload_clears_caches_and_sees_new_teacher(bot, model, faculty_db):
    profile_then_answer(model)
    bot.respond("who is ravi verma")
    assert len(bot.response_cache) == 1

    with sqlite3.connect(faculty_db) as conn:
        conn.execute(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES (?, ?, ?)",
            ("kavya iyer", "D-303", "T503"),
        )
        conn.commit()

    bot.reload_knowledge_base()
    assert len(bot.response_cache) == 0
    model.default = GEMINI_DOWN
    assert bot.respond("where is kavya iyer") == (
        "Kavya Iyer is a faculty member at VIT Bhopal (Cabin: D-303, Room: T503)."
    )


def test_a_teacher_database_change_is_picked_up_without_a_reload(
    bot, model, faculty_db
):
    profile_then_answer(model)
    bot.respond("who is ravi verma")
    assert len(bot.response_cache) == 1

    # Written by someone else (e.g. POST /teachers); nobody calls reload
    with sqlite3.connect(faculty_db) as conn:
        conn.execute(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES (?, ?, ?)",
            ("kavya iyer", "D-303", "T503"),
        )
        conn.commit()

    model.default = GEMINI_DOWN
    assert bot.respond("where is kavya iyer") == (
        "Kavya Iyer is a faculty member at VIT Bhopal (Cabin: D-303, Room: T503)."
    )
    # The stale answers were dropped too
    assert "who is ravi verma" not in bot.response_cache


def test_an_unchanged_teacher_database_keeps_the_caches(bot, model):
    profile_then_answer(model)
    bot.respond("who is ravi verma")
    calls = len(model.calls)
    bot.respond("who is ravi verma")
    assert len(model.calls) == calls


def test_freshness_checks_are_counted_and_reload_only_on_change(
    bot, model, faculty_db, monkeypatch
):
    reloads = []
    real_reload = bot.reload_knowledge_base
    monkeypatch.setattr(
        bot, "reload_knowledge_base", lambda: reloads.append(1) or real_reload()
    )
    timing.reset_cache_stats()
    model.default = GEMINI_DOWN
    for _ in range(3):
        bot.respond("who is ravi verma")
    assert reloads == []
    assert timing.cache_stats()["teacher_knowledge"] == {"hits": 3, "misses": 0}

    with sqlite3.connect(faculty_db) as conn:
        conn.execute(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES (?, ?, ?)",
            ("kavya iyer", "D-303", "T503"),
        )
        conn.commit()
    bot.respond("who is ravi verma")
    bot.respond("who is ravi verma")
    assert reloads == [1]
    assert timing.cache_stats()["teacher_knowledge"] == {"hits": 4, "misses": 1}


# ============================================================
# VOICE INPUT
# ============================================================


def test_audio_is_transcribed_then_answered(bot, model):
    model.steps.extend(
        [
            AIMessage(content=EXPERIENCE_QUESTION),
            tool_call("get_teacher_profile", {"name": "Ravi Verma"}),
            answer_from_last_tool(),
        ]
    )
    result = bot.chat_audio(b"fake-webm-bytes", "audio/webm")

    assert result["transcript"] == EXPERIENCE_QUESTION
    assert "12 years" in result["response"]
    transcribe_request = model.calls[0][-1]
    assert isinstance(transcribe_request, HumanMessage)
    audio_blocks = [
        block
        for block in transcribe_request.content
        if isinstance(block, dict) and block.get("type") == "audio"
    ]
    assert audio_blocks == [
        {
            "type": "audio",
            "base64": base64.b64encode(b"fake-webm-bytes").decode("ascii"),
            "mime_type": "audio/webm",
        }
    ]


def test_audio_transcript_feeds_route_tool(bot, model, route_engine):
    model.steps.extend(
        [
            AIMessage(content="take me from 301 to 305"),
            tool_call("find_route", {"start": "301", "end": "305"}),
            AIMessage(content="Here is the route."),
        ]
    )
    result = bot.chat_audio(b"fake", "audio/ogg")
    assert result["transcript"] == "take me from 301 to 305"
    assert result["files"] == ROUTE_FILES


def test_audio_without_gemini_non_wav_cannot_be_transcribed(bot, model):
    model.default = GEMINI_DOWN
    result = bot.chat_audio(b"fake", "audio/webm")
    assert "transcript" not in result
    assert result["response"] == chat_bot_module.UNTRANSCRIBABLE_REPLY


def test_audio_without_gemini_wav_uses_speech_recognition(bot, model, monkeypatch):
    model.default = GEMINI_DOWN
    monkeypatch.setattr(
        chat_bot_module, "recognize_wav", lambda audio: "who is ravi verma"
    )
    result = bot.chat_audio(b"RIFF-fake-wav", "audio/wav")
    assert result["transcript"] == "who is ravi verma"
    assert result["response"] == RAVI_BIO


# ============================================================
# HTTP CONTRACT (/chat, /chat/audio, /reload_knowledge)
# ============================================================


def test_chat_route_contract(chat_client, model):
    profile_then_answer(model)
    response = chat_client.post("/chat", json={"message": EXPERIENCE_QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == CHAT_SUCCESS_KEYS
    assert body["status"] == "success"
    assert "12 years" in body["response"]


def test_chat_route_contract_in_fallback_mode(chat_client, model):
    model.default = GEMINI_DOWN
    body = chat_client.post("/chat", json={"message": UNKNOWN_QUESTION}).json()
    assert set(body) == CHAT_SUCCESS_KEYS
    assert body["response"]


def test_chat_route_includes_files_for_routes(chat_client, model, route_engine):
    model.steps.extend(
        [
            tool_call("find_route", {"start": "301", "end": "305"}),
            AIMessage(content="Here is the route."),
        ]
    )
    body = chat_client.post("/chat", json={"message": "from 301 to 305"}).json()
    assert body == {
        "response": "Here is the route.",
        "status": "success",
        "files": ROUTE_FILES,
    }


def test_chat_route_missing_message_is_422(chat_client):
    assert chat_client.post("/chat", json={}).status_code == 422


def test_chat_route_error_shape(bot, monkeypatch):
    def broken_chat(user_input):
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "chatbot", bot)
    monkeypatch.setattr(bot, "chat", broken_chat)
    # The app-level handler sends the 500 JSON, then Starlette re-raises for logging.
    with TestClient(app_module.app, raise_server_exceptions=False) as raw_client:
        response = raw_client.post("/chat", json={"message": "hi"})
    assert response.status_code == 500
    assert response.json() == {"detail": "An error occurred: boom"}


def test_chat_audio_route(chat_client, model):
    model.steps.extend(
        [AIMessage(content="who is ravi verma"), AIMessage(content=RAVI_BIO)]
    )
    response = chat_client.post(
        "/chat/audio",
        files={"audio_file": ("voice.webm", b"fake", "audio/webm;codecs=opus")},
    )
    assert response.status_code == 200
    assert response.json() == {
        "response": RAVI_BIO,
        "status": "success",
        "transcript": "who is ravi verma",
    }


def test_chat_audio_rejects_unsupported_type(chat_client):
    response = chat_client.post(
        "/chat/audio", files={"audio_file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 415


def test_chat_audio_rejects_oversized_upload(chat_client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_AUDIO_BYTES", 8)
    response = chat_client.post(
        "/chat/audio",
        files={"audio_file": ("voice.webm", b"0123456789", "audio/webm")},
    )
    assert response.status_code == 413


def test_chat_audio_rejects_empty_upload(chat_client):
    response = chat_client.post(
        "/chat/audio", files={"audio_file": ("voice.webm", b"", "audio/webm")}
    )
    assert response.status_code == 400


def test_chat_audio_missing_file_is_422(chat_client):
    assert chat_client.post("/chat/audio").status_code == 422


def test_reload_knowledge_route_contract(chat_client, bot, model):
    profile_then_answer(model)
    bot.respond("who is ravi verma")
    assert len(bot.response_cache) == 1
    response = chat_client.post("/reload_knowledge")
    assert response.status_code == 200
    assert response.json() == {
        "message": "Knowledge base reloaded successfully",
        "status": "success",
    }
    assert len(bot.response_cache) == 0


def test_reload_knowledge_route_error_shape(bot, monkeypatch):
    def broken_reload():
        raise RuntimeError("db locked")

    monkeypatch.setattr(app_module, "chatbot", bot)
    monkeypatch.setattr(bot, "reload_knowledge_base", broken_reload)
    with TestClient(app_module.app, raise_server_exceptions=False) as raw_client:
        response = raw_client.post("/reload_knowledge")
    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to reload knowledge base: db locked"}
