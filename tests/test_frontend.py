"""Frontend render smoke test (Node + a tiny DOM shim, the real static/js modules, a static export).

Every route's view renders without throwing; feed slides carry the action rail (reactions, comments,
share); the profile has the post grid before the collapsed "about" sections; the disclaimer footer, the
robot note and the "Ding" badge for kind "other" are there; share links use the canonical #/p/<id>;
curated preview cards (generation without public_id) still show the model line and every credit."""
import json
import shutil
import subprocess

import pytest

from weide import db
from weide.export_static import export_static
from weide.importer import import_public

from .conftest import REPO

DISCLAIMER = "Fiktive, KI-generierte Inhalte. Keine Verbindung zu Unitree Robotics oder NVIDIA; Marken gehören ihren Inhabern."

SHIM = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [site, js] = process.argv.slice(2);
class Node { constructor() { this.parentNode = null; } }
class Text extends Node { constructor(t) { super(); this.data = String(t); } get textContent() { return this.data; } }
class El extends Node {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.attrs = {}; this.className = "";
    this.style = { setProperty() {} }; this.dataset = {}; this.htmlSet = false; }
  append(...cs) { for (const c of cs) { const n = c instanceof Node ? c : new Text(c); n.parentNode = this; this.childNodes.push(n); } }
  prepend(...cs) { this.append(...cs); }
  replaceChildren(...cs) { this.childNodes = []; this.append(...cs); }
  remove() { if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((c) => c !== this); }
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
  tags(tag, acc = []) {
    for (const c of this.childNodes) if (c instanceof El) { if (c.tagName === tag) acc.push(c); c.tags(tag, acc); }
    return acc;
  }
  anyHtml() { return this.htmlSet && this.tagName !== "SVG" || this.childNodes.some((c) => c instanceof El && c.anyHtml()); }
}
globalThis.Node = Node;
globalThis.window = { APP_STATIC: true };
globalThis.document = { createElement: (t) => new El(t), createElementNS: (_, t) => new El(t),
  createTextNode: (t) => new Text(t), body: new El("body"), documentElement: new El("html") };
globalThis.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.fetch = async (p) => {
  try { const body = readFileSync(`${site}/${p}`, "utf8"); return { ok: true, status: 200, json: async () => JSON.parse(body) }; }
  catch { return { ok: false, status: 404, json: async () => ({}) }; }
};
const ui = await import(pathToFileURL(`${js}/ui.js`).href);
const views = await import(pathToFileURL(`${js}/views.js`).href);
const util = await import(pathToFileURL(`${js}/util.js`).href);
ui.state.meta = JSON.parse(readFileSync(`${site}/data/meta.json`, "utf8"));
const feed = JSON.parse(readFileSync(`${site}/data/feed.json`, "utf8")).items;
const tick = () => new Promise((r) => setTimeout(r, 20));
const out = { routes: {}, html: false };
async function route(name, run) {
  const root = new El("main");
  try { await run(root); await tick(); root.append(ui.siteFooter()); }
  catch (e) { if (e.status === 404) { views.notFound(root, "x"); root.append(ui.siteFooter()); } else { out.routes[name] = { error: String(e.stack || e) }; return null; } }
  out.html = out.html || root.anyHtml();
  out.routes[name] = { foot: root.all("site-foot").map((f) => f.textContent), text: root.textContent.length };
  return root;
}
const feedRoot = await route("feed", (r) => views.feedView(r));
const slides = feedRoot.all("slide");
out.slides = slides.map((s) => ({
  rail: s.all("reacts--rail").length,
  share: s.all("react--share").length, comments: s.all("react--comments").map((a) => a.attrs.href),
  commentsPopup: s.all("react--comments").map((a) => a.attrs["aria-haspopup"]),
  video: s.all("media--video").length,
  tap: s.all("media__tap").map((b) => [b.tagName, b.attrs.type, b.attrs["aria-label"]]),
  sound: s.all("media__sound").map((b) => [b.tagName, b.attrs["aria-label"], b.attrs["aria-pressed"]]),
  reactions: s.all("react").length,
}));
out.stories = feedRoot.all("story").length;
await route("world", (r) => views.feedView(r, "weide"));
await route("world-dinge", (r) => views.feedView(r, "dinge"));
await route("world-bad", (r) => views.feedView(r, "nope"));
for (const kind of ["video", "image", "text"]) {
  const p = feed.find((i) => (i.media_kind || "text") === kind);
  const root = await route(`post-${kind}`, (r) => views.postView(r, p.id));
  out.routes[`post-${kind}`].share = root.all("react--share").length;
  out.routes[`post-${kind}`].comments = root.all("comments-block").length;
}
const robot = feed.find((i) => i.account.kind === "robot");
const robotCard = ui.postCard(robot);
out.robotNote = robotCard.all("robot-note").map((n) => n.textContent);
await route("post-404", (r) => views.postView(r, "999999"));
const prof = await route("profile", (r) => views.profileView(r, "lisa"));
const page = prof.all("profilepage")[0];
out.profile = { grid: prof.all("grid__item").length, order: page.childNodes.map((c) => c.className),
  aboutCollapsed: prof.all("profile__more").map((d) => d.tagName) };
const thing = await route("profile-thing", (r) => views.profileView(r, "pfandi"));
out.thing = { tags: thing.all("tag").map((t) => ({ cls: t.className, text: t.textContent })),
  badge: thing.all("tag__kind").map((b) => b.textContent) };
await route("people", (r) => views.peopleView(r));
await route("heute", (r) => views.topView(r, "heute"));
await route("heute-day", (r) => views.topView(r, "heute", "2026-11-16"));
await route("woche", (r) => views.topView(r, "woche"));
await route("woche-week", (r) => views.topView(r, "woche", "2026-W46"));
await route("gen-404", (r) => views.generationView(r, "nope"));
await route("rail", (r) => views.railView(r));
out.share = util.postLink(12, "https://example.org/sub/index.html#/u/lisa");
out.shareFd = util.postLink("fd-7", "http://preview.example:18510/#/");
// curated preview card: generation without a public summary still shows the model line + every credit
const curated = { ...feed[0], id: "fd-1", draft: false, lab_url: "http://lab.example/run/1",
  generation: { public_id: null, model_label: "Modell X", credits: [{ text: "Powered by Example" }, { text: "Sound: CC0" }] } };
const cc = ui.postCard(curated);
const badge = cc.all("gen-badge")[0];
const sib = badge.parentNode.childNodes.filter((n) => n instanceof El);
out.curated = { badge: badge.textContent, tag: badge.tagName, next: sib[sib.indexOf(badge) + 1].className,
  credits: cc.all("gen-credit").map((c) => c.textContent), links: cc.tags("A").map((a) => a.attrs.href) };
out.rings = views.storiesFrom([{ account: { slug: "a", kind: "animal", display_name: "A" }, post_ids: ["fd-9", 3] }],
  [{ id: 5, posted_at: "2026-11-17T10:00:00+01:00", account: { slug: "b", kind: "animal", display_name: "B" } }])
  .map((s) => [s.account.slug, s.latest, s.ring]);
out.derived = views.storiesFrom(undefined,
  [{ id: 5, posted_at: "2026-11-17T10:00:00+01:00", account: { slug: "b", kind: "animal", display_name: "B" } },
   { id: 4, posted_at: "2026-11-10T10:00:00+01:00", account: { slug: "c", kind: "animal", display_name: "C" } }],
  { today: "2026-11-17" }).map((s) => [s.account.slug, s.latest, s.ring]);
// comment sheet: markup + lazily loaded comments (same data as the post page)
const sheet = await import(pathToFileURL(`${js}/sheet.js`).href);
const withComments = feed.find((i) => i.comment_count > 0) || feed[0];
const sm = sheet.sheetMarkup(withComments);
const busy = sm.list.attrs["aria-busy"];
await sheet.loadComments(withComments, sm.list);
const postData = JSON.parse(readFileSync(`${site}/data/posts/${withComments.id}.json`, "utf8"));
const countAll = (cs) => cs.reduce((n, c) => n + 1 + countAll(c.replies || []), 0);
out.sheet = { role: sm.sheet.attrs.role, modal: sm.sheet.attrs["aria-modal"], labelledby: sm.sheet.attrs["aria-labelledby"],
  titleId: sm.title.attrs.id, title: sm.title.textContent, close: [sm.close.tagName, sm.close.attrs["aria-label"]],
  link: [sm.link.attrs.href, sm.link.textContent], handle: sm.handle.attrs["aria-hidden"], busy,
  busyAfter: sm.list.attrs["aria-busy"], comments: sm.list.all("comment").length, want: countAll(postData.comments || []),
  replies: sm.list.all("comment__replies").length, wantId: String(withComments.id) };
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("front")
    dbp = tmp / "weide.db"
    con = db.connect(dbp)
    import_public(con, REPO)
    db.upsert_account(con, "maehbot", kind="robot", handle="@maehbot", display_name="Mähbot 7")
    db.upsert_account(con, "pfandi", kind="other", handle="@pfandi", display_name="Pfandi | Flasche")
    for slug, src in (("maehbot", "test:robot"), ("pfandi", "test:thing")):
        aid = con.execute("SELECT id FROM accounts WHERE slug=?", (slug,)).fetchone()[0]
        db.upsert_post(con, account_id=aid, kind="text", caption="Rasen gemäht.", posted_at="2026-11-17T09:00:00+01:00",
                       source=src, status="published")
    con.commit()
    con.close()
    out = tmp / "site"
    assert export_static(out, dbp, today="2026-11-17", link=True)["leaks"] == []
    return out


@pytest.fixture(scope="module")
def rendered(site, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = tmp_path_factory.mktemp("js") / "render.mjs"
    script.write_text(SHIM, encoding="utf-8")
    r = subprocess.run(["node", str(script), str(site), str(site / "static/js")], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_every_route_renders_with_disclaimer_footer(rendered):
    assert rendered["html"] is False                                  # no innerHTML with data anywhere
    for name, res in rendered["routes"].items():
        assert "error" not in res, (name, res)
        assert res["foot"] == [DISCLAIMER], name


def test_feed_slides_have_the_action_rail(rendered):
    slides = rendered["slides"]
    assert len(slides) >= 8 and rendered["stories"] > 0
    for s in slides:
        assert s["rail"] == 1 and s["share"] == 1 and s["reactions"] == 4 + 2     # 4 reactions, comments, share
        assert len(s["comments"]) == 1 and s["comments"][0].startswith("#/p/")
    for kind in ("video", "image", "text"):
        assert rendered["routes"][f"post-{kind}"]["share"] == 1 and rendered["routes"][f"post-{kind}"]["comments"] == 1


def test_robot_note_on_robot_cards(rendered):
    assert rendered["robotNote"] == [DISCLAIMER]


def test_profile_grid_before_collapsed_about(rendered):
    p = rendered["profile"]
    assert p["grid"] >= 3
    assert p["order"] == ["profile", "profilepage__posts", "profile__more"] and p["aboutCollapsed"] == ["DETAILS"]


def test_things_read_as_objects(rendered):
    t = rendered["thing"]
    assert t["badge"] == ["Ding"]
    assert any("tag--thing" in x["cls"] and "Ding" in x["text"] for x in t["tags"])
    assert not any("other" in x["text"] for x in t["tags"])


def test_share_link_is_canonical(rendered):
    assert rendered["share"] == "https://example.org/sub/index.html#/p/12"
    assert rendered["shareFd"] == "http://preview.example:18510/#/p/fd-7"


def test_curated_card_shows_model_line_and_every_credit(rendered):
    c = rendered["curated"]
    assert c["badge"] == "Erstellt mit Modell X" and c["tag"] == "SPAN" and c["next"] == "gen-credits"
    assert c["credits"] == ["Powered by Example", "Sound: CC0"]
    assert not any(h.startswith("#/g/") for h in c["links"])           # no summary link without public_id


def test_story_rings_from_api_and_derived(rendered):
    assert rendered["rings"] == [["a", "fd-9", True], ["b", 5, False]]
    assert rendered["derived"] == [["b", 5, True], ["c", 4, False]]


def test_video_tap_pauses_and_speaker_button_toggles_sound(rendered):
    videos = [s for s in rendered["slides"] if s["video"]]
    assert videos, "fixture feed has no video post"
    for s in videos:
        assert s["tap"] == [["BUTTON", "button", "Video abspielen"]]            # tap: play / pause
        assert s["sound"] == [["BUTTON", "Ton einschalten", "false"]]          # separate speaker button
    js = (REPO / "static/js/player.js").read_text(encoding="utf-8")
    assert "userPaused" in js and "media__flash" in js and "reducedMotion()" in js
    assert "Video anhalten" in js and "Ton ausschalten" in js


def test_comment_button_opens_the_sheet(rendered):
    for s in rendered["slides"]:
        assert s["commentsPopup"] == ["dialog"] and s["comments"][0].startswith("#/p/")   # link still works
    sh = rendered["sheet"]
    assert sh["role"] == "dialog" and sh["modal"] == "true" and sh["labelledby"] == sh["titleId"]
    assert sh["close"] == ["BUTTON", "Kommentare schließen"] and sh["handle"] == "true"
    assert sh["link"] == [f"#/p/{sh['wantId']}", "Zum Beitrag"]
    assert sh["busy"] == "true" and sh["busyAfter"] == "false"                 # lazy: loaded on open
    assert sh["comments"] == sh["want"] and sh["want"] > 0
    js = (REPO / "static/js/sheet.js").read_text(encoding="utf-8")
    for hook in ('"Escape"', '"Tab"', "pushState", "popstate", "history.back()", ".inert", "sheet-open",
                 "opener", "pointerdown"):
        assert hook in js, hook
    assert "innerHTML" not in js
    main = (REPO / "static/js/main.js").read_text(encoding="utf-8")
    assert '"app:comments"' in main and "closeSheetNow()" in main


def test_css_tabs_fit_and_post_actions_do_not_wrap():
    css = (REPO / "static/app.css").read_text(encoding="utf-8")
    item = css[css.index(".worlds__item {"):]
    item = item[:item.index("}")]
    assert "font-size: clamp(12.5px" in item and "padding: 8px clamp(4px" in item and "white-space: nowrap" in item
    # worst case at 320 px: 5 labels at 12.5 px (about .6 em per glyph, bold) + 2 x 4 px padding each
    labels = ["Für dich", "Weide", "Menschen", "Roboter", "Dinge"]
    assert sum(len(t) * 12.5 * 0.6 + 8 for t in labels) + 8 <= 320
    assert ".postpage .reacts { flex-wrap: nowrap; }" in css
    assert ".sheet {" in css and "70dvh" in css and "html.sheet-open" in css


def test_feed_overlay_and_rail_keep_clear_of_the_bottom_tabs():
    """The caption/credits/disclaimer block and the action rail stick above the bottom tabs (--nav-h includes
    the safe-area inset), so a partly visible slide (the first one, under the story rings) shows them too."""
    import re
    css = (REPO / "static/app.css").read_text(encoding="utf-8")
    assert "--nav-h: calc(58px + env(safe-area-inset-bottom))" in css

    def rule(sel):
        return re.search(re.escape(sel) + r" \{([^}]*)\}", css).group(1)
    assert "position: sticky" in rule(".post__overlay") and "bottom: var(--nav-h)" in rule(".post__overlay")
    assert "position: sticky" in rule(".reacts--rail") and "bottom: calc(var(--nav-h) + 16px)" in rule(".reacts--rail")
    assert "overflow: clip" in rule(".post__frame")            # hidden would make the frame the sticky scroller
