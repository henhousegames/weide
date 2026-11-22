"""pull-approved creates a missing character account from identity.md front matter (synthetic files only)."""
import json
import sqlite3

import httpx
import pytest

from weide import config, db
from weide.characters import IdentityError, account_from_identity, identity_fields
from weide.deploy import deploy
from weide.feed import BIO_MAX
from weide.publish import ServiceSettings, pull_approved

from .test_publish import BASE, KEY, FakeService, publication

BODY_MARKER = "PRIVATE-BODY-EXAMPLE-must-never-be-read"


def identity(root, dirname, **fm):
    d = root / dirname
    d.mkdir(parents=True)
    lines = ["---", *(f"{k}: {v}" for k, v in fm.items()), "---", "", f"# {dirname} (private)", BODY_MARKER]
    (d / "identity.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def chars(tmp_path):
    root = tmp_path / "characters"
    identity(root, "spatz", name="Spatz Example", slug="spatz", handle='"@spatz.example"', kind="animal",
             bio='"' + "Ein Spatz. " * 30 + '"', avatar="gen_0123456789abcdef")
    identity(root, "blech", name="Blech", handle="@blech.example", kind="robot", bio="Roboter.")
    identity(root, "haus", name="Haus", kind="house")
    identity(root, "klon", name="Klon", handle="@maehbot", kind="animal")
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "h.db"
    con = db.connect(dbp)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot")
    con.commit()
    monkeypatch.setattr(config, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(config, "THUMB_ROOT", tmp_path / "thumb")
    yield con, FakeService(), dbp
    con.close()


def run(con, svc, chars_dir, **kw):
    return pull_approved(con, ServiceSettings(BASE, KEY, chars_dir), transport=httpx.MockTransport(svc), **kw)


def account(con, slug):
    return con.execute("SELECT * FROM accounts WHERE slug=?", (slug,)).fetchone()


def test_identity_fields_are_allowlisted(chars):
    f = identity_fields(chars, "spatz")
    assert set(f) == {"slug", "display_name", "handle", "kind", "bio"}
    assert f["display_name"] == "Spatz Example" and f["handle"] == "@spatz.example" and f["kind"] == "animal"
    assert len(f["bio"]) == BIO_MAX and f["bio"].endswith("…")
    with pytest.raises(IdentityError):
        identity_fields(chars, "haus")                     # house is not a posting character
    with pytest.raises(IdentityError):
        identity_fields(chars, "fehlt")


def test_unknown_character_created_from_identity_and_published(env, chars, tmp_path):
    con, svc, dbp = env
    svc.pubs = [publication("pub_SPATZ01", character="spatz"), publication("pub_BLECH01", character="blech")]
    res = run(con, svc, chars)
    assert res["failed"] == [] and res["new"] == 2 and sorted(res["characters_created"]) == ["blech", "spatz"]
    a = account(con, "spatz")
    assert (a["kind"], a["handle"], a["display_name"], a["avatar"], a["listed"]) == \
        ("animal", "@spatz.example", "Spatz Example", None, 0)
    assert con.execute("SELECT COUNT(*) FROM characters WHERE account_id=?", (a["id"],)).fetchone()[0] == 1
    dump = "\n".join(con.iterdump())
    assert BODY_MARKER not in dump and "gen_0123456789abcdef" not in dump
    # public once it has a published post; the robot gets the robot flag
    site = tmp_path / "site"
    out = deploy(site, dbp, settings=ServiceSettings(BASE, KEY, chars), transport=httpx.MockTransport(svc),
                 today="2026-11-17")
    assert out["errors"] == [] and out["warnings"] == []
    briefs = {i["account"]["slug"]: i["account"] for i in json.loads((site / "data/feed.json").read_text())["items"]
              if i.get("account")}
    assert briefs["blech"]["robot"] is True and briefs["spatz"]["robot"] is False
    assert (site / "data/accounts/spatz.json").is_file()
    assert BODY_MARKER not in "".join(p.read_text() for p in site.rglob("*.json"))


def test_existing_account_is_never_overwritten(env, chars):
    con, svc, _ = env
    db.upsert_account(con, "spatz", kind="animal", handle="@spatz", display_name="Schon da", bio="alt")
    con.commit()
    svc.pubs = [publication("pub_SPATZ01", character="spatz")]
    res = run(con, svc, chars)
    assert res["new"] == 1 and res["characters_created"] == []
    a = account(con, "spatz")
    assert (a["display_name"], a["handle"], a["bio"]) == ("Schon da", "@spatz", "alt")


@pytest.mark.parametrize("slug", ["haus", "klon", "fehlt"])
def test_unusable_identity_fails_the_item_only(env, chars, slug):
    con, svc, _ = env
    svc.pubs = [publication("pub_BAD01", character=slug), publication("pub_OK01", approved_at="2026-11-18T10:00:00Z")]
    res = run(con, svc, chars)
    assert res["new"] == 1 and res["failed_ids"] == ["pub_BAD01"] and "unknown character" in res["failed"][0]
    assert account(con, slug) is None
    assert db.get_meta(con, "publications_since") is None          # retried next time


def test_without_characters_dir_unknown_still_fails(env):
    con, svc, _ = env
    svc.pubs = [publication("pub_SPATZ01", character="spatz")]
    res = run(con, svc, None)
    assert res["failed_ids"] == ["pub_SPATZ01"] and account(con, "spatz") is None


def test_dry_run_creates_nothing(env, chars):
    con, svc, _ = env
    svc.pubs = [publication("pub_SPATZ01", character="spatz")]
    res = run(con, svc, chars, dry_run=True)
    assert res["new"] == 1 and res["failed"] == [] and account(con, "spatz") is None


def test_handle_clash_check(env, chars):
    con, *_ = env
    with pytest.raises(IdentityError):
        account_from_identity(con, chars, "klon")
    assert account_from_identity(con, chars, "maehbot") is None       # exists: untouched


def test_settings_read_the_characters_dir(tmp_path, monkeypatch):
    for k in ("HENHOUSE_CHARACTERS_DIR", "HENHOUSE_FEED_DIR", "WEIDE_CHARACTERS_DIR", "WEIDE_FEED_DIR"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"HENHOUSE_SERVICE_URL={BASE}\nHENHOUSE_SERVICE_KEY={KEY}\n")
    assert ServiceSettings.load(env).characters_dir is None
    env.write_text(env.read_text() + f"HENHOUSE_CHARACTERS_DIR={tmp_path}/chars\n")
    assert ServiceSettings.load(env).characters_dir == (tmp_path / "chars").resolve()
    env.write_text(f"HENHOUSE_SERVICE_URL={BASE}\nHENHOUSE_SERVICE_KEY={KEY}\n")
    monkeypatch.setenv("HENHOUSE_FEED_DIR", str(tmp_path / "stories/feed"))
    assert ServiceSettings.load(env).characters_dir == (tmp_path / "stories/characters").resolve()
    monkeypatch.setenv("HENHOUSE_CHARACTERS_DIR", str(tmp_path / "other"))
    assert ServiceSettings.load(env).characters_dir == (tmp_path / "other").resolve()
    assert KEY not in repr(ServiceSettings(BASE, KEY, tmp_path))
