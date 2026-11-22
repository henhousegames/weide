"""`weide pull-approved` against a fake generation service (httpx.MockTransport), and `weide deploy`."""
import hashlib
import json
import logging
import shutil
from pathlib import Path

import httpx
import pytest

from weide import cli, config, db
from weide.deploy import deploy
from weide.export_static import export_static
from weide.importer import import_public
from weide.publish import PublishError, ServiceSettings, pull_approved, read_dotenv

from .conftest import REPO

BASE = "https://svc.example.test"
KEY = "hh-EXAMPLE-not-a-real-key-0123456789"
PID = "pub_EXAMPLE01"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200      # header only (not playable)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100                                       # magic only
API = "/api/v1/publications"
SUMMARY = {
    "summary_version": 1, "public_id": PID, "character": "maehbot", "caption": "Rasenmäher-Test",
    "model": {"id": "model-x", "display_name": "Model X 1.0", "license": {"id": "Apache-2.0"}},
    "task": "t2v", "seed": 42, "parameters": {"steps": 30, "key": "first-frame"},
    "prompt": {"original": "A robot sheep reviews a lawn mower"},
    "notes": ["made on gpu-box.local:8188 from /home/EXAMPLE/in.png"],
    "api_key": "EXAMPLE_LEAKED", "output_path": "/srv/EXAMPLE/out.mp4",
    "media": [{"kind": "output", "url": f"{API}/{PID}/media/output"}],
    "links": {"summary": f"{API}/{PID}/summary"},
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def publication(pid=PID, *, character="maehbot", caption="Rasenmäher-Test", approved_at="2026-11-17T17:00:00+00:00",
                mime="video/mp4", content=MP4):
    video = mime.startswith("video/")
    return {"public_id": pid, "character": character, "caption": caption, "approved_at": approved_at,
            "task": "t2v" if video else "t2i", "model": {"id": "model-x", "display_name": "Model X 1.0"},
            "sha256": sha(content), "mime": mime,
            "video_url": f"{API}/{pid}/media/output" if video else None,
            "image_url": None if video else f"{API}/{pid}/media/output",
            "poster_url": f"{API}/{pid}/media/poster",
            "media": [{"kind": "output", "mime": mime, "sha256": sha(content), "url": f"{API}/{pid}/media/output"}],
            "links": {"summary": f"{API}/{pid}/summary"}}


class FakeService:
    """The service's publish API (docs/API.md "Publishing"): `since` filters strictly after, oldest first."""

    def __init__(self):
        self.pubs = [publication()]
        self.summaries: dict[str, dict] = {}
        self.media: dict[str, bytes] = {}
        self.media_status = 200
        self.revoked: list = []
        self.poster: bytes | None = None
        self.calls: list[str] = []
        self.auth: dict[str, str | None] = {}
        self.page_size: int | None = None          # simulate a paginating server

    def summary(self, pid):
        return self.summaries.get(pid) or {**SUMMARY, "public_id": pid}

    def listing(self, since):
        def after(t):
            return since is None or db.parse_time(t) > db.parse_time(since)
        pubs = sorted((p for p in self.pubs if after(p["approved_at"])), key=lambda p: db.parse_time(p["approved_at"]))
        revs = [r for r in self.revoked if after(r["revoked_at"])]
        if self.page_size is not None:
            pubs = pubs[:self.page_size]
        times = [p["approved_at"] for p in pubs] + [r["revoked_at"] for r in revs]
        nxt = max(times, key=db.parse_time) if times else since or "2026-01-01T00:00:00+00:00"
        return {"publications": pubs, "revoked": revs, "since": since, "next_since": nxt}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(str(req.url) if path == API else path)
        self.auth[str(req.url)] = req.headers.get("authorization")
        if req.url.host == "svc.example.test" and req.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad key"}})
        if path == API:
            return httpx.Response(200, json=self.listing(req.url.params.get("since")))
        parts = path.removeprefix(API + "/").split("/")
        active = {p["public_id"]: p for p in self.pubs}
        if req.url.host == "cdn.example.test":
            parts = [parts[-1].split(".")[0], "media", "output"]
        if parts[0] not in active:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "unknown"}})
        if parts[1:] == ["summary"]:
            return httpx.Response(200, json=self.summary(parts[0]))
        if parts[1:] == ["media", "poster"]:
            if self.poster is None:
                return httpx.Response(404)
            return httpx.Response(200, content=self.poster, headers={"content-type": "image/jpeg"})
        if parts[1:] == ["media", "output"]:
            if self.media_status != 200:
                return httpx.Response(self.media_status, json={"error": {"code": "not_found", "message": "gone"}})
            return httpx.Response(200, content=self.media.get(parts[0], MP4), headers={"content-type": active[parts[0]]["mime"]})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "h.db"
    con = db.connect(dbp)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot")
    con.commit()
    media, thumb = tmp_path / "media", tmp_path / "thumb"
    monkeypatch.setattr(config, "MEDIA_ROOT", media)
    monkeypatch.setattr(config, "THUMB_ROOT", thumb)
    svc = FakeService()
    yield con, svc, media, thumb, dbp
    con.close()


def run(con, svc, **kw):
    return pull_approved(con, ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), **kw)


def posts(con):
    return con.execute("SELECT * FROM posts WHERE source LIKE 'generation:%'").fetchall()




def test_publish_and_idempotent(env):
    con, svc, media, _, _ = env
    res = run(con, svc)
    assert (res["new"], res["updated"], res["unchanged"], res["failed"]) == (1, 0, 0, [])
    assert res["incremental"] is False
    [p] = posts(con)
    assert p["status"] == "published" and p["public_id"] == PID and p["source"] == f"generation:{PID}"
    assert p["kind"] == "reel" and p["media"] == f"gen/{PID}.mp4" and p["caption"] == "Rasenmäher-Test"
    assert p["posted_at"] == "2026-11-17T17:00:00+00:00"
    assert (media / f"gen/{PID}.mp4").read_bytes() == MP4
    assert not [f for f in (media / "gen").iterdir() if f.name.startswith(".")]      # temp dir cleaned
    gen = json.loads(p["generation_json"])
    assert gen["model_label"] == "Model X 1.0" and gen["seed"] == 42 and gen["parameters"]["key"] == "first-frame"
    assert db.get_meta(con, "publications_since") == "2026-11-17T17:00:00+00:00"

    res = run(con, svc)                                     # incremental: nothing after the cursor
    assert (res["new"], res["updated"], res["unchanged"], res["incremental"]) == (0, 0, 0, True)
    assert "since=2026-11-17T17" in svc.calls[-1]
    res = run(con, svc, full=True)                          # full reconcile: unchanged
    assert (res["new"], res["updated"], res["unchanged"]) == (0, 0, 1)
    assert len(posts(con)) == 1
    assert svc.calls.count(f"{API}/{PID}/media/output") == 1        # not downloaded again


def test_caption_update_keeps_post_time(env):
    con, svc, *_ = env
    run(con, svc)
    svc.pubs[0] = publication(caption="Neue Caption", approved_at="2026-11-18T09:00:00+00:00")
    res = run(con, svc)
    assert (res["new"], res["updated"], res["incremental"]) == (0, 1, True)
    [p] = posts(con)
    assert p["caption"] == "Neue Caption" and p["posted_at"] == "2026-11-17T17:00:00+00:00"


def test_dry_run_writes_nothing(env):
    con, svc, media, *_ = env
    res = run(con, svc, dry_run=True)
    assert res["new"] == 1 and posts(con) == [] and not (media / f"gen/{PID}.mp4").exists()
    assert db.get_meta(con, "publications_since") is None


def test_image_publication(env):
    con, svc, media, thumb, _ = env
    svc.pubs = [publication("pub_IMG", mime="image/png", content=PNG)]
    svc.media["pub_IMG"] = PNG
    res = run(con, svc)
    assert res["new"] == 1 and res["items"][0]["kind"] == "image", res
    [p] = posts(con)
    assert p["kind"] == "image" and p["media"] == "gen/pub_IMG.png"
    assert (media / "gen/pub_IMG.png").read_bytes() == PNG
    # wrong bytes for the mime type
    svc.pubs = [publication("pub_IMG2", mime="image/png", content=MP4, approved_at="2026-11-18T10:00:00Z")]
    svc.media["pub_IMG2"] = MP4
    res = run(con, svc)
    assert "not a image/png file" in res["failed"][0]
    svc.pubs = [publication("pub_GIF", mime="image/gif", content=PNG)]
    res = run(con, svc, full=True)
    assert any("unsupported mime" in f for f in res["failed"])


def test_unknown_character_fails_without_post_and_keeps_cursor(env):
    con, svc, media, *_ = env
    svc.pubs[0] = publication(character="nobody")
    res = run(con, svc)
    assert res["new"] == 0 and len(res["failed"]) == 1 and "unknown character" in res["failed"][0]
    assert posts(con) == [] and not (media / f"gen/{PID}.mp4").exists()
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE slug='nobody'").fetchone()[0] == 0
    assert db.get_meta(con, "publications_since") is None          # retried next time
    db.upsert_account(con, "nobody", kind="robot", handle="@nobody", display_name="Nobody")
    con.commit()
    assert run(con, svc)["new"] == 1


def test_missing_media_leaves_nothing(env):
    con, svc, media, *_ = env
    svc.media_status = 404
    res = run(con, svc)
    assert len(res["failed"]) == 1 and "HTTP 404" in res["failed"][0] and posts(con) == []
    assert not (media / "gen").exists() or not list((media / "gen").iterdir())


def test_sha256_and_content_checks(env):
    con, svc, *_ = env
    svc.pubs[0]["sha256"] = sha(b"something else")
    res = run(con, svc)
    assert "sha256 mismatch" in res["failed"][0] and posts(con) == []
    svc.pubs[0]["sha256"] = sha(b"<html>")
    svc.media[PID] = b"<html>"
    res = run(con, svc)
    assert "not a video/mp4" in res["failed"][0] and posts(con) == []
    svc.pubs[0]["sha256"] = "nothex"
    assert "invalid sha256" in run(con, svc)["failed"][0]
    svc.media[PID] = MP4
    svc.pubs[0]["sha256"] = sha(MP4)
    assert run(con, svc)["new"] == 1


def test_sanitizer_and_service_ids(env):
    con, svc, *_ = env
    run(con, svc)
    blob = posts(con)[0]["generation_json"]
    for bad in ("EXAMPLE_LEAKED", "/home/", "/srv/", "gpu-box", "api_key", "output_path", "/api/v1", "summary_version"):
        assert bad not in blob, bad
    assert "A robot sheep reviews a lawn mower" in blob


def test_inputs_published_or_private(env, tmp_path):
    con, svc, _, _, dbp = env
    start = publication("pub_START", mime="image/png", content=PNG, approved_at="2026-11-17T16:00:00+00:00")
    svc.pubs.insert(0, start)
    svc.media["pub_START"] = PNG
    svc.summaries[PID] = {**SUMMARY, "inputs": [
        {"label": "input-1", "role": "start_frame", "position": 0, "strength": 1.0, "published": True,
         "sha256": sha(PNG), "mime": "image/png", "source_public_id": "pub_START", "url": f"{API}/{PID}/media/input-1"},
        {"label": "input-2", "role": "keyframe", "published": False, "note": "private input"},
        {"label": "input-3", "role": "keyframe", "published": True, "source_public_id": "bad id!"}]}
    assert run(con, svc)["new"] == 2
    gen = json.loads(con.execute("SELECT generation_json FROM posts WHERE public_id=?", (PID,)).fetchone()[0])
    assert gen["inputs"][0] == {"label": "input-1", "role": "start_frame", "position": 0, "strength": 1.0,
                                "public_id": "pub_START"}
    assert gen["inputs"][1] == {"role": "keyframe", "private": True, "label": "privater Input"}
    assert gen["inputs"][2]["private"] is True
    assert "media/input" not in json.dumps(gen)
    con.commit()
    export_static(tmp_path / "site", dbp)
    out = json.loads((tmp_path / f"site/data/gen/{PID}.json").read_text())
    assert out["inputs"][0]["public_id"] == "pub_START" and out["inputs"][1]["label"] == "privater Input"


def test_pagination_follows_next_since(env):
    con, svc, *_ = env
    svc.pubs = [publication(f"pub_P{i}", approved_at=f"2026-11-17T1{i}:00:00+00:00") for i in range(4)]
    svc.page_size = 1
    res = run(con, svc)
    assert res["new"] == 4 and res["failed"] == []
    assert sum(1 for c in svc.calls if c.startswith(f"{BASE}{API}")) == 5         # 4 pages + the empty one
    assert db.get_meta(con, "publications_since") == "2026-11-17T13:00:00+00:00"


def test_key_only_sent_to_service_origin(env):
    con, svc, *_ = env
    svc.pubs[0]["video_url"] = f"https://cdn.example.test/v/{PID}.mp4?sig=abc"
    assert run(con, svc)["new"] == 1
    assert svc.auth[f"https://cdn.example.test/v/{PID}.mp4?sig=abc"] is None
    assert svc.auth[f"{BASE}{API}"] == f"Bearer {KEY}"


def test_key_never_in_output_or_logs(env, capsys, caplog, monkeypatch, tmp_path):
    con, svc, *_ = env
    caplog.set_level(logging.DEBUG)
    svc.pubs[0]["character"] = "nobody"
    lines: list[str] = []
    res = run(con, svc, log=lines.append)
    text = json.dumps(res) + "\n".join(lines) + caplog.text
    assert KEY not in text and res["failed"]
    assert KEY not in repr(ServiceSettings(BASE, KEY))
    # CLI path: settings from a .env in the private dir
    (tmp_path / "priv").mkdir()
    (tmp_path / "priv/.env").write_text(f"HENHOUSE_SERVICE_URL={BASE}\nHENHOUSE_SERVICE_KEY='{KEY}'\n")
    monkeypatch.setattr(config, "PRIVATE_DIR", tmp_path / "priv")
    monkeypatch.delenv("HENHOUSE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("HENHOUSE_SERVICE_URL", raising=False)
    assert read_dotenv(tmp_path / "priv/.env")["HENHOUSE_SERVICE_KEY"] == KEY
    assert ServiceSettings.load().key == KEY
    # a wrong key: 401 reported without the key
    res = pull_approved(con, ServiceSettings(BASE, KEY + "x"), transport=httpx.MockTransport(svc))
    assert "HTTP 401" in res["failed"][0] and KEY not in json.dumps(res)


def test_missing_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PRIVATE_DIR", tmp_path)
    for k in ("HENHOUSE_SERVICE_KEY", "HENHOUSE_SERVICE_URL", "WEIDE_SERVICE_KEY", "WEIDE_SERVICE_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(PublishError):
        ServiceSettings.load()
    with pytest.raises(SystemExit) as e:
        cli.main(["pull-approved", "--db", str(tmp_path / "x.db")])
    assert e.value.code == 2


def test_cli_exit_code_and_output(env, monkeypatch, capsys):
    con, svc, _, _, dbp = env
    import weide.publish as pub
    real = pub.ServiceClient.__init__
    monkeypatch.setattr(pub.ServiceClient, "__init__",
                        lambda self, s, t=None, timeout=60: real(self, s, httpx.MockTransport(svc), timeout))
    monkeypatch.setenv("HENHOUSE_SERVICE_URL", BASE)
    monkeypatch.setenv("HENHOUSE_SERVICE_KEY", KEY)
    cli.main(["pull-approved", "--db", str(dbp)])
    out = capsys.readouterr()
    assert json.loads(out.out)["new"] == 1 and KEY not in out.out + out.err
    svc.pubs.append(publication("pub_EXAMPLE02", character="nobody", approved_at="2026-11-18T10:00:00+00:00"))
    with pytest.raises(SystemExit) as e:
        cli.main(["pull-approved", "--db", str(dbp)])
    out = capsys.readouterr()
    assert e.value.code == 1 and KEY not in out.out + out.err
    with pytest.raises(SystemExit):
        cli.main(["pull-approved", "--db", str(dbp), "--dry-run", "--full"])
    assert json.loads(capsys.readouterr().out)["unchanged"] == 1      # full: the first one is listed again


@pytest.fixture(scope="module")
def base_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("pub") / "base.db"
    con = db.connect(p)
    import_public(con, REPO)
    con.close()
    return p


def test_export_and_deploy_have_no_source_or_key(base_db, tmp_path, monkeypatch):
    dbp = tmp_path / "h.db"
    shutil.copy(base_db, dbp)
    media, thumb = tmp_path / "media", tmp_path / "thumb"
    # empty media roots: legacy posts only count as "missing" media in the export (a warning)
    monkeypatch.setattr(config, "MEDIA_ROOT", media)
    monkeypatch.setattr(config, "THUMB_ROOT", thumb)
    con = db.connect(dbp)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot")
    con.commit()
    con.close()
    svc = FakeService()
    site = tmp_path / "site"
    kw = dict(settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), today="2026-11-17")
    res = deploy(site, dbp, **kw)
    assert res["errors"] == [] and res["changed"] and res["pull"]["new"] == 1 and res["export"]["generations"] == 1
    assert (site / f"media/gen/{PID}.mp4").is_file() and (site / f"data/gen/{PID}.json").is_file()
    for f in site.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".html", ".js"):
            text = f.read_text(encoding="utf-8")
            assert "generation:" not in text and KEY not in text, f
    again = deploy(site, dbp, **kw)
    assert again["errors"] == [] and again["changed"] is False and again["pull"]["new"] == 0
    assert deploy(site, dbp, full=True, **kw)["pull"]["unchanged"] == 1
    # a failing item is a warning: the rest is still exported
    svc.pubs[0] = publication(caption="geändert", character="nobody", approved_at="2026-11-18T10:00:00+00:00")
    bad = deploy(site, dbp, **kw)
    assert bad["errors"] == [] and bad["warnings"] and bad["failed_items"] == [PID] and bad["export"] is not None
    # a failing listing stops before the export
    down = deploy(site, dbp, **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(503))})
    assert down["errors"] and down["pull"]["listing_failed"] and down["export"] is None and down["changed"] is False


def test_one_bad_item_does_not_block_publishes_and_revocations(env, tmp_path):
    con, svc, media, _, dbp = env
    site = tmp_path / "site"
    kw = dict(settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), today="2026-11-17")
    assert deploy(site, dbp, **kw)["errors"] == []
    cursor = db.get_meta(con, "publications_since")
    svc.pubs.append(publication("pub_BAD01", character="nobody", approved_at="2026-11-18T09:00:00+00:00"))
    svc.pubs.append(publication("pub_GOOD1", approved_at="2026-11-18T10:00:00+00:00"))
    svc.revoked.append({"public_id": PID, "revoked_at": "2026-11-18T11:00:00+00:00"})
    svc.pubs = [p for p in svc.pubs if p["public_id"] != PID]
    res = deploy(site, dbp, **kw)
    assert res["errors"] == [] and res["failed_items"] == ["pub_BAD01"]
    assert res["pull"]["new"] == 1 and res["pull"]["revoked"] == 1 and res["changed"]
    assert (site / "data/gen/pub_GOOD1.json").is_file() and not (site / f"data/gen/{PID}.json").exists()
    assert not (site / "data/gen/pub_BAD01.json").exists()
    assert db.get_meta(con, "publications_since") == cursor             # the bad item is fetched again
    db.upsert_account(con, "nobody", kind="animal", handle="@nobody", display_name="Nobody")
    con.commit()
    res = deploy(site, dbp, **kw)
    assert res["errors"] == [] and res["failed_items"] == [] and res["pull"]["new"] == 1
    assert (site / "data/gen/pub_BAD01.json").is_file()
    assert db.get_meta(con, "publications_since") == "2026-11-18T11:00:00+00:00"


def test_export_of_published_generation_post(env, tmp_path):
    con, svc, *_ = env
    run(con, svc)
    con.commit()
    out = export_static(tmp_path / "out", env[4], today="2026-11-17")
    assert out["leaks"] == [] and out["generations"] == 1


def _ffmpeg(*args):
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", *map(str, args)], check=True)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_poster_from_real_video(env, tmp_path):
    con, svc, media, thumb, _ = env
    vid = tmp_path / "v.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=green:s=96x160:d=1", "-pix_fmt", "yuv420p", vid)
    svc.media[PID] = vid.read_bytes()
    svc.pubs[0]["sha256"] = sha(svc.media[PID])
    res = run(con, svc)                                       # no service poster (404): a video frame
    assert res["new"] == 1 and res["items"][0]["poster"] is True
    assert (thumb / f"540/gen/{PID}.jpg").stat().st_size > 0


def test_no_ffmpeg_skips_poster(env, monkeypatch):
    import weide.publish as pub
    con, svc, _, thumb, _ = env
    monkeypatch.setattr(pub, "ffmpeg_available", lambda: False)
    res = run(con, svc)
    assert res["new"] == 1 and res["items"][0]["poster"] is False and not (thumb / f"540/gen/{PID}.jpg").exists()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100        # magic only


def test_service_poster_used_without_ffmpeg(env, monkeypatch):
    import weide.publish as pub
    con, svc, _, thumb, _ = env
    monkeypatch.setattr(pub, "ffmpeg_available", lambda: False)
    svc.poster = JPEG
    res = run(con, svc)
    assert res["items"][0]["poster"] is True and (thumb / f"540/gen/{PID}.jpg").read_bytes() == JPEG


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_service_poster_preferred_then_video_fallback(env, tmp_path):
    import subprocess
    con, svc, _, thumb, _ = env
    vid, img = tmp_path / "v.mp4", tmp_path / "p.jpg"
    _ffmpeg("-f", "lavfi", "-i", "color=c=green:s=96x160:d=1", "-pix_fmt", "yuv420p", vid)
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=1080x1920", "-frames:v", "1", img)
    svc.media[PID], svc.poster = vid.read_bytes(), img.read_bytes()
    svc.pubs[0]["sha256"] = sha(svc.media[PID])
    assert run(con, svc)["items"][0]["poster"] is True
    assert f"{API}/{PID}/media/poster" in svc.calls
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                          "-of", "csv=p=0", str(thumb / f"540/gen/{PID}.jpg")], capture_output=True, text=True)
    assert out.stdout.strip() == "540"                  # scaled from the service poster (the video is 96 px)
    # poster missing at the service -> frame of the video
    (thumb / f"540/gen/{PID}.jpg").unlink()
    svc.poster = None
    svc.pubs[0]["caption"] = "neu"
    assert run(con, svc, full=True)["items"][0]["poster"] is True and (thumb / f"540/gen/{PID}.jpg").is_file()


def test_malformed_item_fails_alone(env):
    con, svc, *_ = env
    good = svc.pubs[0]
    svc.pubs = [{**good, "public_id": "bad id!"}, {"public_id": "pub_09", "approved_at": "2026-11-17T10:00:00Z"},
                {**good, "public_id": "pub_05", "mime": "text/html"}, good]
    res = run(con, svc)
    assert res["new"] == 1 and len(res["failed"]) == 3 and [p["public_id"] for p in posts(con)] == [PID]


def test_upsert_and_later_timestamp_wins():
    from weide.publish import _merge
    fails: list[str] = []
    a1 = {"public_id": "pub_A", "approved_at": "2026-11-17T10:00:00Z", "caption": "old"}
    a2 = {**a1, "approved_at": "2026-11-17T12:00:00Z", "caption": "new"}
    b = {"public_id": "pub_B", "approved_at": "2026-11-17T10:00:00Z"}
    todo, revoked = _merge([a1, b, a2], [{"public_id": "pub_B", "revoked_at": "2026-11-17T11:00:00Z"},
                                         {"public_id": "pub_A", "revoked_at": "2026-11-17T11:00:00Z"},
                                         {"public_id": "no good"}], fails.append)
    assert [d["caption"] for _, d in todo] == ["new"]          # pub_A re-approved after its revocation
    assert revoked == {"pub_B": "2026-11-17T11:00:00Z"} and len(fails) == 1


def test_revocations(env, tmp_path):
    con, svc, media, _, dbp = env
    from fastapi.testclient import TestClient
    from weide.server import create_app
    svc.pubs.append(publication("pub_EXAMPLE02", approved_at="2026-11-17T18:00:00+00:00"))
    assert run(con, svc)["new"] == 2
    site = tmp_path / "site"
    exp = export_static(site, dbp)
    assert (site / f"data/gen/{PID}.json").is_file() and (site / f"media/gen/{PID}.mp4").is_file()

    # the owner revokes both: the service drops them from `publications` and lists them in `revoked`
    svc.pubs = []
    svc.revoked = [{"public_id": PID, "revoked_at": "2026-11-18T09:00:00+00:00"},
                   {"public_id": "pub_EXAMPLE02", "revoked_at": "2026-11-18T09:00:00+00:00"},
                   {"public_id": "pub_NOTHERE", "revoked_at": "2026-11-18T09:00:00+00:00"}]
    dry = run(con, svc, dry_run=True)
    assert dry["revoked"] == 2 and all(p["status"] == "published" for p in posts(con))
    res = run(con, svc)
    assert (res["revoked"], res["new"], res["failed"], res["incremental"]) == (2, 0, [], True)
    assert any("pub_NOTHERE" in n for n in res["notes"])
    assert {p["status"] for p in posts(con)} == {"draft"}
    assert (media / f"gen/{PID}.mp4").is_file()                     # kept on disk
    assert run(con, svc)["revoked"] == 0                            # cursor moved past them
    again = run(con, svc, full=True)
    assert again["revoked"] == 0 and again["failed"] == []          # idempotent

    client = TestClient(create_app(dbp))
    assert client.get(f"/api/generations/{PID}").status_code == 404
    assert PID not in client.get("/api/feed").text
    exp = export_static(site, dbp)
    assert exp["leaks"] == [] and exp["pruned"] >= 2
    assert not (site / f"data/gen/{PID}.json").exists() and not (site / f"media/gen/{PID}.mp4").exists()
    assert "pub_EXAMPLE0" not in (site / "data/feed.json").read_text()

    # approved again later: same public_id is published again
    svc.revoked = [r for r in svc.revoked if r["public_id"] != PID]
    svc.pubs = [publication(approved_at="2026-11-19T09:00:00+00:00")]
    res = run(con, svc)
    assert res["updated"] == 1 and con.execute("SELECT status FROM posts WHERE public_id=?", (PID,)).fetchone()[0] == "published"


def test_export_today_defaults_to_newest_post(env, tmp_path, monkeypatch):
    con, svc, _, _, dbp = env
    run(con, svc)
    monkeypatch.setattr(config, "TODAY_OVERRIDE", None)
    res = export_static(tmp_path / "a", dbp)
    assert res["today"] == "2026-11-17"                              # approved_at of the only post
    assert export_static(tmp_path / "b", dbp, today="2026-11-20")["today"] == "2026-11-20"
    assert config.TODAY_OVERRIDE is None


# --- start source ("Startbild aus eigener Simulation") -------------------------------------------------

def _export_gen(env, tmp_path):
    con, _, _, _, dbp = env
    con.commit()
    out = export_static(tmp_path / "site", dbp, today="2026-11-17")
    assert out["leaks"] == []
    feed = json.loads((tmp_path / "site/data/feed.json").read_text())["items"]
    card = next(i for i in feed if (i.get("generation") or {}).get("public_id") == PID)
    return card["generation"], json.loads((tmp_path / f"site/data/gen/{PID}.json").read_text())


def _record(con):
    return json.loads(con.execute("SELECT generation_json FROM posts WHERE public_id=?", (PID,)).fetchone()[0])


SIM_SUMMARY = json.loads((Path(__file__).parent / "fixtures/publication_summary.json").read_text(encoding="utf-8"))


def _summary(**kw):
    """A compose summary shaped like the service's API.md example (fixtures/publication_summary.json)."""
    d = {k: v for k, v in SIM_SUMMARY.items() if k != "_note"}
    return {**d, "public_id": PID, "character": "maehbot", "media": [], "links": {}, **kw}


def test_start_source_summary_top_level_reaches_card_and_gen(env, tmp_path):
    con, svc, *_ = env
    svc.summaries[PID] = _summary()
    assert run(con, svc)["new"] == 1
    rec = _record(con)
    assert rec["start_source_sim"] is True and rec["start_source"] == "isaac-sim"   # the sanitizer keeps the value
    card_gen, gen = _export_gen(env, tmp_path)
    assert card_gen == {"public_id": PID, "model_label": "Compose", "start_source_sim": True,   # card: boolean only
                        "credits": []}                                     # (+ the always-present credits list)
    assert gen["start_source_sim"] is True and gen["start_source"] == "isaac-sim"


def test_start_source_segment_only(env, tmp_path):
    con, svc, *_ = env
    svc.summaries[PID] = _summary(start_source=None)                 # only composition.segments[1] says so
    run(con, svc)
    card_gen, gen = _export_gen(env, tmp_path)
    assert card_gen["start_source_sim"] is True and gen["start_source_sim"] is True


def test_start_source_null_gives_no_note(env, tmp_path):
    con, svc, *_ = env
    segs = [{**s, "start_source": None} for s in SIM_SUMMARY["composition"]["segments"]]
    svc.summaries[PID] = _summary(start_source=None, composition={**SIM_SUMMARY["composition"], "segments": segs})
    run(con, svc)
    assert "start_source_sim" not in _record(con) and "start_source" not in _record(con)
    card_gen, gen = _export_gen(env, tmp_path)
    assert "start_source_sim" not in card_gen and "start_source_sim" not in gen


@pytest.mark.parametrize("labels", [{"start_source": "isaac-sim"}, {"start_source": "blender"}, None])
def test_start_source_labels_in_publications_are_ignored(env, tmp_path, labels):
    con, svc, *_ = env
    if labels is not None:
        svc.pubs = [{**publication(), "labels": labels}]
        svc.summaries[PID] = {**SUMMARY, "labels": labels, "parents": [{"labels": labels}],
                              "composition": {"segments": [{"labels": labels, "parents": [{"labels": labels}]}]}}
    run(con, svc)
    assert "start_source_sim" not in _record(con)
    card_gen, gen = _export_gen(env, tmp_path)
    assert "start_source_sim" not in card_gen and "start_source_sim" not in gen


def test_start_source_flag_is_dropped_when_field_goes_away(env):
    con, svc, *_ = env
    svc.summaries[PID] = _summary()
    run(con, svc)
    assert _record(con)["start_source_sim"] is True
    segs = [{**s, "start_source": None} for s in SIM_SUMMARY["composition"]["segments"]]
    svc.summaries[PID] = _summary(start_source=None, composition={**SIM_SUMMARY["composition"], "segments": segs})
    svc.pubs = [{**publication(), "approved_at": "2026-11-18T17:00:00+00:00"}]
    assert run(con, svc, full=True)["updated"] == 1
    assert "start_source_sim" not in _record(con)
