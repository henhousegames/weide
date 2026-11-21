"""Private, always-on PREVIEW of the feed (`weide serve --preview`): what is live, what is approved but
not deployed yet, and drafts waiting for review in the generation service.

Three kinds of cards, one feed:
  1. published posts of henhouse.db, exactly as the public site shows them (no marker);
  2. approved, not yet deployed: a generation with an active `publication` whose public_id is not a
     published post in henhouse.db -> marker "FREIGEGEBEN – noch nicht live" (already published: not
     shown again, the post is);
  3. drafts: generations with status ready_for_review and no active publication (none, or revoked)
     -> marker "ENTWURF". A revoked publication on any other status is hidden.
Every generation card links to the Lab run page (lab_path) on the same host the browser used (port 18500).
The service lists only ready_for_review / published generations; any other status is ignored.

Polling: every 60 s only what finished after the cursor; every 10 min the full list is re-read, because a
later approval or revocation of an item already seen does not come back through the cursor.

Safety: henhouse.db is opened read-only (SQLite mode=ro) and never migrated; every non-GET request is
refused; nothing here imports or runs export, deploy or publish code; the publish key
(HENHOUSE_SERVICE_KEY) is dropped from the environment at startup. The service is polled with a
read-only `review` key (review.py) that never reaches the browser: draft media are streamed through
/preview/media/... (auth required, Range passed through). Every route needs the login cookie
(preview_auth.py; owner auto-login over Tailscale and one-time links in
preview_login.py). Hosts: a comma separated list (HENHOUSE_PREVIEW_HOSTS or --host), never a wildcard
address; non-loopback addresses are bound with IP_FREEBIND so a Tailscale address that is not up yet
at boot does not fail the start.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import config
from .db import parse_time
from .preview_auth import install_auth, load_key
from .preview_login import LinkStore, OwnerAutoLogin, owner_logins
from .review import DRAFT_STATUS, GEN_ID, LAB_PATH, SLUG, ReviewClient, ReviewError, ReviewItem, ReviewSettings
from .server import POST_SQL, PUBLISHED, account_brief, create_app, feed_page, _csv

log = logging.getLogger("weide.preview")

DEFAULT_PORT = 18510
LAB_PORT = 18500
POLL_SECONDS = 60
FULL_RELOAD_SECONDS = 600         # full re-read: picks up later approvals/revocations and drops items gone
PREVIEW_STATIC = Path(__file__).resolve().parent / "preview_static"
ID_PREFIX = "vs-"
STUDIO = {"slug": "studio-entwurf", "handle": "@studio.entwurf", "display_name": "Studio (Entwurf)",
          "kind": "other", "avatar_url": None, "color": "#ffc928"}
MARK_APPROVED = "FREIGEGEBEN – noch nicht live"
MARK_DRAFT = "ENTWURF"
PROXY_HEADERS = ("content-type", "content-encoding", "content-length", "content-range", "accept-ranges", "last-modified", "etag")
FORBIDDEN_ENV = "SERVICE_KEY"     # the publish key: never in the preview process
BACKOFF_START = 5                 # seconds before the first retry after a failed poll, doubled per failure
BACKOFF_MAX = 600
NOTICE_OFF = "Studio-Verbindung fehlt – Entwürfe und Freigaben nicht sichtbar"
NOTICE_DOWN = "Studio nicht erreichbar – Entwürfe und Freigaben nicht aktuell"
NOTICE_AUTH = "Studio verweigert den Zugriff – Entwürfe und Freigaben nicht aktuell"


class PreviewConfigError(Exception):
    pass


# --- hosts and sockets ------------------------------------------------------------------------------

def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_hosts(value: str | list[str] | None) -> list[str]:
    """Comma separated IP addresses (or localhost). Wildcards (0.0.0.0, ::) and host names are refused."""
    raw = value if isinstance(value, list) else (value or "").split(",")
    hosts: list[str] = []
    for h in (x.strip() for x in raw):
        if not h:
            continue
        h = h.strip("[]")
        if h == "localhost":
            h = "127.0.0.1"
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            raise PreviewConfigError(f"preview host must be an IP address, got {h!r}") from None
        if ip.is_unspecified:
            raise PreviewConfigError(f"refusing to listen on {h}: list the addresses explicitly "
                                     "(e.g. 127.0.0.1 and the Tailscale address)")
        if h not in hosts:
            hosts.append(h)
    return hosts or ["127.0.0.1"]


IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)           # Linux
IPV6_FREEBIND = getattr(socket, "IPV6_FREEBIND", 78)


def bind_sockets(hosts: list[str], port: int, sock_factory: Callable[..., Any] = socket.socket) -> list:
    """One listening socket per address. Non-loopback addresses get IP_FREEBIND (the Tailscale address
    may appear only after the unit starts); loopback binds stay strict. Mirrors video-service."""
    hosts = parse_hosts(hosts)                      # re-checked here: never a wildcard bind
    socks = []
    try:
        for host in hosts:
            v6 = ":" in host
            s = sock_factory(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_STREAM)
            socks.append(s)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if v6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if not is_loopback(host):
                with contextlib.suppress(OSError):          # not Linux: a missing address then fails below
                    if v6:
                        s.setsockopt(socket.IPPROTO_IPV6, IPV6_FREEBIND, 1)
                    else:
                        s.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
            try:
                s.bind((host, port))
            except OSError as e:
                raise OSError(f"cannot bind {host}:{port}: {e}") from e
            s.listen(128)
            s.setblocking(False)
    except OSError:
        for s in socks:
            s.close()
        raise
    return socks


def drop_publish_key() -> bool:
    """The preview must hold no publish key: remove it from this process's environment."""
    dropped = False
    for name in (config.ENV_PREFIX + FORBIDDEN_ENV, config.LEGACY_ENV_PREFIX + FORBIDDEN_ENV):
        if os.environ.pop(name, None) is not None:
            dropped = True
    if dropped:
        log.warning("%s%s is ignored in preview mode (removed from the environment)", config.ENV_PREFIX, FORBIDDEN_ENV)
    return dropped


def load_review_settings() -> ReviewSettings | None:
    """The review API settings, or None (with one warning, no values logged) when they are missing or
    invalid: the preview then serves published posts only. Never falls back to the publish key."""
    try:
        settings = ReviewSettings.load()
    except ReviewError as e:
        log.warning("studio connection disabled: %s; showing published posts only", e)
        return None
    if settings is None:
        missing = [config.ENV_PREFIX + n for n in ("SERVICE_URL", "REVIEW_KEY") if not config.env(n)]
        log.warning("studio connection not configured (%s missing); showing published posts only",
                    " and ".join(missing))
    return settings


# --- review store ------------------------------------------------------------------------------------

class ReviewStore:
    """In-memory copy of the reviewable generations, refreshed by polling (never written to disk)."""

    def __init__(self, client: ReviewClient | None, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.clock = clock
        self.last_full: float | None = None      # clock() of the last successful full reload
        self.items: dict[str, ReviewItem] = {}
        self.cursor: str | None = None
        self.polls = 0
        self.last_ok: str | None = None
        self.error: str | None = None
        self.failures = 0                    # consecutive failed polls
        self.auth_failed = False

    @property
    def connected(self) -> bool:
        return self.client is not None

    @property
    def state(self) -> str:
        """off: not configured; auth: key refused (401/403); down: unreachable/failing; ok."""
        if self.client is None:
            return "off"
        if self.failures:
            return "auth" if self.auth_failed else "down"
        return "ok"

    @property
    def notice(self) -> str | None:
        return {"off": NOTICE_OFF, "down": NOTICE_DOWN, "auth": NOTICE_AUTH}.get(self.state)

    def retry_delay(self, interval: float) -> float:
        if not self.failures:
            return interval
        return min(BACKOFF_START * 2 ** (self.failures - 1), BACKOFF_MAX)

    async def poll_once(self) -> None:
        if self.client is None:
            return
        now = self.clock()
        full = self.cursor is None or self.last_full is None or now - self.last_full >= FULL_RELOAD_SECONDS
        try:
            raw, cursor = await self.client.generations(None if full else self.cursor)
        except ReviewError as e:
            # messages carry status codes / exception class names only, never the key or the URL
            self.error = str(e)
            self.failures += 1
            self.auth_failed = e.status in (401, 403)
            log.warning("review poll failed (%d in a row): %s", self.failures, self.error)
            return
        fresh: dict[str, ReviewItem] = {} if full else dict(self.items)
        bad = 0
        for d in raw:
            try:
                it = ReviewItem.parse(d)
            except ReviewError:
                bad += 1
                continue
            fresh[it.id] = it
        self.items = fresh
        self.cursor = cursor
        self.polls += 1
        if full:
            self.last_full = now
        if self.failures:
            log.warning("review API reachable again")
        self.failures, self.auth_failed = 0, False
        self.last_ok = datetime.now(config.TZ).isoformat(timespec="seconds")
        self.error = f"{bad} malformed item(s) skipped" if bad else None

    async def run(self, interval: float, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        """Poll forever; after failures retry with exponential backoff instead of the normal interval.
        Never raises (an unexpected error is counted as a failed poll) so serving goes on."""
        if self.client is None:
            return
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001 - the poller must never take the server down
                self.error = f"review poll crashed: {type(e).__name__}"
                self.failures += 1
                self.auth_failed = False
                log.warning("%s", self.error)
            await sleep(self.retry_delay(interval))

    def get(self, gid: str) -> ReviewItem | None:
        it = self.items.get(gid)
        return it if it and it.visible else None


# --- cards -------------------------------------------------------------------------------------------

def lab_url(request: Request, gid: str, lab_path: str | None = None) -> str | None:
    host = request.url.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        host = f"[{ip}]" if ip.version == 6 else str(ip)
    except ValueError:
        if not host or not all(c.isalnum() or c in ".-" for c in host):
            return None
    path = lab_path if lab_path and LAB_PATH.match(lab_path) else f"/#/g/{quote(gid, safe='')}"
    return f"http://{host}:{LAB_PORT}{path}"


def _when(it: ReviewItem, approved: bool) -> str:
    raw = (it.publication.approved_at if approved and it.publication else None) or it.finished_at or it.created_at
    try:
        return parse_time(raw).isoformat(timespec="seconds") if raw else datetime.now(config.TZ).isoformat(timespec="seconds")
    except ValueError:
        return datetime.now(config.TZ).isoformat(timespec="seconds")


def classify(it: ReviewItem, published_ids: set[str]) -> str | None:
    """approved | draft | None (not shown: unlisted status, already a published post, or revoked and not
    ready_for_review)."""
    if not it.visible:
        return None
    pub = it.publication
    if pub and pub.active:
        return None if pub.public_id in published_ids else "approved"
    return "draft" if it.status == DRAFT_STATUS else None


def preview_card(it: ReviewItem, state: str, accounts: dict[str, dict], request: Request) -> dict:
    pub = it.publication if state == "approved" else None
    slugs = [s for s in ((pub.character if pub else None), it.labels.get("character")) if isinstance(s, str)]
    account = next((accounts[s] for s in slugs if s in accounts), STUDIO)
    posted_at = _when(it, state == "approved")
    model = it.model_label or it.model or "unbekanntes Modell"
    label_caption = it.labels.get("caption") if isinstance(it.labels.get("caption"), str) else None
    caption = (pub.caption if pub else None) or label_caption or f"{model} · {parse_time(posted_at):%d.%m.%Y}"
    base = f"/preview/media/{quote(it.id, safe='')}"
    media_url = f"{base}/output" if it.output_url else None
    poster_url = f"{base}/poster" if it.poster_url else (media_url if it.output_kind == "image" else None)
    kind = "reel" if it.output_kind == "video" else "image" if it.output_kind == "image" else "text"
    return {
        "id": ID_PREFIX + it.id, "kind": kind, "title": None, "caption": caption, "hashtags": None,
        "posted_at": posted_at, "source": None, "clip_id": None,
        "media_url": media_url, "media_path": None, "media_kind": it.output_kind if media_url else None,
        "media_exists": bool(media_url), "poster_url": poster_url,
        "funny_score": None, "why": None, "episode": None,
        "reactions": {t: 0 for t in config.REACTION_TYPES}, "comment_count": 0, "moment_count": 0, "score": 0,
        "account": account, "generation": None,
        "preview": {"state": state, "label": MARK_APPROVED if state == "approved" else MARK_DRAFT,
                    "lab_url": lab_url(request, it.id, it.lab_path), "model": model, "task": it.task, "prompt": it.prompt},
    }


def _accounts(con: sqlite3.Connection, slugs: set[str]) -> dict[str, dict]:
    slugs = {s for s in slugs if SLUG.match(s)}
    if not slugs:
        return {}
    rows = con.execute(f"SELECT * FROM accounts WHERE slug IN ({','.join('?' * len(slugs))})", sorted(slugs)).fetchall()
    return {r["slug"]: account_brief(r) for r in rows}


def _published_ids(con: sqlite3.Connection, ids: set[str]) -> set[str]:
    if not ids:
        return set()
    q = f"SELECT public_id FROM posts p WHERE p.public_id IN ({','.join('?' * len(ids))}) AND {PUBLISHED}"
    return {r[0] for r in con.execute(q, sorted(ids))}


def preview_cards(store: ReviewStore, con: sqlite3.Connection, request: Request) -> list[dict]:
    items = [it for it in store.items.values() if it.visible]
    published = _published_ids(con, {it.publication.public_id for it in items if it.publication})
    shown = [(it, st) for it in items if (st := classify(it, published))]
    slugs = {s for it, _ in shown for s in ((it.publication.character if it.publication else None),
                                            it.labels.get("character")) if isinstance(s, str)}
    accounts = _accounts(con, slugs)
    cards = [preview_card(it, st, accounts, request) for it, st in shown]
    cards.sort(key=_ts, reverse=True)           # newest first, like the public feed
    return cards


def _ts(card: dict) -> float:
    return parse_time(card["posted_at"]).timestamp()


def _account_kind_world(con: sqlite3.Connection, slug: str) -> tuple[str | None, str | None]:
    r = con.execute("SELECT kind, world FROM accounts WHERE slug=?", (slug,)).fetchone()
    if not r:
        return None, None
    return r["kind"], r["world"] or config.WORLD_BY_KIND.get(r["kind"])


# --- app ---------------------------------------------------------------------------------------------

def _index_html() -> str:
    html = (config.TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{APP_NAME}}", config.APP_NAME).replace("{{APP_TAGLINE}}", config.APP_TAGLINE)
    html = html.replace("<title>", '<meta name="robots" content="noindex">\n  <title>', 1)
    html = html.replace("</head>", '  <link rel="stylesheet" href="/preview/static/preview.css">\n</head>', 1)
    # a classic script runs before the (deferred) module, so it sees every API response
    return html.replace('<script type="module"', '<script src="/preview/static/preview.js"></script>\n  '
                        '<script type="module"', 1)


def create_preview_app(db_path: str | Path | None = None, *, token: str | None = None,
                       settings: ReviewSettings | None = None, transport: httpx.AsyncBaseTransport | None = None,
                       poll_interval: float | None = POLL_SECONDS, auto: OwnerAutoLogin | None = None,
                       links: LinkStore | None = None) -> FastAPI:
    """The preview app. token: the login token (default: read from the key file). settings/transport:
    the review API (default: from the environment; None = not connected). poll_interval None: no
    background polling (tests call app.state.store.poll_once()). auto/links: owner auto-login and the
    one-time link store (default: from the private config dir)."""
    drop_publish_key()
    token = token or load_key()
    if settings is None:
        settings = load_review_settings()
    client = ReviewClient(settings, transport=transport) if settings else None
    store = ReviewStore(client)
    auto = auto if auto is not None else OwnerAutoLogin(owner_logins())
    links = links if links is not None else LinkStore()

    def setup(app: FastAPI, db: Callable[[], Any]) -> None:
        app.state.store = store
        install_auth(app, token, auto=auto, links=links)

        @contextlib.asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            task = asyncio.create_task(store.run(poll_interval)) if (client and poll_interval) else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if client:
                    await client.aclose()

        app.router.lifespan_context = lifespan
        index_html = _index_html()

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def index() -> HTMLResponse:
            return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

        app.mount("/preview/static", StaticFiles(directory=PREVIEW_STATIC), name="preview-static")

        @app.get("/api/preview/status")
        def status() -> dict:
            items = [it for it in store.items.values() if it.visible]
            return {"preview": True, "connected": store.connected, "studio": store.state, "notice": store.notice,
                    "last_poll": store.last_ok, "error": store.error, "generations": len(items)}

        @app.get("/api/feed")
        def feed(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                 account: str | None = None, kind: str | None = None, world: str | None = None) -> dict:
            # preview cards interleave with the published posts by time (newest first), like the public feed
            with db() as con:
                extra = preview_cards(store, con, request)
                kinds, worlds = set(_csv(kind)), set(_csv(world))
                if account:
                    extra = [c for c in extra if c["account"]["slug"] == account]
                if kinds:
                    extra = [c for c in extra if c["account"]["kind"] in kinds]
                if worlds:
                    extra = [c for c in extra if _account_kind_world(con, c["account"]["slug"])[1] in worlds]
                pub = feed_page(con, offset + limit + 1, 0, account=account, kind=kind, world=world)["items"]
            merged = sorted(extra + pub, key=_ts, reverse=True)      # stable: published keep their order
            window = merged[offset:offset + limit]
            more = len(merged) > offset + limit
            return {"items": window, "next_offset": offset + limit if more else None}

        @app.get("/api/posts/" + ID_PREFIX + "{gid}")
        def preview_post(gid: str, request: Request) -> dict:
            with db() as con:
                card = next((c for c in preview_cards(store, con, request) if c["id"] == ID_PREFIX + gid), None)
            if card is None:
                raise HTTPException(404, "no such preview post")
            return {**card, "comments": [], "moments": [], "parent": None, "uwe_reacted": False}

        @app.get(f"/api/accounts/{STUDIO['slug']}")
        def studio(request: Request) -> dict:
            with db() as con:
                posts = [c for c in preview_cards(store, con, request) if c["account"]["slug"] == STUDIO["slug"]]
            return {**STUDIO, "bio": "Entwürfe aus dem Studio, die noch keiner Figur zugeordnet sind.",
                    "followers": 0, "followers_note": None, "following": 0, "about": [], "facts": None,
                    "values": [], "day": [], "people": [], "posts": posts}

        @app.get("/preview/media/{gid}/{which}")
        async def media(gid: str, which: str, request: Request) -> StreamingResponse:
            it = store.get(gid) if GEN_ID.match(gid) else None
            ref = (it.output_url if which == "output" else it.poster_url if which == "poster" else None) if it else None
            if client is None or not ref:
                raise HTTPException(404, "not found")
            try:
                upstream = await client.open_media(ref, request.headers.get("range"))
            except ReviewError as e:
                raise HTTPException(502, str(e)) from None
            if upstream.status_code not in (200, 206, 304, 416):
                await upstream.aclose()
                raise HTTPException(404 if upstream.status_code == 404 else 502, "media not available")
            headers = {k: upstream.headers[k] for k in PROXY_HEADERS if k in upstream.headers}
            headers["Cache-Control"] = "private, max-age=300"
            return StreamingResponse(_body(upstream), status_code=upstream.status_code, headers=headers,
                                     background=BackgroundTask(upstream.aclose))

    return create_app(db_path, readonly=True, setup=setup)


async def _body(upstream: httpx.Response) -> AsyncIterator[bytes]:
    if upstream.is_stream_consumed:          # already buffered (e.g. a mock transport)
        yield upstream.content
        return
    async for chunk in upstream.aiter_raw():
        yield chunk


def serve(db_path: str | Path | None, hosts: list[str], port: int) -> int:
    """Bind every host (FREEBIND for non-loopback) and run uvicorn on the pre-bound sockets."""
    import uvicorn
    hosts = parse_hosts(hosts)
    app = create_preview_app(db_path)
    socks = bind_sockets(hosts, port)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", proxy_headers=False, server_header=False))
    log.warning("preview on %s", ", ".join(f"{h}:{port}" for h in hosts))
    server.run(sockets=socks)
    return 0
