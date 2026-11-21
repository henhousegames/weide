import json

import pytest
from fastapi.testclient import TestClient

from weide import db
from weide.importer import import_public
from weide.server import create_app

from .conftest import REPO

DATA = REPO / "data"


@pytest.fixture
def client(db_path):
    con = db.connect(db_path)
    import_public(con, REPO)
    con.close()
    return TestClient(create_app(db_path))


def add_post(db_path, status, source, **kw):
    con = db.connect(db_path)
    aid = con.execute("SELECT id FROM accounts WHERE slug='lisa'").fetchone()[0]
    pid = db.upsert_post(con, account_id=aid, kind="text", caption="neu", posted_at="2026-11-17T20:00:00+01:00",
                         source=source, status=status, **kw)
    con.commit()
    con.close()
    return pid


def test_meta(client):
    m = client.get("/api/meta").json()
    assert m["app_name"] == "Weide" and m["today"] == "2026-11-17" and m["week"] == "2026-W47"
    assert m["posts"] == json.loads((DATA / "meta.json").read_text())["posts"]


def test_feed_contract(client):
    r = client.get("/api/feed?limit=5").json()
    assert set(r) == {"items", "next_offset"} and r["next_offset"] == 5
    feed_ids = [i["id"] for i in json.loads((DATA / "feed.json").read_text())["items"]]
    allitems = client.get("/api/feed?limit=100").json()["items"]
    assert [i["id"] for i in allitems] == feed_ids
    assert all(i["generation"] is None for i in allitems)
    card = allitems[0]
    assert card["media_url"].startswith("/media/") and card["poster_url"].startswith("/thumb/540/")
    assert card["account"]["avatar_url"].startswith("/avatar/")


def test_feed_kind_and_world_filters(client):
    animals = client.get("/api/feed?limit=100&kind=animal").json()["items"]
    assert animals and all(i["account"]["kind"] == "animal" for i in animals)
    assert client.get("/api/feed?limit=100&world=weide").json()["items"] == animals
    assert client.get("/api/feed?limit=100&kind=robot").json()["items"] == []
    assert client.get("/api/feed?limit=100&world=roboter").json()["items"] == []
    assert len(client.get("/api/feed?limit=100&kind=animal,human").json()["items"]) == len(animals)


def test_post_detail_and_react(client):
    p = client.get("/api/posts/1").json()
    assert p["id"] == 1 and p["uwe_reacted"] is True and p["moments"] == []
    assert p["reactions"] == json.loads((DATA / "posts" / "1.json").read_text())["reactions"]
    r = client.post("/api/posts/1/react", json={"type": "lol"}).json()
    assert r["reactions"]["lol"] == p["reactions"]["lol"] + 1
    assert client.post("/api/posts/1/react", json={"type": "boo"}).status_code == 400
    assert client.get("/api/posts/9999").status_code == 404
    assert client.get("/api/posts/episode:e001-v3:02-pia-umfrage").json()["id"] == 1


def test_accounts(client):
    items = client.get("/api/accounts").json()["items"]
    expected = json.loads((DATA / "accounts.json").read_text())["items"]
    assert [a["slug"] for a in items] == [a["slug"] for a in expected]
    hannes = client.get("/api/accounts/hannes").json()
    assert hannes["handle"] == "@hannes.vumm.hof" and hannes["kind"] == "human" and hannes["posts"] == []
    assert client.get("/api/accounts/@hannes.vumm.hof").json()["slug"] == "hannes"
    assert client.get("/api/accounts/nobody").status_code == 404


def test_top(client):
    t = client.get("/api/top?day=2026-11-17&limit=10").json()
    expected = json.loads((DATA / "top" / "day-2026-11-17.json").read_text())
    assert t["window"] == expected["window"]
    assert [i["id"] for i in t["items"]] == [i["id"] for i in expected["items"]]
    assert client.get("/api/top?day=x").status_code == 400


def test_only_published_posts_are_visible(client, db_path):
    draft = add_post(db_path, "draft", "cli:draft")
    approved = add_post(db_path, "approved", "cli:approved")
    n = client.get("/api/meta").json()["posts"]
    ids = [i["id"] for i in client.get("/api/feed?limit=100").json()["items"]]
    assert draft not in ids and approved not in ids
    for pid in (draft, approved):
        assert client.get(f"/api/posts/{pid}").status_code == 404
        assert client.post(f"/api/posts/{pid}/react", json={"type": "lol"}).status_code == 404
    assert all(p["id"] not in (draft, approved) for p in client.get("/api/accounts/lisa").json()["posts"])
    assert all(i["id"] not in (draft, approved) for i in client.get("/api/top?day=2026-11-17&limit=50").json()["items"])
    lisa = [a for a in client.get("/api/accounts").json()["items"] if a["slug"] == "lisa"][0]
    pub = add_post(db_path, "published", "cli:pub")
    assert client.get("/api/meta").json()["posts"] == n + 1
    assert client.get(f"/api/posts/{pub}").status_code == 200
    lisa2 = [a for a in client.get("/api/accounts").json()["items"] if a["slug"] == "lisa"][0]
    assert lisa2["posts"] == lisa["posts"] + 1


def test_generation_field_and_private_source(client, db_path):
    pid = add_post(db_path, "published", "generation:run-abc123", public_id="g7f3k2",
                   generation={"model_label": "Model X 1.0", "seed": 42, "workflow": "private"})
    card = client.get(f"/api/posts/{pid}").json()
    assert card["generation"] == {"public_id": "g7f3k2", "model_label": "Model X 1.0"}
    assert card["source"] is None


def test_media_routes(client):
    assert client.get("/media/e001-v3/out/single/02-pia-umfrage.mp4").status_code == 200
    assert client.get("/thumb/540/e001-v3/out/single/02-pia-umfrage.mp4").status_code == 200
    assert client.get("/avatar/lisa.jpg").status_code == 200
    assert client.get("/avatar/sandra_85.jpg").status_code == 404
    assert client.get("/media/../pyproject.toml").status_code in (403, 404)
    assert client.get("/media/x/.hidden.mp4").status_code == 403


def test_index_and_static(client):
    html = client.get("/").text
    assert "<title>Weide</title>" in html and "{{" not in html
    assert client.get("/static/js/api.js").status_code == 200
