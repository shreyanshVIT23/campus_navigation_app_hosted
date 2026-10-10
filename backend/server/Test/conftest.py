import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure server directory is in sys.path
server_dir = Path(__file__).resolve().parent.parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from Utils.test_copies import use_copies

# Tests get temporary copies of the data the app writes (Utils.test_copies)
test_data_dir = Path(tempfile.mkdtemp(prefix="campus-nav-tests-"))


def pytest_configure():
    # Runs before any test module (and so the app and Utils.loader) is imported
    use_copies(test_data_dir)


def pytest_unconfigure():
    # A file still held open on Windows must not fail the run; the OS cleans
    # its temp directory anyway
    shutil.rmtree(test_data_dir, ignore_errors=True)


log_dir = Path(__file__).resolve().parent


def log_test_interaction(
    filename: str,
    method: str,
    url: str,
    status_code: int,
    req_data=None,
    res_data=None,
):
    filepath = log_dir / filename
    timestamp = datetime.now(timezone.utc).astimezone().isoformat()
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 60}\n")
        f.write(f"TIMESTAMP   : {timestamp}\n")
        f.write(f"TESTED ROUTE: {method.upper()} {url}\n")
        if req_data is not None:
            try:
                f.write(f"REQUEST DATA:\n{json.dumps(req_data, indent=2)}\n")
            # Not JSON-serializable (e.g. bytes or form objects)
            except (TypeError, ValueError):
                f.write(f"REQUEST DATA:\n{req_data}\n")
        else:
            f.write("REQUEST DATA: None\n")
        f.write(f"STATUS CODE : {status_code}\n")
        if res_data is not None:
            try:
                f.write(f"RESPONSE:\n{json.dumps(res_data, indent=2)}\n")
            except (TypeError, ValueError):
                f.write(f"RESPONSE:\n{res_data}\n")
        f.write(f"{'=' * 60}\n")


class LoggingTestClient:
    def __init__(self, client: TestClient, log_filename: str = "api_test_log.txt"):
        self._client = client
        self.log_filename = log_filename
        # Truncate log file at fixture init
        filepath = log_dir / self.log_filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"=== TEST SUITE LOG: {log_filename} ===\n")

    def request(self, method: str, url: str, **kwargs):
        req_data = (
            kwargs.get("json")
            if "json" in kwargs
            else (kwargs.get("data") if "data" in kwargs else kwargs.get("params"))
        )
        response = self._client.request(method, url, **kwargs)
        try:
            res_data = response.json()
        # Non-JSON body (e.g. an SVG FileResponse)
        except ValueError:
            res_data = response.text
        log_test_interaction(
            self.log_filename,
            method,
            url,
            response.status_code,
            req_data,
            res_data,
        )
        return response

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)


@pytest.fixture
def client(request):
    stem = request.path.stem if hasattr(request, "path") else "test_suite"
    log_filename = f"{stem}_log.txt"
    # Imported here, after pytest_configure has pointed the app at test copies
    from app import app

    with TestClient(app) as test_client:
        yield LoggingTestClient(test_client, log_filename)


@pytest.fixture
def auth_headers(client):
    test_user = {
        "username": "pytest_user_auth",
        "password": "TestPassword123!",
        "email": "pytest_user_auth@example.com",
    }
    client.post("/user/register", json=test_user)
    response = client.post(
        "/user/login",
        json={"username": test_user["username"], "password": test_user["password"]},
    )
    token = response.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}
