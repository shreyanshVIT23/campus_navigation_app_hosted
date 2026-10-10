"""Campus assistant: a Gemini tool-calling agent (LangChain) run as a LangGraph loop.

START -> transcribe (voice only) -> agent <-> tools -> END
                                     \\-> fallback (Gemini unavailable) -> END
"""

import base64
import binascii
import io
import json
import os
import random
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal

import httpx
from google.genai import errors as genai_errors
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
from langgraph.cache.memory import InMemoryCache
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError
from langgraph.types import CachePolicy
from typing_extensions import NotRequired, TypedDict

from Utils.route_types import BuildingFloorFiles

from .__init__ import env_variables
from .navigation_tools import (
    create_navigation_tools,
    extract_room_numbers,
    route_within_building,
)
from .teacher_tools import TeacherKnowledge, create_teacher_tools
from .transcription import TRANSCRIBE_PROMPT, WAV_MIME_TYPES, recognize_wav

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
CHAT_CACHE_TTL = int(os.getenv("CHAT_CACHE_TTL", "3600"))
CHAT_CACHE_MAX_SIZE = int(os.getenv("CHAT_CACHE_MAX_SIZE", "256"))
# Tool rounds per question before answering without Gemini
MAX_TOOL_ROUNDS = 4
GRAPH_RECURSION_LIMIT = 25

# Errors from the Gemini client: API errors (quota, auth, bad request) and network failures
LLM_ERRORS = (genai_errors.APIError, ChatGoogleGenerativeAIError, httpx.HTTPError)
# Errors a tool may raise; reported back to the model as an error ToolMessage
TOOL_ERRORS = (ToolInvocationError, ValueError, LookupError, sqlite3.Error)

SYSTEM_PROMPT = """You are the VIT Bhopal campus assistant. You answer questions \
about faculty members and give directions between rooms.
- Always look facts up with the tools; never answer from memory.
- For a question about a teacher, call get_teacher_profile with their name. If it \
returns candidates, ask the user which teacher they mean.
- For topics that name no teacher (e.g. "who works on machine learning"), call \
search_faculty_knowledge.
- For directions, call find_route (or find_route_between_buildings). To reach a \
teacher, first get their room from get_teacher_profile, then route to that room.
- Answer only with information from tool results, in 1-2 complete sentences. If the \
tools do not have the answer, say you don't have that information. Never invent \
details."""

UNTRANSCRIBABLE_REPLY = (
    "Sorry, I couldn't understand that audio. Please try again or type your question."
)
ROUTE_REPLY = "Here is the route on the map."

LEADING_FILLER = re.compile(
    r"^(Based on the context provided|Based on the context|According to the context"
    r"|According to the provided context|From the context provided|From the context"
    r"|I think|Based on|According to)[\s,:]*",
    re.IGNORECASE,
)


def default_api_key() -> str | None:
    key = env_variables.get("gemini_api_key")
    return key if isinstance(key, str) and key not in ("", "None") else None


DEFAULT_API_KEY = default_api_key()


class AudioInput(TypedDict):
    base64: str
    mime_type: str


class AgentState(MessagesState):
    user_input: NotRequired[str]
    audio: NotRequired[AudioInput | None]
    transcript: NotRequired[str | None]
    tool_rounds: NotRequired[int]
    llm_failed: NotRequired[bool]
    # Set when the deterministic fallback produced the answer (never cached)
    used_fallback: NotRequired[bool]
    # Map links from the offline fallback (tool routes travel as ToolMessage artifacts)
    files: NotRequired[BuildingFloorFiles | None]


class AgentUpdate(TypedDict, total=False):
    messages: list[AnyMessage]
    user_input: str
    transcript: str | None
    tool_rounds: int
    llm_failed: bool
    used_fallback: bool
    files: BuildingFloorFiles | None


class ChatResult(TypedDict):
    response: str
    files: NotRequired[BuildingFloorFiles]
    transcript: NotRequired[str]


def build_default_llm(api_key: str | None) -> BaseChatModel | None:
    if not api_key:
        return None
    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        api_key=api_key,
        # No temperature: Gemini 3 models use fixed sampling defaults and ignore it
        max_tokens=400,
        timeout=30,
        max_retries=2,
    )


def ensure_complete_sentences(text):
    if not text or len(text) < 5:
        return text

    text = text.strip()
    if text[-1] not in (".", "!", "?"):
        last_period = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
        if last_period != -1 and last_period > len(text) * 0.7:
            return text[: last_period + 1]
        return text + "."

    return text


def format_answer_summary(text):
    if not text:
        return text
    lines = [line.strip("- *").strip() for line in text.split("\n") if line.strip()]
    if len(lines) > 2:
        return " ".join(lines[:2])
    return text.strip()


def clean_answer(text: str) -> str:
    cleaned = LEADING_FILLER.sub("", text.strip()).strip()
    cleaned = re.sub(r"\*\*|\*", "", cleaned)
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return ensure_complete_sentences(cleaned)


def normalize_query(text: str) -> str:
    return " ".join(text.lower().split())


def conversation_key(state: AgentState) -> str:
    """Cache key for the agent node: the conversation without per-run message ids."""
    parts = []
    for message in state["messages"]:
        part: dict[str, object] = {"type": message.type, "content": message.content}
        if isinstance(message, AIMessage):
            part["tool_calls"] = [
                [call["name"], call["args"], call["id"]] for call in message.tool_calls
            ]
        if isinstance(message, ToolMessage):
            part["tool_call_id"] = message.tool_call_id
        parts.append(part)
    return json.dumps(parts, sort_keys=True, default=str)


class Chatbot:
    def __init__(
        self,
        db_path: str | Path = env_variables["teacher_des"],
        llm: BaseChatModel | None = None,
        api_key: str | None = DEFAULT_API_KEY,
    ):
        self.knowledge = TeacherKnowledge(db_path)
        self.llm = llm if llm is not None else build_default_llm(api_key)
        self.tools = [*create_teacher_tools(self.knowledge), *create_navigation_tools()]
        self.llm_with_tools: Runnable[LanguageModelInput, AIMessage] | None = (
            self.llm.bind_tools(self.tools) if self.llm is not None else None
        )

        self.fallback_responses = [
            "I don't have specific information about that.",
            "That detail isn't available in my knowledge base.",
            "I can only answer based on the information I have about the teacher.",
        ]

        self.cache_ttl = CHAT_CACHE_TTL
        self.cache_max_size = CHAT_CACHE_MAX_SIZE
        self.response_cache: OrderedDict[str, tuple[float, ChatResult]] = OrderedDict()

        self.graph = self._build_graph()

    def reload_knowledge_base(self):
        self.knowledge.reload()
        self.response_cache.clear()
        self.graph.clear_cache()

    # ============================================================
    # LANGGRAPH NODES
    # ============================================================

    def transcribe_node(self, state: AgentState) -> AgentUpdate:
        audio = state.get("audio")
        transcript = self._transcribe(audio) if audio else ""
        if not transcript:
            return {
                "transcript": None,
                "messages": [AIMessage(content=UNTRANSCRIBABLE_REPLY)],
            }
        return {
            "transcript": transcript,
            "user_input": transcript,
            "messages": [HumanMessage(content=transcript)],
        }

    def agent_node(self, state: AgentState) -> AgentUpdate:
        if self.llm_with_tools is None:
            return {"llm_failed": True}
        try:
            reply = self.llm_with_tools.invoke(
                [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
            )
        except LLM_ERRORS:
            return {"llm_failed": True}

        update: AgentUpdate = {"messages": [reply]}
        if reply.tool_calls:
            update["tool_rounds"] = state.get("tool_rounds", 0) + 1
        return update

    def fallback_node(self, state: AgentState) -> AgentUpdate:
        answer, files = self.fallback_answer(state.get("user_input", ""))
        return {
            "messages": [AIMessage(content=answer)],
            "files": files,
            "used_fallback": True,
        }

    # ============================================================
    # LANGGRAPH ROUTERS
    # ============================================================

    def route_entry(self, state: AgentState) -> Literal["transcribe", "agent"]:
        return "transcribe" if state.get("audio") else "agent"

    def route_after_transcribe(self, state: AgentState) -> Literal["agent", "done"]:
        return "agent" if state.get("transcript") else "done"

    def route_after_agent(
        self, state: AgentState
    ) -> Literal["tools", "fallback", "done"]:
        if state.get("llm_failed"):
            return "fallback"
        last = state["messages"][-1] if state["messages"] else None
        if not isinstance(last, AIMessage):
            return "fallback"
        if not last.tool_calls:
            # An empty final answer is treated like an unavailable model
            return "done" if last.text.strip() else "fallback"
        if state.get("tool_rounds", 0) > MAX_TOOL_ROUNDS:
            return "fallback"
        return "tools"

    def _build_graph(self):
        builder = StateGraph(AgentState)

        builder.add_node("transcribe", self.transcribe_node)
        builder.add_node(
            "agent",
            self.agent_node,
            cache_policy=CachePolicy(key_func=conversation_key, ttl=self.cache_ttl),
        )
        builder.add_node("tools", ToolNode(self.tools, handle_tool_errors=TOOL_ERRORS))
        builder.add_node("fallback", self.fallback_node)

        builder.add_conditional_edges(
            START, self.route_entry, {"transcribe": "transcribe", "agent": "agent"}
        )
        builder.add_conditional_edges(
            "transcribe", self.route_after_transcribe, {"agent": "agent", "done": END}
        )
        builder.add_conditional_edges(
            "agent",
            self.route_after_agent,
            {"tools": "tools", "fallback": "fallback", "done": END},
        )
        builder.add_edge("tools", "agent")
        builder.add_edge("fallback", END)

        return builder.compile(cache=InMemoryCache())

    # ============================================================
    # TRANSCRIPTION AND OFFLINE FALLBACK
    # ============================================================

    def _transcribe(self, audio: AudioInput) -> str:
        if self.llm is not None:
            request = HumanMessage(
                content=[
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                    {
                        "type": "audio",
                        "base64": audio["base64"],
                        "mime_type": audio["mime_type"],
                    },
                ]
            )
            try:
                return self.llm.invoke([request]).text.strip()
            except LLM_ERRORS:
                pass

        if audio["mime_type"] in WAV_MIME_TYPES:
            try:
                wav_bytes = base64.b64decode(audio["base64"], validate=True)
            except binascii.Error:
                return ""
            transcript = recognize_wav(io.BytesIO(wav_bytes))
            if not transcript.startswith(("Error", "Unexpected error")):
                return transcript.strip()
        return ""

    def fallback_answer(self, text: str) -> tuple[str, BuildingFloorFiles | None]:
        """Answer without Gemini: routes, then teacher facts, then topic search."""
        rooms = extract_room_numbers(text)
        if len(rooms) >= 2:
            start, end = str(rooms[0]), str(rooms[1])
            summary, artifact = route_within_building(start, end, "AB-01")
            if artifact is None:
                return summary, None
            return f"Here is the route from {start} to {end}.", artifact["files"]

        key = self.knowledge.resolve(text)["teacher"]
        if key is not None:
            answer = self.knowledge.best_answer(key, text)
            if answer:
                return ensure_complete_sentences(format_answer_summary(answer)), None
            entry = self.knowledge.entry(key)
            if entry["cabin_no"] or entry["room_no"]:
                cabin_str = f"Cabin: {entry['cabin_no'] or 'N/A'}"
                room_str = f"Room: {entry['room_no'] or 'N/A'}"
                return (
                    f"{entry['display_name']} is a faculty member at VIT Bhopal "
                    f"({cabin_str}, {room_str})."
                ), None

        search_answer = self.knowledge.best_search_answer(text)
        if search_answer:
            return ensure_complete_sentences(format_answer_summary(search_answer)), None

        return random.choice(self.fallback_responses), None

    # ============================================================
    # PUBLIC API
    # ============================================================

    def chat(self, user_input: str) -> ChatResult:
        cache_key = normalize_query(user_input)
        cached = self._get_cached_response(cache_key)
        if cached is not None:
            return cached

        state: AgentState = {
            "messages": [HumanMessage(content=user_input)],
            "user_input": user_input,
        }
        result, cacheable = self._run(state)
        if cacheable:
            self._set_cached_response(cache_key, result)
        return result

    def chat_audio(self, audio_bytes: bytes, mime_type: str) -> ChatResult:
        state: AgentState = {
            "messages": [],
            "audio": {
                "base64": base64.b64encode(audio_bytes).decode("ascii"),
                "mime_type": mime_type,
            },
        }
        result, _ = self._run(state)
        return result

    def respond(self, user_input: str) -> str:
        return self.chat(user_input)["response"]

    def _run(self, state: AgentState) -> tuple[ChatResult, bool]:
        """Run the graph; returns the result and whether it may be cached."""
        try:
            final = self.graph.invoke(
                state, config={"recursion_limit": GRAPH_RECURSION_LIMIT}
            )
        except GraphRecursionError:
            answer, files = self.fallback_answer(state.get("user_input", ""))
            fallback: ChatResult = {"response": answer}
            if files:
                fallback["files"] = files
            return fallback, False

        files = final.get("files") or self._route_files(final["messages"])
        last = final["messages"][-1] if final["messages"] else None
        answer = (
            clean_answer(last.text)
            if isinstance(last, AIMessage) and not last.tool_calls
            else ""
        )
        if not answer:
            answer = ROUTE_REPLY if files else random.choice(self.fallback_responses)

        result: ChatResult = {"response": answer}
        if files:
            result["files"] = files
        transcript = final.get("transcript")
        if transcript:
            result["transcript"] = transcript

        cacheable = (
            not files
            and not final.get("llm_failed")
            and not final.get("used_fallback")
            and answer not in self.fallback_responses
        )
        return result, cacheable

    @staticmethod
    def _route_files(messages: list[AnyMessage]) -> BuildingFloorFiles | None:
        # The latest route found by find_route / find_route_between_buildings
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
                files = message.artifact.get("files")
                if files:
                    return files
        return None

    # ============================================================
    # RESPONSE CACHE
    # ============================================================

    def _get_cached_response(self, key: str) -> ChatResult | None:
        entry = self.response_cache.get(key)
        if entry is None:
            return None

        timestamp, result = entry
        if time.monotonic() - timestamp > self.cache_ttl:
            del self.response_cache[key]
            return None

        self.response_cache.move_to_end(key)
        return result

    def _set_cached_response(self, key: str, result: ChatResult):
        self.response_cache[key] = (time.monotonic(), result)
        self.response_cache.move_to_end(key)
        while len(self.response_cache) > self.cache_max_size:
            self.response_cache.popitem(last=False)


if __name__ == "__main__":
    chatbot = Chatbot()
    print(chatbot.respond("how many years of experience does ravi verma have"))
