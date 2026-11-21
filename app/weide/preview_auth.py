"""Auth for the private preview: one shared token, a login form, an HMAC-derived session cookie.

The token lives in a file (default ~/.config/henhouse/preview.key, override HENHOUSE_PREVIEW_KEY_FILE),
created by `weide preview-key` (dir 700, file 600). The cookie value is HMAC(token, label), never the
token itself; rotating the token invalidates every session. Ways to get the cookie (preview_login.py):
owner auto-login over Tailscale, a one-time login link (`weide preview-login-link`, POST /login/link),
and as a hidden fallback the token form (GET /login/token, POST /login, constant-time compare). Both
login POSTs are CSRF-checked (Origin/Referer must be this host) on top of SameSite=Strict and are
rate-limited. Everything else (pages, /api, /media, /thumb, /avatar, /static, /preview)
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
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config

if TYPE_CHECKING:
    from .preview_login import LinkStore, OwnerAutoLogin

COOKIE = f"{config.APP_SLUG}_preview"
COOKIE_MAX_AGE = 365 * 24 * 3600
SESSION_LABEL = f"{config.APP_SLUG}-preview-session-v1".encode()
MAX_FORM_BYTES = 4096
FAIL_WINDOW = 15 * 60            # seconds
FAIL_PER_CLIENT = 5              # failed logins per client and window before 429
FAIL_GLOBAL = 50                 # failed logins overall per window before 429
FAIL_DELAY = 0.5                 # seconds added to every failed attempt
OPEN_PATHS = {"/login", "/login/token", "/login/link"}
# same-origin (not no-referrer): browsers then send a real Origin on the login POSTs (CSRF check)
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff"}


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




PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{app} Preview – Anmelden</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#173524;color:#eef3e6;
font:16px/1.4 ui-sans-serif,system-ui,sans-serif}}
.box{{box-sizing:border-box;width:min(92vw,380px);display:grid;gap:12px;padding:24px;border:1px solid #2f5a41;border-radius:16px}}
h1{{margin:0;font-size:20px}} .tag{{display:inline-block;background:#ffc928;color:#1b1a12;border-radius:6px;
padding:1px 8px;font-weight:700;font-size:13px;letter-spacing:.04em}}
p{{margin:0}} code{{background:#0f2619;padding:1px 6px;border-radius:6px}}
input{{font:inherit;padding:10px 12px;border-radius:10px;border:1px solid #2f5a41;background:#0f2619;color:inherit}}
button{{font:inherit;font-weight:700;padding:10px;border:0;border-radius:10px;background:#ffc928;color:#1b1a12;cursor:pointer}}
.err{{color:#ff8a4c}}
</style></head><body>
<div class="box">
<h1>{app} <span class="tag">PREVIEW</span></h1>
{error}{body}
</div></body></html>"""

LOGIN_BODY = """<p>Privat. Öffne die Preview über Tailscale von deinem eigenen Gerät aus, oder erzeuge einen
Login-Link mit <code>weide preview-login-link</code>.</p>
<form id="link" method="post" action="/login/link" hidden><input type="hidden" name="nonce"></form>
<script>
function useLink() {
  var m = /[#&]link=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (!m) return;
  history.replaceState(null, "", "/login");
  var f = document.getElementById("link");
  f.nonce.value = m[1];
  f.submit();
}
useLink();
window.addEventListener("hashchange", useLink);
</script>"""

TOKEN_BODY = """<form method="post" action="/login" autocomplete="off" style="display:grid;gap:12px">
<label for="token">Preview-Schlüssel</label>
<input id="token" name="token" type="password" required autofocus>
<button type="submit">Anmelden</button>
</form>"""


def page(body: str, error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(PAGE.format(app=html.escape(config.APP_NAME), error=err, body=body), status_code=status,
                        headers={"Cache-Control": "no-store", **SECURITY_HEADERS})


def wants_html(request: Request) -> bool:
    p = request.url.path
    if request.method not in ("GET", "HEAD") or p.startswith(("/api/", "/media/", "/thumb/", "/avatar/", "/static/",
                                                                "/preview/", "/openapi.json")):
        return False
    return True


def same_origin(request: Request) -> bool:
    """CSRF check for the login POSTs (besides SameSite=Strict): Origin, else Referer, must be this
    very host. A request with neither (or `Origin: null`) is refused."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or src == "null":
        return False
    u = urlsplit(src)
    return u.scheme in ("http", "https") and bool(u.netloc) and u.netloc == request.headers.get("host")


def peer(request: Request) -> str | None:
    """The TCP peer address. Client-supplied headers (X-Forwarded-For, ...) are never consulted."""
    return request.client.host if request.client else None


def install_auth(app: FastAPI, token: str, limiter: FailLimiter | None = None, *,
                 auto: "OwnerAutoLogin | None" = None, links: "LinkStore | None" = None) -> None:
    """Login routes + a middleware that guards every other path (loopback included)."""
    expected = session_value(token)
    limiter = limiter or FailLimiter()

    def authed(request: Request) -> bool:
        got = request.cookies.get(COOKIE) or ""
        return hmac.compare_digest(got.encode(), expected.encode())

    def grant(resp: Response) -> Response:
        resp.set_cookie(COOKIE, expected, max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="strict",
                        secure=False)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def owner(request: Request) -> bool:
        return auto is not None and await asyncio.to_thread(auto.grants, peer(request))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        ok = authed(request)
        auto_granted = False
        if not ok and await owner(request):
            ok = auto_granted = True
        if path in OPEN_PATHS:
            if auto_granted and request.method == "GET":
                resp = RedirectResponse("/", status_code=303)
            else:
                resp = await call_next(request)
        elif not ok:
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
        if auto_granted:
            grant(resp)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    async def form_value(request: Request, name: str) -> str:
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return ""
        if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            return ""
        return (parse_qs(body.decode("utf-8", "replace")).get(name) or [""])[0].strip()

    async def failed(client: str, msg: str, body: str) -> Response:
        limiter.fail(client)
        await asyncio.sleep(FAIL_DELAY)
        return page(body, msg, status=401)

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> Response:
        if authed(request):
            return RedirectResponse("/", status_code=303)
        return page(LOGIN_BODY)

    @app.get("/login/token", include_in_schema=False)
    def token_page(request: Request) -> Response:          # hidden fallback, not linked from the UI
        if authed(request):
            return RedirectResponse("/", status_code=303)
        return page(TOKEN_BODY)

    @app.post("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        client = peer(request) or "?"
        if not same_origin(request):
            return page(TOKEN_BODY, "Anfrage abgelehnt (fremde Herkunft).", status=403)
        if limiter.blocked(client):
            return page(TOKEN_BODY, "Zu viele Fehlversuche. Bitte später nochmal.", status=429)
        submitted = await form_value(request, "token")
        if not submitted or not hmac.compare_digest(submitted.encode(), token.encode()):
            return await failed(client, "Falscher Schlüssel.", TOKEN_BODY)
        return grant(RedirectResponse("/", status_code=303))

    @app.post("/login/link", include_in_schema=False)
    async def login_link(request: Request) -> Response:
        client = peer(request) or "?"
        if not same_origin(request):
            return page(LOGIN_BODY, "Anfrage abgelehnt (fremde Herkunft).", status=403)
        if limiter.blocked(client):
            return page(LOGIN_BODY, "Zu viele Fehlversuche. Bitte später nochmal.", status=429)
        nonce = await form_value(request, "nonce")
        if links is None or not await asyncio.to_thread(links.consume, nonce):
            return await failed(client, "Der Login-Link ist ungültig, abgelaufen oder schon benutzt.", LOGIN_BODY)
        return grant(RedirectResponse("/", status_code=303))
