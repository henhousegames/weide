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


MODEL = {"id": "ltx-2.5-distilled", "display_name": "LTX-2.5 22B distilled"}
VIDEO_OUT = {"mime": "video/mp4", "width": 768, "height": 1344, "fps": 24.0, "duration_s": 5.04, "has_audio": True,
             "sha256": "a" * 64}
IMAGE_OUT = {"mime": "image/png", "width": 864, "height": 1536, "fps": None, "duration_s": None, "has_audio": False,
             "sha256": "b" * 64}


def gen(gid, status="ready_for_review", **kw):
    """One item exactly as GET /api/v1/review/generations lists it."""
    d = {"id": gid, "status": status, "created_at": "2026-11-20T09:00:00Z", "finished_at": "2026-11-20T10:00:00Z",
         "task": "t2v", "model": MODEL, "labels": {}, "prompt_original": f"prompt of {gid}", "output": VIDEO_OUT,
         "media": {"output": f"/api/v1/review/generations/{gid}/media/output",
                   "poster": f"/api/v1/review/generations/{gid}/media/poster"},
         "publication": None, "lab_path": f"/#/g/{gid}"}
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
    gen("g_live", "published", publication=pub("pubLIVE")),                      # already a published post
    gen("g_revoked", publication=pub("pubOLD", revoked_at="2026-11-20T13:00:00Z"),
        labels={"caption": "Zurückgezogen"}),
    gen("g_pubappr", "published", publication=pub("pubPUB", caption="Status published",
                                                  approved_at="2026-11-20T11:30:00Z")),
    gen("g_pubrev", "published", publication=pub("pubREV", revoked_at="2026-11-20T13:00:00Z"),
        labels={"caption": "REVOKED-PUBLISHED-CAPTION"}),                       # revoked, not ready_for_review: hidden
    gen("g_img", task="t2i", output=IMAGE_OUT, labels={"character": "lisa", "caption": "Ein Standbild"},
        finished_at="2026-11-20T08:00:00Z"),
]
VIDEO = bytes(range(256)) * 4
LISTED = {"ready_for_review", "published"}


class FakeService:
    """The review API: (finished_at, id) cursor, `since` = finished after, pages of `limit` that never end
    inside a group of equal finished_at, `has_more`; `status` other than the two listed ones is a 422."""

    def __init__(self, items=None):
        self.items = list(GENERATIONS if items is None else items)
        self.requests: list[httpx.Request] = []
        self.fail = False
        self.max_limit = 500

    def list(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        statuses = set(q.get("status", "ready_for_review,published").split(","))
        if not statuses <= LISTED:
            return httpx.Response(422, json={"error": {"code": "validation", "field": "status"}})
        since, limit = q.get("since"), min(int(q.get("limit", 100)), self.max_limit)
        rows = sorted((i for i in self.items if i["status"] in statuses and (since is None or i["finished_at"] > since)),
                      key=lambda i: (i["finished_at"], i["id"]))
        n = min(limit, len(rows))
        while 0 < n < len(rows) and rows[n]["finished_at"] == rows[n - 1]["finished_at"]:
            n += 1
        page = rows[:n]
        return httpx.Response(200, json={"items": page, "next_since": page[-1]["finished_at"] if page else since,
                                         "has_more": len(rows) > n})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {REVIEW_KEY}":
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if self.fail:
            return httpx.Response(503)
        path = request.url.path
        if path == "/api/v1/review/generations":
            return self.list(request)
        gid = path.split("/")[5] if path.startswith("/api/v1/review/generations/") else None
        if gid not in {i["id"] for i in self.items}:
            return httpx.Response(404)
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

    def set(self, gid, **kw):
        for i, it in enumerate(self.items):
            if it["id"] == gid:
                self.items[i] = {**it, **kw}


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    """Key, config and link files of the tests live in a temp dir, never in the real home."""
    d = tmp_path / "henhouse-config"
    monkeypatch.setenv("HENHOUSE_PREVIEW_KEY_FILE", str(d / "preview.key"))
    for name in ("HENHOUSE_PREVIEW_CONFIG", "HENHOUSE_PREVIEW_LINKS_FILE", "HENHOUSE_PREVIEW_HOSTS",
                 "HENHOUSE_PREVIEW_PORT"):
        monkeypatch.delenv(name, raising=False)
    return d


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


def origin(c) -> dict:
    return {"Origin": str(c.base_url).rstrip("/")}


def login(c: TestClient, token: str = TOKEN) -> httpx.Response:
    return c.post("/login", data={"token": token}, follow_redirects=False, headers=origin(c))


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
    assert page.status_code == 200 and 'name="token"' not in page.text and "/login/token" not in page.text
    assert 'name="token"' in c.get("/login/token").text            # hidden fallback form
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
    assert ids["vs-g_pubappr"]["preview"]["state"] == "approved"
    for hidden in ("g_live", "g_pubrev", "REVOKED-PUBLISHED-CAPTION"):
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
    assert d2["caption"] == "LTX-2.5 22B distilled · 19.11.2026"
    assert d2["preview"]["model"] == "LTX-2.5 22B distilled"
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


def _clocked_app(henhouse_db, service, now):
    a = preview.create_preview_app(henhouse_db, token=TOKEN, settings=ReviewSettings(BASE, REVIEW_KEY),
                                   transport=httpx.MockTransport(service), poll_interval=None)
    a.state.store.clock = lambda: now[0]
    return a


def _states(c) -> dict:
    return {i["id"]: i["preview"]["state"] for i in c.get("/api/feed?limit=100").json()["items"] if "preview" in i}


def _list_requests(service):
    return [r for r in service.requests if r.url.path == "/api/v1/review/generations"]


def test_paging_follows_has_more(henhouse_db, service):
    service.max_limit = 2
    a = _clocked_app(henhouse_db, service, [0.0])
    asyncio.run(a.state.store.poll_once())
    assert set(a.state.store.items) == {g["id"] for g in GENERATIONS}
    reqs = _list_requests(service)
    assert "since" not in reqs[0].url.params and all("since" in r.url.params for r in reqs[1:])
    assert all("status" not in r.url.params for r in reqs)          # default: both listed statuses
    # the second page grows past the limit instead of splitting the six items finished at 10:00
    assert [r.url.params.get("since") for r in reqs] == [None, "2026-11-20T08:00:00Z", "2026-11-20T10:00:00Z"]
    assert a.state.store.cursor == "2026-11-20T11:00:00Z"


def test_cursor_that_does_not_advance_is_an_error(henhouse_db):
    def stuck(request):
        return httpx.Response(200, json={"items": [gen("g_x")], "next_since": request.url.params.get("since"),
                                         "has_more": True})
    a = _failing_app(henhouse_db, stuck)
    asyncio.run(a.state.store.poll_once())
    assert a.state.store.state == "down" and "cursor" in a.state.store.error


def test_incremental_polls_and_full_reload_every_10_minutes(henhouse_db, service):
    now = [1000.0]
    a = _clocked_app(henhouse_db, service, now)
    store = a.state.store
    asyncio.run(store.poll_once())
    c = TestClient(a)
    login(c)
    assert _states(c)["vs-g_draft1"] == "draft" and _states(c)["vs-g_appr"] == "approved"
    # new item: picked up by the next incremental poll (since = cursor)
    service.items.append(gen("g_new", finished_at="2026-11-20T14:00:00Z"))
    # later approval / revocations of items already seen: not reported through the cursor
    service.set("g_draft1", publication=pub("pubD1", approved_at="2026-11-20T15:00:00Z"))
    service.set("g_appr", publication=pub("pubNEW", revoked_at="2026-11-20T15:00:00Z"))
    service.set("g_pubappr", publication=pub("pubPUB", revoked_at="2026-11-20T15:00:00Z"))
    service.items = [i for i in service.items if i["id"] != "g_draft3"]
    now[0] += 60
    asyncio.run(store.poll_once())
    assert _list_requests(service)[-1].url.params["since"] == "2026-11-20T11:00:00Z"
    st = _states(c)
    assert st["vs-g_new"] == "draft" and st["vs-g_draft1"] == "draft" and st["vs-g_appr"] == "approved"
    assert "vs-g_draft3" in st
    now[0] += preview.FULL_RELOAD_SECONDS - 61                         # still incremental just before 10 min
    asyncio.run(store.poll_once())
    assert "since" in _list_requests(service)[-1].url.params and _states(c)["vs-g_draft1"] == "draft"
    now[0] += 1                                                        # 10 min after the last full reload
    asyncio.run(store.poll_once())
    assert "since" not in _list_requests(service)[-1].url.params
    st = _states(c)
    assert st["vs-g_draft1"] == "approved"                            # later approval
    assert st["vs-g_appr"] == "draft"                                 # revoked, ready_for_review: draft again
    assert "vs-g_pubappr" not in st                                   # revoked, status published: hidden
    assert "vs-g_draft3" not in st and "vs-g_new" in st               # gone from the list: dropped
    # the next poll is incremental again
    now[0] += 60
    asyncio.run(store.poll_once())
    assert _list_requests(service)[-1].url.params["since"] == "2026-11-20T14:00:00Z"


def test_image_item_renders_as_image_post(client):
    d = by_id(client.get("/api/feed?limit=100").json()["items"])["vs-g_img"]
    assert d["kind"] == "image" and d["media_kind"] == "image" and d["caption"] == "Ein Standbild"
    assert d["media_url"] == "/preview/media/g_img/output" and d["poster_url"] == "/preview/media/g_img/poster"
    assert d["preview"]["task"] == "t2i" and d["preview"]["lab_url"] == "http://preview-host.example:18500/#/g/g_img"
    it = client.app.state.store.items["g_img"]
    assert it.duration_s is None and it.has_audio is False and (it.width, it.height) == (864, 1536)


def test_approved_is_not_duplicated_when_already_published(client, service):
    items = client.get("/api/feed?limit=100").json()["items"]
    assert "vs-g_live" not in by_id(items) and sum(1 for i in items if i["caption"] == "Schon live") == 1
    # also when the generation is still ready_for_review: the public_id decides
    service.items.append(gen("g_live2", publication=pub("pubLIVE"), finished_at="2026-11-20T16:00:00Z"))
    service.set("g_pubappr", publication=pub("pubLIVE", caption="Status published"))
    client.app.state.store.cursor = None
    asyncio.run(client.app.state.store.poll_once())
    st = _states(client)
    assert "vs-g_live2" not in st and "vs-g_pubappr" not in st and st["vs-g_appr"] == "approved"


def test_bad_lab_path_falls_back_to_generation_id(henhouse_db):
    svc = FakeService([gen("g_odd", lab_path="javascript:alert(1)")])
    a = _clocked_app(henhouse_db, svc, [0.0])
    asyncio.run(a.state.store.poll_once())
    c = TestClient(a, base_url="http://preview-host.example:18510")
    login(c)
    d = by_id(c.get("/api/feed?limit=100").json()["items"])["vs-g_odd"]
    assert d["preview"]["lab_url"] == "http://preview-host.example:18500/#/g/g_odd"


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
    assert client.get("/api/posts/vs-g_pubrev").status_code == 404
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
    assert client.get("/preview/media/g_missing/output").status_code == 404
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
    assert s["connected"] and s["generations"] == 9 and s["last_poll"] and s["error"] is None
    service.fail = True
    asyncio.run(client.app.state.store.poll_once())
    s = client.get("/api/preview/status").json()
    assert s["error"] and s["generations"] == 9          # last good state is kept


def test_without_service_settings_shows_published_only(henhouse_db, monkeypatch):
    monkeypatch.delenv("HENHOUSE_REVIEW_KEY", raising=False)
    monkeypatch.delenv("WEIDE_REVIEW_KEY", raising=False)
    c = TestClient(preview.create_preview_app(henhouse_db, token=TOKEN, poll_interval=None))
    login(c)
    items = c.get("/api/feed?limit=100").json()["items"]
    assert len(items) == 41 and not any("preview" in i for i in items)
    assert c.get("/api/preview/status").json()["connected"] is False


def _no_service_env(monkeypatch):
    for prefix in ("HENHOUSE_", "WEIDE_"):
        for name in ("REVIEW_KEY", "SERVICE_URL"):
            monkeypatch.delenv(prefix + name, raising=False)


def test_no_review_key_starts_cleanly_without_poller(henhouse_db, monkeypatch, caplog):
    _no_service_env(monkeypatch)
    monkeypatch.setenv("HENHOUSE_SERVICE_URL", BASE)
    monkeypatch.setenv("HENHOUSE_SERVICE_KEY", "EXAMPLE-publish-key-" + "p" * 20)
    started = []
    real_task = asyncio.create_task
    monkeypatch.setattr(preview.asyncio, "create_task", lambda *a, **k: started.append(a) or real_task(*a, **k))
    caplog.set_level(logging.DEBUG, logger="weide.preview")
    a = preview.create_preview_app(henhouse_db, token=TOKEN)            # default poll interval
    assert a.state.store.client is None
    warnings = [r.getMessage() for r in caplog.records if "studio connection" in r.getMessage()]
    assert len(warnings) == 1 and "HENHOUSE_REVIEW_KEY missing" in warnings[0]
    assert "p" * 20 not in caplog.text and BASE not in caplog.text    # no key, no URL
    with TestClient(a) as c:                                            # runs the lifespan
        login(c)
        s = c.get("/api/preview/status").json()
        assert s["connected"] is False and s["studio"] == "off"
        assert s["notice"] == "Studio-Verbindung fehlt – Entwürfe und Freigaben nicht sichtbar"
        items = c.get("/api/feed?limit=100").json()["items"]
        assert len(items) == 41 and not any("preview" in i for i in items)
        assert c.get("/preview/media/g_draft1/output").status_code == 404
    assert started == []                                                # no polling task
    assert asyncio.run(asyncio.wait_for(a.state.store.run(0.01), 1)) is None   # an idle poller returns


def test_invalid_service_url_does_not_crash(henhouse_db, monkeypatch, caplog):
    _no_service_env(monkeypatch)
    monkeypatch.setenv("HENHOUSE_SERVICE_URL", "ftp://somewhere")
    monkeypatch.setenv("HENHOUSE_REVIEW_KEY", REVIEW_KEY)
    a = preview.create_preview_app(henhouse_db, token=TOKEN, poll_interval=None)
    assert a.state.store.state == "off" and REVIEW_KEY not in caplog.text


def _failing_app(henhouse_db, handler):
    return preview.create_preview_app(henhouse_db, token=TOKEN, settings=ReviewSettings(BASE, REVIEW_KEY),
                                      transport=httpx.MockTransport(handler), poll_interval=None)


def test_service_down_shows_notice_and_retries_with_backoff(henhouse_db, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def down(request):
        calls.append(request)
        raise httpx.ConnectError("connection refused")
    a = _failing_app(henhouse_db, down)
    store, delays = a.state.store, []

    async def sleep(d):
        delays.append(d)
        if len(delays) == 5:
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(store.run(60, sleep=sleep))
    assert len(calls) == 5 and delays == [5, 10, 20, 40, 80]
    c = TestClient(a)
    login(c)
    s = c.get("/api/preview/status").json()
    assert s["studio"] == "down" and s["notice"].startswith("Studio nicht erreichbar")
    assert len(c.get("/api/feed?limit=100").json()["items"]) == 41      # still serving published posts
    assert REVIEW_KEY not in caplog.text and "service.test" not in caplog.text
    assert store.retry_delay(60) == 80 and (setattr(store, "failures", 99) or store.retry_delay(60)) == preview.BACKOFF_MAX


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_shows_notice_and_logs_status_only(henhouse_db, caplog, code):
    caplog.set_level(logging.DEBUG)
    a = _failing_app(henhouse_db, lambda r: httpx.Response(code, json={"detail": "bad key " + REVIEW_KEY}))
    asyncio.run(a.state.store.poll_once())
    c = TestClient(a)
    login(c)
    s = c.get("/api/preview/status").json()
    assert s["studio"] == "auth" and s["notice"] and "Studio" in s["notice"]
    assert f"HTTP {code}" in caplog.text and REVIEW_KEY not in caplog.text and REVIEW_KEY not in json.dumps(s)


def test_recovery_clears_notice(client, service):
    service.fail = True
    asyncio.run(client.app.state.store.poll_once())
    assert client.get("/api/preview/status").json()["studio"] == "down"
    service.fail = False
    asyncio.run(client.app.state.store.poll_once())
    s = client.get("/api/preview/status").json()
    assert s["studio"] == "ok" and s["notice"] is None and client.app.state.store.retry_delay(60) == 60


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
            r = h.post("/login", data={"token": TOKEN}, follow_redirects=False, headers={"Origin": base})
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


# --- owner auto-login over Tailscale ------------------------------------------------------------------

from weide import preview_login  # noqa: E402

OWNER = "owner@example.com"
TS_IP = "100.101.102.103"


class FakeWhois:
    def __init__(self, logins: dict[str, str] | None = None, error: bool = False):
        self.logins, self.error, self.calls = logins or {}, error, []

    def __call__(self, ip):
        self.calls.append(ip)
        if self.error:
            raise TimeoutError("whois timed out")
        name = self.logins.get(ip)
        return {"Node": {"Name": "x"}, "UserProfile": {"LoginName": name}} if name else None


def auto_app(henhouse_db, whois, owners=(OWNER,), tmp=None):
    return preview.create_preview_app(henhouse_db, token=TOKEN, settings=None, poll_interval=None,
                                      auto=preview_login.OwnerAutoLogin(set(owners), whois=whois),
                                      links=preview_login.LinkStore(tmp / "links.json") if tmp else None)


def test_owner_on_tailscale_is_logged_in_automatically(henhouse_db):
    whois = FakeWhois({TS_IP: OWNER})
    c = TestClient(auto_app(henhouse_db, whois), client=(TS_IP, 40000))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200 and "preview.js" in r.text
    cookie = r.headers["set-cookie"]
    assert preview_auth.session_value(TOKEN) in cookie and "samesite=strict" in cookie.lower()
    assert c.get("/api/feed").status_code == 200
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"
    assert whois.calls == [TS_IP]                    # one whois; afterwards the cookie is enough


def test_non_owner_and_whois_errors_are_denied(henhouse_db):
    for whois in (FakeWhois({TS_IP: "someone-else@example.com"}), FakeWhois(error=True), FakeWhois({}),
                  FakeWhois({TS_IP: "tagged-devices"})):
        c = TestClient(auto_app(henhouse_db, whois), client=(TS_IP, 40000))
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        assert c.get("/api/feed").status_code == 401
        assert "set-cookie" not in c.get("/login").headers


def test_loopback_and_spoofed_headers_never_auto_login(henhouse_db):
    whois = FakeWhois({TS_IP: OWNER, "127.0.0.1": OWNER})
    spoof = {"X-Forwarded-For": TS_IP, "X-Real-IP": TS_IP, "Forwarded": f"for={TS_IP}"}
    c = TestClient(auto_app(henhouse_db, whois), client=("127.0.0.1", 40000))
    assert c.get("/api/feed", headers=spoof).status_code == 401
    other = TestClient(auto_app(henhouse_db, whois), client=("192.168.1.20", 40000))
    assert other.get("/api/feed", headers=spoof).status_code == 401
    assert whois.calls == []                         # whois is not even asked


def test_tailscale_ip_ranges():
    assert preview_login.tailscale_ip("100.64.0.1") == "100.64.0.1"
    assert preview_login.tailscale_ip("100.127.255.254") == "100.127.255.254"
    assert preview_login.tailscale_ip("::ffff:100.64.0.1") == "100.64.0.1"
    assert preview_login.tailscale_ip("fd7a:115c:a1e0::1") == "fd7a:115c:a1e0::1"
    for no in ("127.0.0.1", "::1", "100.128.0.1", "10.0.0.1", "fd7a:115c:a1e1::1", "testclient", None):
        assert preview_login.tailscale_ip(no) is None, no


def test_whois_cache_ttl():
    now = [0.0]
    whois = FakeWhois({TS_IP: OWNER})
    auto = preview_login.OwnerAutoLogin({OWNER}, whois=whois, ttl=60, clock=lambda: now[0])
    assert auto.grants(TS_IP) and auto.grants(TS_IP) and len(whois.calls) == 1
    whois.logins = {}
    now[0] = 59
    assert auto.grants(TS_IP) and len(whois.calls) == 1
    now[0] = 61
    assert not auto.grants(TS_IP) and len(whois.calls) == 2
    assert not preview_login.OwnerAutoLogin(set(), whois=whois).grants(TS_IP)   # no owners configured


def test_owner_config_file(private_config):
    path = private_config / "preview.toml"
    private_config.mkdir(parents=True, exist_ok=True)
    path.write_text('owner_tailscale_logins = ["owner@example.com", " "]\n')
    path.chmod(0o600)
    assert preview_login.owner_logins() == {OWNER}
    path.chmod(0o644)
    assert preview_login.owner_logins() == set()        # readable by others: ignored
    path.chmod(0o600)
    path.write_text("not = [toml")
    assert preview_login.owner_logins() == set()
    path.unlink()
    assert preview_login.owner_logins() == set()


def test_run_whois_uses_argv_without_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, stdout='{"UserProfile": {"LoginName": "owner@example.com"}}')
    monkeypatch.setattr(preview_login.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(preview_login.subprocess, "run", fake_run)
    assert preview_login.run_whois(TS_IP)["UserProfile"]["LoginName"] == OWNER
    assert seen["argv"][1:] == ["whois", "--json", TS_IP] and not seen.get("shell") and seen["timeout"]

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 3)
    monkeypatch.setattr(preview_login.subprocess, "run", timeout)
    assert preview_login.run_whois(TS_IP) is None


# --- one-time login links ------------------------------------------------------------------------------

def link_login(c, nonce, headers=None):
    return c.post("/login/link", data={"nonce": nonce}, follow_redirects=False,
                  headers=origin(c) if headers is None else headers)


def test_login_link_is_single_use(henhouse_db, tmp_path, monkeypatch):
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    app = auto_app(henhouse_db, FakeWhois(), owners=(), tmp=tmp_path)
    store = preview_login.LinkStore(tmp_path / "links.json")
    nonce = store.create()
    assert nonce not in (tmp_path / "links.json").read_text()
    assert stat.S_IMODE((tmp_path / "links.json").stat().st_mode) == 0o600
    c = TestClient(app)
    r = link_login(c, nonce)
    assert r.status_code == 303 and preview_auth.session_value(TOKEN) in r.headers["set-cookie"]
    assert c.get("/api/feed").status_code == 200
    c2 = TestClient(app)
    assert link_login(c2, nonce).status_code == 401          # used up
    assert c2.get("/api/feed").status_code == 401


def test_login_link_expires(tmp_path):
    now = [1000.0]
    store = preview_login.LinkStore(tmp_path / "links.json", ttl=600, clock=lambda: now[0])
    a, b = store.create(), store.create()
    now[0] += 599
    assert store.consume(a)
    now[0] += 2
    assert not store.consume(b)
    assert not store.consume("nope") and not store.consume("")
    assert json.loads((tmp_path / "links.json").read_text()) == {"links": []}


def test_login_posts_require_same_origin(henhouse_db, tmp_path, monkeypatch):
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    app = auto_app(henhouse_db, FakeWhois(), owners=(), tmp=tmp_path)
    nonce = preview_login.LinkStore(tmp_path / "links.json").create()
    c = TestClient(app)
    for bad in ({}, {"Origin": "http://evil.example"}, {"Origin": "null"}, {"Referer": "http://evil.example/x"}):
        assert link_login(c, nonce, headers=bad).status_code == 403, bad
        assert c.post("/login", data={"token": TOKEN}, headers=bad, follow_redirects=False).status_code == 403
    assert link_login(c, nonce, headers={"Referer": "http://testserver/login"}).status_code == 303


def test_login_page_posts_fragment_nonce(app):
    html = TestClient(app).get("/login").text
    assert 'action="/login/link"' in html and "location.hash" in html and "preview-login-link" in html
    assert "hashchange" in html          # also when the link is opened on an already open login page


def test_cli_preview_login_link(tmp_path, capsys, private_config, monkeypatch):
    env = tmp_path / "preview.env"
    env.write_text("HENHOUSE_PREVIEW_HOSTS=127.0.0.1,100.64.0.7\nHENHOUSE_PREVIEW_PORT=18510\n")
    cli.main(["preview-login-link", "--env-file", str(env)])
    url = capsys.readouterr().out.strip()
    assert url.startswith("http://100.64.0.7:18510/login#link=")
    nonce = url.split("#link=", 1)[1]
    assert preview_login.LinkStore().consume(nonce) and not preview_login.LinkStore().consume(nonce)
    cli.main(["preview-login-link", "--host", "fd7a:115c:a1e0::5", "--env-file", str(tmp_path / "none")])
    assert capsys.readouterr().out.startswith("http://[fd7a:115c:a1e0::5]:18510/login#link=")
    monkeypatch.setenv("HENHOUSE_PREVIEW_HOSTS", "127.0.0.1")
    cli.main(["preview-login-link"])
    assert capsys.readouterr().out.startswith("http://127.0.0.1:18510/login#link=")
