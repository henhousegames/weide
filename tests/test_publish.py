"""`weide pull-approved` against a fake generation service (httpx.MockTransport), and `weide deploy`."""
import hashlib
import json
import logging
import shutil

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
RUN = "run-EXAMPLE-7781"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200      # header only (not playable)
SUMMARY = {
    "public_id": "gpub01", "run_id": RUN, "id": RUN, "model_label": "Model X 1.0",
    "model": {"name": "model-x", "version": "1.0", "license": "Apache-2.0"},
    "parameters": {"seed": 42, "steps": 30, "key": "first-frame"},
    "prompt": "A robot sheep reviews a lawn mower",
    "notes": f"made in {RUN} on gpu-box.local:8188 from /home/EXAMPLE/in.png",
    "api_key": "EXAMPLE_LEAKED", "output_path": "/srv/EXAMPLE/out.mp4",
}


class FakeService:
    def __init__(self):
        self.items = [{"public_id": "gpub01", "run_id": RUN, "character_slug": "maehbot",
                       "caption": "Rasenmäher-Test", "title": "Test", "approved_at": "2026-11-17T18:00:00+01:00",
                       "media_url": "/api/v1/publish/media/gpub01", "summary_url": "/api/v1/publish/summary/gpub01"}]
        self.summary = dict(SUMMARY)
        self.media = MP4
        self.media_status = 200
        self.revoked: list = []
        self.poster: bytes | None = None
        self.calls: list[str] = []
        self.auth: dict[str, str | None] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        self.auth[str(req.url)] = req.headers.get("authorization")
        if req.url.host == "svc.example.test" and req.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad key"}})
        if path == "/api/v1/publish/approved":
            return httpx.Response(200, json={"items": self.items, "revoked": self.revoked})
        if path.startswith("/api/v1/publish/poster/"):
            if self.poster is None:
                return httpx.Response(404)
            return httpx.Response(200, content=self.poster, headers={"content-type": "image/jpeg"})
        if path.startswith("/api/v1/publish/summary/"):
            return httpx.Response(200, json=self.summary)
        if path.startswith("/api/v1/publish/media/") or req.url.host == "cdn.example.test":
            if self.media_status != 200:
                return httpx.Response(self.media_status, json={"error": {"code": "not_found", "message": "gone"}})
            return httpx.Response(200, content=self.media, headers={"content-type": "video/mp4"})
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
    [p] = posts(con)
    assert p["status"] == "published" and p["public_id"] == "gpub01" and p["source"] == f"generation:{RUN}"
    assert p["kind"] == "reel" and p["media"] == "gen/gpub01.mp4" and p["caption"] == "Rasenmäher-Test"
    assert (media / "gen/gpub01.mp4").read_bytes() == MP4
    assert not [f for f in (media / "gen").iterdir() if f.name.startswith(".")]      # temp dir cleaned
    gen = json.loads(p["generation_json"])
    assert gen["model_label"] == "Model X 1.0" and gen["parameters"]["seed"] == 42

    res = run(con, svc)
    assert (res["new"], res["updated"], res["unchanged"]) == (0, 0, 1)
    assert len(posts(con)) == 1
    assert svc.calls.count("/api/v1/publish/media/gpub01") == 1        # not downloaded again


def test_caption_update(env):
    con, svc, *_ = env
    run(con, svc)
    svc.items[0]["caption"] = "Neue Caption"
    res = run(con, svc)
    assert (res["new"], res["updated"]) == (0, 1)
    [p] = posts(con)
    assert p["caption"] == "Neue Caption"


def test_dry_run_writes_nothing(env):
    con, svc, media, *_ = env
    res = run(con, svc, dry_run=True)
    assert res["new"] == 1 and posts(con) == [] and not (media / "gen/gpub01.mp4").exists()


def test_unknown_character_fails_without_post(env):
    con, svc, media, *_ = env
    svc.items[0]["character_slug"] = "nobody"
    res = run(con, svc)
    assert res["new"] == 0 and len(res["failed"]) == 1 and "unknown character" in res["failed"][0]
    assert posts(con) == [] and not (media / "gen/gpub01.mp4").exists()
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE slug='nobody'").fetchone()[0] == 0


def test_missing_media_leaves_nothing(env):
    con, svc, media, *_ = env
    svc.media_status = 404
    res = run(con, svc)
    assert len(res["failed"]) == 1 and "HTTP 404" in res["failed"][0] and posts(con) == []
    assert not (media / "gen").exists() or not list((media / "gen").iterdir())


def test_sha256_and_content_checks(env):
    con, svc, *_ = env
    svc.items[0]["sha256"] = hashlib.sha256(b"something else").hexdigest()
    res = run(con, svc)
    assert "sha256 mismatch" in res["failed"][0] and posts(con) == []
    svc.items[0]["sha256"] = hashlib.sha256(b"<html>").hexdigest()
    svc.media = b"<html>"
    res = run(con, svc)
    assert "not an MP4" in res["failed"][0] and posts(con) == []
    svc.media = MP4
    svc.items[0]["sha256"] = hashlib.sha256(MP4).hexdigest()
    assert run(con, svc)["new"] == 1


def test_sanitizer_and_run_id_scrubbed(env):
    con, svc, *_ = env
    run(con, svc)
    blob = posts(con)[0]["generation_json"]
    for bad in (RUN, "EXAMPLE_LEAKED", "/home/", "/srv/", "gpu-box", "api_key", "output_path", "gpub01"):
        assert bad not in blob, bad
    assert "A robot sheep reviews a lawn mower" in blob


def test_key_only_sent_to_service_origin(env):
    con, svc, *_ = env
    svc.items[0]["media_url"] = "https://cdn.example.test/v/gpub01.mp4?sig=abc"
    assert run(con, svc)["new"] == 1
    assert svc.auth["https://cdn.example.test/v/gpub01.mp4?sig=abc"] is None
    assert svc.auth[f"{BASE}/api/v1/publish/approved"] == f"Bearer {KEY}"


def test_key_never_in_output_or_logs(env, capsys, caplog, monkeypatch, tmp_path):
    con, svc, *_ = env
    caplog.set_level(logging.DEBUG)
    svc.items[0]["character_slug"] = "nobody"
    lines: list[str] = []
    res = run(con, svc, log=lines.append)
    text = json.dumps(res) + "\n".join(lines) + caplog.text
    assert KEY not in text and res["failed"]
    assert KEY not in repr(ServiceSettings(BASE, KEY))
    # CLI path: settings from a .env in the private dir, wrong key -> error without the key
    (tmp_path / "priv").mkdir()
    (tmp_path / "priv/.env").write_text(f"HENHOUSE_SERVICE_URL={BASE}\nHENHOUSE_SERVICE_KEY='{KEY}'\n")
    monkeypatch.setattr(config, "PRIVATE_DIR", tmp_path / "priv")
    monkeypatch.delenv("HENHOUSE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("HENHOUSE_SERVICE_URL", raising=False)
    assert read_dotenv(tmp_path / "priv/.env")["HENHOUSE_SERVICE_KEY"] == KEY
    assert ServiceSettings.load().key == KEY


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
    svc.items[0]["character_slug"] = "nobody"
    svc.items[0]["public_id"] = "gpub02"
    with pytest.raises(SystemExit) as e:
        cli.main(["pull-approved", "--db", str(dbp)])
    out = capsys.readouterr()
    assert e.value.code == 1 and KEY not in out.out + out.err


@pytest.fixture(scope="module")
def base_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("pub") / "base.db"
    con = db.connect(p)
    import_public(con, REPO)
    con.close()
    return p


def test_export_and_deploy_have_no_run_id(base_db, tmp_path, monkeypatch):
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
    res = deploy(site, dbp, settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), today="2026-11-17")
    assert res["errors"] == [] and res["changed"] and res["pull"]["new"] == 1 and res["export"]["generations"] == 1
    assert (site / "media/gen/gpub01.mp4").is_file() and (site / "data/gen/gpub01.json").is_file()
    for f in site.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".html", ".js"):
            text = f.read_text(encoding="utf-8")
            assert RUN not in text and KEY not in text, f
    again = deploy(site, dbp, settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), today="2026-11-17")
    assert again["errors"] == [] and again["changed"] is False and again["pull"]["unchanged"] == 1
    # a failing pull stops before the export
    svc.items[0]["caption"] = "geändert"
    svc.items[0]["character_slug"] = "nobody"
    bad = deploy(site, dbp, settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), today="2026-11-17")
    assert bad["errors"] and bad["export"] is None and bad["changed"] is False


def test_export_of_published_generation_post(env, tmp_path):
    con, svc, *_ = env
    run(con, svc)
    con.commit()
    out = export_static(tmp_path / "out", env[4], today="2026-11-17")
    assert out["leaks"] == [] and out["generations"] == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_poster_from_real_video(env, tmp_path):
    import subprocess
    con, svc, media, thumb, _ = env
    vid = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=96x160:d=1", "-pix_fmt", "yuv420p",
                    str(vid)], check=True)
    svc.media = vid.read_bytes()
    res = run(con, svc)
    assert res["new"] == 1 and res["items"][0]["poster"] is True
    assert (thumb / "540/gen/gpub01.jpg").stat().st_size > 0


def test_no_ffmpeg_skips_poster(env, monkeypatch):
    import weide.publish as pub
    con, svc, _, thumb, _ = env
    monkeypatch.setattr(pub, "ffmpeg_available", lambda: False)
    res = run(con, svc)
    assert res["new"] == 1 and res["items"][0]["poster"] is False and not (thumb / "540/gen/gpub01.jpg").exists()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100        # magic only


def test_service_poster_used_without_ffmpeg(env, monkeypatch):
    import weide.publish as pub
    con, svc, _, thumb, _ = env
    monkeypatch.setattr(pub, "ffmpeg_available", lambda: False)
    svc.items[0]["poster_url"] = "/api/v1/publish/poster/gpub01"
    svc.poster = JPEG
    res = run(con, svc)
    assert res["items"][0]["poster"] is True and (thumb / "540/gen/gpub01.jpg").read_bytes() == JPEG


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_service_poster_preferred_then_video_fallback(env, tmp_path):
    import subprocess
    con, svc, _, thumb, _ = env
    vid, img = tmp_path / "v.mp4", tmp_path / "p.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=96x160:d=1", "-pix_fmt", "yuv420p",
                    str(vid)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=1080x1920", "-frames:v", "1",
                    str(img)], check=True)
    svc.media, svc.poster = vid.read_bytes(), img.read_bytes()
    svc.items[0]["poster_url"] = "/api/v1/publish/poster/gpub01"
    assert run(con, svc)["items"][0]["poster"] is True
    assert "/api/v1/publish/poster/gpub01" in svc.calls
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                          "-of", "csv=p=0", str(thumb / "540/gen/gpub01.jpg")], capture_output=True, text=True)
    assert out.stdout.strip() == "540"                  # scaled from the service poster (the video is 96 px)
    # poster missing at the service -> frame of the video
    (thumb / "540/gen/gpub01.jpg").unlink()
    svc.poster = None
    svc.items[0]["caption"] = "neu"
    assert run(con, svc)["items"][0]["poster"] is True and (thumb / "540/gen/gpub01.jpg").is_file()


def test_malformed_item_fails_alone(env):
    con, svc, *_ = env
    good = svc.items[0]
    svc.items = ["junk", {"public_id": "bad id!", "run_id": "run-x"}, {"public_id": "gpub09"},
                 {**good, "public_id": "gpub05", "status": "weird"}, good]
    res = run(con, svc)
    assert res["new"] == 1 and len(res["failed"]) == 4 and [p["public_id"] for p in posts(con)] == ["gpub01"]


def test_revocations(env, tmp_path):
    con, svc, media, _, dbp = env
    from fastapi.testclient import TestClient
    from weide.server import create_app
    second = {**svc.items[0], "public_id": "gpub02", "run_id": "run-EXAMPLE-2"}
    svc.items.append(second)
    assert run(con, svc)["new"] == 2
    site = tmp_path / "site"
    exp = export_static(site, dbp)
    assert (site / "data/gen/gpub01.json").is_file() and (site / "media/gen/gpub01.mp4").is_file()

    svc.revoked = [{"public_id": "gpub01", "revoked_at": "2026-11-18T09:00:00+01:00"},
                   {"public_id": "gnothere", "revoked_at": "2026-11-18T09:00:00+01:00"}]
    svc.items[1] = {**second, "status": "revoked"}
    dry = run(con, svc, dry_run=True)
    assert dry["revoked"] == 2 and all(p["status"] == "published" for p in posts(con))
    res = run(con, svc)
    assert (res["revoked"], res["new"], res["failed"]) == (2, 0, [])
    assert any("gnothere" in n for n in res["notes"])
    assert {p["status"] for p in posts(con)} == {"draft"}
    assert (media / "gen/gpub01.mp4").is_file()                     # kept on disk
    again = run(con, svc)
    assert again["revoked"] == 0 and again["failed"] == []          # idempotent

    client = TestClient(create_app(dbp))
    assert client.get("/api/generations/gpub01").status_code == 404
    assert "gpub01" not in client.get("/api/feed").text
    exp = export_static(site, dbp)
    assert exp["leaks"] == [] and exp["pruned"] >= 2
    assert not (site / "data/gen/gpub01.json").exists() and not (site / "media/gen/gpub01.mp4").exists()
    assert "gpub0" not in (site / "data/feed.json").read_text()


def test_export_today_defaults_to_newest_post(env, tmp_path, monkeypatch):
    con, svc, _, _, dbp = env
    run(con, svc)
    monkeypatch.setattr(config, "TODAY_OVERRIDE", None)
    res = export_static(tmp_path / "a", dbp)
    assert res["today"] == "2026-11-17"                              # approved_at of the only post
    assert export_static(tmp_path / "b", dbp, today="2026-11-20")["today"] == "2026-11-20"
    assert config.TODAY_OVERRIDE is None
