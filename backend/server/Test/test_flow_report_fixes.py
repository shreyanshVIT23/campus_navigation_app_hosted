"""Fixes for the issues the request flow report found (2026-10-10)."""

import logging

import pytest

from Chatbot.audio_to_text import AudioProcessor
from Events_Management import user_auth_jwt
from Utils import route_utilary

OLD_PLACEHOLDER = "your-secret-key-should-be-in-env-variables"
NEW_TEACHER = {
    "name": "Dr. Flow Report Check",
    "cabin_no": "Z-998",
    "room_no": "101",
    "phone_number": "0000000000",
}


# /upload: a same-floor route answers with the map itself


def test_upload_text_same_floor_route_returns_the_map(client):
    response = client.post("/upload", data={"text": "path from 101 to 105"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content.lstrip().startswith(b"<")


def test_upload_text_cross_floor_route_returns_map_links(client):
    response = client.post("/upload", data={"text": "path from 105 to 201"})

    assert response.status_code == 200
    files = response.json()["files"]
    assert set(files) == {"start_floor", "end_floor"}
    assert all("route=" in url for url in files.values())


# /load_svg: an unknown building is the caller's mistake, not a server error


def test_load_svg_unknown_building_is_400(client):
    response = client.get("/load_svg", params={"floor": "1", "building": "NOPE-99"})

    assert response.status_code == 400
    assert "NOPE-99" in response.json()["detail"]


def test_load_svg_known_building_still_serves_the_floor(client):
    response = client.get("/load_svg", params={"floor": "1", "building": "AB-01"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


# Dead code


def test_unused_prepare_custom_data_is_gone():
    assert not hasattr(route_utilary, "prepare_custom_data")


# Admin-only endpoints need a login


def test_adding_a_teacher_needs_a_login(client):
    response = client.post("/teachers", json=NEW_TEACHER)

    assert response.status_code == 401


def test_adding_a_teacher_works_when_logged_in(client, auth_headers):
    response = client.post("/teachers", json=NEW_TEACHER, headers=auth_headers)

    assert response.status_code in (201, 400)  # 400 if an earlier run added it


def test_reload_knowledge_needs_a_login(client):
    assert client.post("/reload_knowledge").status_code == 401


def test_reload_knowledge_works_when_logged_in(client, auth_headers):
    response = client.post("/reload_knowledge", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "success"


# Token signing key


def test_secret_key_comes_from_the_environment():
    assert user_auth_jwt.load_secret_key({"SECRET_KEY": "from-env"}) == "from-env"


def test_missing_secret_key_is_random_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=user_auth_jwt.__name__):
        first = user_auth_jwt.load_secret_key({})
    second = user_auth_jwt.load_secret_key({})

    assert first != OLD_PLACEHOLDER
    assert len(first) >= 40
    assert first != second
    assert "SECRET_KEY" in caplog.text


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_secret_key_counts_as_missing(blank):
    assert user_auth_jwt.load_secret_key({"SECRET_KEY": blank}).strip()


def test_the_app_never_signs_with_the_old_placeholder():
    assert user_auth_jwt.SECRET_KEY != OLD_PLACEHOLDER


# spaCy is loaded once, not on every request


def test_spacy_model_is_shared_between_processors():
    assert AudioProcessor().nlp is AudioProcessor().nlp
