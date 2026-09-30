"""Every route requires a Cariara admin token (except /healthz and /login)."""

import httpx

from conftest import make_token


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_healthz_is_public(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_api_without_token_is_401(client):
    for path in ("/api/documents/status", "/api/apply/abc/progress", "/health"):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.get_json()["success"] is False


def test_post_api_without_token_is_401(client):
    resp = client.post("/api/process", json={"urls": ["https://example.com/job"]})
    assert resp.status_code == 401


def test_static_and_downloads_require_auth(client):
    assert client.get("/static/css/style.css").status_code == 401
    assert client.get("/download/output/x/resume.pdf").status_code == 401


def test_html_page_without_token_redirects_to_login(client):
    resp = client.get("/profile", headers={"Accept": "text/html"})
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login?next=/profile")


def test_login_page_is_public(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Sign in" in resp.data


def test_invalid_signature_is_401(client, backend):
    token = make_token("admin", secret="some-other-secret-that-is-long-enough-000")
    assert client.get("/api/documents/status", headers=bearer(token)).status_code == 401
    assert backend["calls"] == 0


def test_expired_token_is_401(client, backend):
    token = make_token("admin", exp_offset=-60)
    assert client.get("/api/documents/status", headers=bearer(token)).status_code == 401


def test_refresh_token_is_not_accepted_as_access(client, backend):
    token = make_token("admin", token_type="refresh")
    assert client.get("/api/documents/status", headers=bearer(token)).status_code == 401


def test_non_admin_is_403(client, backend):
    backend["role"] = "user"
    token = make_token("user")
    resp = client.get("/api/documents/status", headers=bearer(token))
    assert resp.status_code == 403
    html = client.get("/", headers={**bearer(token), "Accept": "text/html"})
    assert html.status_code == 403


def test_backend_role_overrides_stale_admin_claim(client, backend):
    backend["role"] = "user"  # demoted since the token was issued
    token = make_token("admin")
    assert client.get("/api/documents/status", headers=bearer(token)).status_code == 403


def test_revoked_session_is_401(client, backend):
    backend["status"] = 401
    token = make_token("admin")
    assert client.get("/api/documents/status", headers=bearer(token)).status_code == 401


def test_admin_is_200_and_role_lookup_is_cached(client, backend):
    token = make_token("admin")
    for role in ("admin", "administrator", "manager", "developer"):
        backend["role"] = role
        from src import auth
        auth._role_cache.clear()
        resp = client.get("/api/documents/status", headers=bearer(token))
        assert resp.status_code == 200, role
    calls = backend["calls"]
    client.get("/api/documents/status", headers=bearer(token))
    client.get("/api/documents/status", headers=bearer(token))
    assert backend["calls"] == calls  # cached for 60s
    page = client.get("/", headers=bearer(token))
    assert page.status_code == 200


def test_admin_cookie_session(client, backend):
    client.set_cookie("cariara_token", make_token("admin"))
    assert client.get("/api/documents/status").status_code == 200
    assert client.get("/static/css/style.css").status_code == 200


def test_backend_unreachable_falls_back_to_token_claim(client, backend):
    backend["error"] = httpx.ConnectError("down")
    assert client.get("/api/documents/status", headers=bearer(make_token("admin"))).status_code == 200
    assert client.get("/api/documents/status", headers=bearer(make_token("user"))).status_code == 403


def test_missing_secret_fails_closed_with_503(client, backend, monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    assert client.get("/api/documents/status", headers=bearer(make_token("admin"))).status_code == 503
    assert client.get("/").status_code == 503
    assert client.get("/login").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_cross_origin_cookie_post_is_rejected(client, backend):
    client.set_cookie("cariara_token", make_token("admin"))
    resp = client.post("/api/clear-history", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_login_proxies_to_cariara_backend(client, backend, monkeypatch):
    from src import auth

    token = make_token("admin")
    captured = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"access_token": token, "refresh_token": "r"}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return Resp()

    monkeypatch.setattr(auth.httpx, "post", fake_post)
    resp = client.post("/login?next=/history", json={"email": "a@b.c", "password": "pw"})
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "next": "/history"}
    assert captured["url"] == "https://backend.test/auth/login"
    cookies = resp.headers.getlist("Set-Cookie")
    assert any(c.startswith("cariara_token=") and "HttpOnly" in c for c in cookies)


def test_login_rejects_offsite_next(client, backend, monkeypatch):
    from src import auth

    class Resp:
        status_code = 200

        def json(self):
            return {"access_token": make_token("admin")}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: Resp())
    resp = client.post("/login?next=https://evil.example/", json={"email": "a@b.c", "password": "pw"})
    assert resp.get_json()["next"] == "/"
