"""import-legacy-db against a small synthetic legacy (v1) database (no real data in the repository)."""
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from weide import db
from weide.characters import seed_characters
from weide.cli import main
from weide.export_static import export_static
from weide.importer import import_public
from weide.legacy_import import import_legacy_db
from weide.server import create_app

PRIVATE = ("PRIVATE-VOICE-TEXT", "PRIVATE-NEVER-TEXT", "personas/uwe.md", "PRIVATE-SCENE-TEXT", "PRIVATE-CACHE",
           "UNPUBLISHED-DRAFT-CAPTION", "draft comment text")


def make_legacy(path):
    con = sqlite3.connect(path)
    con.executescript(db.LEGACY_SCHEMA)
    inworld = {"ueber_mich": ["Ich bin Uwe."], "steckbrief": {"Alter": "52"}, "werte": [], "mein_tag": [],
               "leute": [{"slug": "henne", "text": "Meine Henne."}]}
    con.executemany("INSERT INTO accounts (id, slug, kind, handle, display_name, bio, followers, avatar, color, profile_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, "henne", "animal", "@henne", "Henne", "Gackert.", 1200, "personas/henne.png", "#fa0",
         json.dumps({"never": "PRIVATE-NEVER-TEXT", "inworld": {"ueber_mich": ["Gack."]}})),
        (2, "uwe", "human", "@uwe", "Uwe", "Bauer.", 80, None, "#0af",
         json.dumps({"voice": "PRIVATE-VOICE-TEXT", "persona_file": "personas/uwe.md", "inworld": inworld})),
        (3, "fan.1", "follower", "@fan.1", "Fan", None, 3, None, None, json.dumps({})),
    ])
    con.executemany("INSERT INTO posts (id, account_id, kind, caption, posted_at, ts, source, meta_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, 1, "text", "Erster Post", "2026-11-17T10:00:00+01:00", 1763370000, "sample:henne:1",
         json.dumps({"episode": "e009", "scene": "PRIVATE-SCENE-TEXT"}), "2026-11-16T09:00:00+01:00"),
        (2, 1, "text", "Zweiter Post", "2026-11-17T11:00:00+01:00", 1763373600, "sample:henne:2", None, None),
        (5, 2, "text", "UNPUBLISHED-DRAFT-CAPTION", "2026-11-17T12:00:00+01:00", 1763377200, "sample:uwe:5", None, None),
    ])
    con.executemany("INSERT INTO comments (id, post_id, account_id, reply_to, text, created_at, ts, source) "
                    "VALUES (?,?,?,?,?,?,?,?)", [
        (10, 2, 3, 11, "Antwort vor Frage", "2026-11-17T11:10:00+01:00", 1763374200, "simulate:2026-11-17"),
        (11, 2, 2, None, "Schöner Post", "2026-11-17T11:05:00+01:00", 1763373900, "seed"),
        (12, 5, 3, None, "draft comment text", "2026-11-17T12:05:00+01:00", 1763377500, "seed"),
    ])
    con.executemany("INSERT INTO reactions (post_id, account_id, type, count, source) VALUES (?,?,?,?,?)", [
        (1, None, "lol", 100, "seed"), (1, None, "lol", 5, "simulate:2026-11-17"), (1, None, "fire", 7, "seed"),
        (1, 2, "fire", 1, "uwe"), (1, 2, "heart", 1, "uwe"), (2, 3, "heart", 1, "seed"), (5, None, "lol", 9, "seed"),
    ])
    con.executemany("INSERT INTO follows (follower_id, followee_id, created_at) VALUES (?,?,?)",
                    [(3, 1, "2026-11-17"), (2, 1, "2026-11-17"), (1, 2, "2026-11-17")])
    con.execute("INSERT INTO meta (key, value) VALUES ('scene_de:v1:abc', ?)", (json.dumps("PRIVATE-CACHE"),))
    con.commit()
    con.close()


def make_snapshot(root, posts=None):
    posts = posts or {1: "sample:henne:1", 2: "sample:henne:2"}
    d = root / "data"
    (d / "posts").mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "accounts.json").write_text(json.dumps({"items": [{"slug": s} for s in ("henne", "uwe", "fan.1")]}))
    for pid, src in posts.items():
        (d / "posts" / f"{pid}.json").write_text(json.dumps({"id": pid, "source": src}))
    return root


@pytest.fixture
def legacy(tmp_path):
    p = tmp_path / "legacy" / "old.db"
    p.parent.mkdir()
    make_legacy(p)
    p.chmod(0o400)
    return p


@pytest.fixture
def snapshot(tmp_path):
    return make_snapshot(tmp_path / "public")


@pytest.fixture
def imported(legacy, snapshot, db_path):
    con = db.connect(db_path)
    stats = import_legacy_db(con, legacy, snapshot)
    yield con, stats
    con.close()


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("accounts", "posts", "comments", "reactions", "follows")}


def test_rich_data_and_status(imported):
    con, stats = imported
    assert stats["snapshot"]["draft_posts"] == [5] and stats["published"] == 2 and stats["draft"] == 1
    assert {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM posts")} == \
        {1: "published", 2: "published", 5: "draft"}
    assert counts(con) == {"accounts": 3, "posts": 3, "comments": 3, "reactions": 7, "follows": 3}
    uwe = con.execute("SELECT * FROM accounts WHERE slug='uwe'").fetchone()
    assert json.loads(uwe["profile_json"])["voice"] == "PRIVATE-VOICE-TEXT"        # stored, never served
    assert con.execute("SELECT avatar FROM accounts WHERE slug='henne'").fetchone()[0] == "henne.jpg"
    buckets = {(r["type"], r["source"]): r["count"] for r in con.execute(
        "SELECT * FROM reactions WHERE post_id=1 AND account_id IS NULL")}
    assert buckets == {("lol", "seed"): 100, ("lol", "simulate:2026-11-17"): 5, ("fire", "seed"): 7}
    named = {(r["post_id"], r["slug"], r["type"]) for r in con.execute(
        "SELECT r.post_id, a.slug, r.type FROM reactions r JOIN accounts a ON a.id=r.account_id")}
    assert named == {(1, "uwe", "fire"), (1, "uwe", "heart"), (2, "fan.1", "heart")}
    c = {r["id"]: (r["reply_to"], r["source"]) for r in con.execute("SELECT * FROM comments")}
    assert c[10] == (11, "simulate:2026-11-17") and c[11] == (None, "seed")
    assert json.loads(con.execute("SELECT meta_json FROM posts WHERE id=1").fetchone()[0])["scene"] == "PRIVATE-SCENE-TEXT"
    assert con.execute("SELECT created_at FROM posts WHERE id=1").fetchone()[0] == "2026-11-16T09:00:00+01:00"
    assert db.get_meta(con, "scene_de:v1:abc") == "PRIVATE-CACHE"


def test_legacy_file_is_never_touched(legacy, snapshot, db_path):
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    con = db.connect(db_path)
    import_legacy_db(con, legacy, snapshot)
    import_legacy_db(con, legacy, snapshot)
    con.close()
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in legacy.parent.iterdir()) == ["old.db"]          # no -wal/-shm side files


def test_idempotent(imported, legacy, snapshot):
    con, _ = imported
    queries = ("SELECT id, account_id, parent_id, status, caption, meta_json, created_at, source FROM posts ORDER BY id",
               "SELECT * FROM comments ORDER BY id", "SELECT * FROM follows ORDER BY 1, 2",
               "SELECT post_id, account_id, type, count, source FROM reactions ORDER BY 1, 2, 3, 5",
               "SELECT slug, profile_json, avatar FROM accounts ORDER BY id")
    dump = lambda: [[tuple(r) for r in con.execute(q)] for q in queries]
    before = dump()
    import_legacy_db(con, legacy, snapshot)
    assert dump() == before


def test_source_mismatch_is_an_error(legacy, tmp_path, db_path):
    snap = make_snapshot(tmp_path / "other", {1: "sample:other:1"})
    con = db.connect(db_path)
    with pytest.raises(ValueError, match="differs from the public snapshot"):
        import_legacy_db(con, legacy, snap)
    assert counts(con)["posts"] == 0
    con.close()


def test_server_hides_private_and_unpublished(imported, db_path):
    client = TestClient(create_app(db_path))
    acc = client.get("/api/accounts/uwe").json()
    assert acc["about"] == ["Ich bin Uwe."] and acc["facts"] == {"Alter": "52"} and acc["following"] == 1
    assert acc["people"][0]["slug"] == "henne"
    assert client.get("/api/accounts/henne").json()["following"] == 1
    assert client.get("/api/posts/5").status_code == 404
    assert [i["id"] for i in client.get("/api/feed").json()["items"]] == [2, 1]
    p1 = client.get("/api/posts/1").json()
    assert p1["reactions"] == {"lol": 105, "fire": 8, "chew": 0, "heart": 1} and p1["uwe_reacted"] is True
    assert p1["episode"] == "e009"
    blob = json.dumps([client.get(u).json() for u in ("/api/accounts", "/api/accounts/uwe", "/api/accounts/henne",
                                                      "/api/accounts/fan.1", "/api/feed", "/api/posts/1", "/api/posts/2")])
    for s in PRIVATE:
        assert s not in blob, s


def test_export_and_reimport_public(imported, db_path, tmp_path):
    con, _ = imported
    out = tmp_path / "site"
    res = export_static(out, db_path, today="2026-11-17")
    assert res["leaks"] == [] and res["posts"] == 2
    blob = "\n".join(f.read_text() for f in (out / "data").rglob("*.json"))
    for s in PRIVATE:
        assert s not in blob, s
    # import-public of that export on top changes nothing that matters (no placeholder reactions)
    before, rx = counts(con), db.reaction_totals(con, [1, 2, 5])
    import_public(con, out)
    assert counts(con) == before and db.reaction_totals(con, [1, 2, 5]) == rx
    assert not con.execute("SELECT 1 FROM reactions WHERE source='import'").fetchone()
    assert json.loads(con.execute("SELECT meta_json FROM posts WHERE id=1").fetchone()[0]) == \
        {"episode": "e009", "scene": "PRIVATE-SCENE-TEXT"}
    assert json.loads(con.execute("SELECT profile_json FROM accounts WHERE slug='uwe'").fetchone()[0])["voice"] == \
        "PRIVATE-VOICE-TEXT"
    assert con.execute("SELECT status FROM posts WHERE id=5").fetchone()[0] == "draft"


def test_after_import_public_and_with_seed_characters(legacy, snapshot, tmp_path, imported, db_path):
    # import-public first (placeholders), then the legacy import replaces them with the real rows
    con, _ = imported
    out = tmp_path / "site"
    export_static(out, db_path, today="2026-11-17")
    fresh = db.connect(tmp_path / "fresh.db")
    seed_characters(fresh)
    import_public(fresh, out)
    assert fresh.execute("SELECT COUNT(*) FROM reactions WHERE source='import'").fetchone()[0] > 0
    import_legacy_db(fresh, legacy, snapshot)
    assert not fresh.execute("SELECT 1 FROM reactions WHERE source='import'").fetchone()
    assert db.reaction_totals(fresh, [1, 2, 5]) == db.reaction_totals(con, [1, 2, 5])
    assert counts(fresh)["follows"] == 3 and counts(fresh)["accounts"] == 4
    seed_characters(fresh)
    assert fresh.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1
    fresh.close()


def test_cli(legacy, snapshot, db_path, capsys):
    main(["import-legacy-db", str(legacy), "--public", str(snapshot), "--db", str(db_path)])
    out = json.loads(capsys.readouterr().out)
    assert out["posts"] == 3 and out["follows"] == 3 and out["snapshot"]["draft_posts"] == [5]
    with pytest.raises(SystemExit):
        main(["import-legacy-db", str(legacy), "--public", str(snapshot), "--db", str(legacy)])
