"""Test helpers: a scripted chat model (no network) and a faculty fixture DB."""

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

# A scripted step: a fixed reply, an exception to raise, or a function of the
# conversation so far (used to answer from the latest tool result).
ScriptStep = AIMessage | Exception | Callable[[list[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    steps: list[ScriptStep] = Field(default_factory=list)
    # Used once the scripted steps run out
    default: ScriptStep = Field(default_factory=lambda: AIMessage(content="Done."))
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        self.bound_tool_names = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        step = self.steps.pop(0) if self.steps else self.default
        if isinstance(step, Exception):
            raise step
        message = step if isinstance(step, AIMessage) else step(messages)
        # A fresh copy per call: LangGraph assigns ids to the messages it stores, and
        # a reused object would replace the earlier message instead of appending.
        return ChatResult(
            generations=[ChatGeneration(message=message.model_copy(deep=True))]
        )


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def answer_from_last_tool(prefix: str = "") -> Callable[[list[BaseMessage]], AIMessage]:
    """Final answer that echoes the latest tool result, proving it reached the model."""

    def reply(messages: list[BaseMessage]) -> AIMessage:
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        last = tool_messages[-1].content if tool_messages else ""
        return AIMessage(content=f"{prefix}{last}")

    return reply


RAVI_BIO = (
    "Dr. Ravi Verma is an Associate Professor in the School of Computing Science."
)
RAVI_BACKGROUND = (
    "Dr. Ravi Verma has 12 years of teaching experience and a PhD from IIT Delhi."
)
POOJA_RESEARCH = "Dr. Pooja Verma researches machine learning and computer vision."

FACULTY_QA = [
    ("Who is Dr. Ravi Verma?", RAVI_BIO),
    ("What is Dr. Ravi Verma's background and experience?", RAVI_BACKGROUND),
    (
        "What is Dr. Ravi Verma's position at VIT Bhopal?",
        "Dr. Ravi Verma is an Associate Professor at VIT Bhopal.",
    ),
    ("What are Dr. Pooja Verma's research interests?", POOJA_RESEARCH),
    (
        "What is Dr. Pooja Verma's background and experience?",
        "Dr. Pooja Verma has 5 years of industry experience.",
    ),
    (
        "What awards or recognition has Dr. Pooja Verma received?",
        "Dr. Pooja Verma received the Best Paper Award in 2024.",
    ),
    (
        "What is Dr. Navneet Kumar Verma's position at VIT Bhopal?",
        "Dr. Navneet Kumar Verma is an Assistant Professor at VIT Bhopal.",
    ),
    (
        "What are Dr Komarasamy G's research interests?",
        "Dr Komarasamy G works on cloud computing.",
    ),
    ("Who is Dr. C. P. Koushik?", "Dr. C. P. Koushik is a Professor of Mathematics."),
    # Single significant name token: must not swallow "Suresh <anything>"
    ("Who is Dr. M. Suresh?", "Dr. M. Suresh is an Associate Professor at VIT Bhopal."),
]

FACULTY_DIRECTORY = [
    ("dr. ravi verma", "A-514", "T504", "9999999999"),
    ("pooja verma", "B-101", "T201", "8888888888"),
    ("suresh kumar", "C-202", "T402", "7777777777"),
]


def build_faculty_db(db_path: Path) -> Path:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE qa (id INTEGER PRIMARY KEY, question TEXT, answer TEXT)"
        )
        conn.execute(
            "CREATE TABLE teachers (id INTEGER PRIMARY KEY, name TEXT, "
            "cabin_no TEXT, room_no TEXT, phone_number TEXT)"
        )
        conn.executemany("INSERT INTO qa (question, answer) VALUES (?, ?)", FACULTY_QA)
        conn.executemany(
            "INSERT INTO teachers (name, cabin_no, room_no, phone_number) "
            "VALUES (?, ?, ?, ?)",
            FACULTY_DIRECTORY,
        )
        conn.commit()
    return db_path
