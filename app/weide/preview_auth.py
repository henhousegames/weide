"""Auth for the private preview: one shared token, a login form, an HMAC-derived session cookie.

The token lives in a file (default ~/.config/henhouse/preview.key, override HENHOUSE_PREVIEW_KEY_FILE),
created by `weide preview-key` (dir 700, file 600). POST /login compares the submitted token in constant
time and sets a cookie whose value is HMAC(token, label), never the token itself; rotating the token
invalidates every session. Everything else (pages, /api, /media, /thumb, /avatar, /static, /preview)
requires the cookie: HTML page requests are redirected to /login, the rest answer 401. The token is
never accepted from a query string, and neither token nor cookie is ever logged or echoed.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import os
import secrets
import stat
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config

COOKIE = f"{config.APP_SLUG}_preview"
COOKIE_MAX_AGE = 365 * 24 * 3600
SESSION_LABEL = f"{config.APP_SLUG}-preview-session-v1".encode()
MAX_FORM_BYTES = 4096
FAIL_WINDOW = 15 * 60            # seconds
FAIL_PER_CLIENT = 5              # failed logins per client and window before 429
FAIL_GLOBAL = 50                 # failed logins overall per window before 429
FAIL_DELAY = 0.5                 # seconds added to every failed attempt
OPEN_PATHS = {"/login"}
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


class PreviewKeyError(Exception):
    pass


def key_file() -> Path:
    v = config.env("PREVIEW_KEY_FILE")
    return Path(v).expanduser() if v else Path.home() / ".config" / config.APP_SLUG / "preview.key"


def create_key(path: Path | None = None, *, rotate: bool = False) -> Path:
    """Write a fresh random token (32 bytes, urlsafe). Never returns or prints the token."""
    path = Path(path or key_file()).expanduser()
    if path.exists() and not rotate:
        raise PreviewKeyError(f"{path} exists; use --rotate to replace it (this logs out every browser)")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    return path


def load_key(path: Path | None = None) -> str:
    path = Path(path or key_file()).expanduser()
    if not path.is_file():
        raise PreviewKeyError(f"no preview key at {path}; create it with `weide preview-key`")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PreviewKeyError(f"{path} is readable by others; run chmod 600 on it")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise PreviewKeyError(f"{path} does not contain a valid key; recreate it with `weide preview-key --rotate`")
    return token


def session_value(token: str) -> str:
    return hmac.new(token.encode(), SESSION_LABEL, hashlib.sha256).hexdigest()


class FailLimiter:
    """Light brute-force brake: per-client and global failed-login counts in a sliding window."""

    def __init__(self, per_client: int = FAIL_PER_CLIENT, overall: int = FAIL_GLOBAL, window: float = FAIL_WINDOW):
        self.per_client, self.overall, self.window = per_client, overall, window
        self.fails: dict[str, deque[float]] = {}
        self.all: deque[float] = deque()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.fails.get(client, deque())
        self._trim(q, now)
        self._trim(self.all, now)
        return len(q) >= self.per_client or len(self.all) >= self.overall

    def fail(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.fails.setdefault(client, deque()).append(now)
        self.all.append(now)
        if len(self.fails) > 1000:                       # bounded memory
            self.fails = {k: v for k, v in self.fails.items() if v and v[-1] > now - self.window}


LOGIN_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{app} Preview – Anmelden</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#173524;color:#eef3e6;
font:16px/1.4 ui-sans-serif,system-ui,sans-serif}}
form{{width:min(92vw,360px);display:grid;gap:12px;padding:24px;border:1px solid #2f5a41;border-radius:16px}}
h1{{margin:0;font-size:20px}} .tag{{display:inline-block;background:#ffc928;color:#1b1a12;border-radius:6px;
padding:1px 8px;font-weight:700;font-size:13px;letter-spacing:.04em}}
input{{font:inherit;padding:10px 12px;border-radius:10px;border:1px solid #2f5a41;background:#0f2619;color:inherit}}
button{{font:inherit;font-weight:700;padding:10px;border:0;border-radius:10px;background:#ffc928;color:#1b1a12;cursor:pointer}}
.err{{color:#ff8a4c;margin:0}}
</style></head><body>
<form method="post" action="/login" autocomplete="off">
<h1>{app} <span class="tag">PREVIEW</span></h1>
<label for="token">Preview-Schlüssel</label>
<input id="token" name="token" type="password" required autofocus>
{error}<button type="submit">Anmelden</button>
</form></body></html>"""


def login_page(error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.format(app=html.escape(config.APP_NAME), error=err), status_code=status,
                        headers={"Cache-Control": "no-store", **SECURITY_HEADERS})


def wants_html(request: Request) -> bool:
    p = request.url.path
    if request.method not in ("GET", "HEAD") or p.startswith(("/api/", "/media/", "/thumb/", "/avatar/", "/static/",
                                                                "/preview/", "/openapi.json")):
        return False
    return True


def install_auth(app: FastAPI, token: str, limiter: FailLimiter | None = None) -> None:
    """Login routes + a middleware that guards every other path (loopback included)."""
    expected = session_value(token)
    limiter = limiter or FailLimiter()

    def authed(request: Request) -> bool:
        got = request.cookies.get(COOKIE) or ""
        return hmac.compare_digest(got.encode(), expected.encode())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            resp = await call_next(request)
        elif not authed(request):
            if wants_html(request):
                resp = RedirectResponse("/login", status_code=303)
            else:
                resp = JSONResponse({"detail": "login required"}, status_code=401)
            resp.headers["Cache-Control"] = "no-store"
        elif request.method not in ("GET", "HEAD"):
            resp = JSONResponse({"detail": "the preview is read-only"}, status_code=405,
                                headers={"Allow": "GET, HEAD"})
        else:
            resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    @app.get("/login", include_in_schema=False)
    def login_form(request: Request) -> Response:
        if authed(request):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        client = request.client.host if request.client else "?"
        if limiter.blocked(client):
            return login_page("Zu viele Fehlversuche. Bitte später nochmal.", status=429)
        body = await request.body()
        submitted = ""
        if len(body) <= MAX_FORM_BYTES:
            ctype = request.headers.get("content-type", "")
            if ctype.startswith("application/x-www-form-urlencoded"):
                submitted = (parse_qs(body.decode("utf-8", "replace")).get("token") or [""])[0].strip()
        if not submitted or not hmac.compare_digest(submitted.encode(), token.encode()):
            limiter.fail(client)
            await asyncio.sleep(FAIL_DELAY)
            return login_page("Falscher Schlüssel.", status=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, expected, max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="strict",
                        secure=False)
        resp.headers["Cache-Control"] = "no-store"
        return resp
