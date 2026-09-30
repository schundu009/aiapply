"""Test setup: run the app from an empty temp directory with auth configured."""

import os
import sys
import time
from pathlib import Path

import jwt
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_SECRET = "test-secret-key-for-autoapply-tests-0123456789"

# Must be set before the app (and src.config) are imported.
os.environ["JWT_SECRET_KEY"] = TEST_SECRET
os.environ["JWT_ALGORITHM"] = "HS256"
os.environ["AUTH_COOKIE_SECURE"] = "0"
os.environ["DATABASE_URL"] = ""
os.environ["CARIARA_BACKEND_URL"] = "https://backend.test"



@pytest.fixture(scope="session")
def workdir(tmp_path_factory):
    path = tmp_path_factory.mktemp("autoapply")
    return path


@pytest.fixture(scope="session")
def flask_app(workdir):
    old_cwd = os.getcwd()
    os.chdir(workdir)
    # Templates/static are resolved relative to app.py, so they still load.
    sys.path.insert(0, str(REPO_ROOT))
    import app as app_module  # noqa: E402

    app_module.app.config["TESTING"] = True
    yield app_module
    os.chdir(old_cwd)


@pytest.fixture
def client(flask_app):
    return flask_app.app.test_client()


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch):
    from src import auth

    auth._role_cache.clear()
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_SECRET)
    yield
    auth._role_cache.clear()


def make_token(role="admin", *, secret=TEST_SECRET, token_type="access", exp_offset=900, sub="1"):
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": f"{role}@example.com",
        "role": role,
        "type": token_type,
        "jti": "test",
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def backend(monkeypatch):
    """Mock the Cariara backend's /auth/me role lookup."""
    from src import auth

    state = {"status": 200, "role": "admin", "calls": 0, "error": None}

    def fake_get(url, headers=None, timeout=None):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        return FakeResponse(state["status"], {"id": 1, "email": "me@example.com", "role": state["role"]})

    monkeypatch.setattr(auth.httpx, "get", fake_get)
    return state
