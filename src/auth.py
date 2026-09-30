"""Cariara admin authentication for the AutoApply Flask app.

Every request (pages, API, SSE, static files, downloads) must carry a Cariara
access token issued by the Cariara backend (``POST /auth/login``), either as an
``Authorization: Bearer`` header or in the ``cariara_token`` cookie set by the
local ``/login`` page. The token is verified locally with the same secret and
algorithm the backend signs with (``JWT_SECRET_KEY`` / ``JWT_ALGORITHM``), and
the caller must hold an admin role (admin, administrator, manager, developer).

The role is re-checked against the backend (``GET /auth/me``) and cached for
60 seconds, so revoked sessions and demoted users lose access quickly. If the
backend is unreachable, the role claim inside the verified token is used.

Only ``/healthz`` and the login/logout pages are reachable without a token.
If ``JWT_SECRET_KEY`` is not configured the app fails closed with 503.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
import jwt
from flask import Flask, Response, g, jsonify, make_response, redirect, request

logger = logging.getLogger("autoapply.auth")

ADMIN_ROLES = frozenset({"admin", "administrator", "manager", "developer"})
ALLOWED_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

ACCESS_COOKIE = "cariara_token"
REFRESH_COOKIE = "cariara_refresh"

# Paths reachable without a token. Everything else requires an admin.
PUBLIC_PATHS = frozenset({"/healthz", "/login", "/logout"})

ROLE_CACHE_TTL_SECONDS = 60
_REMOTE_TIMEOUT_SECONDS = 5.0

_role_cache: dict[str, tuple[float, Optional[dict]]] = {}
_role_cache_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def backend_url() -> str:
    return os.environ.get("CARIARA_BACKEND_URL", "https://cariara-backend.up.railway.app").rstrip("/")


def jwt_config() -> Optional[tuple[str, str]]:
    """Return (secret, algorithm) or None when auth is not configured."""
    secret = os.environ.get("JWT_SECRET_KEY", "").strip()
    algorithm = os.environ.get("JWT_ALGORITHM", "HS256").strip() or "HS256"
    if not secret or algorithm not in ALLOWED_ALGORITHMS:
        return None
    return secret, algorithm


def _remote_check_enabled() -> bool:
    return os.environ.get("AUTH_VERIFY_REMOTE", "1").lower() not in ("0", "false", "no")


def _cookie_secure() -> bool:
    return os.environ.get("AUTH_COOKIE_SECURE", "1").lower() not in ("0", "false", "no")


# --------------------------------------------------------------------------
# Token handling
# --------------------------------------------------------------------------

def extract_token() -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    token = request.cookies.get(ACCESS_COOKIE, "").strip()
    return token or None


def decode_access_token(token: str) -> dict:
    """Verify signature/expiry and return the claims. Raises AuthError."""
    cfg = jwt_config()
    if cfg is None:
        raise AuthError(503, "Authentication is not configured")
    secret, algorithm = cfg
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "Token expired")
    except jwt.InvalidTokenError:
        raise AuthError(401, "Invalid token")
    if claims.get("type") != "access":
        raise AuthError(401, "Invalid token type")
    return claims


def fetch_backend_user(token: str) -> Optional[dict]:
    """Ask the Cariara backend who owns this token.

    Returns the user dict, ``None`` when the backend could not be reached, and
    raises AuthError(401) when the backend rejects the token (revoked, user
    disabled, ...). Results are cached for 60 seconds per token.
    """
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _role_cache_lock:
        cached = _role_cache.get(key)
        if cached and cached[0] > now:
            if cached[1] is None:
                raise AuthError(401, "Session is no longer valid")
            return cached[1]

    try:
        resp = httpx.get(
            f"{backend_url()}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REMOTE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Cariara backend unreachable for role check: %s", type(exc).__name__)
        return None

    if resp.status_code in (401, 403):
        with _role_cache_lock:
            _role_cache[key] = (now + ROLE_CACHE_TTL_SECONDS, None)
        raise AuthError(401, "Session is no longer valid")
    if resp.status_code != 200:
        logger.warning("Cariara backend role check returned %s", resp.status_code)
        return None
    try:
        user = resp.json()
    except ValueError:
        return None

    with _role_cache_lock:
        if len(_role_cache) > 1000:
            for k in [k for k, v in _role_cache.items() if v[0] <= now]:
                _role_cache.pop(k, None)
        _role_cache[key] = (now + ROLE_CACHE_TTL_SECONDS, user)
    return user


def _role_of(value) -> str:
    if isinstance(value, dict):
        value = value.get("slug") or value.get("name") or ""
    return str(value or "").strip().lower()


def authenticate(token: Optional[str]) -> dict:
    """Return the admin user for a token, or raise AuthError(401/403/503)."""
    if jwt_config() is None:
        raise AuthError(503, "Authentication is not configured")
    if not token:
        raise AuthError(401, "Authentication required")

    claims = decode_access_token(token)
    user = {"id": claims.get("sub"), "email": claims.get("email"), "role": _role_of(claims.get("role"))}

    if _remote_check_enabled():
        remote = fetch_backend_user(token)
        if remote is not None:
            user["email"] = remote.get("email", user["email"])
            user["role"] = _role_of(remote.get("role"))

    if user["role"] not in ADMIN_ROLES:
        raise AuthError(403, "Admin access required")
    return user


# --------------------------------------------------------------------------
# Backend proxy for login / refresh (server-to-server, avoids CORS)
# --------------------------------------------------------------------------

def _backend_post(path: str, payload: dict) -> tuple[int, dict]:
    try:
        resp = httpx.post(f"{backend_url()}{path}", json=payload, timeout=10.0)
    except httpx.HTTPError:
        return 502, {"detail": "Cariara backend is unreachable"}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


_refresh_lock = threading.Lock()


def try_refresh() -> Optional[tuple[str, Optional[str]]]:
    """Exchange the refresh cookie for a new access token (if present)."""
    refresh = request.cookies.get(REFRESH_COOKIE, "").strip()
    if not refresh:
        return None
    with _refresh_lock:
        status, body = _backend_post("/auth/refresh", {"refresh_token": refresh})
    if status != 200 or not body.get("access_token"):
        return None
    return body["access_token"], body.get("refresh_token")


def set_auth_cookies(resp: Response, access: str, refresh: Optional[str]) -> None:
    secure = _cookie_secure()
    resp.set_cookie(ACCESS_COOKIE, access, max_age=60 * 60 * 24 * 7, httponly=True,
                    secure=secure, samesite="Lax", path="/")
    if refresh:
        resp.set_cookie(REFRESH_COOKIE, refresh, max_age=60 * 60 * 24 * 7, httponly=True,
                        secure=secure, samesite="Strict", path="/")


def clear_auth_cookies(resp: Response) -> None:
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path="/")


# --------------------------------------------------------------------------
# Flask integration
# --------------------------------------------------------------------------

def _wants_html() -> bool:
    if request.path.startswith(("/api/", "/download/", "/static/", "/health")):
        return False
    if request.headers.get("Authorization"):
        return False
    accept = request.headers.get("Accept", "")
    return request.method == "GET" and ("text/html" in accept or not accept or accept == "*/*")


def _safe_next(target: Optional[str]) -> str:
    if not target:
        return "/"
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _same_origin_ok() -> bool:
    """CSRF guard for cookie-authenticated, state-changing requests."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if request.headers.get("Authorization", "").lower().startswith("bearer "):
        return True  # explicit bearer tokens are not sent automatically by browsers
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return True
    host = request.headers.get("X-Forwarded-Host") or request.host
    return urlparse(origin).netloc == host


def _deny(err: AuthError):
    if err.status == 401 and _wants_html():
        resp = redirect(f"/login?next={quote(request.full_path.rstrip('?'), safe='/?=&')}")
        clear_auth_cookies(resp)
        return resp
    if err.status == 403 and _wants_html():
        return make_response(FORBIDDEN_HTML, 403)
    resp = make_response(jsonify({"success": False, "error": err.message}), err.status)
    if err.status == 401:
        resp.headers["WWW-Authenticate"] = "Bearer"
    return resp


def install_auth(app: Flask) -> None:
    """Register the auth gate as the first before_request hook plus login routes."""

    @app.before_request
    def _auth_gate():
        path = request.path
        if path == "/healthz":
            return None
        if jwt_config() is None:
            return make_response(jsonify({"success": False, "error": "Authentication is not configured"}), 503)
        if path in PUBLIC_PATHS:
            return None
        if not _same_origin_ok():
            return make_response(jsonify({"success": False, "error": "Cross-origin request rejected"}), 403)

        token = extract_token()
        try:
            g.user = authenticate(token)
            return None
        except AuthError as err:
            # Expired cookie session: try a silent refresh with the refresh cookie.
            if err.status == 401 and not request.headers.get("Authorization"):
                refreshed = try_refresh()
                if refreshed:
                    try:
                        g.user = authenticate(refreshed[0])
                        g.refreshed_tokens = refreshed
                        return None
                    except AuthError as err2:
                        return _deny(err2)
            return _deny(err)

    @app.after_request
    def _auth_after(resp):
        refreshed = getattr(g, "refreshed_tokens", None)
        if refreshed:
            set_auth_cookies(resp, refreshed[0], refreshed[1])
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        next_url = _safe_next(request.values.get("next"))
        if request.method == "GET":
            return make_response(LOGIN_HTML.replace("{{NEXT}}", quote(next_url, safe="/?=&")), 200)

        data = request.get_json(silent=True) or request.form
        email = (data.get("email") or "").strip()
        password = data.get("password") or ""
        if not email or not password:
            return jsonify({"success": False, "error": "Email and password are required"}), 400

        status, body = _backend_post("/auth/login", {"email": email, "password": password})
        if status != 200 or not body.get("access_token"):
            message = body.get("detail") if isinstance(body.get("detail"), str) else "Invalid email or password"
            return jsonify({"success": False, "error": message}), 401 if status < 500 else 502

        try:
            authenticate(body["access_token"])
        except AuthError as err:
            return jsonify({"success": False, "error": err.message}), err.status

        resp = jsonify({"success": True, "next": next_url})
        set_auth_cookies(resp, body["access_token"], body.get("refresh_token"))
        return resp

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        resp = redirect("/login")
        clear_auth_cookies(resp)
        return resp


FORBIDDEN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AIApply - Access denied</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;background:#f6f8f8;color:#1a2b2b;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px}
.box{background:#fff;border:1px solid #d9e2e2;border-radius:12px;padding:32px;max-width:380px;text-align:center}
a{color:#0f766e}</style></head><body><div class="box"><h1>Access denied</h1>
<p>AIApply is restricted to Cariara administrators.</p><p><a href="/logout">Sign in with a different account</a></p>
</div></body></html>"""


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AIApply - Sign in</title>
<style>
:root{--bg:#f6f8f8;--card:#fff;--text:#1a2b2b;--muted:#5b6b6b;--border:#d9e2e2;--accent:#0f766e;--err:#b42318}
@media (prefers-color-scheme: dark){:root{--bg:#0f1515;--card:#172020;--text:#e6eeee;--muted:#9fb0b0;--border:#2a3838;--accent:#2dd4bf;--err:#f97066}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
form{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:32px;width:100%;max-width:380px}
h1{margin:0 0 4px;font-size:22px}p{margin:0 0 24px;color:var(--muted);font-size:14px}
label{display:block;font-size:13px;margin:0 0 6px}input{width:100%;padding:10px 12px;border:1px solid var(--border);
border-radius:8px;background:transparent;color:var(--text);font-size:15px;margin-bottom:16px}
button{width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer}
button:disabled{opacity:.6}#err{color:var(--err);font-size:13px;min-height:18px;margin-top:12px}
</style></head><body>
<form id="f" autocomplete="on">
<h1>AIApply</h1><p>Sign in with your Cariara admin account.</p>
<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button id="b" type="submit">Sign in</button><div id="err" role="alert"></div>
</form>
<script>
document.getElementById('f').addEventListener('submit', async function (e) {
  e.preventDefault();
  var b = document.getElementById('b'), err = document.getElementById('err');
  b.disabled = true; err.textContent = '';
  try {
    var r = await fetch('/login?next={{NEXT}}', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: this.email.value, password: this.password.value})});
    var d = await r.json();
    if (r.ok && d.success) { window.location.href = d.next || '/'; return; }
    err.textContent = d.error || 'Sign in failed';
  } catch (x) { err.textContent = 'Network error'; }
  b.disabled = false;
});
</script></body></html>"""
