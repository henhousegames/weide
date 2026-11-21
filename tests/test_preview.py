"""Private preview (`weide serve --preview`): auth everywhere, read-only, three kinds of cards."""
import asyncio
import hashlib
import json
import logging
import socket
import stat
import subprocess
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from weide import cli, config, db, preview, preview_auth
from weide.importer import import_public
from weide.review import ReviewSettings

from .conftest import REPO

TOKEN = "EXAMPLE-preview-token-" + "x" * 30
REVIEW_KEY = "EXAMPLE-review-key-" + "y" * 30
BASE = "http://service.test:18500"


def gen(gid, status="ready_for_review", **kw):
    d = {"id": gid, "status": status, "task": "t2v", "model": "ltx-2.5-distilled", "labels": {},
         "prompt_original": f"prompt of {gid}", "created_at": "2026-11-20T09:00:00Z",
         "finished_at": "2026-11-20T10:00:00Z", "output": {"kind": "video", "mime": "video/mp4"},
         "media": {"output": f"/api/v1/review/generations/{gid}/media/output",
                   "poster": f"/api/v1/review/generations/{gid}/media/poster"},
         "publication": None, "lab_path": f"#/g/{gid}"}
    d.update(kw)
    return d


def pub(public_id, **kw):
    return {"public_id": public_id, "character": "lisa", "caption": "Bald live", "approved_at": "2026-11-20T12:00:00Z",
            "revoked_at": None, **kw}


GENERATIONS = [
    gen("g_draft1", labels={"character": "lisa", "caption": "Lisa testet den Melkroboter"},
        finished_at="2026-11-20T11:00:00Z"),
    gen("g_draft2", finished_at="2026-11-19T10:00:00Z"),                          # no character, no caption
    gen("g_draft3", labels={"character": "nobody-xyz"}),                          # unknown character
    gen("g_appr", publication=pub("pubNEW")),
    gen("g_live", publication=pub("pubLIVE")),                                   # already a published post
    gen("g_revoked", publication=pub("pubOLD", revoked_at="2026-11-20T13:00:00Z"),
        labels={"caption": "Zurückgezogen"}),
    gen("g_fail", status="failed", labels={"caption": "FAILED-CAPTION"}),
    gen("g_cancel", status="cancelled", publication=pub("pubCANCEL"), labels={"caption": "CANCELLED-CAPTION"}),
    gen("g_running", status="running", labels={"caption": "RUNNING-CAPTION"}),
]
VIDEO = bytes(range(256)) * 4


class FakeService:
    def __init__(self, items=None):
        self.items = list(GENERATIONS if items is None else items)
        self.requests: list[httpx.Request] = []
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {REVIEW_KEY}":
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if self.fail:
            return httpx.Response(503)
        path = request.url.path
        if path == "/api/v1/review/generations":
            return httpx.Response(200, json={"items": self.items, "next_since": "cursor-1"})
        if path.endswith("/media/output"):
            rng = request.headers.get("range")
            if rng:
                a, b = rng.removeprefix("bytes=").split("-")
                a, b = int(a), int(b or len(VIDEO) - 1)
                return httpx.Response(206, content=VIDEO[a:b + 1], headers={
                    "content-type": "video/mp4", "content-range": f"bytes {a}-{b}/{len(VIDEO)}",
                    "accept-ranges": "bytes", "x-internal": "nope"})
            return httpx.Response(200, content=VIDEO, headers={"content-type": "video/mp4", "accept-ranges": "bytes"})
        if path.endswith("/media/poster"):
            return httpx.Response(200, content=b"\xff\xd8poster", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)


@pytest.fixture
def henhouse_db(db_path):
    con = db.connect(db_path)
    import_public(con, REPO)
    lisa = con.execute("SELECT id FROM accounts WHERE slug='lisa'").fetchone()[0]
    db.upsert_post(con, account_id=lisa, kind="text", caption="Schon live", posted_at="2026-11-18T10:00:00+01:00",
                   source="generation:run-live", status="published", public_id="pubLIVE",
                   generation={"model_label": "Model X"})
    con.commit()
    con.close()
    return db_path


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def app(henhouse_db, service, monkeypatch):
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    a = preview.create_preview_app(henhouse_db, token=TOKEN, settings=ReviewSettings(BASE, REVIEW_KEY),
                                   transport=httpx.MockTransport(service), poll_interval=None)
    asyncio.run(a.state.store.poll_once())
    return a


def login(c: TestClient, token: str = TOKEN) -> httpx.Response:
    return c.post("/login", data={"token": token}, follow_redirects=False)


@pytest.fixture
def client(app):
    c = TestClient(app, base_url="http://preview-host.example:18510")
    assert login(c).status_code == 303
    return c


def by_id(items):
    return {i["id"]: i for i in items}


# --- auth --------------------------------------------------------------------------------------------

PROTECTED = ["/api/feed", "/api/meta", "/api/accounts", "/api/accounts/lisa", "/api/posts/1", "/api/top?day=2026-11-17",
             "/api/preview/status", "/api/posts/vs-g_draft1", "/media/e001-v3/out/single/02-pia-umfrage.mp4",
             "/thumb/540/e001-v3/out/single/02-pia-umfrage.mp4", "/avatar/lisa.jpg", "/static/js/api.js",
             "/static/app.css", "/preview/static/preview.js", "/preview/media/g_draft1/output", "/openapi.json"]


def test_auth_required_on_every_route_group(app):
    c = TestClient(app)
    for path in PROTECTED:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 401, path
        assert TOKEN not in r.text
    for page in ("/", "/api/docs", "/anything"):
        r = c.get(page, follow_redirects=False, headers={"Accept": "text/html"})
        assert r.status_code in (303, 401) and (r.status_code == 401 or r.headers["location"] == "/login"), page
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    # the token is never accepted from a query string, a header or as the raw cookie value
    assert c.get(f"/api/feed?token={TOKEN}").status_code == 401
    assert c.get("/api/feed", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    c.cookies.set(preview_auth.COOKIE, TOKEN)
    assert c.get("/api/feed").status_code == 401


def test_login_sets_strict_httponly_cookie(app):
    c = TestClient(app)
    page = c.get("/login")
    assert page.status_code == 200 and 'name="token"' in page.text
    assert login(c, "wrong").status_code == 401
    r = login(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/" in low and "secure" not in low
    assert f"max-age={365 * 24 * 3600}" in low
    value = cookie.split(";")[0].split("=", 1)[1]
    assert value == preview_auth.session_value(TOKEN) and TOKEN not in cookie
    assert c.get("/api/feed").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200


def test_failed_logins_are_rate_limited(app):
    c = TestClient(app)
    for _ in range(preview_auth.FAIL_PER_CLIENT):
        assert login(c, "wrong").status_code == 401
    assert login(c).status_code == 429            # even the right token, until the window passes


def test_limiter_window():
    lim = preview_auth.FailLimiter(per_client=2, overall=3, window=10)
    lim.fail("a", now=0)
    lim.fail("a", now=1)
    assert lim.blocked("a", now=2) and not lim.blocked("b", now=2)
    assert not lim.blocked("a", now=11.5)
    lim.fail("b", now=12)
    lim.fail("c", now=12)
    lim.fail("d", now=12)
    assert lim.blocked("e", now=13)               # global cap


# --- read-only ---------------------------------------------------------------------------------------

def test_no_write_endpoint_succeeds_and_db_is_unchanged(app, henhouse_db):
    before = sha(henhouse_db)
    c = TestClient(app)
    login(c)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = c.request(method, "/api/posts/1/react", json={"type": "lol"})
        assert r.status_code in (403, 405), method
    for path in ("/", "/api/feed?limit=50", "/api/meta", "/api/accounts", "/api/accounts/lisa", "/api/posts/1",
                 "/api/top?day=2026-11-17", "/api/posts/vs-g_draft1", "/api/accounts/studio-entwurf",
                 "/preview/media/g_draft1/output", "/api/preview/status"):
        assert c.get(path).status_code == 200, path
    asyncio.run(app.state.store.poll_once())
    assert sha(henhouse_db) == before


def test_readonly_app_refuses_reactions_and_never_migrates(henhouse_db, tmp_path):
    from weide.server import create_app
    before = sha(henhouse_db)
    c = TestClient(create_app(henhouse_db, readonly=True))
    assert c.post("/api/posts/1/react", json={"type": "lol"}).status_code == 403
    assert c.get("/api/feed").status_code == 200
    assert sha(henhouse_db) == before
    with pytest.raises(FileNotFoundError):
        create_app(tmp_path / "missing.db", readonly=True)
    assert not (tmp_path / "missing.db").exists()
    con = db.connect_readonly(henhouse_db)
    with pytest.raises(Exception):
        con.execute("UPDATE posts SET caption='x'")
    con.close()


def test_preview_never_calls_publish_export_or_deploy(henhouse_db, service, monkeypatch):
    from weide import deploy, export_static, publish

    def boom(*a, **k):
        raise AssertionError("preview must not publish, export or deploy")
    for mod, names in ((publish, ("pull_approved", "ServiceClient", "ServiceSettings")),
                       (export_static, ("export_static",)), (deploy, ("deploy",))):
        for n in names:
            monkeypatch.setattr(mod, n, boom)
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    a = preview.create_preview_app(henhouse_db, token=TOKEN, settings=ReviewSettings(BASE, REVIEW_KEY),
                                   transport=httpx.MockTransport(service), poll_interval=None)
    asyncio.run(a.state.store.poll_once())
    c = TestClient(a)
    login(c)
    assert c.get("/api/feed?limit=50").status_code == 200
    assert c.get("/preview/media/g_appr/output").status_code == 200


def test_preview_modules_do_not_import_publishing_code():
    code = ("import sys, weide.preview, weide.preview_auth, weide.review; "
            "bad = [m for m in ('weide.publish', 'weide.deploy', 'weide.export_static') if m in sys.modules]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_publish_key_is_dropped(henhouse_db, monkeypatch):
    monkeypatch.setenv("HENHOUSE_SERVICE_KEY", "EXAMPLE-publish-key")
    monkeypatch.setenv("WEIDE_SERVICE_KEY", "EXAMPLE-publish-key")
    preview.create_preview_app(henhouse_db, token=TOKEN, settings=None, poll_interval=None)
    import os
    assert "HENHOUSE_SERVICE_KEY" not in os.environ and "WEIDE_SERVICE_KEY" not in os.environ


# --- content -----------------------------------------------------------------------------------------

def test_feed_has_three_kinds_with_markers(client):
    items = client.get("/api/feed?limit=100").json()["items"]
    ids = by_id(items)
    appr, d1, d2, d3, rev = (ids[f"vs-{g}"] for g in ("g_appr", "g_draft1", "g_draft2", "g_draft3", "g_revoked"))
    assert appr["preview"]["state"] == "approved" and appr["preview"]["label"] == "FREIGEGEBEN – noch nicht live"
    for d in (d1, d2, d3, rev):
        assert d["preview"]["state"] == "draft" and d["preview"]["label"] == "ENTWURF"
    published = [i for i in items if not str(i["id"]).startswith("vs-")]
    assert len(published) == 41 and all("preview" not in i for i in published)
    blob = json.dumps(items)
    for hidden in ("g_fail", "g_cancel", "g_running", "g_live", "FAILED-CAPTION", "CANCELLED-CAPTION",
                   "RUNNING-CAPTION"):
        assert hidden not in blob, hidden
    # approved + already published shows up once, as the normal post
    assert sum(1 for i in items if i["caption"] == "Schon live") == 1
    # newest first, preview cards interleaved with published posts by time
    times = [i["posted_at"] for i in items]
    from weide.db import parse_time
    assert [parse_time(t) for t in times] == sorted((parse_time(t) for t in times), reverse=True)
    assert items[0]["id"] == "vs-g_appr"


def test_author_and_caption_fallbacks(client):
    ids = by_id(client.get("/api/feed?limit=100").json()["items"])
    d1, d2, d3 = ids["vs-g_draft1"], ids["vs-g_draft2"], ids["vs-g_draft3"]
    assert d1["account"]["slug"] == "lisa" and d1["caption"] == "Lisa testet den Melkroboter"
    assert d1["account"]["avatar_url"] == "/avatar/lisa.jpg"
    assert d2["account"]["slug"] == "studio-entwurf" and d2["account"]["display_name"] == "Studio (Entwurf)"
    assert d2["caption"] == "ltx-2.5-distilled · 19.11.2026"
    assert d3["account"]["slug"] == "studio-entwurf"
    assert ids["vs-g_appr"]["account"]["slug"] == "lisa" and ids["vs-g_appr"]["caption"] == "Bald live"
    assert ids["vs-g_revoked"]["caption"] == "Zurückgezogen"
    studio = client.get("/api/accounts/studio-entwurf").json()
    assert {p["id"] for p in studio["posts"]} == {"vs-g_draft2", "vs-g_draft3", "vs-g_revoked"}
    # pseudo account is not in henhouse.db
    assert "studio-entwurf" not in {a["slug"] for a in client.get("/api/accounts").json()["items"]}


def test_lab_link_uses_request_host(client):
    d = by_id(client.get("/api/feed?limit=100").json()["items"])["vs-g_draft1"]
    assert d["preview"]["lab_url"] == "http://preview-host.example:18500/#/g/g_draft1"
    other = client.get("/api/feed?limit=100", headers={"Host": "127.0.0.1:18510"}).json()["items"]
    assert by_id(other)["vs-g_draft1"]["preview"]["lab_url"] == "http://127.0.0.1:18500/#/g/g_draft1"
    assert d["media_url"] == "/preview/media/g_draft1/output" and d["poster_url"] == "/preview/media/g_draft1/poster"
    assert d["media_kind"] == "video" and d["kind"] == "reel"


def test_feed_filters_and_pagination(client):
    allitems = client.get("/api/feed?limit=100").json()["items"]
    pages, off = [], 0
    while off is not None:
        r = client.get(f"/api/feed?limit=7&offset={off}").json()
        pages += r["items"]
        off = r["next_offset"]
    assert [i["id"] for i in pages] == [i["id"] for i in allitems]
    lisa = client.get("/api/feed?limit=100&account=lisa").json()["items"]
    assert {"vs-g_draft1", "vs-g_appr"} <= {i["id"] for i in lisa} and all(i["account"]["slug"] == "lisa" for i in lisa)
    humans = client.get("/api/feed?limit=100&kind=human").json()["items"]
    assert not any(str(i["id"]).startswith("vs-") for i in humans)


def test_preview_post_detail(client):
    p = client.get("/api/posts/vs-g_draft1").json()
    assert p["comments"] == [] and p["moments"] == [] and p["preview"]["prompt"] == "prompt of g_draft1"
    assert client.get("/api/posts/vs-g_fail").status_code == 404
    assert client.get("/api/posts/vs-g_live").status_code == 404
    assert client.get("/api/posts/1").json()["id"] == 1


def test_media_proxy_streams_with_range_and_hides_key(client, service):
    r = client.get("/preview/media/g_draft1/output", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == VIDEO[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(VIDEO)}" and "x-internal" not in r.headers
    sent = service.requests[-1]
    assert sent.headers["range"] == "bytes=10-19" and sent.headers["authorization"] == f"Bearer {REVIEW_KEY}"
    assert client.get("/preview/media/g_draft1/output").content == VIDEO
    assert client.get("/preview/media/g_draft1/poster").headers["content-type"] == "image/jpeg"
    assert client.get("/preview/media/g_fail/output").status_code == 404
    assert client.get("/preview/media/g_draft1/manifest").status_code == 404
    assert client.get("/preview/media/unknown/output").status_code == 404


def test_key_never_in_responses_or_logs(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = []
    for path in ("/", "/api/feed?limit=100", "/api/preview/status", "/api/posts/vs-g_draft1",
                 "/api/accounts/studio-entwurf", "/preview/static/preview.js", "/preview/media/g_draft1/poster"):
        r = client.get(path)
        bodies.append(r.text + json.dumps(dict(r.headers)))
    service.fail = True
    asyncio.run(client.app.state.store.poll_once())
    bodies.append(client.get("/api/preview/status").text)
    responses = "\n".join(bodies)
    assert REVIEW_KEY not in responses and TOKEN not in responses and "service.test" not in responses
    assert REVIEW_KEY not in caplog.text and TOKEN not in caplog.text


def test_status_and_unreachable_service(client, service):
    s = client.get("/api/preview/status").json()
    assert s["connected"] and s["generations"] == 7 and s["last_poll"] and s["error"] is None
    service.fail = True
    asyncio.run(client.app.state.store.poll_once())
    s = client.get("/api/preview/status").json()
    assert s["error"] and s["generations"] == 7          # last good state is kept


def test_without_service_settings_shows_published_only(henhouse_db, monkeypatch):
    monkeypatch.delenv("HENHOUSE_REVIEW_KEY", raising=False)
    monkeypatch.delenv("WEIDE_REVIEW_KEY", raising=False)
    c = TestClient(preview.create_preview_app(henhouse_db, token=TOKEN, poll_interval=None))
    login(c)
    items = c.get("/api/feed?limit=100").json()["items"]
    assert len(items) == 41 and not any("preview" in i for i in items)
    assert c.get("/api/preview/status").json()["connected"] is False


def test_index_injects_preview_assets_but_public_export_does_not(client):
    html = client.get("/").text
    assert "/preview/static/preview.js" in html and "/preview/static/preview.css" in html and "noindex" in html
    from weide.export_static import render_index
    assert "preview" not in render_index(static=True).lower()
    assert not any("preview" in p.name.lower() for p in (REPO / "static").rglob("*"))


# --- hosts / sockets / CLI ---------------------------------------------------------------------------

def test_wildcard_hosts_are_refused():
    for bad in ("0.0.0.0", "::", "127.0.0.1,0.0.0.0", "[::]", "example.com", "*"):
        with pytest.raises(preview.PreviewConfigError):
            preview.parse_hosts(bad)
    with pytest.raises(preview.PreviewConfigError):
        preview.bind_sockets(["0.0.0.0"], 18510)
    assert preview.parse_hosts("127.0.0.1, 100.64.0.7,127.0.0.1") == ["127.0.0.1", "100.64.0.7"]
    assert preview.parse_hosts(None) == ["127.0.0.1"] and preview.parse_hosts("localhost") == ["127.0.0.1"]


def test_cli_refuses_wildcard(monkeypatch):
    monkeypatch.setattr(preview, "serve", lambda *a, **k: pytest.fail("must not serve"))
    with pytest.raises(SystemExit):
        cli.main(["serve", "--preview", "--host", "0.0.0.0"])
    monkeypatch.setenv("HENHOUSE_PREVIEW_HOSTS", "127.0.0.1,0.0.0.0")
    with pytest.raises(SystemExit):
        cli.main(["serve", "--preview"])


def test_cli_preview_uses_env_hosts_and_port(monkeypatch, db_path):
    seen = {}
    monkeypatch.setattr(preview, "serve", lambda dbp, hosts, port: seen.update(hosts=hosts, port=port))
    monkeypatch.setenv("HENHOUSE_PREVIEW_HOSTS", "127.0.0.1,100.64.0.7")
    cli.main(["serve", "--preview", "--db", str(db_path)])
    assert seen == {"hosts": ["127.0.0.1", "100.64.0.7"], "port": 18510}


class FakeSock:
    def __init__(self, family, type_):
        self.opts, self.bound, self.closed = [], None, False

    def setsockopt(self, level, opt, val):
        self.opts.append((level, opt, val))

    def bind(self, addr):
        self.bound = addr

    def listen(self, n):
        pass

    def setblocking(self, flag):
        pass

    def close(self):
        self.closed = True


def test_bind_sockets_freebind_only_for_non_loopback():
    socks = preview.bind_sockets(["127.0.0.1", "100.64.0.7"], 18510, sock_factory=FakeSock)
    freebind = (socket.IPPROTO_IP, preview.IP_FREEBIND, 1)
    assert freebind not in socks[0].opts and freebind in socks[1].opts
    assert [s.bound for s in socks] == [("127.0.0.1", 18510), ("100.64.0.7", 18510)]


def test_preview_key_command(tmp_path, capsys):
    path = tmp_path / "cfg" / "preview.key"
    cli.main(["preview-key", "--path", str(path)])
    out = capsys.readouterr().out
    token = path.read_text().strip()
    assert len(token) >= 43 and token not in out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(SystemExit):
        cli.main(["preview-key", "--path", str(path)])
    assert path.read_text().strip() == token
    cli.main(["preview-key", "--path", str(path), "--rotate"])
    new = path.read_text().strip()
    assert new != token and new not in capsys.readouterr().out
    assert preview_auth.load_key(path) == new
    path.chmod(0o644)
    with pytest.raises(preview_auth.PreviewKeyError):
        preview_auth.load_key(path)


def test_real_server_access_log_has_no_token_or_cookie(henhouse_db, monkeypatch):
    """uvicorn on a pre-bound loopback socket: the access log never contains the token or the cookie."""
    import uvicorn
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    app = preview.create_preview_app(henhouse_db, token=TOKEN, settings=None, poll_interval=None)
    sock = preview.bind_sockets(["127.0.0.1"], 0)[0]
    port = sock.getsockname()[1]
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    handler = Grab()
    for name in ("uvicorn.access", "uvicorn.error", "weide.preview"):
        logging.getLogger(name).addHandler(handler)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", access_log=True, log_config=None))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base) as h:
            assert h.get("/", follow_redirects=False).status_code == 303
            r = h.post("/login", data={"token": TOKEN}, follow_redirects=False)
            assert r.status_code == 303
            assert h.get("/api/feed").status_code == 200
            assert h.get(f"/api/meta?x={'z' * 5}").status_code == 200
    finally:
        server.should_exit = True
        t.join(10)
        for name in ("uvicorn.access", "uvicorn.error", "weide.preview"):
            logging.getLogger(name).removeHandler(handler)
    blob = "\n".join(records)
    assert "/api/feed" in blob                           # the access log did record requests
    assert TOKEN not in blob and preview_auth.session_value(TOKEN) not in blob
