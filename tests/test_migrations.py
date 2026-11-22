import sqlite3

import pytest

from weide import db
from weide.cli import main


def cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_reaches_current_version(db_path):
    con = db.connect(db_path)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert {"accounts", "posts", "comments", "reactions", "follows", "meta", "characters", "controllers",
            "character_controllers"} <= tables(con)
    assert {"status", "public_id", "generation_json", "source"} <= cols(con, "posts")
    assert "world" in cols(con, "accounts")
    assert {"account_id", "kind", "identity", "style_guidance", "reference_assets", "voice"} <= cols(con, "characters")
    assert {"id", "name", "type", "key_hash", "permissions"} <= cols(con, "controllers")
    con.close()


def test_migrate_is_idempotent(db_path):
    db.connect(db_path).close()
    con = sqlite3.connect(db_path, isolation_level=None)
    assert db.migrate(con) == []
    con.close()


def legacy_db(path):
    con = sqlite3.connect(path)
    con.executescript(db.LEGACY_SCHEMA)
    con.execute("INSERT INTO accounts (id, slug, kind, handle, display_name) VALUES (5,'lisa','animal','@lisa','Lisa')")
    con.execute("INSERT INTO accounts (id, slug, kind, handle, display_name) VALUES (9,'sandra_85','follower','@s','S')")
    con.execute("INSERT INTO posts (id, account_id, kind, caption, posted_at, ts, source) "
                "VALUES (3, 5, 'reel', 'hi', '2026-11-17T10:00:00+01:00', 1, 'episode:x:1')")
    con.execute("INSERT INTO comments (id, post_id, account_id, text, created_at, ts) VALUES (7, 3, 9, 'lol', 'x', 1)")
    con.execute("INSERT INTO reactions (post_id, account_id, type, count, source) VALUES (3, NULL, 'lol', 12, 'seed')")
    con.execute("INSERT INTO follows (follower_id, followee_id) VALUES (9, 5)")
    con.commit()
    # the legacy CHECK rejects new kinds
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO accounts (slug, kind, handle, display_name) VALUES ('bot','robot','@bot','Bot')")
    con.close()


def test_migration_from_legacy_schema(db_path):
    legacy_db(db_path)
    con = sqlite3.connect(db_path, isolation_level=None)
    assert db.schema_version(con) == 1
    assert db.migrate(con) == [2, 3, 4, 5, 6]
    con.close()
    con = db.connect(db_path)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    a = con.execute("SELECT * FROM accounts WHERE slug='lisa'").fetchone()
    assert a["id"] == 5 and a["kind"] == "animal" and a["world"] is None
    assert [r[0] for r in con.execute("SELECT listed FROM accounts ORDER BY id")] == [1, 1]   # stay public
    p = con.execute("SELECT * FROM posts WHERE id=3").fetchone()
    assert p["status"] == "published" and p["public_id"] is None and p["generation_json"] is None
    assert p["source"] == "episode:x:1"
    assert con.execute("SELECT account_id FROM comments WHERE id=7").fetchone()[0] == 9
    assert con.execute("SELECT count FROM reactions WHERE post_id=3").fetchone()[0] == 12
    assert con.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    # foreign keys still point at the rebuilt accounts table
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO comments (post_id, account_id, text, created_at, ts) VALUES (3, 999, 'x', 'x', 1)")
    con.execute("DELETE FROM accounts WHERE id=9")       # cascades
    assert con.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0
    # robot is accepted by the schema now; kinds are validated in code
    db.upsert_account(con, "maehbot_test", kind="robot")
    with pytest.raises(ValueError):
        db.upsert_account(con, "x", kind="alien")
    con.close()


def test_new_posts_default_to_draft_and_status_is_checked(db_path):
    con = db.connect(db_path)
    aid = db.upsert_account(con, "lisa", kind="animal")
    pid = db.upsert_post(con, account_id=aid, kind="text", caption="x", posted_at="2026-11-17T10:00:00")
    assert con.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0] == "draft"
    with pytest.raises(ValueError):
        db.upsert_post(con, account_id=aid, kind="text", posted_at="2026-11-17", status="live")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE posts SET status='live' WHERE id=?", (pid,))
    con.close()


def test_upsert_post_keeps_ids_by_source(db_path):
    con = db.connect(db_path)
    aid = db.upsert_account(con, "lisa", kind="animal")
    a = db.upsert_post(con, id=40, account_id=aid, kind="text", caption="a", posted_at="2026-11-17", source="s:1")
    b = db.upsert_post(con, id=40, account_id=aid, kind="text", caption="b", posted_at="2026-11-17", source="s:1")
    assert a == b == 40
    with pytest.raises(ValueError):
        db.upsert_post(con, id=41, account_id=aid, kind="text", posted_at="2026-11-17", source="s:1")
    con.close()


def test_newer_schema_is_refused(db_path):
    con = db.connect(db_path)
    db.set_meta(con, "schema_version", 999)
    con.commit()
    con.close()
    with pytest.raises(RuntimeError):
        db.connect(db_path)


def test_cli_migrate(db_path, capsys):
    legacy_db(db_path)
    main(["migrate", "--db", str(db_path)])
    assert '"applied": [2, 3, 4, 5, 6]' in capsys.readouterr().out


def test_m005_lists_accounts_with_posts_or_without_character(db_path):
    """Migration 5 on a version-4 database: imported accounts (no characters row) and characters with a
    published post stay public; a seeded character without a published post becomes unlisted."""
    con = sqlite3.connect(db_path, isolation_level=None)
    con.execute("BEGIN")
    for v, _n, fn in db.MIGRATIONS[:4]:
        fn(con)
    con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
    con.execute("COMMIT")
    for i, (slug, kind) in enumerate([("hannes", "human"), ("maehbot", "robot"), ("gustav", "robot")], 1):
        con.execute("INSERT INTO accounts (id, slug, kind, handle, display_name) VALUES (?,?,?,?,?)",
                    (i, slug, kind, "@" + slug, slug))
    con.execute("INSERT INTO characters (account_id, kind) VALUES (2, 'robot'), (3, 'robot')")
    con.execute("INSERT INTO posts (account_id, kind, posted_at, ts, status) VALUES "
                "(3, 'reel', '2026-11-17T10:00:00+01:00', 1, 'published'), "
                "(2, 'reel', '2026-11-17T10:00:00+01:00', 1, 'draft')")
    assert db.migrate(con) == [5, 6]
    assert dict(con.execute("SELECT slug, listed FROM accounts").fetchall()) == {"hannes": 1, "maehbot": 0, "gustav": 1}
    con.close()
