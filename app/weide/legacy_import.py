"""Import a database of the legacy (v1) backend into the current schema (`weide import-legacy-db`).

The legacy file is opened strictly read-only (`?mode=ro&immutable=1`: no writes, no locks, no -wal/-shm
side files) and its rows are merged into the target database through the normal helpers. This is
preferred over copying the file and migrating the copy because
  * migration 3 marks every existing post `published`, but a legacy post is only public if it is in the
    public snapshot; here each post gets its status explicitly (snapshot -> published, else draft);
  * the target may already hold data (import-public, seed-characters): accounts are matched by slug
    (account ids differ between imports), posts and comments keep their legacy ids (they are public links);
  * legacy values that do not fit v2 are translated in one place (avatar paths, reaction placeholders).

What the public JSON lacks and this import restores: follow edges, reaction buckets per source and named
reactions (who reacted with what), the full profile_json (private writer keys included; the server only
ever serves profile_json['inworld']), full meta_json (only `episode` is served), comment sources and the
legacy meta cache. Idempotent: re-running converges to the same rows. Placeholders that import-public
derived from the public totals (source `import`) are replaced by the real legacy reactions.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from . import config
from .db import (check_account_kind, check_count, check_reaction_type, jdump, jload, now_iso, schema_version,
                 set_bucket, set_meta, upsert_account, upsert_post)
from .importer import IMPORT_SOURCE, _data_dir

SKIP_META = {"schema_version"}


def open_legacy(path: str | Path) -> sqlite3.Connection:
    """Open a legacy database without ever writing to it (or next to it)."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no legacy database at {path}")
    wal = p.with_name(p.name + "-wal")
    if wal.is_file() and wal.stat().st_size:
        raise ValueError(f"{p.name} has an uncheckpointed -wal file; import a consistent copy instead")
    con = sqlite3.connect(f"file:{quote(str(p))}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    if schema_version(con) != 1:
        con.close()
        raise ValueError(f"{p.name} is not a legacy (v1) database")
    return con


def public_snapshot(root: str | Path) -> dict:
    """Post id -> source and the account slugs of a public export (data/)."""
    data = _data_dir(Path(root))
    posts = {}
    for f in (data / "posts").glob("*.json"):
        card = json.loads(f.read_text(encoding="utf-8"))
        posts[int(card["id"])] = card.get("source")
    accounts = {a["slug"] for a in json.loads((data / "accounts.json").read_text(encoding="utf-8"))["items"]}
    return {"posts": posts, "accounts": accounts}


def _avatar(slug: str, legacy: str | None) -> str | None:
    """The legacy backend pointed at the persona source image; v2 serves AVATAR_ROOT/<slug>.jpg."""
    return f"{slug}.jpg" if legacy else None


def _import_accounts(src: sqlite3.Connection, con: sqlite3.Connection) -> dict[int, int]:
    ids: dict[int, int] = {}
    for r in src.execute("SELECT * FROM accounts ORDER BY id"):
        slug = r["slug"]
        clash = con.execute("SELECT slug FROM accounts WHERE handle=? AND slug!=?", (r["handle"], slug)).fetchone()
        if clash:
            raise ValueError(f"handle {r['handle']} of {slug!r} already belongs to account {clash['slug']!r}")
        check_account_kind(r["kind"])
        row = con.execute("SELECT id, profile_json FROM accounts WHERE slug=?", (slug,)).fetchone()
        if row is None and not con.execute("SELECT 1 FROM accounts WHERE id=?", (r["id"],)).fetchone():
            con.execute("INSERT INTO accounts (id, slug, kind, handle, display_name) VALUES (?,?,?,?,?)",
                        (r["id"], slug, r["kind"], r["handle"], r["display_name"]))      # keep the legacy id
        profile = (jload(row["profile_json"], {}) if row else {}) or {}
        legacy_profile = jload(r["profile_json"], None)
        if isinstance(legacy_profile, dict):
            profile.update(legacy_profile)
        ids[r["id"]] = upsert_account(
            con, slug, kind=r["kind"], handle=r["handle"], display_name=r["display_name"],
            bio=r["bio"], followers=check_count(r["followers"], "followers"), followers_note=r["followers_note"],
            avatar=_avatar(slug, r["avatar"]), color=r["color"], profile_json=jdump(profile) if profile else None)
    return ids


def _import_posts(src: sqlite3.Connection, con: sqlite3.Connection, accounts: dict[int, int],
                  published: dict[int, str | None]) -> dict[str, int]:
    stats = {"posts": 0, "published": 0, "draft": 0}
    rows = src.execute("SELECT * FROM posts ORDER BY parent_id IS NOT NULL, id").fetchall()
    for r in rows:
        pid = r["id"]
        if pid in published and published[pid] != r["source"]:
            raise ValueError(f"post {pid}: source {r['source']!r} differs from the public snapshot ({published[pid]!r})")
        # Only a row that came from this legacy post (same id AND same source) may be updated or demoted;
        # posts from elsewhere (generation runs, later approvals) are never touched.
        existing = con.execute("SELECT status, source FROM posts WHERE id=? OR source=?", (pid, r["source"])).fetchall()
        if any(e["source"] != r["source"] for e in existing) or len(existing) > 1:
            raise ValueError(f"legacy post {pid} ({r['source']!r}) collides with a post of another origin")
        existing = existing[0] if existing else None
        if pid in published:
            status = "published"
        elif existing and existing["status"] == "approved":
            status = "approved"                  # keep a later approval, but never publish from here
        else:
            status = "draft"
        upsert_post(con, id=pid, account_id=accounts[r["account_id"]], parent_id=r["parent_id"], kind=r["kind"],
                    title=r["title"], caption=r["caption"], hashtags=r["hashtags"], media=r["media"],
                    poster=r["poster"], posted_at=r["posted_at"], source=r["source"], clip_id=r["clip_id"],
                    funny_score=r["funny_score"], why=r["why"], meta_json=r["meta_json"], status=status)
        if r["created_at"]:
            con.execute("UPDATE posts SET created_at=? WHERE id=?", (r["created_at"], pid))
        stats["posts"] += 1
        stats[status] = stats.get(status, 0) + 1
    return stats


def _import_comments(src: sqlite3.Connection, con: sqlite3.Connection, accounts: dict[int, int]) -> int:
    rows = src.execute("SELECT * FROM comments ORDER BY id").fetchall()
    for c in rows:                                          # first without reply_to (any order), then links
        row = con.execute("SELECT post_id FROM comments WHERE id=?", (c["id"],)).fetchone()
        if row and row["post_id"] != c["post_id"]:
            raise ValueError(f"comment {c['id']} belongs to post {row['post_id']}, not {c['post_id']}")
        vals = (accounts[c["account_id"]], c["text"], c["created_at"], c["ts"], c["source"])
        if row:
            con.execute("UPDATE comments SET account_id=?, text=?, created_at=?, ts=?, source=? WHERE id=?",
                        (*vals, c["id"]))
        else:
            con.execute("INSERT INTO comments (account_id, text, created_at, ts, source, id, post_id, reply_to) "
                        "VALUES (?,?,?,?,?,?,?,NULL)", (*vals, c["id"], c["post_id"]))
    for c in rows:
        con.execute("UPDATE comments SET reply_to=? WHERE id=?", (c["reply_to"], c["id"]))
    return len(rows)


def _import_reactions(src: sqlite3.Connection, con: sqlite3.Connection, accounts: dict[int, int]) -> dict[str, int]:
    stats = {"named": 0, "buckets": 0}
    post_ids = [r[0] for r in src.execute("SELECT id FROM posts")]
    for pid in post_ids:          # public-total placeholders of import-public are superseded by the real rows
        con.execute("DELETE FROM reactions WHERE post_id=? AND source=?", (pid, IMPORT_SOURCE))
    for r in src.execute("SELECT * FROM reactions ORDER BY id"):
        check_reaction_type(r["type"])
        if r["account_id"] is None:
            set_bucket(con, r["post_id"], r["type"], check_count(r["count"]), r["source"])
            stats["buckets"] += 1
        else:
            key = (r["post_id"], accounts[r["account_id"]], r["type"])
            con.execute("INSERT OR IGNORE INTO reactions (post_id, account_id, type, count, source, created_at) "
                        "VALUES (?,?,?,1,?,?)", (*key, r["source"], r["created_at"]))
            con.execute("UPDATE reactions SET source=? WHERE post_id=? AND account_id=? AND type=?", (r["source"], *key))
            stats["named"] += 1
    return stats


def _import_follows(src: sqlite3.Connection, con: sqlite3.Connection, accounts: dict[int, int]) -> int:
    n = 0
    for f in src.execute("SELECT * FROM follows"):
        con.execute("INSERT OR IGNORE INTO follows (follower_id, followee_id, created_at) VALUES (?,?,?)",
                    (accounts[f["follower_id"]], accounts[f["followee_id"]], f["created_at"]))
        n += 1
    return n


def import_legacy_db(con: sqlite3.Connection, legacy_path: str | Path, public_root: str | Path | None = None) -> dict:
    """Merge the legacy database into `con`. Posts in the public snapshot (default: the repo's data/) are
    published, all others drafts (also on a re-run: a post missing from the snapshot is demoted to draft).
    Returns counts plus the differences to the snapshot."""
    snap = public_snapshot(public_root or config.REPO_ROOT)
    src = open_legacy(legacy_path)
    try:
        legacy_slugs = {r[0] for r in src.execute("SELECT slug FROM accounts")}
        legacy_posts = {r[0] for r in src.execute("SELECT id FROM posts")}
        try:
            accounts = _import_accounts(src, con)
            stats: dict = {"accounts": len(accounts)}
            stats.update(_import_posts(src, con, accounts, snap["posts"]))
            stats["comments"] = _import_comments(src, con, accounts)
            stats["reactions"] = _import_reactions(src, con, accounts)
            stats["follows"] = _import_follows(src, con, accounts)
            for k, v in src.execute("SELECT key, value FROM meta"):
                if k not in SKIP_META:
                    con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (k, v))
            stats["snapshot"] = {
                "draft_posts": sorted(legacy_posts - set(snap["posts"])),
                "public_posts_not_in_legacy": sorted(set(snap["posts"]) - legacy_posts),
                "accounts_not_public": sorted(legacy_slugs - snap["accounts"]),
                "public_accounts_not_in_legacy": sorted(snap["accounts"] - legacy_slugs),
            }
            set_meta(con, "import_legacy", {k: v for k, v in stats.items() if k != "snapshot"} | {"at": now_iso()})
            con.commit()
        except BaseException:
            con.rollback()
            raise
    finally:
        src.close()
    return stats
