"""Model labels: one short string everywhere (preview cards, captions, publish, badge, data/gen).

The fixtures in tests/fixtures/ are trimmed, sanitized copies of the live service answers (see their
`_note`): the review API and the publications API both send `model` as {id, display_name}.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from weide import config, db, preview, preview_auth
from weide.export_static import export_static
from weide.generation import model_short_label
from weide.importer import import_public
from weide.publish import ServiceSettings, pull_approved
from weide.review import ReviewSettings

from .conftest import REPO
from .test_preview import TOKEN, login

FIXTURES = Path(__file__).parent / "fixtures"
REVIEW = json.loads((FIXTURES / "review_generations.json").read_text(encoding="utf-8"))
PUBLICATIONS = json.loads((FIXTURES / "publications.json").read_text(encoding="utf-8"))
REVIEW_KEY = "EXAMPLE-review-key-" + "r" * 30
PUBLISH_KEY = "EXAMPLE-publish-key-" + "p" * 30
BASE = "http://service.test:18500"
BAD = ("{'", "display_name")


def clean(label) -> bool:
    return isinstance(label, str) and label and not any(b in label for b in BAD)


@pytest.mark.parametrize("model, label", [
    ({"id": "flux2-klein-4b", "display_name": "FLUX.2 Klein 4B (text + reference to image)"}, "FLUX.2 Klein 4B"),
    ({"id": "ltx-2.5-distilled", "display_name": "LTX-2.5 22B distilled (I2V + joint audio)"}, "LTX-2.5 22B distilled"),
    ({"id": "model-x", "display_name": "Model X 1.0"}, "Model X 1.0"),
    ({"id": "flux2-klein-4b", "display_name": None}, "flux2-klein-4b"),
    ({"id": "flux2-klein-4b"}, "flux2-klein-4b"),
    ("FLUX.2 Klein 4B (text + reference to image)", "FLUX.2 Klein 4B"),
    ("plain-model", "plain-model"),
    ("{'id': 'flux2-klein-4b', 'display_name': 'FLUX.2 Klein 4B (text + reference to image)'}", "FLUX.2 Klein 4B"),
    ("(only parens)", "(only parens)"),
    (None, None), ({}, None), ("", None), (42, None), ("{'broken", "{'broken"),
])
def test_model_short_label(model, label):
    assert model_short_label(model) == label


def test_fixtures_are_sanitized():
    for f in FIXTURES.glob("*.json"):
        text = f.read_text(encoding="utf-8")
        for bad in ("http://", "https://", "/home/", "/Users/", "vsk_", "Bearer"):
            assert bad not in text, (f.name, bad)


def test_live_fixture_items_have_model_objects():
    assert len(REVIEW["items"]) >= 2 and all(isinstance(i["model"], dict) for i in REVIEW["items"])
    assert all(isinstance(p["model"], dict) for p in PUBLICATIONS["publications"])


# --- preview -------------------------------------------------------------------------------------------

def review_service(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != f"Bearer {REVIEW_KEY}":
        return httpx.Response(401)
    if request.url.path == "/api/v1/review/generations":
        return httpx.Response(200, json={k: v for k, v in REVIEW.items() if k != "_note"})
    return httpx.Response(404)


@pytest.fixture
def preview_client(db_path, monkeypatch, tmp_path):
    monkeypatch.setenv("HENHOUSE_PREVIEW_KEY_FILE", str(tmp_path / "preview.key"))
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    con = db.connect(db_path)
    import_public(con, REPO)
    con.commit()
    con.close()
    app = preview.create_preview_app(db_path, token=TOKEN, settings=ReviewSettings(BASE, REVIEW_KEY),
                                     transport=httpx.MockTransport(review_service), poll_interval=None)
    asyncio.run(app.state.store.poll_once())
    c = TestClient(app, base_url="http://preview-host.example:18510")
    assert login(c).status_code == 303
    return c


def test_preview_cards_show_short_model_labels(preview_client):
    items = [i for i in preview_client.get("/api/feed?limit=100").json()["items"] if "preview" in i]
    assert len(items) == len(REVIEW["items"])
    by_gid = {i["id"].removeprefix("vs-"): i for i in items}
    for raw in REVIEW["items"]:
        card = by_gid[raw["id"]]
        want = model_short_label(raw["model"])
        assert card["preview"]["model"] == want and clean(want)
        assert "(" not in want
        caption = raw["labels"].get("caption")
        if not caption:                                            # fallback caption: "<model> · <date>"
            assert card["caption"].startswith(want + " · ")
        assert not any(b in json.dumps(card, ensure_ascii=False) for b in ("{'",)), card["caption"]
        detail = preview_client.get(f"/api/posts/{card['id']}").json()
        assert detail["preview"]["model"] == want
    # draft detail facts come from the review item
    vid = next(r for r in REVIEW["items"] if r["output"]["mime"].startswith("video/"))
    pv = by_gid[vid["id"]]["preview"]
    assert (pv["width"], pv["height"], pv["fps"], pv["duration_s"], pv["has_audio"]) == (
        vid["output"]["width"], vid["output"]["height"], vid["output"]["fps"], vid["output"]["duration_s"], vid["output"]["has_audio"])
    assert pv["created_at"] == vid["created_at"] and pv["prompt"] == vid["prompt_original"] and pv["task"] == vid["task"]
    assert pv["caption_is_fallback"] is True and pv["lab_url"].endswith(vid["lab_path"])
    assert by_gid["gen_f5d6526cb5374a54a4c2"]["preview"]["model"] == "FLUX.2 Klein 4B"
    assert by_gid["gen_f5d6526cb5374a54a4c2"]["caption"] == "FLUX.2 Klein 4B · 21.11.2026"


# --- publish -> card -> export ---------------------------------------------------------------------------

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200


def publish_service(pubs):
    import hashlib
    sha = hashlib.sha256(MP4).hexdigest()
    items = [{**p, "sha256": sha, "media": [{**m, "sha256": sha} for m in p["media"]]} for p in pubs]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != f"Bearer {PUBLISH_KEY}":
            return httpx.Response(401)
        path = req.url.path
        if path == "/api/v1/publications":
            if req.url.params.get("since"):
                return httpx.Response(200, json={"publications": [], "revoked": [], "next_since": req.url.params["since"]})
            return httpx.Response(200, json={"publications": items, "revoked": [],
                                             "next_since": PUBLICATIONS["next_since"]})
        pid = path.split("/")[4]
        p = next((x for x in items if x["public_id"] == pid), None)
        if p is None:
            return httpx.Response(404)
        if path.endswith("/summary"):          # the summary repeats the list's model object
            return httpx.Response(200, json={"summary_version": 1, "public_id": pid, "model": p["model"],
                                             "task": p["task"], "prompt": {"original": "a robot sheep"}})
        if path.endswith("/media/output"):
            return httpx.Response(200, content=MP4, headers={"content-type": p["mime"]})
        return httpx.Response(404)
    return handler


@pytest.fixture
def published(tmp_path, monkeypatch):
    dbp = tmp_path / "h.db"
    con = db.connect(dbp)
    import_public(con, REPO)
    for p in PUBLICATIONS["publications"]:
        if not con.execute("SELECT 1 FROM accounts WHERE slug=?", (p["character"],)).fetchone():
            db.upsert_account(con, p["character"], kind="robot", handle="@" + p["character"], display_name=p["character"])
    con.commit()
    monkeypatch.setattr(config, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(config, "THUMB_ROOT", tmp_path / "thumb")
    pubs = [{k: v for k, v in p.items()} for p in PUBLICATIONS["publications"]]
    res = pull_approved(con, ServiceSettings(BASE, PUBLISH_KEY), transport=httpx.MockTransport(publish_service(pubs)))
    con.commit()
    assert res["failed"] == [] and res["new"] == len(pubs)
    yield con, dbp
    con.close()


def test_publish_stores_short_label_and_card_shows_it(published):
    con, dbp = published
    from weide.server import create_app
    c = TestClient(create_app(dbp))
    pid = PUBLICATIONS["publications"][0]["public_id"]
    card = next(i for i in c.get("/api/feed?limit=100").json()["items"] if (i.get("generation") or {}).get("public_id") == pid)
    assert card["generation"]["model_label"] == "LTX-2.5 22B distilled"
    stored = json.loads(con.execute("SELECT generation_json FROM posts WHERE public_id=?", (pid,)).fetchone()[0])
    assert stored["model_label"] == "LTX-2.5 22B distilled"


def test_export_gen_and_badge_have_no_dict_repr(published, tmp_path):
    con, dbp = published
    # a record stored by older code: model_label is the repr of the model dict
    con.execute("UPDATE posts SET generation_json=json_set(generation_json, '$.model_label', ?) WHERE public_id IS NOT NULL",
                (str(PUBLICATIONS["publications"][0]["model"]),))
    con.commit()
    out = export_static(tmp_path / "site", dbp, today="2026-11-21")
    assert out["generations"] == len(PUBLICATIONS["publications"]) and out["leaks"] == []
    gens = list((tmp_path / "site/data/gen").glob("*.json"))
    assert gens
    for f in gens:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["model_label"] == "LTX-2.5 22B distilled"
        assert "{'" not in f.read_text(encoding="utf-8")
    labels = []
    for f in (tmp_path / "site/data").rglob("*.json"):
        if "gen" in f.parts:
            continue
        def walk(o):
            if isinstance(o, dict):
                g = o.get("generation")
                if isinstance(g, dict):
                    labels.append(g.get("model_label"))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(json.loads(f.read_text(encoding="utf-8")))
    assert labels and all(clean(lb) and lb == "LTX-2.5 22B distilled" for lb in labels), labels
    badge = (REPO / "static/js/ui.js").read_text(encoding="utf-8")
    assert "Erstellt mit ${typeof g.model_label === \"string\"" in badge      # the badge prints a string label only
