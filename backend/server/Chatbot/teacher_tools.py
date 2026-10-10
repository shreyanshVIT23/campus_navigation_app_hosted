"""Read-only SQLite search over the faculty knowledge base, exposed as LangChain tools."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from langchain_core.tools import BaseTool, tool
from rank_bm25 import BM25Okapi
from rapidfuzz import fuzz
from typing_extensions import TypedDict

TITLE_TOKENS = frozenset({"dr", "prof", "professor", "mr", "ms", "mrs"})
# A query word matches a name word exactly, or (for longer words) despite a typo
TOKEN_MATCH_RATIO = 85
MAX_CANDIDATES = 5
MAX_SEARCH_RESULTS = 5

# One pattern per question template in the qa table
QUESTION_NAME_PATTERNS = (
    re.compile(r"^Who is (?P<name>.+?)\?$", re.IGNORECASE),
    re.compile(
        r"^What is (?P<name>.+?)['’]s background and experience\?$", re.IGNORECASE
    ),
    re.compile(r"^What is (?P<name>.+?)['’]s position at VIT Bhopal\?$", re.IGNORECASE),
    re.compile(r"^What are (?P<name>.+?)['’]s research interests\?$", re.IGNORECASE),
    re.compile(
        r"^What awards or recognition has (?P<name>.+?) received\?$", re.IGNORECASE
    ),
)

SEARCH_STOP_WORDS = frozenset(
    [
        "a",
        "about",
        "an",
        "and",
        "are",
        "at",
        "does",
        "for",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "she",
        "the",
        "their",
        "to",
        "what",
        "which",
        "who",
        "whom",
        "with",
        "works",
    ]
)


class TeacherEntry(TypedDict):
    display_name: str
    questions: list[str]
    cabin_no: str | None
    room_no: str | None


class Resolution(TypedDict):
    # Name key of the single teacher the text refers to, if unambiguous
    teacher: str | None
    candidates: list[str]
    # True when several names match equally well (as opposed to none matching)
    ambiguous: bool


class Fact(TypedDict):
    question: str
    answer: str


class TeacherSummary(TypedDict):
    name: str
    cabin_no: str | None
    room_no: str | None
    has_profile: bool


class FindTeacherResult(TypedDict):
    matches: list[TeacherSummary]


class TeacherProfile(TypedDict):
    name: str
    cabin_no: str | None
    room_no: str | None
    facts: list[Fact]


class TeacherNotFound(TypedDict):
    error: str
    candidates: list[str]


class SearchResult(TypedDict):
    results: list[Fact]


def normalize_name(text: str) -> str:
    """'Dr. Ravi Verma's' -> 'ravi verma'; 'Dr.Rohit Sharma' -> 'rohit sharma'."""
    text = re.sub(r"['’]s\b", "", text.lower())
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(token for token in text.split() if token not in TITLE_TOKENS)


def name_key(name: str) -> str:
    """Index key: the normalized name without initials ('c p koushik' -> 'koushik')."""
    normalized = normalize_name(name)
    significant = [token for token in normalized.split() if len(token) >= 2]
    return " ".join(significant) or normalized


def extract_question_name(question: str) -> str | None:
    for pattern in QUESTION_NAME_PATTERNS:
        match = pattern.match(question.strip())
        if match:
            return match.group("name").strip()
    return None


def tokens_match(name_token: str, query_token: str) -> bool:
    if name_token == query_token:
        return True
    return (
        len(name_token) >= 4
        and len(query_token) >= 4
        and fuzz.ratio(name_token, query_token) >= TOKEN_MATCH_RATIO
    )


def search_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in SEARCH_STOP_WORDS
    ]


class TeacherKnowledge:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.reload()

    def connect(self) -> sqlite3.Connection:
        # mode=ro: the tools can only ever read the knowledge base
        return sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)

    def reload(self):
        with closing(self.connect()) as conn:
            qa_rows = conn.execute("SELECT question, answer FROM qa").fetchall()
            teacher_rows = conn.execute(
                "SELECT name, cabin_no, room_no FROM teachers"
            ).fetchall()

        index: dict[str, TeacherEntry] = {}
        for question, _ in qa_rows:
            raw_name = extract_question_name(question)
            if not raw_name:
                continue
            entry = index.setdefault(
                name_key(raw_name),
                {
                    "display_name": raw_name,
                    "questions": [],
                    "cabin_no": None,
                    "room_no": None,
                },
            )
            entry["questions"].append(question)

        for name, cabin_no, room_no in teacher_rows:
            if not name or not name_key(name):
                continue
            entry = index.get(name_key(name))
            if entry is None:
                index[name_key(name)] = {
                    "display_name": name.strip().title(),
                    "questions": [],
                    "cabin_no": cabin_no,
                    "room_no": room_no,
                }
            else:
                entry["cabin_no"] = entry["cabin_no"] or cabin_no
                entry["room_no"] = entry["room_no"] or room_no

        self._index = index
        self._facts: list[Fact] = [
            {"question": question, "answer": answer} for question, answer in qa_rows
        ]
        corpus = [search_tokens(f"{q} {a}") for q, a in qa_rows]
        self._bm25 = BM25Okapi(corpus) if corpus else None
        # Lookup memo; the data only changes through reload()
        self._profile_cache: dict[str, TeacherProfile | TeacherNotFound] = {}

    def entry(self, key: str) -> TeacherEntry:
        return self._index[key]

    def resolve(self, text: str, exact_name: bool = False) -> Resolution:
        """Find the teacher named in text.

        Free text (exact_name=False): every word of the teacher's name must appear in
        the text. A name given by a tool (exact_name=True): additionally, every word
        the caller wrote must belong to that name, so "Suresh Patel" is not taken
        to mean "Dr. M. Suresh".
        """
        query_tokens = normalize_name(text).split()
        query_name_tokens = [token for token in query_tokens if len(token) >= 2]
        # How many teachers' names contain each query word: "kumar" is common,
        # "suresh" is rare, so a match on "suresh" says more about who is meant
        query_token_counts = {
            q: sum(
                1
                for key in self._index
                if any(tokens_match(name_token, q) for name_token in key.split())
            )
            for q in query_name_tokens
        }
        full_matches: list[str] = []
        # (weight of the matched words, share of the name matched, key)
        partial_matches: list[tuple[float, float, str]] = []
        for key in self._index:
            significant = key.split()
            matched_tokens = [
                name_token
                for name_token in significant
                if any(tokens_match(name_token, q) for q in query_tokens)
            ]
            covers_query = not exact_name or all(
                any(tokens_match(name_token, q) for name_token in significant)
                for q in query_name_tokens
            )
            if len(matched_tokens) == len(significant) and covers_query:
                full_matches.append(key)
            elif matched_tokens:
                weight = sum(
                    # Exact word matches outrank typo-distance ones ("kumari")
                    (1.0 if q in significant else 0.5) / query_token_counts[q]
                    for q in query_name_tokens
                    if query_token_counts[q]
                    and any(tokens_match(name_token, q) for name_token in significant)
                )
                share = len(matched_tokens) / len(significant)
                partial_matches.append((weight, share, key))

        if full_matches:
            # Prefer the most specific name ("navneet kumar verma" over "kumar verma")
            full_matches.sort(key=lambda key: (-len(key.split()), key))
            longest = len(full_matches[0].split())
            best = [key for key in full_matches if len(key.split()) == longest]
            if len(best) == 1:
                return {
                    "teacher": best[0],
                    "candidates": full_matches[:MAX_CANDIDATES],
                    "ambiguous": False,
                }
            return {
                "teacher": None,
                "candidates": best[:MAX_CANDIDATES],
                "ambiguous": True,
            }

        partial_matches.sort(key=lambda match: (-match[0], -match[1], match[2]))
        return {
            "teacher": None,
            "candidates": [key for _, _, key in partial_matches[:MAX_CANDIDATES]],
            "ambiguous": False,
        }

    def _fetch_facts(self, questions: list[str]) -> list[Fact]:
        if not questions:
            return []
        placeholders = ", ".join("?" for _ in questions)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"SELECT question, answer FROM qa WHERE question IN ({placeholders})",
                questions,
            ).fetchall()
        answers = dict(rows)
        return [
            {"question": question, "answer": answers[question]}
            for question in questions
            if question in answers
        ]

    def get_profile(self, name: str) -> TeacherProfile | TeacherNotFound:
        cache_key = normalize_name(name)
        cached = self._profile_cache.get(cache_key)
        if cached is not None:
            return cached

        resolution = self.resolve(name, exact_name=True)
        key = resolution["teacher"]
        result: TeacherProfile | TeacherNotFound
        if key is None:
            candidates = [
                self._index[candidate]["display_name"]
                for candidate in resolution["candidates"]
            ]
            if resolution["ambiguous"]:
                error = "Several teachers match that name; ask which one is meant."
            elif candidates:
                error = (
                    f"No teacher named '{name}'. The closest names are listed in "
                    "candidates; ask the user whether they meant one of them."
                )
            else:
                error = f"No teacher named '{name}' was found."
            result = {"error": error, "candidates": candidates}
        else:
            entry = self._index[key]
            result = {
                "name": entry["display_name"],
                "cabin_no": entry["cabin_no"],
                "room_no": entry["room_no"],
                "facts": self._fetch_facts(entry["questions"]),
            }

        self._profile_cache[cache_key] = result
        return result

    def find_teachers(self, name: str) -> FindTeacherResult:
        resolution = self.resolve(name, exact_name=True)
        keys = resolution["candidates"]
        if resolution["teacher"] is not None:
            keys = [resolution["teacher"]] + [
                key for key in keys if key != resolution["teacher"]
            ]
        return {
            "matches": [
                {
                    "name": self._index[key]["display_name"],
                    "cabin_no": self._index[key]["cabin_no"],
                    "room_no": self._index[key]["room_no"],
                    "has_profile": bool(self._index[key]["questions"]),
                }
                for key in keys[:MAX_CANDIDATES]
            ]
        }

    def search(self, query: str) -> SearchResult:
        tokens = search_tokens(query)
        if self._bm25 is None or not tokens:
            return {"results": []}
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return {
            "results": [
                self._facts[i] for i in ranked[:MAX_SEARCH_RESULTS] if scores[i] > 0
            ]
        }

    def best_search_answer(self, query: str, min_coverage: float = 0.5) -> str | None:
        """Top search hit, only if it contains most of the query's words."""
        tokens = set(search_tokens(query))
        for fact in self.search(query)["results"]:
            fact_tokens = set(search_tokens(f"{fact['question']} {fact['answer']}"))
            if tokens and len(tokens & fact_tokens) / len(tokens) >= min_coverage:
                return fact["answer"]
        return None

    def best_answer(self, key: str, query: str) -> str | None:
        """The teacher's answer whose question best matches the query's intent."""
        entry = self._index[key]
        facts = self._fetch_facts(entry["questions"])
        if not facts:
            return None
        name_tokens = set(key.split())

        def intent(text: str) -> str:
            return " ".join(
                token
                for token in normalize_name(text).split()
                if not any(tokens_match(name, token) for name in name_tokens)
            )

        query_intent = intent(query)
        best = max(
            facts,
            key=lambda fact: fuzz.token_set_ratio(
                query_intent, intent(fact["question"])
            ),
        )
        return best["answer"]


def create_teacher_tools(knowledge: TeacherKnowledge) -> list[BaseTool]:
    @tool(parse_docstring=True)
    def find_teacher(name: str) -> str:
        """Find faculty members by name, with their cabin and room numbers.

        Args:
            name: Full or partial teacher name, e.g. "Ravi Verma".
        """
        return json.dumps(knowledge.find_teachers(name))

    @tool(parse_docstring=True)
    def get_teacher_profile(name: str) -> str:
        """Get everything known about one teacher: bio, background and experience, position, research interests, awards, cabin and room.

        Args:
            name: The teacher's name as the user wrote it, e.g. "ravi verma".
        """
        return json.dumps(knowledge.get_profile(name))

    @tool(parse_docstring=True)
    def search_faculty_knowledge(query: str) -> str:
        """Search all faculty information by topic, for questions that do not name a teacher.

        Args:
            query: Topic keywords, e.g. "machine learning research".
        """
        return json.dumps(knowledge.search(query))

    return [find_teacher, get_teacher_profile, search_faculty_knowledge]
