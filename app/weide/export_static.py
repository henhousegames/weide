"""Static snapshot of the app for a plain file host (GitHub Pages): no server, no database.

Every GET the frontend makes is answered once through the real FastAPI app (TestClient) and written
as JSON under data/; media, posters and avatars are copied (or hard-linked) from the web-sized files;
URLs are rewritten to relative paths so the site works under any sub-path. index.html sets
`window.APP_STATIC = true` (static/js/api.js also still honours the legacy `window.WEIDE_STATIC`), which
makes static/js/api.js read these files.

Only published posts are exported (the server hides everything else). Writer-facing fields stay out:
file paths (media_path, raw moment video/poster), dev CLI hints (top `commands`, meta `cli`), moment
`why` strings that are pipeline ids. Generation posts additionally get data/gen/<public_id>.json, the
sanitized public summary from generation.py (all settings and the prompt; no paths, keys, hosts or run
ids). Files of an earlier export that are no longer referenced (revoked
posts) are removed. The world's "today" defaults to the date of the newest published post, so the same
data exports to the same files on any day. `leak_check()` scans the result for paths, secrets and run ids (summaries with their own rules).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from . import config
from .generation import PUBLIC_ID, credits_leaks, summary_leaks
from .media import MediaError, avatar_file, media_file, thumb_file

URL = re.compile(r"^/(media|thumb/(\d+)|avatar)/(.+)$")
DROP_KEYS = {"media_path", "commands", "cli"}
MOMENT_DROP_KEYS = {"video", "poster"}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


class _Files:
    """Collects every media/thumb/avatar URL in the JSON, rewrites it relative, places the file once."""

    def __init__(self, out: Path, con: sqlite3.Connection, link: bool):
        self.out, self.con, self.link = out, con, link
        self.done: dict[str, str | None] = {}
        self.missing: list[str] = []

    def url(self, u: str) -> str | None:
        if u in self.done:
            return self.done[u]
        m = URL.match(u.split("?")[0])
        if not m:
            return u
        kind, width, rest = m.groups()
        rel: str | None
        try:
            if kind == "media":
                src, rel = media_file(rest), f"media/{rest}"
            elif kind == "avatar":
                slug = rest.removesuffix(".jpg")
                r = self.con.execute("SELECT avatar FROM accounts WHERE slug = ?", (slug,)).fetchone()
                if not r or not r[0]:
                    raise MediaError("no avatar")
                src, rel = avatar_file(r[0]), f"avatar/{slug}.jpg"
            else:
                src = thumb_file(int(width), rest)
                rel = str(Path(f"thumb/{width}/{rest}").with_suffix(".jpg"))
            self._place(src, self.out / rel)
        except MediaError as e:
            self.missing.append(f"{u}: {e}")
            rel = None
        self.done[u] = rel
        return rel

    def _place(self, src: Path, dst: Path) -> None:
        if dst.exists() and (dst.resolve() == src.resolve() or os.path.samefile(src, dst)):
            return
        if dst.is_file() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.unlink(missing_ok=True)
        if self.link:
            try:
                os.link(src, dst)
                return
            except OSError:
                pass
        shutil.copy2(src, dst)


def _clean(obj: Any, files: _Files, key: str | None = None) -> Any:
    """Rewrite URLs, drop writer-facing fields (recursive)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROP_KEYS:
                continue
            if key == "moments" and k in MOMENT_DROP_KEYS:
                continue
            if k in ("media_url", "poster_url", "avatar_url") and isinstance(v, str):
                v = files.url(v)
            elif k == "why" and isinstance(v, str) and re.match(r"^(episode|story|collection|generation):", v):
                v = None
            elif k == "source" and isinstance(v, str) and v.startswith("generation:"):
                v = None
            out[k] = _clean(v, files, k)
        if out.get("why", 1) is None and ("caption" in out or "title" in out):
            out["why"] = (out.get("title") or (out.get("caption") or "")[:70]) or None
        return out
    if isinstance(obj, list):
        return [_clean(x, files, key) for x in obj]
    return obj


def render_index(static: bool = True) -> str:
    html = (config.TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{APP_NAME}}", config.APP_NAME).replace("{{APP_TAGLINE}}", config.APP_TAGLINE)
    if static:
        html = html.replace('href="/static/', 'href="static/').replace('src="/static/', 'src="static/')
        html = html.replace('<script type="module"', '<script>window.APP_STATIC = true;</script>\n  <script type="module"', 1)
    return html


# --- leak check ----------------------------------------------------------------------------------

ABS_PATH = re.compile(r"(^/|/home/|/Users/|^[A-Za-z]:[\\/]|file://|\\\\)")
SECRET_WORD = re.compile(r"key|token|secret|password", re.I)
PRIVATE_KEYS = DROP_KEYS | {"style_guidance", "key_hash", "permissions", "generation_json", "profile_json",
                            "identity", "reference_assets", "voice", "controller_id"}
ALLOWED_KEYS = {"key"}          # reactions[].key, window.key


def _scan(obj: Any, where: str, hits: list[str], parent: str | None = None) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PRIVATE_KEYS or (k not in ALLOWED_KEYS and SECRET_WORD.search(k)):
                hits.append(f"{where}: private key {k!r}")
            if parent == "moments" and k in MOMENT_DROP_KEYS:
                hits.append(f"{where}: moment {k!r}")
            if k == "credits":                 # card summaries: the same rule as data/gen/<id>.json
                pid = obj.get("public_id")
                credits_leaks(v, where, hits, pid if isinstance(pid, str) else None)
            _scan(v, where, hits, k)
    elif isinstance(obj, list):
        for x in obj:
            _scan(x, where, hits, parent)
    elif isinstance(obj, str):
        if ABS_PATH.search(obj):
            hits.append(f"{where}: absolute path {obj[:80]!r}")
        if SECRET_WORD.search(obj):
            hits.append(f"{where}: secret-like value {obj[:80]!r}")
        if obj.startswith("generation:"):
            hits.append(f"{where}: generation run id {obj[:80]!r}")


def leak_check(out: Path) -> list[str]:
    """Scan an export: JSON values with absolute paths, secret-like words, generation run ids or private
    fields; text files with home directories. Returns human-readable hits (empty = clean)."""
    out = Path(out)
    hits: list[str] = []
    for f in sorted((out / "data").rglob("*.json")):
        rel = f.relative_to(out)
        data = json.loads(f.read_text(encoding="utf-8"))
        if rel.parts[:2] == ("data", "gen"):
            summary_leaks(data, str(rel), hits, post=f.stem)        # prompts/settings are public here, paths etc. not
        else:
            _scan(data, str(rel), hits)
    for f in [out / "index.html", *sorted((out / "static").rglob("*"))]:
        if f.is_file() and f.suffix in (".html", ".js", ".css"):
            text = f.read_text(encoding="utf-8")
            for pat in ("/home/", "/Users/", "generation:"):
                if pat in text:
                    hits.append(f"{f.relative_to(out)}: contains {pat!r}")
    return hits


# --- export ----------------------------------------------------------------------------------------

def newest_post_day(db_file: Path) -> str | None:
    """World date of the newest published post: the default "today" of an export, so re-exporting the
    same data on another day gives the same files (no daily churn). None for an empty database."""
    if not Path(db_file).is_file():
        return None
    con = sqlite3.connect(str(db_file))
    try:
        row = con.execute("SELECT substr(posted_at, 1, 10) FROM posts WHERE status='published' "
                          "ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return row[0] if row else None


def _prune(out: Path, keep: set[str]) -> list[str]:
    """Remove media/thumb/avatar files of an earlier export that are no longer referenced (e.g. a
    revoked post). Skipped where the export dir *is* the source root (exporting into the repo root)."""
    removed = []
    for sub, root in (("media", config.MEDIA_ROOT), ("thumb", config.THUMB_ROOT), ("avatar", config.AVATAR_ROOT)):
        d = out / sub
        if not d.is_dir() or d.resolve() == Path(root).resolve():
            continue
        for f in sorted(d.rglob("*")):
            rel = f.relative_to(out).as_posix()
            if f.is_file() and rel not in keep:
                f.unlink()
                removed.append(rel)
    return removed


def export_static(out: Path, db_path: str | Path | None = None, *, today: str | None = None,
                  link: bool = False) -> dict:
    from fastapi.testclient import TestClient

    from .server import create_app

    saved_today = config.TODAY_OVERRIDE
    db_file = Path(db_path or config.DEFAULT_DB)
    today = today or config.TODAY_OVERRIDE or newest_post_day(db_file)
    if today:
        date.fromisoformat(today)
        config.TODAY_OVERRIDE = today
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        client = TestClient(create_app(db_file))
        con = sqlite3.connect(str(db_file))
        try:
            stats = _export(out, client, con, link)
        finally:
            con.close()
    finally:
        config.TODAY_OVERRIDE = saved_today
    stats["leaks"] = leak_check(out)
    return stats


def _export(out: Path, client: Any, con: sqlite3.Connection, link: bool) -> dict:
    def get(path: str) -> Any:
        r = client.get(path)
        r.raise_for_status()
        return r.json()

    # data/ is rebuilt from scratch so posts that are no longer published do not linger
    shutil.rmtree(out / "data", ignore_errors=True)
    files = _Files(out, con, link)
    stats: dict[str, Any] = {"posts": 0, "accounts": 0, "days": 0, "weeks": 0, "generations": 0}

    meta = get("/api/meta")
    meta["static"] = True
    _write(out / "data/meta.json", _clean(meta, files))

    items, offset = [], 0
    while offset is not None:
        page = get(f"/api/feed?limit=100&offset={offset}")
        items += page["items"]
        offset = page["next_offset"]
    _write(out / "data/feed.json", {"items": _clean(items, files)})

    published = [r[0] for r in con.execute("SELECT id FROM posts WHERE status='published' ORDER BY id")]
    for pid in published:
        _write(out / f"data/posts/{pid}.json", _clean(get(f"/api/posts/{pid}"), files))
        stats["posts"] += 1

    gens = [r[0] for r in con.execute("SELECT public_id FROM posts WHERE status='published' AND public_id IS NOT NULL "
                                      "AND generation_json IS NOT NULL ORDER BY id")]
    for gid in gens:
        if PUBLIC_ID.match(gid):
            _write(out / f"data/gen/{gid}.json", get(f"/api/generations/{gid}"))
            stats["generations"] += 1

    accounts = get("/api/accounts")
    _write(out / "data/accounts.json", _clean(accounts, files))
    for a in accounts["items"]:
        _write(out / f"data/accounts/{a['slug']}.json", _clean(get(f"/api/accounts/{a['slug']}"), files))
        stats["accounts"] += 1

    days = {r[0] for r in con.execute("SELECT DISTINCT substr(posted_at, 1, 10) FROM posts WHERE status='published'")}
    days.add(meta["today"])
    weeks = {meta["week"]}
    for d in days:
        y, w, _ = date.fromisoformat(d).isocalendar()
        weeks.add(f"{y}-W{w:02d}")
    for d in sorted(days):
        _write(out / f"data/top/day-{d}.json", _clean(get(f"/api/top?day={d}&limit=10"), files))
        stats["days"] += 1
    for w in sorted(weeks):
        _write(out / f"data/top/week-{w}.json", _clean(get(f"/api/top?week={w}&limit=10"), files))
        stats["weeks"] += 1

    # the app shell: static/ copied, index.html filled in with relative asset paths + the static flag
    if config.STATIC_DIR.resolve() != (out / "static").resolve():
        shutil.copytree(config.STATIC_DIR, out / "static", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("index.html"))
    (out / "index.html").write_text(render_index(static=True), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    pruned = _prune(out, {v for v in files.done.values() if v})
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file() and ".git" not in p.parts)
    return {**stats, "files": len([v for v in files.done.values() if v]), "missing": files.missing, "pruned": len(pruned),
            "bytes": size, "today": meta["today"], "week": meta["week"]}
