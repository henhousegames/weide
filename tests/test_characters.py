import json

from fastapi.testclient import TestClient

from weide import db
from weide.characters import SEED_DIR, load_seeds
from weide.cli import main
from weide.export_static import _scan
from weide.importer import import_public
from weide.server import create_app

from .conftest import REPO

PROFILE_KEYS = set(json.loads((REPO / "data/accounts/hannes.json").read_text())) | {"robot"}   # robot: added after that export


def test_seed_files_are_public_and_complete():
    seeds = {s["slug"]: s for s in load_seeds()}
    m = seeds["maehbot"]
    assert m["handle"] == "@maehbot" and m["display_name"] == "Mähbot 7"
    assert m["kind"] == "robot" and m["world"] == "roboter"
    assert m["bio"] and m["about"] and m["facts"] and m["values"] and m["day"]
    assert any(p["slug"] == "hannes" for p in m["people"])
    assert m["character"]["style_guidance"] == ""
    for f in SEED_DIR.glob("*.json"):
        hits: list[str] = []
        _scan({k: v for k, v in json.loads(f.read_text()).items() if k != "character"}, f.name, hits)
        assert hits == []


def test_seed_characters_idempotent(db_path, capsys):
    con = db.connect(db_path)
    import_public(con, REPO)
    con.close()
    main(["seed-characters", "--db", str(db_path)])
    main(["seed-characters", "--db", str(db_path)])
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"characters": ["maehbot"]}
    con = db.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE slug='maehbot'").fetchone()[0] == 1
    ch = con.execute("SELECT c.kind, c.style_guidance FROM characters c JOIN accounts a ON a.id=c.account_id "
                     "WHERE a.slug='maehbot'").fetchall()
    assert [tuple(r) for r in ch] == [("robot", "")]
    assert con.execute("SELECT COUNT(*) FROM posts p JOIN accounts a ON a.id=p.account_id "
                       "WHERE a.slug='maehbot'").fetchone()[0] == 0
    con.close()

    c = TestClient(create_app(db_path))
    a = c.get("/api/accounts/maehbot").json()
    assert set(a) == PROFILE_KEYS
    assert a["kind"] == "robot" and a["display_name"] == "Mähbot 7" and a["posts"] == []
    assert a["people"][0]["slug"] == "hannes" and a["people"][0]["account"]["display_name"] == "Hannes vum Hof"
    assert "maehbot" in [x["slug"] for x in c.get("/api/accounts?kind=robot").json()["items"]]
