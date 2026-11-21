"""The robot characters (seed files) and the account visibility gate of the public API / export:
an account is public when it is `listed` (the legacy public snapshot) or has a published post."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from weide import config, db, preview, preview_auth
from weide.characters import SEED_DIR, load_seeds, seed_characters
from weide.export_static import export_static
from weide.importer import import_public
from weide.publish import ServiceSettings, pull_approved
from weide.review import ReviewSettings
from weide.server import create_app

from . import test_preview as tp
from . import test_publish as tpub
from .conftest import REPO
from .test_preview import private_config  # noqa: F401  (autouse: keys/config in a temp dir)

ROBOTS = ("maehbot", "gustav", "hopfried")
NEW = {"gustav": "@gustav", "hopfried": "@hopfried"}
BRANDS = ("unitree", "nvidia", "isaac", " g1", "g1 ")
LEGACY_SLUGS = [a["slug"] for a in json.loads((REPO / "data/accounts.json").read_text())["items"]]


def test_robot_seed_files():
    seeds = {s["slug"]: s for s in load_seeds()}
    for slug, handle in NEW.items():
        s = seeds[slug]
        assert s["handle"] == handle and s["display_name"]
        assert s["kind"] == "robot" and s["world"] == "roboter"
        assert s["bio"] and len(s["about"]) >= 2 and s["facts"] and s["values"] and len(s["day"]) >= 5
        assert s["character"]["identity"] and s["character"]["style_guidance"] == ""
        assert 0 < s["followers"] < 100
        text = (SEED_DIR / f"{slug}.json").read_text(encoding="utf-8").lower()
        for brand in BRANDS:
            assert brand not in text, (slug, brand)
        # provisional names live in one place: no text repeats the own display name
        body = json.dumps({k: v for k, v in s.items() if k not in ("slug", "handle", "display_name")},
                          ensure_ascii=False)
        assert s["display_name"] not in body, slug
        for p in s["people"]:
            assert p["slug"] in seeds or p["slug"] in LEGACY_SLUGS, p["slug"]
    assert "Restschluck" not in json.dumps(seeds["hopfried"], ensure_ascii=False)     # hinted, not spoiled


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The public snapshot plus the seed characters, media roots in a temp dir."""
    dbp = tmp_path / "h.db"
    con = db.connect(dbp)
    import_public(con, REPO)
    seed_characters(con)
    monkeypatch.setattr(config, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(config, "THUMB_ROOT", tmp_path / "thumb")
    yield con, dbp
    con.close()


def publish(con, *pubs):
    svc = tpub.FakeService()
    svc.pubs = list(pubs)
    res = pull_approved(con, ServiceSettings(tpub.BASE, tpub.KEY), transport=httpx.MockTransport(svc))
    con.commit()
    assert res["failed"] == [] and res["new"] == len(pubs)
    return res


def public_slugs(c):
    return [a["slug"] for a in c.get("/api/accounts").json()["items"]]


def test_seeded_characters_hidden_until_first_post(world):
    con, dbp = world
    c = TestClient(create_app(dbp))
    assert public_slugs(c) == LEGACY_SLUGS                   # unchanged public list, robots absent
    for slug in ROBOTS:
        assert c.get(f"/api/accounts/{slug}").status_code == 404
        assert c.get(f"/api/accounts/@{slug}").status_code == 404
    hannes = c.get("/api/accounts/hannes").json()             # legacy, no posts: still public
    assert hannes["posts"] == [] and hannes["display_name"] == "Hannes vum Hof"
    # the preview sees every account
    pv = TestClient(create_app(dbp, all_accounts=True))
    assert set(ROBOTS) <= set(public_slugs(pv))
    g = pv.get("/api/accounts/gustav").json()
    assert g["robot"] is True and {p["slug"]: bool(p["account"]) for p in g["people"]} == \
        {"hannes": True, "maehbot": True, "hopfried": True}


def test_pull_approved_maps_character_and_first_post_makes_public(world, tmp_path):
    con, dbp = world
    publish(con, tpub.publication("pub_EXAMPLEG1", character="gustav", caption="Handtuch auf Kante"))
    row = con.execute("SELECT a.slug FROM posts p JOIN accounts a ON a.id=p.account_id "
                      "WHERE p.public_id='pub_EXAMPLEG1'").fetchone()
    assert row["slug"] == "gustav"
    c = TestClient(create_app(dbp))
    slugs = public_slugs(c)
    assert "gustav" in slugs and [s for s in slugs if s != "gustav"] == LEGACY_SLUGS
    g = c.get("/api/accounts/gustav").json()
    assert [p["caption"] for p in g["posts"]] == ["Handtuch auf Kante"] and g["posts"][0]["account"]["robot"]
    # references to hidden accounts render with account null (views.js shows them unlinked)
    assert {p["slug"]: p["account"] for p in g["people"]}["maehbot"] is None
    assert {p["slug"]: p["account"] for p in g["people"]}["hopfried"] is None
    assert {p["slug"]: p["account"] for p in g["people"]}["hannes"]["display_name"] == "Hannes vum Hof"
    assert c.get("/api/accounts/hopfried").status_code == 404

    out = tmp_path / "site"
    res = export_static(out, dbp, today="2026-11-17")
    assert res["leaks"] == []
    accs = [a["slug"] for a in json.loads((out / "data/accounts.json").read_text())["items"]]
    assert "gustav" in accs and "maehbot" not in accs and "hopfried" not in accs
    assert (out / "data/accounts/gustav.json").is_file()
    assert not (out / "data/accounts/maehbot.json").exists() and not (out / "data/accounts/hopfried.json").exists()
    for slug in LEGACY_SLUGS:
        assert (out / f"data/accounts/{slug}.json").is_file(), slug

    publish(con, tpub.publication("pub_EXAMPLEH1", character="hopfried", caption="Flasche 13.481",
                                  approved_at="2026-11-18T09:00:00+00:00"))
    assert con.execute("SELECT a.slug FROM posts p JOIN accounts a ON a.id=p.account_id "
                       "WHERE p.public_id='pub_EXAMPLEH1'").fetchone()["slug"] == "hopfried"
    c = TestClient(create_app(dbp))
    assert {"gustav", "hopfried"} <= set(public_slugs(c)) and "maehbot" not in public_slugs(c)
    assert {p["slug"]: p["account"] for p in c.get("/api/accounts/gustav").json()["people"]}["hopfried"]["slug"] == \
        "hopfried"


def test_export_without_robot_posts_has_no_robot_files(world, tmp_path):
    _, dbp = world
    out = tmp_path / "site"
    export_static(out, dbp, today="2026-11-17")
    assert sorted(f.stem for f in (out / "data/accounts").glob("*.json")) == sorted(LEGACY_SLUGS)
    blob = (out / "data/accounts.json").read_text()
    for slug in ROBOTS:
        assert f'"{slug}"' not in blob


def test_preview_maps_label_and_publication_character(world, monkeypatch):
    con, dbp = world
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    svc = tp.FakeService([
        tp.gen("g_gus", labels={"character": "gustav", "caption": "Das Glas ist jetzt offen"}),
        tp.gen("g_hop", publication=tp.pub("pubHOP", character="hopfried", caption="Restschluck")),
    ])
    app = preview.create_preview_app(dbp, token=tp.TOKEN, settings=ReviewSettings(tp.BASE, tp.REVIEW_KEY),
                                     transport=httpx.MockTransport(svc), poll_interval=None)
    asyncio.run(app.state.store.poll_once())
    c = TestClient(app, base_url="http://preview-host.example:18510")
    assert tp.login(c).status_code == 303
    ids = tp.by_id(c.get("/api/feed?limit=100").json()["items"])
    assert ids["vs-g_gus"]["account"]["slug"] == "gustav" and ids["vs-g_gus"]["account"]["robot"] is True
    assert ids["vs-g_hop"]["account"]["slug"] == "hopfried" and ids["vs-g_hop"]["preview"]["state"] == "approved"
    assert set(ROBOTS) <= {a["slug"] for a in c.get("/api/accounts").json()["items"]}
    hop = next(s for s in load_seeds() if s["slug"] == "hopfried")
    assert c.get("/api/accounts/hopfried").json()["display_name"] == hop["display_name"]


def test_preview_reads_a_database_before_migration_5(tmp_path):
    """The preview opens the database read-only and never migrates: it must not need `listed`."""
    import sqlite3
    p = tmp_path / "v4.db"
    con = sqlite3.connect(p, isolation_level=None)
    con.execute("BEGIN")
    for _v, _n, fn in db.MIGRATIONS[:4]:
        fn(con)
    con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
    con.execute("INSERT INTO accounts (slug, kind, handle, display_name) VALUES ('hannes','human','@h','Hannes')")
    con.execute("COMMIT")
    con.close()
    c = TestClient(create_app(p, readonly=True, all_accounts=True))
    assert [a["slug"] for a in c.get("/api/accounts").json()["items"]] == ["hannes"]
    assert c.get("/api/accounts/hannes").status_code == 200
