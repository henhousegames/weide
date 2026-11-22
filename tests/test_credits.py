"""`credits` from the generation service: licence notices and audio sources reach every output surface.

A `license_notice` text ("Powered by Stability AI") is a licence obligation: these tests assert that it
survives pull-approved into generation_json, the API card, the static export (feed card, post file,
data/gen/<public_id>.json) and the rendered frontend (feed card, post page, generation page), and the
private preview (cards + detail). Empty credits render nothing; audio-only publications are skipped.
Fixtures: tests/fixtures/publications_credits.json, publication_summaries_credits.json,
review_generations_credits.json (see their `_note`).
"""
import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from weide import config, db, preview, preview_auth
from weide.export_static import export_static, leak_check
from weide.generation import credit_internal, credits_leaks, sanitize_credits
from weide.publish import ServiceSettings, audio_only, pull_approved
from weide.review import ReviewItem, ReviewSettings

from .conftest import REPO
from .test_preview import TOKEN, login

FIXTURES = Path(__file__).parent / "fixtures"
PUBS = json.loads((FIXTURES / "publications_credits.json").read_text(encoding="utf-8"))
SUMMARIES = json.loads((FIXTURES / "publication_summaries_credits.json").read_text(encoding="utf-8"))
REVIEW = json.loads((FIXTURES / "review_generations_credits.json").read_text(encoding="utf-8"))
BASE = "https://svc.example.test"
KEY = "EXAMPLE-publish-key-" + "c" * 30
REVIEW_KEY = "EXAMPLE-review-key-" + "c" * 30
REVIEW_BASE = "http://service.test:18500"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200
SHA = hashlib.sha256(MP4).hexdigest()

NOTICE = "Powered by Stability AI"
NOTICE_URL = "https://stability.ai/community-license-agreement"
CC0_TEXT = 'Sound: "Hen clucking" (Freesound, CC0 1.0)'
WITH, EMPTY, AUDIO = "pub_CREDITS01", "pub_NOCREDIT1", "pub_AUDIO0001"
WANT = [{"kind": "license_notice", "text": NOTICE, "model": "Stable Audio 3 Small SFX",
         "license": {"id": "Stability-AI-Community-2024-07-05", "url": NOTICE_URL}},
        {"kind": "audio_source", "text": CC0_TEXT,
         "license": {"id": "CC0-1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"}}]


# --- sanitizing --------------------------------------------------------------------------------------

def test_sanitize_credits_keeps_only_public_fields():
    raw = [
        {**WANT[0], "extra": "x", "internal_path": "/srv/EXAMPLE/sfx.wav",
         "license": {**WANT[0]["license"], "sha256": "ab" * 32, "note": "drop me"}},
        {**WANT[1], "model": None},
        {"kind": "license_notice", "text": 42},                                      # text not a string
        {"kind": "audio_source"},                                                     # no text
        "Powered by nothing", None, 7,                                                # not objects
        {"kind": "license_notice", "text": "Bad URL A", "license": {"id": "X", "url": "javascript:alert(1)"}},
        {"kind": "license_notice", "text": "Bad URL B", "license": {"url": "file:///home/EXAMPLE/x"}},
        {"kind": "license_notice", "text": "Bad URL C", "license": {"url": "http://192.168.1.5:8188/lic"}},
        {"kind": "license_notice", "text": "Bad URL D", "license": {"url": "https://user:pw@example.org/l"}},
        {"kind": "license_notice", "text": "Bad URL E", "license": {"url": "http://gpu-box.local/l"}},
        {"kind": ["x"], "text": "  Spaced\n  text  ", "model": {"id": "m"}, "license": "MIT"},
        dict(WANT[0]),                                                                # duplicate
    ]
    out = sanitize_credits(raw)
    assert out[:2] == WANT
    assert out[2:] == [{"kind": "license_notice", "text": "Bad URL A", "license": {"id": "X"}},
                       {"kind": "license_notice", "text": "Bad URL B"},
                       {"kind": "license_notice", "text": "Bad URL C"},
                       {"kind": "license_notice", "text": "Bad URL D"},
                       {"kind": "license_notice", "text": "Bad URL E"},
                       {"text": "Spaced text"}]
    for v in (None, [], {}, "x", 3, {"credits": WANT}):
        assert sanitize_credits(v) == []
    assert sanitize_credits(out) == out                                               # idempotent
    hits: list[str] = []
    credits_leaks(out, "t", hits)
    assert hits == []
    credits_leaks([{**WANT[0], "path": "/home/EXAMPLE"}], "t", hits)
    credits_leaks([{"text": "x", "license": {"url": "http://10.0.0.1/x"}}], "t", hits)
    assert len(hits) == 2


# synthetic examples of what the service may send for audio sources (invented ids, not real ones)
INTERNAL_TEXTS = [
    "Musik: own generation: LTX-2.5 audio of gen_0a1b2c3d4e5f60718293 (h007 b2 t3), 0.00-2.40 s, cut locally "
    "(own generation (same terms as the source take))",
    "Musik: example-pack (example_sfx-pack.zip) :: Audio/doorCreak.ogg - Example Studio (example.org) (CC0-1.0)",
    "Sound: run gen_deadbeef01",
    "Sound: take h123",
    "Sound: pack :: clip",
    "Sound: thunder.WAV (CC0)",
    "Sound: weights model.safetensors",
    "Sound: Audio/clip",
    "Sound: from sfx/archive",
]
PUBLIC_TEXTS = [NOTICE, CC0_TEXT,
                "SoundCTM by Example Lab, CC BY 4.0",
                "Musik: Example Artist - Example Song (CC BY 4.0), 0.00-1.15 s",
                "Ton: eigene Generation (Stable Audio 3 Small SFX)"]


def test_internal_credit_details_fail_the_leak_check():
    for t in INTERNAL_TEXTS:
        c = {"kind": "audio_source", "text": t}
        assert credit_internal(c), t
        hits: list[str] = []
        credits_leaks(sanitize_credits([WANT[0], c]), "data/gen/pub_X.json", hits, "pub_X")
        assert len(hits) == 1 and "pub_X" in hits[0] and t not in hits[0], t
        assert "gen_" not in hits[0] and "::" not in hits[0]
    assert credit_internal({"text": "Powered by Example", "model": "ckpt/model-v2"})          # model counts too
    for t in PUBLIC_TEXTS:
        c = {"kind": "license_notice", "text": t,
             "license": {"id": "CC-BY-4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}
        assert not credit_internal(c), t                                                     # license.url may have /
        hits = []
        credits_leaks([c], "t", hits, "pub_X")
        assert hits == [], t


def test_fixture_items_carry_credits():
    by = {p["public_id"]: p for p in PUBS["publications"]}
    assert [c["kind"] for c in by[WITH]["credits"]] == ["license_notice", "audio_source"]
    assert by[EMPTY]["credits"] == []
    assert by[AUDIO]["audio_url"] and by[AUDIO]["poster_url"] is None and not by[AUDIO]["video_url"]
    assert audio_only(by[AUDIO]) and not audio_only(by[WITH]) and not audio_only(by[EMPTY])
    assert audio_only({"public_id": "x", "audio_url": "/a", "video_url": None, "image_url": None, "mime": None})


# --- publish -> DB -> API -> export -------------------------------------------------------------------

class Service:
    def __init__(self):
        self.pubs = [{**p, "sha256": SHA, "media": [{**m, "sha256": SHA} for m in p["media"]]}
                     for p in PUBS["publications"]]
        self.summaries = {k: json.loads(json.dumps(v)) for k, v in SUMMARIES.items() if k != "_note"}
        self.calls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.path)
        if req.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401)
        path = req.url.path
        if path == "/api/v1/publications":
            if req.url.params.get("since"):
                return httpx.Response(200, json={"publications": [], "revoked": [], "next_since": req.url.params["since"]})
            return httpx.Response(200, json={"publications": self.pubs, "revoked": [], "next_since": PUBS["next_since"]})
        pid = path.split("/")[4]
        if pid == AUDIO:
            raise AssertionError("audio-only publication must not be fetched")
        if path.endswith("/summary"):
            return httpx.Response(200, json=self.summaries[pid])
        if path.endswith("/media/output"):
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)


@pytest.fixture
def published(tmp_path, monkeypatch):
    dbp = tmp_path / "h.db"
    con = db.connect(dbp)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot")
    con.commit()
    monkeypatch.setattr(config, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(config, "THUMB_ROOT", tmp_path / "thumb")
    svc = Service()
    lines: list[str] = []
    res = pull_approved(con, ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), log=lines.append)
    yield con, svc, dbp, res, lines
    con.close()


def gen_json(con, pid):
    return json.loads(con.execute("SELECT generation_json FROM posts WHERE public_id=?", (pid,)).fetchone()[0])


def test_pull_keeps_credits_and_skips_audio_only(published):
    con, svc, _, res, lines = published
    assert (res["new"], res["skipped"], res["failed"]) == (2, 1, []), res
    assert {"public_id": AUDIO, "status": "skipped", "reason": "audio-only"} in res["items"]
    assert f"skipped {AUDIO}: audio-only" in lines
    assert con.execute("SELECT COUNT(*) FROM posts WHERE public_id=? OR source=?", (AUDIO, f"generation:{AUDIO}")).fetchone()[0] == 0
    assert not any(AUDIO in c for c in svc.calls[1:])
    assert db.get_meta(con, "publications_since") == PUBS["next_since"]           # a skip is no failure
    assert gen_json(con, WITH)["credits"] == WANT
    assert gen_json(con, EMPTY)["credits"] == []
    assert gen_json(con, WITH)["composition"]["credits"] == []                    # nested field untouched


def test_list_item_credits_when_summary_has_none(published):
    con, svc, *_ = published
    del svc.summaries[WITH]["credits"]
    for p in svc.pubs:
        p["credits"] = [{**WANT[0], "extra": 1}] if p["public_id"] == WITH else None
    del svc.summaries[EMPTY]["credits"]
    res = pull_approved(con, ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), full=True)
    assert res["failed"] == [] and res["updated"] == 1 and res["unchanged"] == 1, res
    assert gen_json(con, WITH)["credits"] == [WANT[0]]
    assert gen_json(con, EMPTY)["credits"] == []                                  # null -> []


def test_repull_updates_credits_idempotently(published):
    con, svc, *_ = published
    run = lambda: pull_approved(con, ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), full=True)
    res = run()
    assert (res["new"], res["updated"], res["unchanged"], res["skipped"]) == (0, 0, 2, 1)
    # a post imported before the service sent credits: the next full pull adds them
    old = gen_json(con, WITH)
    old.pop("credits")
    con.execute("UPDATE posts SET generation_json=? WHERE public_id=?", (json.dumps(old), WITH))
    con.commit()
    res = run()
    assert (res["updated"], res["unchanged"]) == (1, 1) and gen_json(con, WITH)["credits"] == WANT
    assert run()["unchanged"] == 2
    svc.summaries[EMPTY]["credits"] = [WANT[1]]                                  # a credit added later
    res = run()
    assert res["updated"] == 1 and gen_json(con, EMPTY)["credits"] == [WANT[1]]


def test_card_summary_and_export_carry_credits(published, tmp_path):
    con, _, dbp, *_ = published
    from weide.server import create_app
    c = TestClient(create_app(dbp))
    cards = {i["generation"]["public_id"]: i for i in c.get("/api/feed?limit=50").json()["items"] if i["generation"]}
    assert cards[WITH]["generation"]["credits"] == WANT and cards[EMPTY]["generation"]["credits"] == []
    assert c.get(f"/api/posts/{cards[WITH]['id']}").json()["generation"]["credits"] == WANT
    assert c.get(f"/api/generations/{WITH}").json()["credits"] == WANT
    assert c.get(f"/api/generations/{EMPTY}").json()["credits"] == []

    out = tmp_path / "site"
    stats = export_static(out, dbp)
    assert stats["leaks"] == [] and stats["generations"] == 2
    feed = {i["generation"]["public_id"]: i for i in json.loads((out / "data/feed.json").read_text())["items"]
            if i.get("generation")}
    assert feed[WITH]["generation"]["credits"] == WANT and feed[EMPTY]["generation"]["credits"] == []
    post = json.loads((out / f"data/posts/{feed[WITH]['id']}.json").read_text())
    assert post["generation"]["credits"] == WANT
    assert json.loads((out / f"data/posts/{feed[EMPTY]['id']}.json").read_text())["generation"]["credits"] == []
    gen = json.loads((out / f"data/gen/{WITH}.json").read_text())
    assert gen["credits"] == WANT
    assert json.loads((out / f"data/gen/{EMPTY}.json").read_text())["credits"] == []
    assert not (out / f"data/gen/{AUDIO}.json").exists()
    # the leak check still bites on a credits list that is not in its public form
    gen["credits"].append({"text": "x", "license": {"url": "http://10.1.2.3/l"}})
    (out / f"data/gen/{WITH}.json").write_text(json.dumps(gen))
    assert any("credits" in h for h in leak_check(out))


def test_export_refuses_internal_credits_but_ingest_keeps_them(tmp_path, monkeypatch):
    """Ingest does not filter (the private preview shows the raw text); the export leak check aborts and
    names the post's public_id, not the credit text."""
    internal = {"kind": "audio_source", "text": INTERNAL_TEXTS[1],
                "license": {"id": "CC0-1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"}}
    dbp = tmp_path / "h.db"
    con = db.connect(dbp)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot")
    con.commit()
    monkeypatch.setattr(config, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(config, "THUMB_ROOT", tmp_path / "thumb")
    svc = Service()
    svc.summaries[WITH]["credits"] = [WANT[0], internal]
    pull_approved(con, ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc), log=lambda _: None)
    assert gen_json(con, WITH)["credits"] == [WANT[0], internal]                     # stored raw (private)
    con.close()
    stats = export_static(tmp_path / "site", dbp)
    hits = stats["leaks"]
    assert hits and all(WITH in h for h in hits)
    assert {h.split(":")[0] for h in hits} >= {"data/feed.json", f"data/gen/{WITH}.json"}
    assert not any("example_sfx" in h or "::" in h or "doorCreak" in h for h in hits)


# --- frontend render (Node + a tiny DOM shim; the real static/js modules) ------------------------------

SHIM = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [site, js] = process.argv.slice(2);
class Node {
  constructor() { this.parentNode = null; }
  get nextElementSibling() {
    const sib = this.parentNode ? this.parentNode.childNodes : [];
    return sib.slice(sib.indexOf(this) + 1).find((n) => n instanceof El) || null;
  }
}
class Text extends Node { constructor(t) { super(); this.data = String(t); } get textContent() { return this.data; } }
class El extends Node {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.attrs = {}; this.className = "";
    this.style = { setProperty() {} }; this.dataset = {}; this.htmlSet = false; }
  append(...cs) { for (const c of cs) { const n = c instanceof Node ? c : new Text(c); n.parentNode = this; this.childNodes.push(n); } }
  prepend(...cs) { this.append(...cs); }
  replaceChildren(...cs) { this.childNodes = []; this.append(...cs); }
  remove() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener() {}
  pause() {}
  set innerHTML(v) { this.htmlSet = true; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.replaceChildren(String(v)); }
  all(cls, acc = []) {
    for (const c of this.childNodes) if (c instanceof El) {
      if (c.className.split(" ").includes(cls)) acc.push(c);
      c.all(cls, acc);
    }
    return acc;
  }
  anyHtml() { return this.htmlSet && this.tagName !== "SVG" || this.childNodes.some((c) => c instanceof El && c.anyHtml()); }
}
globalThis.Node = Node;
globalThis.window = { APP_STATIC: true };
globalThis.document = { createElement: (t) => new El(t), createElementNS: (_, t) => new El(t),
  createTextNode: (t) => new Text(t), body: new El("body") };
globalThis.IntersectionObserver = class { observe() {} unobserve() {} };
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.fetch = async (p) => {
  try { const body = readFileSync(`${site}/${p}`, "utf8"); return { ok: true, status: 200, json: async () => JSON.parse(body) }; }
  catch { return { ok: false, status: 404, json: async () => ({}) }; }
};
const ui = await import(pathToFileURL(`${js}/ui.js`).href);
const views = await import(pathToFileURL(`${js}/views.js`).href);
const credits = (root) => root.all("gen-credits").map((ul) => ({
  items: ul.childNodes.map((li) => ({ text: li.textContent,
    links: li.all ? li.childNodes.filter((n) => n.tagName === "A").map((a) => a.attrs) : [] })),
  prev: (() => { const sib = ul.parentNode.childNodes.filter((n) => n instanceof El); const i = sib.indexOf(ul); return i > 0 ? sib[i - 1].className : null; })(),
}));
const out = { feed: {}, post: {}, gen: {} };
const feed = JSON.parse(readFileSync(`${site}/data/feed.json`, "utf8")).items.filter((i) => i.generation);
for (const item of feed) {
  const pid = item.generation.public_id;
  const card = ui.postCard(item);
  out.feed[pid] = credits(card);
  const page = new El("main");
  await views.postView(page, item.id);
  out.post[pid] = credits(page);
  const gen = new El("main");
  await views.generationView(gen, pid);
  out.gen[pid] = credits(gen);
  out.html = (out.html || false) || card.anyHtml() || page.anyHtml() || gen.anyHtml();
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_frontend_renders_credits_on_card_post_and_generation_page(published, tmp_path):
    _, _, dbp, *_ = published
    out = tmp_path / "site"
    assert export_static(out, dbp)["leaks"] == []
    script = tmp_path / "render.mjs"
    script.write_text(SHIM, encoding="utf-8")
    r = subprocess.run(["node", str(script), str(out), str(out / "static/js")], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert res["html"] is False                                     # no innerHTML anywhere in these views
    for surface in ("feed", "post", "gen"):
        assert res[surface][EMPTY] == [], surface                   # empty list: no element at all
        [block] = res[surface][WITH]
        texts = [i["text"] for i in block["items"]]
        assert texts[0].startswith(NOTICE) and texts[1].startswith(CC0_TEXT), (surface, texts)
        link = block["items"][0]["links"][0]
        assert link["href"] == NOTICE_URL and link["target"] == "_blank" and "noopener" in link["rel"]
    # directly under the model line ("Erstellt mit …" badge; generation page: the "Erstellt mit" sub line)
    assert res["feed"][WITH][0]["prev"] == "gen-badge" and res["post"][WITH][0]["prev"] == "gen-badge"
    assert res["gen"][WITH][0]["prev"] == "view-sub"
    assert "Stable Audio 3 Small SFX" in res["gen"][WITH][0]["items"][0]["text"]       # model + licence id there
    assert "Stability-AI-Community-2024-07-05" in res["gen"][WITH][0]["items"][0]["text"]


def test_frontend_sources_build_credits_safely():
    ui = (REPO / "static/js/ui.js").read_text(encoding="utf-8")
    views = (REPO / "static/js/views.js").read_text(encoding="utf-8")
    pv = (REPO / "app/weide/preview_static/preview.js").read_text(encoding="utf-8")
    body = ui[ui.index("export function genCredits"):ui.index("export const disclaimer")]
    assert "innerHTML" not in body and 'rel: "noopener noreferrer"' in body
    assert ui.count("genCredits(post.generation?.credits)") == 1                     # one slide overlay: visual + text
    assert "genCredits(p.generation?.credits)" in views and "genCredits(credits, { detail: true })" in views
    assert "innerHTML" not in pv and "function credits(p)" in pv and "credits(p)]" in pv
    assert 'noopener noreferrer' in pv and "/^https?:" in pv


# --- private preview ---------------------------------------------------------------------------------

def review_service(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != f"Bearer {REVIEW_KEY}":
        return httpx.Response(401)
    if request.url.path == "/api/v1/review/generations":
        return httpx.Response(200, json={k: v for k, v in REVIEW.items() if k != "_note"})
    if request.url.path.endswith("/media/output"):
        return httpx.Response(200, content=b"RIFF\x00\x00\x00\x00WAVE", headers={"content-type": "audio/wav"})
    return httpx.Response(404)


@pytest.fixture
def preview_client(db_path, monkeypatch, tmp_path):
    monkeypatch.setenv("HENHOUSE_PREVIEW_KEY_FILE", str(tmp_path / "preview.key"))
    monkeypatch.setattr(preview_auth, "FAIL_DELAY", 0)
    db.connect(db_path).close()
    app = preview.create_preview_app(db_path, token=TOKEN, settings=ReviewSettings(REVIEW_BASE, REVIEW_KEY),
                                     transport=httpx.MockTransport(review_service), poll_interval=None)
    asyncio.run(app.state.store.poll_once())
    c = TestClient(app, base_url="http://preview-host.example:18510")
    assert login(c).status_code == 303
    return c


def test_review_item_credits_parse():
    items = {i["id"]: ReviewItem.parse(i) for i in REVIEW["items"]}
    assert items["gen_credits0000000000001"].credits == WANT
    assert items["gen_nocredits00000000001"].credits == [] and items["gen_nullcredits0000000001"].credits == []
    a = items["gen_audio000000000000001"]
    assert a.output_kind == "audio" and a.poster_url is None and a.credits == [WANT[0]]


def test_preview_shows_credits_and_audio_only_safely(preview_client):
    c = preview_client
    cards = {i["id"].removeprefix("vs-"): i for i in c.get("/api/feed?limit=100").json()["items"] if "preview" in i}
    assert set(cards) == {i["id"] for i in REVIEW["items"]}
    assert cards["gen_credits0000000000001"]["preview"]["credits"] == WANT
    assert cards["gen_nocredits00000000001"]["preview"]["credits"] == []
    assert cards["gen_nullcredits0000000001"]["preview"]["credits"] == []
    audio = cards["gen_audio000000000000001"]
    assert (audio["kind"], audio["media_kind"], audio["poster_url"]) == ("text", "audio", None)
    assert audio["preview"]["credits"][0]["text"] == NOTICE
    detail = c.get(f"/api/posts/{audio['id']}").json()
    assert detail["preview"]["credits"] == [WANT[0]] and detail["media_kind"] == "audio"
    r = c.get(audio["media_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/")
    assert c.get("/preview/media/gen_audio000000000000001/poster").status_code == 404
    assert c.get(f"/api/posts/{cards['gen_credits0000000000001']['id']}").json()["preview"]["credits"] == WANT
