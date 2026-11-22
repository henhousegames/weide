"""Uwe's butcher shop is fictional: 'Metzgerei Schneider, Zweibrücken' (real town, common real business
name) became 'Metzgerei Wammerich, Schwartenbrück' via migration 6, in the seeds and in the live data/."""
import json
import re
import sqlite3

from weide import db
from weide.export_static import export_static
from weide.importer import import_public

from .conftest import REPO

OLD = re.compile(r"schneider|zweibr", re.I)
OLD_PROFILE = {"home": "Metzgerei Schneider in Zweibrücken (Pfalz)",
               "life": "Uwe Schneider, 56; his father opened Metzgerei Schneider in Zweibrücken in 1971.",
               "inworld": {"ueber_mich": ["Metzgerei Schneider, Zweibrücken. Seit 1971."]}}


def db_at_v5(path):
    """A database at schema 5 holding uwe and jens as they were before migration 6."""
    con = sqlite3.connect(path, isolation_level=None)
    for _version, _name, fn in db.MIGRATIONS[:5]:
        con.execute("BEGIN")
        fn(con)
        con.execute("COMMIT")
    con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '5') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    con.execute("INSERT INTO accounts (id, slug, kind, handle, display_name, bio, profile_json, listed) VALUES "
                "(9, 'uwe', 'human', '@metzgerei_schneider_official', 'Metzgerei Schneider | Uwe', "
                "'Metzgerei Schneider, Zweibrücken. Fleisch- und Wurstwaren seit 1971.', ?, 1)",
                (json.dumps(OLD_PROFILE, ensure_ascii=False),))
    con.execute("INSERT INTO accounts (id, slug, kind, handle, display_name, bio, profile_json, listed) VALUES "
                "(3, 'jens', 'animal', '@eber.bulk', 'Jens', "
                "'Coach: @metzgerei_schneider_official 🙏 | Zweibrücken', ?, 1)",
                (json.dumps({"home": "Zweibrücken"}),))          # ASCII-escaped JSON (ü as \\u00fc)
    con.execute("INSERT INTO posts (id, account_id, kind, caption, posted_at, ts, status) "
                "VALUES (26, 3, 'image', 'Bald WAS, @metzgerei_schneider_official??', '2026-10-29', 1, 'published')")
    con.execute("INSERT INTO comments (id, post_id, account_id, text, created_at, ts) "
                "VALUES (7, 26, 9, 'Gruß aus der Metzgerei Schneider', 'x', 1)")
    con.close()


def dump(con):
    rows = []
    for t in ("accounts", "posts", "comments"):
        rows += [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY id")]
    return rows


def test_migration_renames_shop_and_is_idempotent(db_path):
    db_at_v5(db_path)
    con = sqlite3.connect(db_path, isolation_level=None)
    assert db.migrate(con) == [6]
    con.row_factory = sqlite3.Row
    uwe = con.execute("SELECT * FROM accounts WHERE id=9").fetchone()
    assert uwe["slug"] == "uwe" and uwe["kind"] == "human"
    assert uwe["handle"] == "@metzgerei_wammerich_official"
    assert uwe["display_name"] == "Metzgerei Wammerich | Uwe"
    assert uwe["bio"] == "Metzgerei Wammerich, Schwartenbrück. Fleisch- und Wurstwaren seit 1971."
    profile = json.loads(uwe["profile_json"])
    assert profile["home"] == "Metzgerei Wammerich in Schwartenbrück (Pfalz)"
    assert profile["life"].startswith("Uwe Wammerich, 56")
    assert profile["inworld"]["ueber_mich"] == ["Metzgerei Wammerich, Schwartenbrück. Seit 1971."]
    jens = con.execute("SELECT * FROM accounts WHERE id=3").fetchone()
    assert jens["bio"] == "Coach: @metzgerei_wammerich_official 🙏 | Schwartenbrück"
    assert json.loads(jens["profile_json"]) == {"home": "Schwartenbrück"}
    assert con.execute("SELECT caption FROM posts WHERE id=26").fetchone()[0] == \
        "Bald WAS, @metzgerei_wammerich_official??"
    assert con.execute("SELECT text FROM comments WHERE id=7").fetchone()[0] == "Gruß aus der Metzgerei Wammerich"
    assert not any(OLD.search(str(v)) for row in dump(con) for v in row)

    before = dump(con)
    db._m006_uwe_fictional_shop(con)                   # running the data step again changes nothing
    assert dump(con) == before
    assert db.migrate(con) == []
    con.close()


def test_rename_function():
    assert db.rename_uwe_shop(None) is None
    once = db.rename_uwe_shop("@metzgerei_schneider_official, Metzgerei Schneider, Zweibrücken")
    assert once == "@metzgerei_wammerich_official, Metzgerei Wammerich, Schwartenbrück"
    assert db.rename_uwe_shop(once) == once


def test_seeds_and_live_data_have_no_old_name():
    files = list((REPO / "data").rglob("*.json")) + list((REPO / "app/weide/seed").glob("*.json"))
    assert files
    hits = [str(f.relative_to(REPO)) for f in files if OLD.search(f.read_text(encoding="utf-8"))]
    assert hits == []
    uwe = json.loads((REPO / "data/accounts/uwe.json").read_text(encoding="utf-8"))
    assert uwe["slug"] == "uwe" and uwe["handle"] == "@metzgerei_wammerich_official"


def test_export_has_no_old_name(tmp_path):
    dbp = tmp_path / "weide.db"
    con = db.connect(dbp)
    import_public(con, REPO)
    con.close()
    out = tmp_path / "site"
    export_static(out, dbp, today="2026-11-17", link=True)
    uwe = json.loads((out / "data/accounts/uwe.json").read_text(encoding="utf-8"))
    assert uwe["slug"] == "uwe" and uwe["handle"] == "@metzgerei_wammerich_official"
    hits = [str(f.relative_to(out)) for f in (out / "data").rglob("*.json")
            if OLD.search(f.read_text(encoding="utf-8"))]
    assert hits == []
    assert not OLD.search((out / "index.html").read_text(encoding="utf-8"))
