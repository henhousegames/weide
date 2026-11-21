"""Disclaimer in the footer of every page (public site and preview) and the robot flag on account briefs."""
import json

import pytest
from fastapi.testclient import TestClient

from weide import config, db, preview, preview_auth
from weide.export_static import export_static
from weide.importer import import_public
from weide.server import create_app

from .conftest import REPO
from .test_preview import TOKEN, login

TEXT = "Fiktive, KI-generierte Inhalte. Keine Verbindung zu Unitree Robotics oder NVIDIA; Marken gehören ihren Inhabern."


def test_config_text_is_exact():
    assert config.DISCLAIMER == TEXT


def test_js_fallback_matches_config():
    # the live site serves an older data/meta.json without `disclaimer`: the JS carries the same text
    assert f'"{TEXT}"' in (REPO / "static/js/util.js").read_text(encoding="utf-8")


@pytest.fixture
def robot_db(db_path):
    con = db.connect(db_path)
    import_public(con, REPO)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot 7")
    mid = con.execute("SELECT id FROM accounts WHERE slug='maehbot'").fetchone()[0]
    db.upsert_post(con, account_id=mid, kind="text", caption="Rasen gemäht.", posted_at="2026-11-17T09:00:00+01:00",
                   source="test:robot", status="published")
    con.commit()
    con.close()
    return db_path


def test_robot_flag_in_api(robot_db):
    c = TestClient(create_app(robot_db))
    assert c.get("/api/meta").json()["disclaimer"] == TEXT
    items = {a["slug"]: a for a in c.get("/api/accounts").json()["items"]}
    assert items["maehbot"]["robot"] is True and items["lisa"]["robot"] is False
    assert c.get("/api/accounts/maehbot").json()["robot"] is True
    post = next(p for p in c.get("/api/feed?limit=100").json()["items"] if p["account"]["slug"] == "maehbot")
    assert post["account"]["robot"] is True


def test_profile_flag_overrides_kind(robot_db):
    con = db.connect(robot_db)
    con.execute("UPDATE accounts SET profile_json=? WHERE slug='lisa'", (json.dumps({"robot": True}),))
    con.commit()
    con.close()
    c = TestClient(create_app(robot_db))
    assert c.get("/api/accounts/lisa").json()["robot"] is True


def test_export_has_disclaimer_and_robot_flag(robot_db, tmp_path):
    out = tmp_path / "site"
    res = export_static(out, robot_db, today="2026-11-17")
    assert res["leaks"] == []
    assert json.loads((out / "data/meta.json").read_text())["disclaimer"] == TEXT
    assert any(TEXT in f.read_text(encoding="utf-8") for f in (out / "static").rglob("*.js"))
    assert json.loads((out / "data/accounts/maehbot.json").read_text())["robot"] is True
    accs = {a["slug"]: a for a in json.loads((out / "data/accounts.json").read_text())["items"]}
    assert accs["maehbot"]["robot"] is True
    feed = json.loads((out / "data/feed.json").read_text())["items"]
    assert next(p for p in feed if p["account"]["slug"] == "maehbot")["account"]["robot"] is True
    assert "siteFooter()" in (out / "static/js/main.js").read_text(encoding="utf-8")


def test_preview_pages_include_disclaimer(robot_db, monkeypatch, tmp_path):
    monkeypatch.setenv("HENHOUSE_PREVIEW_KEY_FILE", str(tmp_path / "preview.key"))
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    app = preview.create_preview_app(robot_db, token=TOKEN, settings=None, poll_interval=None)
    c = TestClient(app)
    assert login(c).status_code == 303
    assert c.get("/api/meta").json()["disclaimer"] == TEXT
    html = c.get("/").text
    assert "/static/js/main.js" in html
    assert TEXT in c.get("/static/js/util.js").text and "siteFooter()" in c.get("/static/js/main.js").text
