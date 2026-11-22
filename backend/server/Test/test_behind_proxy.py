"""Hosting behind SWAG: the proxy strips /api and the image ships only what runs."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

backend_dir = Path(__file__).resolve().parents[2]
dockerignore = backend_dir / ".dockerignore"

RUNTIME_DBS = [
    "server/DB/ab01-01.db",
    "server/DB/lab.db",
    "server/DB/chatbot2.db",
    "server/DB/event1.db",
    "server/DB/user_auth.db",
]


@pytest.fixture
def proxied():
    # Imported here, after pytest_configure has pointed the app at test copies
    from app import app

    with TestClient(app, root_path="/api") as test_client:
        yield test_client


# Behind the proxy: SWAG forwards /api/<route> as /<route>, uvicorn adds root_path


def test_routes_still_answer_under_the_root_path(proxied):
    response = proxied.get("/teachers")

    assert response.status_code == 200


def test_docs_load_the_schema_through_the_prefix(proxied):
    response = proxied.get("/docs")

    assert response.status_code == 200
    assert "/api/openapi.json" in response.text


def test_openapi_names_the_prefix_as_its_server(proxied):
    response = proxied.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["servers"] == [{"url": "/api"}]


def test_map_urls_stay_relative_for_the_frontend_to_prefix(proxied):
    response = proxied.post(
        "/process_path",
        json={"start": "105", "end": "201", "building": "AB-01", "preference": None},
    )

    assert response.status_code == 200
    files = response.json()["files"]
    assert all(url.startswith("/load_shortest_path_svg?") for url in files.values())


# The image: .dockerignore rules, matched the way Docker matches them


def pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\*\*/?|\*|\?)", pattern.strip("/"))
    wildcards = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    return re.compile(
        "".join(wildcards.get(part, re.escape(part)) for part in parts) + "$"
    )


def ignore_rules() -> list[tuple[re.Pattern[str], bool]]:
    rules = []
    for line in dockerignore.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((pattern_regex(line.lstrip("!")), negated))
    return rules


def is_shipped(path: str) -> bool:
    # The last matching line wins; a line matching a parent directory matches too
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    excluded = False
    for regex, negated in ignore_rules():
        if any(regex.match(candidate) for candidate in candidates):
            excluded = not negated
    return not excluded


def test_the_matcher_follows_docker_rules():
    assert pattern_regex("*.md").match("README.md")
    assert not pattern_regex("*.md").match("server/README.md")
    assert pattern_regex("**/*.md").match("server/README.md")
    assert pattern_regex("**/.env").match(".env")


@pytest.mark.parametrize("path", RUNTIME_DBS)
def test_runtime_databases_are_shipped(path):
    assert (backend_dir / path).exists()
    assert is_shipped(path)


@pytest.mark.parametrize(
    "path",
    [
        "server/app.py",
        "server/Floor_Maps/AB-01/floor 0 copy path.svg",
        "server/Teacher_Data/faculty.xlsx",
        "pyproject.toml",
        "uv.lock",
    ],
)
def test_app_files_are_shipped(path):
    assert is_shipped(path)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "server/.env",
        "server/DB/ab01-01.backup.db",
        "server/DB/chatbot2.before-cleanup.db",
        "server/DB/Test_Use_3.db",
        "server/Test/conftest.py",
        "server/Utils/__pycache__/loader.cpython-310.pyc",
        "server/client_api_log.txt",
        ".venv/pyvenv.cfg",
    ],
)
def test_secrets_backups_and_tests_are_not_shipped(path):
    assert not is_shipped(path)
