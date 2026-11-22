"""Export contract: import(current data/) -> export-static reproduces the published site.

Intentional differences to the current data/ (documented, asserted below):
  * every post card gains `"generation": null` (legacy posts have no generation data).
  * the app is renamed (config.APP_NAME / APP_TAGLINE): meta.json and index.html carry the new name,
    and index.html sets the neutral `window.APP_STATIC` flag instead of `window.WEIDE_STATIC`.
Everything else (file set, key sets, ids, slugs, values, index.html, static/, media files) is identical.
"""
import json
import shutil
from pathlib import Path

import pytest

from weide import config, db
from weide.cli import main
from weide.export_static import export_static, leak_check
from weide.importer import import_public

from .conftest import REPO

DATA = REPO / "data"
INTENTIONAL_NEW_KEYS = {"generation", "robot", "disclaimer"}   # additive fields the live data/ predates
LIVE_NAME, LIVE_TAGLINE = "Weide", "Das Netzwerk für Creator mit Auslauf"     # what the live site still shows


def as_live(obj):
    """Undo the intentional rename so the rest of an export can be compared with the live data/."""
    if isinstance(obj, dict) and "app_name" in obj:
        return {**obj, "app_name": LIVE_NAME, "tagline": LIVE_TAGLINE}
    return obj


def as_live_html(html: str) -> str:
    return (html.replace(config.APP_NAME, LIVE_NAME).replace(config.APP_TAGLINE, LIVE_TAGLINE)
            .replace("window.APP_STATIC", "window.WEIDE_STATIC"))


def without(obj, keys):
    if isinstance(obj, dict):
        return {k: without(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [without(x, keys) for x in obj]
    return obj


def keypaths(obj, p=""):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(f"{p}.{k}")
            out |= keypaths(v, f"{p}.{k}")
    elif isinstance(obj, list):
        for x in obj:
            out |= keypaths(x, p + "[]")
    return out


def json_files(root: Path) -> dict[str, Path]:
    return {str(f.relative_to(root)): f for f in root.rglob("*.json")}


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("export")
    dbp = tmp / "weide.db"
    con = db.connect(dbp)
    import_public(con, REPO)
    con.close()
    out = tmp / "site"
    res = export_static(out, dbp, today="2026-11-17", link=True)
    return out, res, dbp


def test_same_files_ids_and_slugs(exported):
    out, res, _ = exported
    assert res["missing"] == [] and res["leaks"] == []
    assert set(json_files(out / "data")) == set(json_files(DATA))
    feed = json.loads((out / "data/feed.json").read_text())["items"]
    assert [i["id"] for i in feed] == [i["id"] for i in json.loads((DATA / "feed.json").read_text())["items"]]
    accs = json.loads((out / "data/accounts.json").read_text())["items"]
    assert [a["slug"] for a in accs] == [a["slug"] for a in json.loads((DATA / "accounts.json").read_text())["items"]]


def test_same_shape_and_values(exported):
    out, _, _ = exported
    for rel, f in json_files(DATA).items():
        ours = json.loads((out / "data" / rel).read_text())
        theirs = json.loads(f.read_text())
        assert keypaths(ours) - keypaths(theirs) <= {p for p in keypaths(ours) if p.rsplit(".", 1)[-1] in INTENTIONAL_NEW_KEYS}, rel
        assert keypaths(theirs) <= keypaths(ours), rel
        assert without(as_live(ours), INTENTIONAL_NEW_KEYS) == theirs, rel


def test_generation_is_null_on_legacy_cards(exported):
    out, _, _ = exported
    for i in json.loads((out / "data/feed.json").read_text())["items"]:
        assert i["generation"] is None


def test_app_shell_and_media(exported):
    out, _, _ = exported
    html = (out / "index.html").read_text()
    assert f"<title>{config.APP_NAME}</title>" in html and "window.APP_STATIC = true" in html
    assert as_live_html(html) == (REPO / "index.html").read_text()
    assert (out / ".nojekyll").is_file()
    for f in (REPO / "static").rglob("*"):
        if f.is_file():
            assert (out / "static" / f.relative_to(REPO / "static")).read_bytes() == f.read_bytes()
    for d in ("media", "thumb", "avatar"):
        ours = {p.relative_to(out) for p in (out / d).rglob("*") if p.is_file()}
        theirs = {p.relative_to(REPO) for p in (REPO / d).rglob("*") if p.is_file()}
        assert ours == theirs, d


def test_current_repo_data_passes_leak_check():
    assert leak_check(REPO) == []


def test_unpublished_and_private_data_stay_out(exported, tmp_path):
    _, _, base_db = exported
    dbp = tmp_path / "weide.db"
    shutil.copy(base_db, dbp)
    con = db.connect(dbp)
    lisa = con.execute("SELECT id FROM accounts WHERE slug='lisa'").fetchone()[0]
    draft = db.upsert_post(con, account_id=lisa, kind="text", caption="DRAFT-SECRET-CAPTION",
                           posted_at="2026-11-17T21:00:00+01:00", source="cli:draft", status="draft")
    approved = db.upsert_post(con, account_id=lisa, kind="text", caption="APPROVED-NOT-YET",
                              posted_at="2026-11-17T21:05:00+01:00", source="cli:approved", status="approved")
    gen = db.upsert_post(con, account_id=lisa, kind="text", caption="Neu hier",
                         posted_at="2026-11-17T21:10:00+01:00", source="generation:run-0042", status="published",
                         public_id="g1a2b3", generation={"model_label": "Model X 1.0", "seed": 7,
                                                         "workflow_path": "/home/EXAMPLE/wf.json"})
    story = db.upsert_post(con, account_id=lisa, kind="story", caption="Story", media="e001-v3/out/single/05-lisa-mukbang.mp4",
                           posted_at="2026-11-17T22:00:00+01:00", source="story:test", status="published", funny_score=20)
    db.upsert_post(con, account_id=lisa, parent_id=story, kind="reel", caption="Beat", clip_id="beat-1",
                   media="e001-v3/out/single/05-lisa-mukbang.mp4", posted_at="2026-11-17T22:00:00+01:00",
                   source="story:test:beat-1", status="published", funny_score=21, why="story:test:beat-1")
    con.execute("INSERT INTO characters (account_id, kind, identity, style_guidance) VALUES (?,?,?,?)",
                (lisa, "animal", "Kuh", "PRIVATE-STYLE-GUIDANCE"))
    con.execute("INSERT INTO controllers (name, type, key_hash, permissions) VALUES ('bot','agent','EXAMPLE_KEY_HASH','[]')")
    con.commit()
    con.close()
    out = tmp_path / "site"
    (out / "data/posts").mkdir(parents=True)
    (out / "data/posts/999.json").write_text("{}")             # stale file from an older export
    res = export_static(out, dbp, today="2026-11-17", link=True)
    assert res["leaks"] == []
    assert not (out / f"data/posts/{draft}.json").exists() and not (out / f"data/posts/{approved}.json").exists()
    assert not (out / "data/posts/999.json").exists()
    blob = "\n".join(f.read_text() for f in (out / "data").rglob("*.json"))
    for s in ("DRAFT-SECRET-CAPTION", "APPROVED-NOT-YET", "generation:run-0042", "PRIVATE-STYLE-GUIDANCE",
              "EXAMPLE_KEY_HASH", "/home/", "/Users/", "workflow_path", "media_path"):
        assert s not in blob, s
    card = json.loads((out / f"data/posts/{gen}.json").read_text())
    assert card["generation"] == {"public_id": "g1a2b3", "model_label": "Model X 1.0"}
    assert card["source"] is None
    assert json.loads((out / "data/gen/g1a2b3.json").read_text()) == {
        "public_id": "g1a2b3", "post_id": gen, "model_label": "Model X 1.0", "seed": 7}
    assert res["generations"] == 1
    assert json.loads((out / "data/meta.json").read_text())["posts"] == 42
    top = json.loads((out / "data/top/day-2026-11-17.json").read_text())
    st = [i for i in top["items"] if i["id"] == story][0]
    assert st["rank"] == 1 and len(st["moments"]) == 1
    assert set(st["moments"][0]) == {"post_id", "clip", "funny_score", "why", "score"}
    assert st["moments"][0]["why"] is None            # pipeline id dropped


def test_leak_check_finds_planted_leaks(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/feed.json").write_text(json.dumps({"items": [
        {"media_url": "/home/EXAMPLE/x.mp4", "caption": "api_token=EXAMPLE_TOKEN", "source": "generation:run-1",
         "media_path": "x", "moments": [{"video": "e/x.mp4"}], "why": "C:\\Users\\EXAMPLE"}]}))
    hits = "\n".join(leak_check(tmp_path))
    for s in ("absolute path", "secret-like", "generation run id", "media_path", "moment 'video'"):
        assert s in hits, s


@pytest.mark.parametrize("caption", [
    "Hockey am Teich", "Token-Ring und Monkey-Business", "Keyboard-Solo", "/me ist müde", "/r/aww hat gefragt",
    "AC/DC auf der Weide", "24/7 Rasenpflege", "Passwort: Fisch", "https://example.org/home/x"])
def test_leak_check_passes_harmless_values(tmp_path, caption):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/feed.json").write_text(json.dumps({"items": [{"caption": caption, "title": caption}]}))
    assert leak_check(tmp_path) == []


@pytest.mark.parametrize("caption,kind", [
    ("/home/EXAMPLE/x.mp4", "absolute path"), ("liegt in /tmp/EXAMPLE/x", "absolute path"),
    ("~/EXAMPLE/x", "absolute path"), ("/Users/EXAMPLE", "absolute path"), ("/var/EXAMPLE/x", "absolute path"),
    ("/mnt/EXAMPLE/x", "absolute path"), ("C:\\EXAMPLE\\x", "absolute path"), ("file:///EXAMPLE", "absolute path"),
    ("api_token=EXAMPLE_TOKEN", "secret-like"), ("password = EXAMPLE", "secret-like"),
    ("key vsk_EXAMPLE0000000000", "secret-like"), ("sk-EXAMPLE0000000000000", "secret-like"),
    ("ghp_EXAMPLE00000000000000", "secret-like"), ("Authorization: Bearer EXAMPLE", "secret-like")])
def test_leak_check_finds_real_paths_and_secrets(tmp_path, caption, kind):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/feed.json").write_text(json.dumps({"items": [{"caption": caption}]}))
    hits = leak_check(tmp_path)
    assert len(hits) == 1 and kind in hits[0], hits


def test_leak_check_key_names_and_configured_secret(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/feed.json").write_text(json.dumps({"items": [{"api_key": "x", "caption": "EXAMPLEsecretvalue1"}]}))
    hits = leak_check(tmp_path, ("EXAMPLEsecretvalue1",))
    assert any("private key 'api_key'" in h for h in hits) and any("configured secret" in h for h in hits)
    assert not any("EXAMPLEsecretvalue1" in h for h in hits if "configured" in h)


def test_cli_export_static(exported, tmp_path, capsys):
    _, _, base_db = exported
    main(["export-static", str(tmp_path / "o"), "--db", str(base_db), "--today", "2026-11-17", "--link"])
    res = json.loads(capsys.readouterr().out)
    assert res["posts"] == 40 and res["leaks"] == []
