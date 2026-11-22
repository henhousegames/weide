"""Curated feed in the private preview (HENHOUSE_FEED_DIR): only published posts + feed posts.

Synthetic fixtures only: invented characters, captions and notes."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from weide import config, preview, preview_auth
from weide.export_static import export_static
from weide.feed import BIO_MAX, FeedSource, read_front_matter
from weide.review import ReviewSettings

from .test_preview import (BASE, GENERATIONS, IMAGE_OUT, REVIEW_KEY, TOKEN, FakeService, by_id, gen, henhouse_db,  # noqa: F401
                           login, private_config)

TZ = config.TZ
BODY_MARKER = "PRIVATE-NOTES-MARKER-7f3a"
FEED_GENS = [
    gen("g_feed_v", labels={"character": "lisa", "caption": "label caption"}),
    gen("g_feed_v2"),
    gen("g_feed_i", task="t2i", output=IMAGE_OUT),
    gen("g_avatar", task="t2i", output=IMAGE_OUT),
    gen("g_feed_hidden"),
]


def line(**d) -> str:
    return json.dumps(d)


POSTS = [
    line(id="p1", generation_id="g_feed_v", author="lisa", kind="video", caption="First caption",
         posted_at="2026-11-20T09:00:00+01:00", story="Heuballen"),
    line(id="p2", generation_id="g_feed_i", author="zorro-bot", kind="image", caption="Blinkende Lichter",
         posted_at="2026-11-20T10:00:00+01:00"),
    line(id="p3", generation_id="g_feed_v2", author="teapot", kind="video", caption="Noch nicht",
         posted_at="2026-11-20T15:00:00+01:00"),                                      # future
    line(id="p4", generation_id="g_feed_hidden", author="lisa", kind="video", caption="Versteckt",
         posted_at="2026-11-20T08:00:00+01:00", hidden=True),
    line(id="p5", generation_id="g_not_there", author="lisa", kind="video", caption="Unlesbar",
         posted_at="2026-11-20T08:00:00+01:00"),                                      # generation not readable
    "this is { not json",
    line(id="p6", generation_id="g_feed_v2", author="lisa", kind="video", caption="Naiv",
         posted_at="2026-11-20T08:00:00"),                                            # naive timestamp
    line(id="p7", generation_id="g_feed_v2", author="lisa", kind="audio", caption="Falsche Art",
         posted_at="2026-11-20T08:00:00+01:00"),                                      # unknown kind
    "[1, 2, 3]",
    line(id="p1", generation_id="g_feed_v", author="lisa", kind="video", caption="Lisa im Heu",
         posted_at="2026-11-20T09:30:00+01:00", story="Heuballen"),                  # replaces the first p1
]
INTERACTIONS = [
    line(type="like", post="p1", author="ronny", at="2026-11-20T10:00:00+01:00"),
    line(type="like", post="p1", author="ronny", at="2026-11-20T10:05:00+01:00"),     # same character again
    line(type="like", post="p1", author="zorro-bot", at="2026-11-20T10:10:00+01:00"),
    line(type="comment", post="p1", author="zorro-bot", text="Piep, schönes Heu!", at="2026-11-20T10:30:00+01:00"),
    line(type="comment", post="p1", author="teapot", text="Später Kommentar", at="2026-11-20T13:00:00+01:00"),
    line(type="comment", post="p3", author="lisa", text="Zu früh", at="2026-11-20T11:00:00+01:00"),
    line(type="comment", post="p5", author="lisa", text="Unsichtbar", at="2026-11-20T11:00:00+01:00"),
    line(type="comment", post="p2", author="lisa", text="Muh!", at="2026-11-20T11:00:00+01:00"),
    line(type="comment", post="p2", author="lisa", text="Naiv", at="2026-11-20T11:00:00"),
    line(type="comment", post="p2", author="lisa", at="2026-11-20T11:00:00+01:00"),  # comment without text
    "{broken",
]


def identity(front: str) -> str:
    return f"---\n{front}---\n# Notizen\n{BODY_MARKER}\nname: Body Name\nbio: body bio\n"


IDENTITIES = {
    "lisa": identity('name: "Lisa (Feed)"\nslug: lisa\nhandle: lisa.feed\nkind: animal\n'
                     f"bio: {'Gras ' * 50}\navatar: g_avatar\n"),
    "zorro-bot": identity("name: Zorro Bot\nslug: zorro-bot\nhandle: '@zorro.bot'\nkind: robot\n"
                          "bio: Piept nur bei Sonnenschein  # comment\n"),
    "teapot": identity("name: Kanne\nslug: teapot\nhandle: kanne\nkind: thing\nbio: Eine Teekanne.\n"),
}


@pytest.fixture
def stories_root(tmp_path) -> Path:
    root = tmp_path / "stories"
    (root / "feed").mkdir(parents=True)
    (root / "feed" / "posts.jsonl").write_text("\n".join(POSTS) + "\n", encoding="utf-8")
    (root / "feed" / "interactions.jsonl").write_text("\n".join(INTERACTIONS) + "\n", encoding="utf-8")
    for slug, text in IDENTITIES.items():
        (root / "characters" / slug).mkdir(parents=True)
        (root / "characters" / slug / "identity.md").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def clock():
    return [datetime(2026, 11, 20, 12, 0, tzinfo=TZ)]


@pytest.fixture
def service():
    return FakeService(GENERATIONS + FEED_GENS)


def make_app(henhouse_db, service, feed_dir=None, clock=None, **kw):
    a = preview.create_preview_app(henhouse_db, token=TOKEN, settings=ReviewSettings(BASE, REVIEW_KEY),
                                   transport=httpx.MockTransport(service), poll_interval=None, feed_dir=feed_dir,
                                   feed_clock=(lambda: clock[0]) if clock else None, **kw)
    asyncio.run(a.state.store.poll_once())
    return a


def client_for(app) -> TestClient:
    c = TestClient(app, base_url="http://preview-host.example:18510")
    assert login(c).status_code == 303
    return c


@pytest.fixture
def client(henhouse_db, service, stories_root, clock, monkeypatch):
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    monkeypatch.setattr(config, "TODAY_OVERRIDE", "2026-11-20")
    return client_for(make_app(henhouse_db, service, stories_root / "feed", clock))


def feed_items(c) -> list[dict]:
    return c.get("/api/feed?limit=100").json()["items"]


# --- feed --------------------------------------------------------------------------------------------

def test_unset_feed_dir_keeps_old_behaviour(henhouse_db, service, monkeypatch, stories_root):
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    monkeypatch.delenv("HENHOUSE_FEED_DIR", raising=False)
    c = client_for(make_app(henhouse_db, service, feed_dir=preview._UNSET))
    ids = by_id(feed_items(c))
    assert ids["vs-g_draft1"]["preview"]["label"] == "ENTWURF"
    assert not any(str(i).startswith("fd-") for i in ids)
    assert c.get("/api/posts/vs-g_draft1").status_code == 200
    assert c.get("/api/accounts/studio-entwurf").status_code == 200
    assert c.get("/api/preview/status").json()["feed"] is None


def test_env_var_turns_the_feed_on(henhouse_db, service, monkeypatch, stories_root):
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    monkeypatch.setenv("HENHOUSE_FEED_DIR", str(stories_root / "feed"))
    a = make_app(henhouse_db, service, feed_dir=preview._UNSET)
    assert a.state.feed is not None and a.state.feed.source.characters.root == stories_root / "characters"
    monkeypatch.setenv("HENHOUSE_CHARACTERS_DIR", str(stories_root / "elsewhere"))
    a = make_app(henhouse_db, service, feed_dir=preview._UNSET)
    assert a.state.feed.source.characters.root == stories_root / "elsewhere"


def test_feed_shows_only_published_and_feed_posts(client):
    items = feed_items(client)
    feed = [i for i in items if str(i["id"]).startswith("fd-")]
    assert [i["id"] for i in feed] == ["fd-p2", "fd-p1"]
    assert not any(str(i["id"]).startswith("vs-") for i in items)
    published = [i for i in items if not str(i["id"]).startswith("fd-")]
    assert len(published) == 41
    blob = json.dumps(items)
    for gone in ("Versteckt", "Unlesbar", "Naiv", "Falsche Art", "Noch nicht", "First caption",
                 "Lisa testet den Melkroboter", "ENTWURF", "FREIGEGEBEN"):
        assert gone not in blob, gone
    from weide.db import parse_time
    times = [parse_time(i["posted_at"]) for i in items]
    assert times == sorted(times, reverse=True)
    assert items[0]["id"] == "fd-p2"


def test_feed_card_shape(client):
    ids = by_id(feed_items(client))
    p1, p2 = ids["fd-p1"], ids["fd-p2"]
    pub = next(i for i in ids.values() if isinstance(i["id"], int))
    assert set(pub) <= set(p1)                          # every field a published card has
    assert "preview" not in p1 and p1["draft"] is False
    assert p1["caption"] == "Lisa im Heu" and p1["posted_at"] == "2026-11-20T09:30:00+01:00"
    assert p1["story"] == "Heuballen" and p2["story"] is None
    assert p1["kind"] == "reel" and p1["media_kind"] == "video"
    assert p1["media_url"] == "/preview/media/g_feed_v/output" and p1["poster_url"] == "/preview/media/g_feed_v/poster"
    assert p2["kind"] == "image" and p2["media_kind"] == "image"
    assert p1["lab_url"] == "http://preview-host.example:18500/#/g/g_feed_v"
    assert p1["generation"]["model_label"] == "LTX-2.5 22B distilled" and p1["generation"]["public_id"] is None
    assert p1["feed"]["generation_id"] == "g_feed_v" and p1["feed"]["post_id"] == "p1"


def test_replace_by_later_line(client):
    assert by_id(feed_items(client))["fd-p1"]["caption"] == "Lisa im Heu"


def test_future_post_appears_after_clock_advance(client, clock):
    assert "fd-p3" not in by_id(feed_items(client))
    assert client.get("/api/posts/fd-p3").status_code == 404
    clock[0] = datetime(2026, 11, 20, 15, 0, tzinfo=TZ)
    ids = by_id(feed_items(client))
    assert ids["fd-p3"]["caption"] == "Noch nicht"
    assert ids["fd-p1"]["comment_count"] == 2           # the 13:00 comment is due now as well
    assert client.get("/api/posts/fd-p3").json()["comments"][0]["text"] == "Zu früh"


def test_hidden_unreadable_and_malformed_are_skipped(client, service):
    ids = by_id(feed_items(client))
    assert "fd-p4" not in ids and "fd-p5" not in ids and "fd-p6" not in ids and "fd-p7" not in ids
    st = client.get("/api/preview/status").json()["feed"]
    assert st["posts"] == 2 and st["waiting"] == 1 and st["scheduled"] == 1
    assert st["hidden"] == 1 and st["skipped_lines"] == 4 + 3     # posts: junk, naive, audio, array; interactions: naive, no text, broken
    # the generation becomes readable: the post shows up
    service.items.append(gen("g_not_there", finished_at="2026-11-20T18:00:00Z"))
    asyncio.run(client.app.state.store.poll_once())
    assert "fd-p5" in by_id(feed_items(client))


def test_interactions_counts_and_comments(client):
    p1 = by_id(feed_items(client))["fd-p1"]
    assert p1["reactions"]["heart"] == 2 and p1["like_count"] == 2      # one like per character
    assert p1["comment_count"] == 1
    d = client.get("/api/posts/fd-p1").json()
    assert [c["text"] for c in d["comments"]] == ["Piep, schönes Heu!"]
    com = d["comments"][0]
    assert com["account"]["slug"] == "zorro-bot" and com["account"]["display_name"] == "Zorro Bot"
    assert com["created_at"] == "2026-11-20T10:30:00+01:00" and com["replies"] == []
    assert d["moments"] == [] and d["parent"] is None
    p2 = client.get("/api/posts/fd-p2").json()
    assert [c["text"] for c in p2["comments"]] == ["Muh!"] and p2["comment_count"] == 1
    assert p2["comments"][0]["account"]["display_name"] == "Lisa (Feed)"
    blob = json.dumps(feed_items(client))
    assert "Unsichtbar" not in blob and "Zu früh" not in blob


def test_non_feed_draft_detail_is_404(client):
    for path in ("/api/posts/vs-g_draft1", "/api/posts/vs-g_appr", "/api/posts/vs-g_feed_v",
                 "/api/accounts/studio-entwurf", "/api/posts/fd-p4", "/api/posts/fd-nope"):
        assert client.get(path).status_code == 404, path
    assert client.get("/api/posts/1").status_code == 200
    # draft media are not streamed either; feed media and avatars are
    assert client.get("/preview/media/g_draft1/output").status_code == 404
    assert client.get("/preview/media/g_feed_v/output").status_code == 200
    assert client.get("/preview/media/g_avatar/output").status_code == 200
    assert client.get("/preview/media/g_feed_hidden/output").status_code == 404


# --- profiles ----------------------------------------------------------------------------------------

def test_front_matter_merges_with_db_account(client):
    p1 = by_id(feed_items(client))["fd-p1"]
    acc = p1["account"]
    assert acc["slug"] == "lisa" and acc["display_name"] == "Lisa (Feed)" and acc["handle"] == "@lisa.feed"
    assert acc["kind"] == "animal" and acc["avatar_url"] == "/preview/media/g_avatar/output"
    prof = client.get("/api/accounts/lisa").json()
    assert prof["display_name"] == "Lisa (Feed)" and len(prof["bio"]) == BIO_MAX and prof["bio"].endswith("…")
    assert prof["followers"] > 0 and prof["about"]                     # the rest comes from the database
    assert prof["posts"][0]["id"] == "fd-p1" and prof["post_count"] == len(prof["posts"])
    assert all(p["account"]["display_name"] == "Lisa (Feed)" for p in prof["posts"])
    # published lisa posts in the feed carry the override too
    assert all(i["account"]["display_name"] == "Lisa (Feed)" for i in feed_items(client) if i["account"]["slug"] == "lisa")
    listed = {a["slug"]: a for a in client.get("/api/accounts").json()["items"]}
    db_posts = client.get("/api/accounts/lisa").json()["post_count"] - 1
    assert listed["lisa"]["posts"] == db_posts + 1 and listed["lisa"]["display_name"] == "Lisa (Feed)"


def test_db_is_never_written(client, henhouse_db):
    before = hashlib.sha256(Path(henhouse_db).read_bytes()).hexdigest()
    for p in ("/api/feed?limit=100", "/api/accounts", "/api/accounts/lisa", "/api/posts/fd-p1", "/api/meta",
              "/api/preview/stories"):
        assert client.get(p).status_code == 200
    assert hashlib.sha256(Path(henhouse_db).read_bytes()).hexdigest() == before


def test_unknown_slug_and_kind_mapping(client, clock):
    z = client.get("/api/accounts/zorro-bot").json()
    assert z["preview_only"] is True and z["kind"] == "robot" and z["robot"] is True
    assert z["handle"] == "@zorro.bot" and z["bio"] == "Piept nur bei Sonnenschein"
    assert [p["id"] for p in z["posts"]] == ["fd-p2"] and z["post_count"] == 1
    t = client.get("/api/accounts/teapot").json()
    assert t["kind"] == "other" and t["robot"] is False and t["handle"] == "@kanne" and t["posts"] == []
    listed = {a["slug"]: a for a in client.get("/api/accounts").json()["items"]}
    assert listed["zorro-bot"]["posts"] == 1 and listed["teapot"]["kind"] == "other"
    assert "zorro-bot" in {a["slug"] for a in client.get("/api/accounts?kind=robot").json()["items"]}
    assert client.get("/api/accounts/nobody-here").status_code == 404
    clock[0] = datetime(2026, 11, 20, 16, 0, tzinfo=TZ)
    assert client.get("/api/accounts/teapot").json()["posts"][0]["id"] == "fd-p3"


def test_identity_body_never_in_any_response(client, clock, caplog):
    caplog.set_level(logging.DEBUG)
    clock[0] = datetime(2026, 11, 21, 12, 0, tzinfo=TZ)
    paths = ["/api/feed?limit=100", "/api/meta", "/api/accounts", "/api/preview/status", "/api/preview/stories",
             "/api/posts/fd-p1", "/api/posts/fd-p2", "/api/posts/fd-p3", "/api/posts/1"]
    paths += [f"/api/accounts/{s}" for s in ("lisa", "zorro-bot", "teapot", "ronny")]
    for p in paths:
        r = client.get(p)
        assert r.status_code == 200, p
        assert BODY_MARKER not in r.text and "Body Name" not in r.text and "body bio" not in r.text, p
    assert BODY_MARKER not in caplog.text


def test_front_matter_reader_stops_at_body(tmp_path):
    f = tmp_path / "identity.md"
    f.write_text(identity("name: A\n# a comment\nnested:\n  - x\nkind: thing\n"), encoding="utf-8")
    assert read_front_matter(f) == {"name": "A", "kind": "thing"}
    f.write_text(f"no front matter\n{BODY_MARKER}\n", encoding="utf-8")
    assert read_front_matter(f) is None
    f.write_text("---\nname: A\n" + "x: y\n" * 100, encoding="utf-8")         # never closed
    assert read_front_matter(f) is None


def test_files_are_reread_when_changed(stories_root, clock):
    src = FeedSource(stories_root / "feed", now=lambda: clock[0])
    assert src.posts()["p1"].caption == "Lisa im Heu"
    rows = src._posts.rows
    assert src.posts() and src._posts.rows is rows                  # unchanged file: not parsed again
    with (stories_root / "feed" / "posts.jsonl").open("a", encoding="utf-8") as f:
        f.write(line(id="p1", generation_id="g_feed_v", author="lisa", kind="video", caption="Dritte Fassung",
                     posted_at="2026-11-20T09:30:00+01:00") + "\n")
    assert src.posts()["p1"].caption == "Dritte Fassung"
    assert FeedSource(stories_root / "missing").posts() == {}


def test_meta_counts_and_story_rings(client):
    meta = client.get("/api/meta").json()
    assert meta["posts"] == 41 + 2
    st = client.get("/api/preview/stories").json()
    assert st["day"] == "2026-11-20"
    rings = {r["account"]["slug"]: r for r in st["items"]}
    assert list(rings) == ["zorro-bot", "lisa"]
    assert rings["lisa"]["post_ids"] == ["fd-p1"] and rings["lisa"]["stories"] == ["Heuballen"]
    assert rings["lisa"]["account"]["display_name"] == "Lisa (Feed)"
    assert rings["zorro-bot"]["latest_at"] == "2026-11-20T10:00:00+01:00"


def test_story_rings_include_published_posts_of_the_day(client, henhouse_db, service, stories_root, clock,
                                                        monkeypatch):
    monkeypatch.setattr(config, "TODAY_OVERRIDE", "2026-11-18")
    clock[0] = datetime(2026, 11, 21, 12, 0, tzinfo=TZ)
    st = client.get("/api/preview/stories").json()
    assert st["day"] == "2026-11-18"
    assert [r["account"]["slug"] for r in st["items"]] == ["lisa"]      # the published "Schon live" post
    assert st["items"][0]["account"]["display_name"] == "Lisa (Feed)"
    monkeypatch.setattr(config, "TODAY_OVERRIDE", None)
    clock[0] = datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)          # already the 21st in Berlin
    assert client.get("/api/preview/stories").json() == {"day": "2026-11-21", "items": []}


def test_feed_filters(client):
    robots = client.get("/api/feed?limit=100&kind=robot").json()["items"]
    assert [i["id"] for i in robots] == ["fd-p2"]
    lisa = client.get("/api/feed?limit=100&account=lisa").json()["items"]
    assert lisa[0]["id"] == "fd-p1" and all(i["account"]["slug"] == "lisa" for i in lisa)
    weide = client.get("/api/feed?limit=100&world=roboter").json()["items"]
    assert "fd-p2" in {i["id"] for i in weide}


# --- export ------------------------------------------------------------------------------------------

def tree(root: Path) -> dict[str, str]:
    return {str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(root.rglob("*")) if f.is_file()}


def test_export_static_ignores_feed_dir(henhouse_db, stories_root, tmp_path, monkeypatch):
    monkeypatch.delenv("HENHOUSE_FEED_DIR", raising=False)
    export_static(tmp_path / "a", henhouse_db, today="2026-11-17")
    monkeypatch.setenv("HENHOUSE_FEED_DIR", str(stories_root / "feed"))
    export_static(tmp_path / "b", henhouse_db, today="2026-11-17")
    assert tree(tmp_path / "a") == tree(tmp_path / "b")
    blob = "".join(f.read_text(errors="ignore") for f in (tmp_path / "b").rglob("*.json"))
    for s in ("Lisa im Heu", "Lisa (Feed)", "Zorro Bot", "fd-p1", BODY_MARKER):
        assert s not in blob


@pytest.mark.parametrize("raw,expected", [("animal", "animal"), ("robot", "robot"), ("thing", "other"),
                                          ("human", "human"), ("house", "house"), ("spaceship", "other")])
def test_identity_kind_mapping(raw, expected):
    from weide.feed import Identity
    assert Identity.from_front_matter("x", {"kind": raw}).kind == expected
