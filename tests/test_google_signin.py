"""Continue with Google: /login/google -> backend OAuth -> /auth/callback -> /auth/session."""

import pytest

from conftest import make_token

ORIGIN = {"Origin": "http://localhost"}


def session_post(client, body, headers=ORIGIN):
    return client.post("/auth/session", json=body, headers=headers)


def set_cookies(resp):
    return resp.headers.getlist("Set-Cookie")


def test_login_page_has_google_button_and_preserves_next(client):
    resp = client.get("/login?next=/history")
    assert resp.status_code == 200
    assert b"Continue with Google" in resp.data
    assert b'href="/login/google?next=/history"' in resp.data


def test_login_google_redirects_to_backend_and_stores_next(client):
    resp = client.get("/login/google?next=/history%3Ftab%3D2")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://backend.test/auth/google/login?redirect=aiapply"
    cookie = next(c for c in set_cookies(resp) if c.startswith("aiapply_next="))
    assert "HttpOnly" in cookie and "Max-Age=600" in cookie
    assert cookie.startswith("aiapply_next=/history?tab=2;")


@pytest.mark.parametrize("bad", [
    "https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)", "evil", "/x\r\nSet-Cookie:a=b",
])
def test_login_google_sanitizes_next(client, bad):
    resp = client.get("/login/google", query_string={"next": bad})
    cookie = next(c for c in set_cookies(resp) if c.startswith("aiapply_next="))
    assert cookie.startswith("aiapply_next=/;")


def test_callback_page_is_public(client):
    resp = client.get("/auth/callback", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert b"/auth/session" in resp.data
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_session_rejects_missing_origin(client, backend):
    resp = session_post(client, {"access_token": make_token("admin")}, headers={})
    assert resp.status_code == 403
    assert not any(c.startswith("cariara_token=") for c in set_cookies(resp))


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost.evil.example", "http://evil/localhost"])
def test_session_rejects_foreign_origin(client, backend, origin):
    resp = session_post(client, {"access_token": make_token("admin")}, headers={"Origin": origin})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "bad_origin"
    assert backend["calls"] == 0


def test_session_rejects_form_posts(client, backend):
    resp = client.post("/auth/session", data={"access_token": make_token("admin")}, headers=ORIGIN)
    assert resp.status_code == 415


@pytest.mark.parametrize("token", [
    "", "not-a-jwt",
    make_token("admin", secret="some-other-secret-that-is-long-enough-000"),
    make_token("admin", exp_offset=-60),
    make_token("admin", token_type="refresh"),
])
def test_session_rejects_invalid_tokens(client, backend, token):
    resp = session_post(client, {"access_token": token})
    assert resp.status_code in (400, 401)
    assert not any(c.startswith("cariara_token=") for c in set_cookies(resp))


def test_session_rejects_non_admin(client, backend):
    backend["role"] = "user"
    resp = session_post(client, {"access_token": make_token("user")})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "not_admin"
    assert not any(c.startswith("cariara_token=") for c in set_cookies(resp))


def test_session_rejects_demoted_admin_claim(client, backend):
    backend["role"] = "user"
    assert session_post(client, {"access_token": make_token("admin")}).status_code == 403


def test_session_rejects_revoked_token(client, backend):
    backend["status"] = 401
    resp = session_post(client, {"access_token": make_token("admin")})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "invalid_token"


def test_session_accepts_admin_and_sets_cookies(client, backend):
    access = make_token("admin")
    refresh = make_token("admin", token_type="refresh")
    client.set_cookie("aiapply_next", "/history")
    resp = session_post(client, {"access_token": access, "refresh_token": refresh})
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "next": "/history"}
    cookies = set_cookies(resp)
    assert any(c.startswith(f"cariara_token={access}") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith(f"cariara_refresh={refresh}") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith("aiapply_next=;") for c in cookies)  # consumed
    # The new cookie session works on protected routes.
    assert client.get("/api/documents/status").status_code == 200


def test_session_ignores_refresh_token_for_another_user(client, backend):
    refresh = make_token("admin", token_type="refresh", sub="999")
    resp = session_post(client, {"access_token": make_token("admin"), "refresh_token": refresh})
    assert resp.status_code == 200
    assert not any(c.startswith("cariara_refresh=") for c in set_cookies(resp))


@pytest.mark.parametrize("stored", ["https://evil.example/", "//evil.example", "/\\evil.example", ""])
def test_session_sanitizes_stored_next(client, backend, stored):
    client.set_cookie("aiapply_next", stored)
    resp = session_post(client, {"access_token": make_token("admin")})
    assert resp.get_json()["next"] == "/"


def test_login_page_shows_google_errors(client):
    body = client.get("/login").data
    assert b"Google sign-in could not be completed" in body
    assert b"not a Cariara admin" in body


def test_password_error_mentions_google(client, backend, monkeypatch):
    from src import auth

    class Resp:
        status_code = 401

        def json(self):
            return {"detail": "Invalid email or password"}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: Resp())
    resp = client.post("/login", json={"email": "a@b.c", "password": "pw"})
    assert resp.status_code == 401
    assert resp.get_json()["error"] == (
        "Invalid email or password. If you usually sign in with Google, use Continue with Google."
    )
