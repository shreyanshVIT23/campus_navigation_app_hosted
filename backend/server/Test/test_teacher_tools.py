import json
import sqlite3

import pytest
from chat_fakes import POOJA_RESEARCH, RAVI_BACKGROUND, RAVI_BIO, build_faculty_db

from Chatbot.teacher_tools import (
    TeacherKnowledge,
    create_teacher_tools,
    extract_question_name,
    normalize_name,
)


@pytest.fixture
def knowledge(tmp_path):
    return TeacherKnowledge(build_faculty_db(tmp_path / "faculty.db"))


# ============================================================
# NAME HANDLING
# ============================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Dr. Ravi Verma", "ravi verma"),
        ("Dr.Rohit Sharma", "rohit sharma"),
        ("Dr Komarasamy G", "komarasamy g"),
        ("Dr. C. P. Koushik", "c p koushik"),
        ("dr. lakshmi d.", "lakshmi d"),
        ("Prof. Ravi Verma's", "ravi verma"),
        ("RAVI VERMA?!", "ravi verma"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "question, name",
    [
        ("Who is Dr. Ravi Verma?", "Dr. Ravi Verma"),
        ("What is Dr. Ravi Verma's background and experience?", "Dr. Ravi Verma"),
        ("What is Dr. Ravi Verma's position at VIT Bhopal?", "Dr. Ravi Verma"),
        ("What are Dr Komarasamy G's research interests?", "Dr Komarasamy G"),
        (
            "What awards or recognition has Dr. Pooja Verma received?",
            "Dr. Pooja Verma",
        ),
        ("Where is the library?", None),
    ],
)
def test_extract_question_name(question, name):
    assert extract_question_name(question) == name


# ============================================================
# RESOLVING A TEACHER FROM FREE TEXT
# ============================================================


@pytest.mark.parametrize(
    "query, display_name",
    [
        ("how many years of experience does ravi verma have", "Dr. Ravi Verma"),
        ("WHO IS RAVI VERMA?!", "Dr. Ravi Verma"),
        ("ravi vrma experience", "Dr. Ravi Verma"),
        ("navneet kumar verma position", "Dr. Navneet Kumar Verma"),
        ("tell me about pooja verma's research", "Dr. Pooja Verma"),
        ("who is koushik", "Dr. C. P. Koushik"),
        ("komarasamy research", "Dr Komarasamy G"),
        ("where is suresh kumar's cabin", "Suresh Kumar"),
    ],
)
def test_resolve_finds_the_named_teacher(knowledge, query, display_name):
    resolution = knowledge.resolve(query)
    assert resolution["teacher"] is not None
    assert knowledge.entry(resolution["teacher"])["display_name"] == display_name


def test_free_text_prefers_the_longest_name(knowledge):
    # "suresh" (Dr. M. Suresh) is contained in "suresh kumar"; the longer name wins
    resolution = knowledge.resolve("where is suresh kumar's cabin")
    assert resolution["teacher"] == "suresh kumar"


def test_exact_name_must_match_every_word(knowledge):
    # A tool given a name must not resolve "Suresh Patel" to Dr. M. Suresh
    resolution = knowledge.resolve("Suresh Patel", exact_name=True)
    assert resolution["teacher"] is None
    assert "suresh" in resolution["candidates"]
    assert knowledge.resolve("M. Suresh", exact_name=True)["teacher"] == "suresh"
    assert knowledge.resolve("ravi verma", exact_name=True)["teacher"] == "ravi verma"


def test_profile_tool_does_not_guess_a_different_teacher(knowledge):
    profile = knowledge.get_profile("Suresh Patel")
    assert "error" in profile
    assert "Dr. M. Suresh" in profile["candidates"]


@pytest.fixture
def many_kumars(tmp_path):
    # Mirrors the real data: no "Suresh Kumar", a few Sureshes, many Kumars
    db_path = tmp_path / "kumars.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE qa (question TEXT, answer TEXT)")
        conn.execute(
            "CREATE TABLE teachers (name TEXT, cabin_no TEXT, room_no TEXT, "
            "phone_number TEXT)"
        )
        conn.execute(
            "INSERT INTO qa VALUES (?, ?)",
            ("Who is Dr. M. Suresh?", "Dr. M. Suresh is an Associate Professor."),
        )
        # "kumari" is a typo-distance match for "kumar" and must not outrank Sureshes
        names = ["xavier suresh", "suresh dara", "nancy kumari"] + [
            f"{first} kumar"
            for first in ("aanjan", "abhinav", "abhishek", "ajay", "anil", "dileep")
        ]
        conn.executemany(
            "INSERT INTO teachers (name) VALUES (?)", [(name,) for name in names]
        )
        conn.commit()
    return TeacherKnowledge(db_path)


def test_no_exact_match_suggests_rare_name_parts_first(many_kumars):
    profile = many_kumars.get_profile("Suresh Kumar")
    assert "error" in profile
    assert profile["error"].startswith("No teacher named")
    # "suresh" is rare among names, "kumar" is common: Sureshes come first
    assert set(profile["candidates"][:3]) == {
        "Dr. M. Suresh",
        "Xavier Suresh",
        "Suresh Dara",
    }


def test_two_equally_good_names_are_ambiguous(knowledge):
    resolution = knowledge.resolve("compare ravi verma and pooja verma")
    assert resolution["teacher"] is None
    assert resolution["ambiguous"] is True
    assert set(resolution["candidates"]) == {"ravi verma", "pooja verma"}


def test_partial_name_is_not_ambiguous_but_unmatched(knowledge):
    resolution = knowledge.resolve("verma", exact_name=True)
    assert resolution["teacher"] is None
    assert resolution["ambiguous"] is False
    profile = knowledge.get_profile("verma")
    assert "error" in profile
    assert profile["error"].startswith("No teacher named")


def test_search_answer_requires_most_query_words(knowledge):
    assert knowledge.best_search_answer("who works on machine learning") == (
        POOJA_RESEARCH
    )
    assert knowledge.best_search_answer("what is the weather on mars") is None


def test_resolve_ambiguous_surname_returns_candidates(knowledge):
    resolution = knowledge.resolve("who is verma")
    assert resolution["teacher"] is None
    assert {"ravi verma", "pooja verma", "navneet kumar verma"} <= set(
        resolution["candidates"]
    )


def test_resolve_unknown_text(knowledge):
    resolution = knowledge.resolve("what is the weather on mars")
    assert resolution == {"teacher": None, "candidates": [], "ambiguous": False}


# ============================================================
# PROFILE / DIRECTORY / SEARCH
# ============================================================


def test_get_profile_returns_only_that_teacher(knowledge):
    profile = knowledge.get_profile("ravi verma")
    assert "error" not in profile
    assert profile["name"] == "Dr. Ravi Verma"
    assert profile["cabin_no"] == "A-514"
    assert profile["room_no"] == "T504"
    answers = [fact["answer"] for fact in profile["facts"]]
    assert RAVI_BIO in answers
    assert RAVI_BACKGROUND in answers
    assert all("Ravi Verma" in answer for answer in answers)


def test_get_profile_never_exposes_phone_numbers(knowledge):
    dumped = json.dumps(knowledge.get_profile("ravi verma"))
    assert "9999999999" not in dumped
    assert "phone" not in dumped


def test_get_profile_ambiguous_lists_candidates(knowledge):
    profile = knowledge.get_profile("verma")
    assert "error" in profile
    assert "Dr. Ravi Verma" in profile["candidates"]


def test_get_profile_unknown(knowledge):
    profile = knowledge.get_profile("zz nobody zz")
    assert "error" in profile
    assert profile["candidates"] == []


def test_directory_only_teacher(knowledge):
    result = knowledge.find_teachers("suresh kumar")
    assert result["matches"][0] == {
        "name": "Suresh Kumar",
        "cabin_no": "C-202",
        "room_no": "T402",
        "has_profile": False,
    }


def test_search_knowledge_ranks_relevant_entry_first(knowledge):
    results = knowledge.search("who works on machine learning")["results"]
    assert results[0]["answer"] == POOJA_RESEARCH


def test_best_answer_matches_question_intent(knowledge):
    teacher = knowledge.resolve("how many years of experience does ravi verma have")
    assert teacher["teacher"] is not None
    best = knowledge.best_answer(
        teacher["teacher"], "how many years of experience does ravi verma have"
    )
    assert best == RAVI_BACKGROUND
    assert knowledge.best_answer(teacher["teacher"], "who is ravi verma") == RAVI_BIO


def test_profile_lookups_are_memoized_until_reload(knowledge, monkeypatch):
    fetches = []
    real_fetch = knowledge._fetch_facts

    def counting_fetch(questions):
        fetches.append(len(questions))
        return real_fetch(questions)

    monkeypatch.setattr(knowledge, "_fetch_facts", counting_fetch)
    knowledge.get_profile("Ravi Verma")
    knowledge.get_profile("ravi verma")
    assert len(fetches) == 1

    knowledge.reload()
    knowledge.get_profile("ravi verma")
    assert len(fetches) == 2


def test_connection_is_read_only(knowledge):
    with knowledge.connect() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO qa (question, answer) VALUES ('x', 'y')")


def test_reload_picks_up_new_rows(knowledge, tmp_path):
    with sqlite3.connect(tmp_path / "faculty.db") as conn:
        conn.execute(
            "INSERT INTO teachers (name, cabin_no, room_no) VALUES (?, ?, ?)",
            ("kavya iyer", "D-303", "T503"),
        )
        conn.commit()
    assert knowledge.resolve("who is kavya iyer")["teacher"] is None
    knowledge.reload()
    assert knowledge.resolve("who is kavya iyer")["teacher"] == "kavya iyer"


# ============================================================
# LANGCHAIN TOOLS
# ============================================================


def test_tools_are_named_and_return_json(knowledge):
    tools = {tool.name: tool for tool in create_teacher_tools(knowledge)}
    assert set(tools) == {
        "find_teacher",
        "get_teacher_profile",
        "search_faculty_knowledge",
    }

    profile = json.loads(tools["get_teacher_profile"].invoke({"name": "Ravi Verma"}))
    assert profile["name"] == "Dr. Ravi Verma"

    found = json.loads(tools["find_teacher"].invoke({"name": "suresh kumar"}))
    assert found["matches"][0]["room_no"] == "T402"

    searched = json.loads(
        tools["search_faculty_knowledge"].invoke({"query": "machine learning"})
    )
    assert searched["results"][0]["answer"] == POOJA_RESEARCH
