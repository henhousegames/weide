"""HTTP API + web UI (FastAPI). Started by `weide serve`.

The JSON contract is the one static/js/api.js expects (and export-static writes). Only posts with
status 'published' are visible anywhere: feed, post detail, moments, profiles, rankings, counts.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .db import bump_bucket, comment_counts, connect, jload, reaction_totals, resolve_post
from .media import MediaError, avatar_file, media_exists, media_file, media_kind, thumb_file
from .ranking import combine, engagement, engagement_score, iso_week, today, top, window

PUBLISHED = "p.status = 'published'"


def media_url(rel: str | None) -> str | None:
    return f"/media/{rel}" if rel else None


def thumb_url(rel: str | None, w: int = 540) -> str | None:
    return f"/thumb/{w}/{rel}" if rel else None


def account_brief(r: sqlite3.Row | dict, prefix: str = "") -> dict:
    g = (lambda k: r[prefix + k])
    return {
        "slug": g("slug"), "handle": g("handle"), "display_name": g("display_name"), "kind": g("kind"),
        "avatar_url": f"/avatar/{g('slug')}.jpg" if g("avatar") else None, "color": g("color"),
    }


def public_generation(r: sqlite3.Row) -> dict | None:
    """Card summary of how a post was generated; None for legacy posts (no invented data).
    Only whitelisted keys of generation_json leave the server, under the opaque public_id."""
    gen = jload(r["generation_json"], None)
    if not r["public_id"] or not isinstance(gen, dict):
        return None
    return {"public_id": r["public_id"], **{k: gen[k] for k in config.PUBLIC_GENERATION_KEYS if k in gen}}


def public_source(src: str | None) -> str | None:
    """Generation posts are identified publicly by public_id; their internal run source stays private."""
    return None if src and src.startswith("generation:") else src


POST_SQL = """
SELECT p.*, a.slug a_slug, a.handle a_handle, a.display_name a_display_name, a.kind a_kind, a.avatar a_avatar,
       a.color a_color,
       (SELECT COUNT(*) FROM posts m WHERE m.parent_id = p.id AND m.status = 'published') AS moment_count
FROM posts p JOIN accounts a ON a.id = p.account_id
"""


def post_dicts(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    ids = [r["id"] for r in rows]
    rx = reaction_totals(con, ids)
    cc = comment_counts(con, ids)
    cc_others = comment_counts(con, ids, exclude_author=True)
    out = []
    for r in rows:
        e = engagement_score(engagement(rx[r["id"]], cc_others[r["id"]]))
        poster = r["poster"] or (r["media"] if media_kind(r["media"]) in ("video", "image") else None)
        meta = jload(r["meta_json"], {}) or {}
        out.append({
            "id": r["id"], "kind": r["kind"], "title": r["title"], "caption": r["caption"], "hashtags": r["hashtags"],
            "posted_at": r["posted_at"], "source": public_source(r["source"]), "clip_id": r["clip_id"],
            "media_url": media_url(r["media"]), "media_path": r["media"], "media_kind": media_kind(r["media"]),
            "media_exists": media_exists(r["media"]),
            "poster_url": thumb_url(poster, 540) if poster else None,
            "funny_score": r["funny_score"], "why": r["why"], "episode": meta.get("episode"),
            "reactions": rx[r["id"]], "comment_count": cc[r["id"]], "moment_count": r["moment_count"],
            "score": round(combine(r["funny_score"], e), 2),
            "account": account_brief(r, "a_"),
            "generation": public_generation(r),
        })
    return out


def threaded_comments(con: sqlite3.Connection, post_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT c.*, a.slug, a.handle, a.display_name, a.kind, a.avatar, a.color FROM comments c "
        "JOIN accounts a ON a.id = c.account_id WHERE c.post_id = ? ORDER BY c.ts, c.id", (post_id,)).fetchall()
    nodes = {r["id"]: {"id": r["id"], "text": r["text"], "created_at": r["created_at"], "source": r["source"],
                       "reply_to": r["reply_to"], "account": account_brief(r), "replies": []} for r in rows}
    roots = []
    for r in rows:
        n = nodes[r["id"]]
        if r["reply_to"] and r["reply_to"] in nodes:
            nodes[r["reply_to"]]["replies"].append(n)
        else:
            roots.append(n)
    return roots


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


class ReactBody(BaseModel):
    type: str


def create_app(db_path: str | Path | None = None) -> FastAPI:
    db_file = Path(db_path or config.DEFAULT_DB)
    connect(db_file).close()          # create / migrate schema once
    app = FastAPI(title=config.APP_NAME, docs_url="/api/docs", redoc_url=None)

    @contextmanager
    def db() -> Iterator[sqlite3.Connection]:
        con = connect(db_file, init=False)
        try:
            yield con
        finally:
            con.close()

    index_html = (config.TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = index_html.replace("{{APP_NAME}}", config.APP_NAME).replace("{{APP_TAGLINE}}", config.APP_TAGLINE)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    if config.STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

    @app.get("/api/meta")
    def meta() -> dict:
        d = today()
        with db() as con:
            n = con.execute(f"SELECT COUNT(*) FROM posts p WHERE p.parent_id IS NULL AND {PUBLISHED}").fetchone()[0]
        return {
            "app_name": config.APP_NAME, "tagline": config.APP_TAGLINE, "today": d.isoformat(), "week": iso_week(d),
            "reactions": [{"key": k, **{kk: v[kk] for kk in ("label", "emoji")}} for k, v in config.REACTIONS.items()],
            "posts": n,
            "cli": "uv run weide",
            "ranking": {"curated_weight": config.CURATED_WEIGHT, "uncurated_max": config.UNCURATED_MAX,
                        "score_max": config.SCORE_MAX, "comment_weight": config.COMMENT_WEIGHT,
                        "engagement_ref": config.ENGAGEMENT_REF,
                        "weights": {k: v["weight"] for k, v in config.REACTIONS.items()}},
        }

    @app.get("/api/feed")
    def feed(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), account: str | None = None,
             kind: str | None = Query(None, description="account kinds, comma separated (animal,human,robot,...)"),
             world: str | None = Query(None, description="worlds, comma separated (weide,menschen,roboter,...)")) -> dict:
        with db() as con:
            q = POST_SQL + f" WHERE p.parent_id IS NULL AND {PUBLISHED}"
            args: list[Any] = []
            if account:
                q += " AND a.slug = ?"
                args.append(account)
            kinds = _csv(kind)
            if kinds:
                q += f" AND a.kind IN ({','.join('?' * len(kinds))})"
                args += kinds
            worlds = _csv(world)
            if worlds:
                derived = " ".join(f"WHEN '{k}' THEN '{w}'" for k, w in config.WORLD_BY_KIND.items())
                q += f" AND COALESCE(a.world, CASE a.kind {derived} END) IN ({','.join('?' * len(worlds))})"
                args += worlds
            q += " ORDER BY p.ts DESC, p.id DESC LIMIT ? OFFSET ?"
            rows = con.execute(q, (*args, limit + 1, offset)).fetchall()
            items = post_dicts(con, rows[:limit])
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    @app.get("/api/posts/{post_id}")
    def post_detail(post_id: str) -> dict:
        with db() as con:
            try:
                pid = resolve_post(con, post_id, published_only=True)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            row = con.execute(POST_SQL + " WHERE p.id = ?", (pid,)).fetchone()
            item = post_dicts(con, [row])[0]
            item["comments"] = threaded_comments(con, pid)
            moments = con.execute(POST_SQL + f" WHERE p.parent_id = ? AND {PUBLISHED} "
                                  "ORDER BY COALESCE(p.funny_score,0) DESC, p.id", (pid,)).fetchall()
            item["moments"] = post_dicts(con, moments)
            if row["parent_id"]:
                parent = con.execute(POST_SQL + f" WHERE p.id = ? AND {PUBLISHED}", (row["parent_id"],)).fetchone()
                item["parent"] = post_dicts(con, [parent])[0] if parent else None
            uwe = con.execute("SELECT 1 FROM reactions r JOIN accounts a ON a.id=r.account_id "
                              "WHERE r.post_id=? AND a.slug='uwe' LIMIT 1", (pid,)).fetchone()
            item["uwe_reacted"] = bool(uwe)
        return item

    @app.post("/api/posts/{post_id}/react")
    def react(post_id: int, body: ReactBody) -> dict:
        if body.type not in config.REACTIONS:
            raise HTTPException(400, f"type must be one of {', '.join(config.REACTION_TYPES)}")
        with db() as con:
            try:
                pid = resolve_post(con, post_id, published_only=True)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            bump_bucket(con, pid, body.type, 1, "web")
            con.commit()
            return {"reactions": reaction_totals(con, [pid])[pid]}

    @app.get("/api/accounts")
    def accounts(kind: str | None = None) -> dict:
        with db() as con:
            q = ("SELECT a.*, (SELECT COUNT(*) FROM posts p WHERE p.account_id=a.id AND p.parent_id IS NULL "
                 f"AND {PUBLISHED}) posts FROM accounts a")
            args: list[Any] = []
            kinds = _csv(kind)
            if kinds:
                q += f" WHERE a.kind IN ({','.join('?' * len(kinds))})"
                args += kinds
            q += (" ORDER BY CASE a.kind WHEN 'animal' THEN 0 WHEN 'human' THEN 1 WHEN 'robot' THEN 2 "
                  "WHEN 'other' THEN 3 WHEN 'house' THEN 4 ELSE 5 END, a.followers DESC, a.id")
            rows = con.execute(q, args).fetchall()
        return {"items": [{**account_brief(r), "bio": r["bio"], "followers": r["followers"], "posts": r["posts"]}
                          for r in rows]}

    @app.get("/api/accounts/{slug}")
    def account(slug: str) -> dict:
        with db() as con:
            r = con.execute("SELECT * FROM accounts WHERE slug = ? OR handle = ? OR handle = ?",
                            (slug.lstrip("@"), slug, "@" + slug.lstrip("@"))).fetchone()
            if not r:
                raise HTTPException(404, f"no account {slug!r}")
            prof = jload(r["profile_json"], {}) or {}
            # Only the in-world profile is shown; writer-facing keys never leave the server.
            inworld = prof.get("inworld") or {}
            people = []
            for e in inworld.get("leute") or []:
                o = con.execute("SELECT * FROM accounts WHERE slug = ?", (e["slug"],)).fetchone()
                people.append({"slug": e["slug"], "text": e["text"], "account": account_brief(o) if o else None})
            following = con.execute("SELECT COUNT(*) FROM follows WHERE follower_id=?", (r["id"],)).fetchone()[0]
            if not following:
                following = int(prof.get("public_following") or 0)      # imported count, edges unknown
            posts = con.execute(POST_SQL + f" WHERE p.account_id=? AND p.parent_id IS NULL AND {PUBLISHED} "
                                "ORDER BY p.ts DESC", (r["id"],)).fetchall()
            return {
                **account_brief(r), "bio": r["bio"], "followers": r["followers"], "followers_note": r["followers_note"],
                "following": following,
                "about": inworld.get("ueber_mich") or [],
                "facts": inworld.get("steckbrief"),
                "values": inworld.get("werte") or [],
                "day": inworld.get("mein_tag") or [],
                "people": people,
                "posts": post_dicts(con, posts),
            }

    @app.get("/api/top")
    def top_api(day: str | None = None, week: str | None = None, since: str | None = None, until: str | None = None,
                limit: int = Query(10, ge=1, le=50), collections: bool = False) -> dict:
        try:
            win = window(day=day, week=week, since=since, until=until)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        with db() as con:
            items = top(con, win, limit=limit, include_collections=collections)
            ids = [i["post_id"] for i in items]
            rows = con.execute(POST_SQL + f" WHERE p.id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
            cards = {d["id"]: d for d in post_dicts(con, rows)}
        return {
            "window": {"kind": win["kind"], "key": win["key"], "label": win["label"],
                       "since": win["start"].isoformat(), "until": win["end"].isoformat()},
            "items": [{**cards[i["post_id"]], "rank": i["rank"], "score": i["score"], "E": i["E"],
                       "engagement": i["engagement"], "moments": i["moments"]} for i in items],
            "commands": [],       # dev CLI shortcuts (top / export-collection) are not ported yet
        }

    @app.get("/media/{path:path}")
    def media(path: str) -> FileResponse:
        try:
            p = media_file(path)
        except MediaError as e:
            raise HTTPException(404 if str(e) == "not found" else 403, str(e)) from e
        return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/thumb/{width}/{path:path}")
    def thumb(width: int, path: str) -> FileResponse:
        try:
            p = thumb_file(width, path)
        except MediaError as e:
            raise HTTPException(404 if str(e) == "not found" else 403, str(e)) from e
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/avatar/{slug}.jpg")
    def avatar(slug: str) -> FileResponse:
        with db() as con:
            r = con.execute("SELECT avatar FROM accounts WHERE slug = ?", (slug,)).fetchone()
        if not r or not r["avatar"]:
            raise HTTPException(404, "no avatar")
        try:
            p = avatar_file(r["avatar"])
        except MediaError as e:
            raise HTTPException(404, str(e)) from e
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.exception_handler(sqlite3.OperationalError)
    def db_error(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        return JSONResponse({"detail": f"database error: {exc}"}, status_code=500)

    return app
