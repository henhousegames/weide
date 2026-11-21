import json
import shutil

import pytest

from weide import db
from weide.importer import import_public

from .conftest import REPO

DATA = REPO / "data"


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("accounts", "posts", "comments", "reactions", "follows")}


@pytest.fixture
def imported(db_path):
    con = db.connect(db_path)
    import_public(con, REPO)
    yield con
    con.close()


def test_import_keeps_ids_and_slugs(imported):
    con = imported
    ids = sorted(int(p.stem) for p in (DATA / "posts").glob("*.json"))
    assert [r[0] for r in con.execute("SELECT id FROM posts ORDER BY id")] == ids
    slugs = [a["slug"] for a in json.loads((DATA / "accounts.json").read_text())["items"]]
    assert sorted(r[0] for r in con.execute("SELECT slug FROM accounts")) == sorted(slugs)
    for f in (DATA / "posts").glob("*.json"):
        card = json.loads(f.read_text())
        row = con.execute("SELECT * FROM posts WHERE id=?", (card["id"],)).fetchone()
        assert row["source"] == card["source"]
        assert row["status"] == "published"
        assert row["generation_json"] is None and row["public_id"] is None
        tot = db.reaction_totals(con, [card["id"]])[card["id"]]
        assert tot == card["reactions"]


def test_import_is_idempotent(imported, db_path):
    con = imported
    before = counts(con)
    snapshot = [tuple(r) for r in con.execute("SELECT id, account_id, source, caption FROM posts ORDER BY id")]
    comments = [tuple(r) for r in con.execute("SELECT id, post_id, account_id, reply_to, text FROM comments ORDER BY id")]
    import_public(con, REPO)
    import_public(con, REPO / "data")
    assert counts(con) == before
    assert [tuple(r) for r in con.execute("SELECT id, account_id, source, caption FROM posts ORDER BY id")] == snapshot
    assert [tuple(r) for r in con.execute(
        "SELECT id, post_id, account_id, reply_to, text FROM comments ORDER BY id")] == comments


def test_reimport_with_local_reactions_keeps_public_totals(imported):
    con = imported
    db.bump_bucket(con, 1, "lol", 5, "web")
    import_public(con, REPO)
    card = json.loads((DATA / "posts" / "1.json").read_text())
    assert db.reaction_totals(con, [1])[1] == card["reactions"]


def test_hannes_stays_single_human_account(imported):
    con = imported
    import_public(con, REPO)
    rows = con.execute("SELECT slug, handle FROM accounts WHERE slug='hannes' OR handle='@hannes.vumm.hof'").fetchall()
    assert [tuple(r) for r in rows] == [("hannes", "@hannes.vumm.hof")]
    humans = sorted(r[0] for r in con.execute("SELECT slug FROM accounts WHERE kind='human'"))
    assert humans == ["hannes", "uwe"]
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE kind='robot'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 0


def test_import_rejects_handle_clash(db_path):
    con = db.connect(db_path)
    db.upsert_account(con, "hannes2", kind="human", handle="@hannes.vumm.hof")
    with pytest.raises(ValueError):
        import_public(con, REPO)
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0      # rolled back
    con.close()


def test_import_rejects_unknown_kind(db_path, tmp_path):
    src = tmp_path / "site"
    shutil.copytree(DATA, src / "data")
    f = src / "data" / "accounts.json"
    d = json.loads(f.read_text())
    d["items"][0]["kind"] = "alien"
    f.write_text(json.dumps(d))
    con = db.connect(db_path)
    with pytest.raises(ValueError):
        import_public(con, src)
    con.close()


def test_missing_export(db_path, tmp_path):
    con = db.connect(db_path)
    with pytest.raises(FileNotFoundError):
        import_public(con, tmp_path)
    con.close()


def test_cli_import_public(db_path, capsys):
    from weide.cli import main
    main(["import-public", str(REPO), "--db", str(db_path)])
    out = json.loads(capsys.readouterr().out)
    assert out["posts"] == len(list((DATA / "posts").glob("*.json")))
    assert out["accounts"] == 31
