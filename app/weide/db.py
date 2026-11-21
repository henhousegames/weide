"""SQLite storage: connection, ordered schema migrations and small upsert helpers.

The schema version is stored in meta('schema_version'). `migrate()` applies every migration whose
number is above the stored version, each in its own transaction. A database created by the legacy
backend (CREATE IF NOT EXISTS, no schema_version) is treated as version 1.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config

# Schema of the legacy backend (version 1). Kept verbatim so old databases and fresh ones start equal.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id            INTEGER PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('animal','human','follower','house')),
  handle        TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  bio           TEXT,
  followers     INTEGER NOT NULL DEFAULT 0,
  followers_note TEXT,
  avatar        TEXT,
  color         TEXT,
  profile_json  TEXT,
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id          INTEGER PRIMARY KEY,
  account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  parent_id   INTEGER REFERENCES posts(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('story','post','reel','image','text','collection')),
  title       TEXT,
  caption     TEXT,
  hashtags    TEXT,
  media       TEXT,
  poster      TEXT,
  posted_at   TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  source      TEXT UNIQUE,
  clip_id     TEXT,
  funny_score REAL,
  why         TEXT,
  meta_json   TEXT,
  created_at  TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS posts_ts ON posts(ts);
CREATE INDEX IF NOT EXISTS posts_parent ON posts(parent_id);
CREATE TABLE IF NOT EXISTS comments (
  id          INTEGER PRIMARY KEY,
  post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  reply_to    INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  text        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  source      TEXT NOT NULL DEFAULT 'cli'
);
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id);
CREATE TABLE IF NOT EXISTS reactions (
  id          INTEGER PRIMARY KEY,
  post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
  type        TEXT NOT NULL,
  count       INTEGER NOT NULL DEFAULT 1,
  source      TEXT NOT NULL DEFAULT 'cli',
  created_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS reactions_named  ON reactions(post_id, account_id, type) WHERE account_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS reactions_bucket ON reactions(post_id, type, source) WHERE account_id IS NULL;
CREATE TABLE IF NOT EXISTS follows (
  follower_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  followee_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  created_at  TEXT,
  PRIMARY KEY (follower_id, followee_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# --- migrations ------------------------------------------------------------------------------

def _script(con: sqlite3.Connection, sql: str) -> None:
    """Run ';'-separated DDL inside the current transaction (executescript would COMMIT first)."""
    for stmt in sql.split(";"):
        if stmt.strip():
            con.execute(stmt)


def _m001_legacy(con: sqlite3.Connection) -> None:
    """Baseline: the legacy schema (no-op on a legacy database)."""
    _script(con, LEGACY_SCHEMA)


def _m002_accounts_kind_world(con: sqlite3.Connection) -> None:
    """accounts: drop the kind CHECK (kinds are validated in code, robot/other allowed), add `world`.
    SQLite cannot drop a constraint, so the table is rebuilt (ids kept; foreign keys are off here)."""
    con.execute("""
CREATE TABLE accounts_new (
  id            INTEGER PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,
  kind          TEXT NOT NULL,
  handle        TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  bio           TEXT,
  followers     INTEGER NOT NULL DEFAULT 0,
  followers_note TEXT,
  avatar        TEXT,
  color         TEXT,
  profile_json  TEXT,
  updated_at    TEXT,
  world         TEXT
)""")
    con.execute("""INSERT INTO accounts_new (id, slug, kind, handle, display_name, bio, followers, followers_note,
                   avatar, color, profile_json, updated_at)
                   SELECT id, slug, kind, handle, display_name, bio, followers, followers_note,
                   avatar, color, profile_json, updated_at FROM accounts""")
    con.execute("DROP TABLE accounts")
    con.execute("ALTER TABLE accounts_new RENAME TO accounts")


def _m003_post_status(con: sqlite3.Connection) -> None:
    """posts: status (draft|approved|published), opaque public_id, generation_json.
    Rows that already exist were all public under the legacy backend, so they become published."""
    con.execute("ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'draft' "
                "CHECK (status IN ('draft','approved','published'))")
    con.execute("ALTER TABLE posts ADD COLUMN public_id TEXT")
    con.execute("ALTER TABLE posts ADD COLUMN generation_json TEXT")
    con.execute("UPDATE posts SET status='published'")
    con.execute("CREATE UNIQUE INDEX posts_public_id ON posts(public_id) WHERE public_id IS NOT NULL")
    con.execute("CREATE INDEX posts_status ON posts(status)")


def _m004_characters_controllers(con: sqlite3.Connection) -> None:
    """characters (1:1 with a posting account; style_guidance is PRIVATE and never exported),
    controllers (agents/humans acting through scoped keys) and who may act as whom."""
    _script(con, """
CREATE TABLE characters (
  id               INTEGER PRIMARY KEY,
  account_id       INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,
  identity         TEXT,
  style_guidance   TEXT,
  reference_assets TEXT,
  voice            TEXT,
  created_at       TEXT,
  updated_at       TEXT
);
CREATE TABLE controllers (
  id          INTEGER PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  type        TEXT NOT NULL,
  key_hash    TEXT,
  permissions TEXT,
  created_at  TEXT
);
CREATE TABLE character_controllers (
  character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  controller_id INTEGER NOT NULL REFERENCES controllers(id) ON DELETE CASCADE,
  created_at    TEXT,
  PRIMARY KEY (character_id, controller_id)
);
""")


def _m005_accounts_listed(con: sqlite3.Connection) -> None:
    """accounts.listed: the account belongs to the public snapshot (set by import-public / import-legacy-db).
    An account is public when it is listed OR has a published post (server.VISIBLE). Existing rows keep
    their visibility: accounts with a published post and every account without a characters row (in a
    database before this migration those came from the importers) are listed; seeded characters without
    a published post are not, until their first post is published."""
    con.execute("ALTER TABLE accounts ADD COLUMN listed INTEGER NOT NULL DEFAULT 0")
    con.execute("UPDATE accounts SET listed = 1 WHERE "
                "EXISTS (SELECT 1 FROM posts p WHERE p.account_id = accounts.id AND p.status = 'published') "
                "OR NOT EXISTS (SELECT 1 FROM characters c WHERE c.account_id = accounts.id)")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "legacy schema", _m001_legacy),
    (2, "accounts: kind validated in code, world column", _m002_accounts_kind_world),
    (3, "posts: status, public_id, generation_json", _m003_post_status),
    (4, "characters, controllers, character_controllers", _m004_characters_controllers),
    (5, "accounts: listed (public snapshot membership)", _m005_accounts_listed),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def schema_version(con: sqlite3.Connection) -> int:
    """0 = empty database, 1 = legacy database without a version, else meta('schema_version')."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in tables:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row:
            return int(json.loads(row[0]))
    return 1 if "accounts" in tables else 0


def migrate(con: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order; returns the applied version numbers."""
    current = schema_version(con)
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema version {current} is newer than this code ({SCHEMA_VERSION})")
    applied = []
    con.commit()
    saved_isolation = con.isolation_level
    con.isolation_level = None                       # explicit BEGIN/COMMIT per migration
    con.execute("PRAGMA foreign_keys = OFF")         # table rebuilds; checked after each step
    try:
        for version, _name, fn in MIGRATIONS:
            if version <= current:
                continue
            con.execute("BEGIN")
            try:
                fn(con)
                bad = con.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise RuntimeError(f"migration {version} broke foreign keys: {[tuple(b) for b in bad][:5]}")
                con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(version),))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            applied.append(version)
    finally:
        con.execute("PRAGMA foreign_keys = ON")
        con.isolation_level = saved_isolation
    return applied


def connect(db_path: str | Path | None = None, init: bool = True) -> sqlite3.Connection:
    """Open the database; with init=True, migrate it to the current schema first."""
    path = Path(db_path or config.DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    if init:
        mc = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        try:
            mc.execute("PRAGMA journal_mode = WAL")
            migrate(mc)
        finally:
            mc.close()
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def connect_readonly(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open an existing database strictly read-only (SQLite URI `mode=ro`): no migration, no writes,
    never creates the file. Writes fail with sqlite3.OperationalError."""
    path = Path(db_path or config.DEFAULT_DB).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path.name}")
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


# --- helpers -----------------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime:
    """Accept ISO strings (with or without offset) or datetimes; naive values are Europe/Berlin."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise ValueError(f"not an ISO date/time: {value!r} (e.g. 2026-11-17T19:30:00+01:00)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt


def jdump(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False)


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, jdump(value)))


def get_meta(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return jload(row[0], default) if row else default


# --- accounts ----------------------------------------------------------------------------------

def check_account_kind(kind: str) -> str:
    if kind not in config.ACCOUNT_KINDS:
        raise ValueError(f"account kind must be one of {', '.join(config.ACCOUNT_KINDS)}, got {kind!r}")
    return kind


def world_of(kind: str | None, world: str | None) -> str | None:
    return world or config.WORLD_BY_KIND.get(kind or "")


ACCOUNT_FIELDS = ("kind", "handle", "display_name", "bio", "followers", "followers_note", "avatar", "color",
                  "profile_json", "world", "listed")


def upsert_account(con: sqlite3.Connection, slug: str, **fields: Any) -> int:
    """Insert or update an account by slug (only the given fields change). Returns the id."""
    if "profile" in fields:
        fields["profile_json"] = jdump(fields.pop("profile"))
    unknown = set(fields) - set(ACCOUNT_FIELDS)
    if unknown:
        raise ValueError(f"unknown account fields: {sorted(unknown)}")
    if "kind" in fields:
        check_account_kind(fields["kind"])
    fields["updated_at"] = now_iso()
    row = con.execute("SELECT id FROM accounts WHERE slug=?", (slug,)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), row["id"]))
        return row["id"]
    fields.setdefault("kind", "follower")
    fields.setdefault("handle", "@" + slug)
    fields.setdefault("display_name", slug)
    cols = ", ".join(["slug", *fields])
    qs = ", ".join("?" * (len(fields) + 1))
    return con.execute(f"INSERT INTO accounts ({cols}) VALUES ({qs})", (slug, *fields.values())).lastrowid


def account_id(con: sqlite3.Connection, slug_or_handle: str) -> int | None:
    key = slug_or_handle.strip()
    row = con.execute("SELECT id FROM accounts WHERE slug=? OR handle=? OR handle=?",
                      (key.lstrip("@"), key, "@" + key.lstrip("@"))).fetchone()
    return row["id"] if row else None


# --- posts -------------------------------------------------------------------------------------

POST_FIELDS = ("id", "account_id", "parent_id", "kind", "title", "caption", "hashtags", "media", "poster",
               "posted_at", "source", "clip_id", "funny_score", "why", "meta_json", "status", "public_id",
               "generation_json")


def upsert_post(con: sqlite3.Connection, **fields: Any) -> int:
    """Insert or update a post, matched by `source` (when given) or explicit `id`. Returns the id.
    An explicit id that belongs to a different source is an error (ids are never reassigned)."""
    if "meta" in fields:
        fields["meta_json"] = jdump(fields.pop("meta"))
    if "generation" in fields:
        fields["generation_json"] = jdump(fields.pop("generation"))
    unknown = set(fields) - set(POST_FIELDS)
    if unknown:
        raise ValueError(f"unknown post fields: {sorted(unknown)}")
    if fields.get("kind") not in config.POST_KINDS:
        raise ValueError(f"kind must be one of {config.POST_KINDS}, got {fields.get('kind')!r}")
    if "status" in fields and fields["status"] not in config.POST_STATUSES:
        raise ValueError(f"status must be one of {config.POST_STATUSES}, got {fields['status']!r}")
    dt = parse_time(fields["posted_at"])
    fields["posted_at"] = dt.isoformat(timespec="seconds")
    fields["ts"] = int(dt.timestamp())
    fields["updated_at"] = now_iso()
    src, pid = fields.get("source"), fields.get("id")
    row = con.execute("SELECT id, source FROM posts WHERE source=?", (src,)).fetchone() if src else None
    if row is None and pid is not None:
        row = con.execute("SELECT id, source FROM posts WHERE id=?", (pid,)).fetchone()
    if row:
        if pid is not None and row["id"] != pid:
            raise ValueError(f"post source {src!r} already has id {row['id']}, not {pid}")
        if src and row["source"] and row["source"] != src:
            raise ValueError(f"post id {row['id']} belongs to source {row['source']!r}, not {src!r}")
        fields.pop("id", None)
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), row["id"]))
        return row["id"]
    fields["created_at"] = fields["updated_at"]
    cols = ", ".join(fields)
    qs = ", ".join("?" * len(fields))
    return con.execute(f"INSERT INTO posts ({cols}) VALUES ({qs})", tuple(fields.values())).lastrowid


def resolve_post(con: sqlite3.Connection, ref: str | int, *, published_only: bool = False) -> int:
    """A post reference is its numeric id or its source string."""
    s = str(ref).strip()
    col = "id" if s.isdigit() else "source"
    q = f"SELECT id FROM posts WHERE {col}=?" + (" AND status='published'" if published_only else "")
    row = con.execute(q, (int(s) if col == "id" else s,)).fetchone()
    if not row:
        raise LookupError(f"no post {ref!r}")
    return row["id"]


# --- reactions -----------------------------------------------------------------------------------

MAX_COUNT = 1_000_000_000      # anonymous reaction buckets: 0 .. 1e9


def check_count(n: Any, what: str = "count") -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a whole number, got {n!r}") from None
    if isinstance(n, float) and n != v:
        raise ValueError(f"{what} must be a whole number, got {n!r}")
    if not 0 <= v <= MAX_COUNT:
        raise ValueError(f"{what} must be between 0 and {MAX_COUNT:,}, got {n!r}")
    return v


def check_reaction_type(rtype: str) -> str:
    if rtype not in config.REACTIONS:
        raise ValueError(f"reaction type must be one of {', '.join(config.REACTION_TYPES)}, got {rtype!r}")
    return rtype


def add_named_reaction(con: sqlite3.Connection, post_id: int, aid: int, rtype: str, source: str = "cli") -> None:
    check_reaction_type(rtype)
    con.execute("INSERT OR IGNORE INTO reactions (post_id, account_id, type, count, source, created_at) "
                "VALUES (?,?,?,?,?,?)", (post_id, aid, rtype, 1, source, now_iso()))


def set_bucket(con: sqlite3.Connection, post_id: int, rtype: str, count: int, source: str) -> None:
    """Anonymous reaction counts are buckets per (post, type, source); setting replaces the count,
    a count of 0 removes the bucket."""
    check_reaction_type(rtype)
    count = check_count(count)
    row = con.execute("SELECT id, count FROM reactions WHERE post_id=? AND type=? AND source=? AND account_id IS NULL",
                      (post_id, rtype, source)).fetchone()
    if not count:
        if row:
            con.execute("DELETE FROM reactions WHERE id=?", (row["id"],))
    elif row:
        if row["count"] != count:
            con.execute("UPDATE reactions SET count=? WHERE id=?", (count, row["id"]))
    else:
        con.execute("INSERT INTO reactions (post_id, account_id, type, count, source, created_at) VALUES (?,?,?,?,?,?)",
                    (post_id, None, rtype, count, source, now_iso()))


def bump_bucket(con: sqlite3.Connection, post_id: int, rtype: str, delta: int, source: str) -> int:
    check_reaction_type(rtype)
    row = con.execute("SELECT count FROM reactions WHERE post_id=? AND type=? AND source=? AND account_id IS NULL",
                      (post_id, rtype, source)).fetchone()
    new = min(MAX_COUNT, max(0, (row["count"] if row else 0) + int(delta)))
    set_bucket(con, post_id, rtype, new, source)
    return new


def reaction_totals(con: sqlite3.Connection, post_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    ids = list(post_ids)
    out: dict[int, dict[str, int]] = {pid: {t: 0 for t in config.REACTION_TYPES} for pid in ids}
    if not ids:
        return out
    qs = ",".join("?" * len(ids))
    for r in con.execute(f"SELECT post_id, type, SUM(count) n FROM reactions WHERE post_id IN ({qs}) "
                         "GROUP BY post_id, type", ids):
        if r["type"] in out[r["post_id"]]:
            out[r["post_id"]][r["type"]] = int(r["n"])
    return out


def comment_counts(con: sqlite3.Connection, post_ids: Iterable[int], exclude_author: bool = False) -> dict[int, int]:
    ids = list(post_ids)
    out = {pid: 0 for pid in ids}
    if not ids:
        return out
    qs = ",".join("?" * len(ids))
    cond = "AND c.account_id != p.account_id" if exclude_author else ""
    for r in con.execute(
        f"SELECT c.post_id, COUNT(*) n FROM comments c JOIN posts p ON p.id=c.post_id "
        f"WHERE c.post_id IN ({qs}) {cond} GROUP BY c.post_id", ids):
        out[r["post_id"]] = int(r["n"])
    return out


FOLLOWER_NAME = re.compile(r"^[a-z0-9][a-z0-9._]{1,29}$")
