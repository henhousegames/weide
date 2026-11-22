// Views: feed, post detail, profile, leaderboards, people, the desktop rail.
// Copy is in-world German; CLI hints live in the README (and the collapsed "Quelle und Ranking").
import { api } from "./api.js";
import { avatar, earTag, genBadge, genCredits, media, postCard, postHead, reactionBar, robotNote, simNote, state } from "./ui.js";
import { compact, copy, dayLabel, dec, h, icons, longDay, mount, num, plural, score, when } from "./util.js";

const PAGE = 8;

export function notFound(root, text) {
  mount(root, h("div", { class: "notfound" },
    h("p", { class: "notfound__text" }, text),
    h("a", { class: "notfound__link", href: "#/" }, "Zum Feed")));
}

// Feed worlds (#/w/<world>): filter by the posting account's kind. "Für dich" is the plain feed (#/).
// English and legacy names are accepted as aliases; "mixed"/"alle" mean everything.
export const WORLDS = [
  { key: "", label: "Für dich", kinds: null },
  { key: "tiere", label: "Weide", kinds: ["animal"] },
  { key: "menschen", label: "Menschen", kinds: ["human"] },
  { key: "roboter", label: "Roboter", kinds: ["robot"] },
  { key: "dinge", label: "Dinge", kinds: ["other", "thing"] },
];
const WORLD_ALIASES = { alle: "", all: "", mixed: "", animals: "tiere", weide: "tiere", humans: "menschen", robots: "roboter",
  things: "dinge", objekte: "dinge", other: "dinge" };

// the tab bar over the full-screen feed
function worldNav(current) {
  return h("nav", { class: "worlds", "aria-label": "Welten" }, WORLDS.map((w) => h("a", {
    class: "worlds__item", href: w.key ? `#/w/${w.key}` : "#/", "aria-current": w.key === current ? "page" : null,
  }, w.label)));
}

// --- story rings -------------------------------------------------------------------------------------
// A row of creators; a ring marks who posted today (tap: their newest post), the others link to their
// profile. Rings: the preview's /api/preview/stories ({day, items: [{account, post_ids, ...}]}, newest
// first) when it answers, else derived from the newest feed posts (posted_at on meta.today).

const firstName = (name) => String(name || "").split("|")[0].trim();

export function storiesFrom(rings, items, { today = state.meta?.today, kinds = null } = {}) {
  const out = new Map();
  for (const s of Array.isArray(rings) ? rings : []) {
    const acc = s?.account;
    if (!acc?.slug || out.has(acc.slug) || (kinds && !kinds.includes(acc.kind))) continue;
    const ids = Array.isArray(s.post_ids) ? s.post_ids : [];
    out.set(acc.slug, { account: acc, latest: ids.length ? ids[0] : null, ring: true });
  }
  const derive = !Array.isArray(rings);
  for (const p of items || []) {
    const acc = p.account;
    if (!acc?.slug || acc.kind === "follower" || out.has(acc.slug)) continue;
    out.set(acc.slug, { account: acc, latest: p.id, ring: derive && Boolean(today && String(p.posted_at || "").slice(0, 10) === today) });
  }
  return [...out.values()].sort((x, y) => Number(y.ring) - Number(x.ring));
}

export function storyRow(stories) {
  if (!stories.length) return null;
  return h("section", { class: "stories", "aria-label": "Creator" },
    h("ul", { class: "stories__list" }, stories.slice(0, 16).map((s) => h("li", {},
      h("a", {
        class: `story${s.ring ? " story--new" : ""}`,
        href: s.ring && s.latest != null ? `#/p/${encodeURIComponent(s.latest)}` : `#/u/${encodeURIComponent(s.account.slug)}`,
        "aria-label": s.ring ? `${s.account.display_name}: neuer Post heute` : `Profil von ${s.account.display_name}`,
      },
      h("span", { class: "story__ring" }, avatar(s.account, 56, false)),
      h("span", { class: "story__name" }, firstName(s.account.display_name)))))));
}

export async function feedView(root, worldArg) {
  const key = worldArg === undefined ? "" : (WORLD_ALIASES[String(worldArg).toLowerCase()] ?? String(worldArg).toLowerCase());
  const world = WORLDS.find((w) => w.key === key);
  if (!world) {
    const e = new Error("unknown world");
    e.status = 404;
    throw e;
  }
  const head = h("div", { class: "feed__head" });
  const list = h("div", { class: "feed" });
  const sentinel = h("div", { class: "sentinel" });
  mount(root, worldNav(world.key), head, list, sentinel);
  let offset = 0;
  let loading = false;
  async function more() {
    if (loading || offset === null) return;
    loading = true;
    try {
      const page = await api.feed(offset, PAGE, world.kinds);
      if (!page.items.length && offset === 0) {
        list.append(h("div", { class: "slide slide--empty" }, h("p", { class: "empty empty--pad" }, world.key === "roboter"
          ? "Noch keine Posts. Die Roboter laden noch." : "Noch keine Posts. Die Creator schlafen noch.")));
      }
      list.append(...page.items.map(postCard));
      offset = page.next_offset ?? null;
      if (offset === null) sentinel.remove();
    } finally {
      loading = false;
    }
  }
  await more();
  // story rings: never block or break the feed
  const preview = document.documentElement?.classList?.contains("pv");
  Promise.all([api.feed(0, 40, world.kinds), preview ? api.stories().catch(() => null) : null])
    .then(([page, rings]) => {
      const row = storyRow(storiesFrom(rings?.items, page.items, { kinds: world.kinds }));
      if (row) head.append(row);
    })
    .catch(() => {});
  // re-observe after each page: a sentinel that stays in range would not report again
  const io = new IntersectionObserver((e) => {
    if (!e[e.length - 1].isIntersecting) return;
    more().then(() => { if (offset !== null) { io.unobserve(sentinel); io.observe(sentinel); } }).catch(() => {});
  }, { rootMargin: "200% 0px" });
  io.observe(sentinel);
  return () => io.disconnect();
}

function backLink() {
  return h("a", {
    class: "back", href: "#/",
    onclick: (e) => { if (history.length > 1) { e.preventDefault(); history.back(); } },
  }, icons.back(), "Zurück");
}

function commentNode(c) {
  return h("li", { class: "comment" },
    avatar(c.account, 30),
    h("div", { class: "comment__body" },
      h("div", { class: "comment__who" },
        h("a", { class: "comment__name", href: `#/u/${encodeURIComponent(c.account.slug)}` }, c.account.display_name),
        h("time", { class: "comment__time", datetime: c.created_at }, when(c.created_at))),
      h("p", { class: "comment__text" }, c.text),
      c.replies?.length ? h("ul", { class: "comment__replies" }, c.replies.map(commentNode)) : null));
}

function likedBy(p) {
  const hearts = p.reactions?.heart || 0;
  if (p.uwe_reacted) {
    const others = Math.max(0, hearts - 1);
    return h("p", { class: "liked-by" }, h("a", { href: "#/u/uwe" }, "Uwe"),
      others === 0 ? " findet das gut" : others === 1 ? " und eine weitere Person finden das gut"
        : ` und ${num(others)} andere finden das gut`);
  }
  return hearts ? h("p", { class: "liked-by" }, `${num(hearts)} finden das gut`) : null;
}

export async function postView(root, id) {
  const p = await api.post(id);
  const n = p.comment_count || 0;
  const moments = p.moments?.length
    ? h("section", { class: "block" }, h("h2", {}, "Beste Momente"),
      h("div", { class: "moments" }, p.moments.map((m) => h("a", { class: "moment", href: `#/p/${m.id}` },
        h("div", { class: "moment__img" }, m.poster_url ? h("img", { src: m.poster_url, alt: "", loading: "lazy" }) : null),
        h("p", { class: "moment__why" }, m.why || m.clip_id)))))
    : null;
  const facts = [
    ["Score", `${score(p.score)} von 25${p.funny_score != null ? ` (Pointe ${score(p.funny_score)})` : " (ohne Pointe-Wertung)"}`],
    ["Quelle", p.source || "–"],
    p.clip_id ? ["Clip", p.clip_id] : null,
    p.media_path ? ["Datei", p.media_path] : null,
    ["Gepostet", new Date(p.posted_at).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" })],
  ].filter(Boolean);
  const caption = p.kind === "text"
    ? (p.hashtags ? h("p", { class: "caption" }, h("span", { class: "caption__tags" }, p.hashtags)) : null)
    : h("p", { class: "caption" }, p.caption || "", p.hashtags ? [" ", h("span", { class: "caption__tags" }, p.hashtags)] : null);
  mount(root,
    backLink(),
    h("article", { class: "postpage" },
      h("div", { class: "postpage__head" }, postHead(p),
        p.parent ? h("p", { class: "liked-by" }, "Moment aus ", h("a", { href: `#/p/${p.parent.id}` }, p.parent.title || "der Story")) : null),
      h("div", { class: "postpage__media" }, media(p)),
      h("div", { class: "postpage__body" },
        reactionBar(p, { showComments: false }),
        likedBy(p),
        caption,
        genBadge(p, { withMissing: true }),
        genCredits(p.generation?.credits),
        robotNote(p),
        moments,
        h("section", { class: "block comments-block", id: "comments" },
          h("h2", {}, n ? plural(n, "Kommentar", "Kommentare") : "Kommentare"),
          p.comments?.length ? h("ul", { class: "comments" }, p.comments.map(commentNode))
            : h("p", { class: "empty" }, "Noch keine Kommentare. Uwe ist bestimmt gleich da.")),
        h("details", { class: "more" }, h("summary", {}, "Quelle und Ranking"),
          h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))))),
  );
}

// --- generation details (#/g/<public_id>) -----------------------------------------------------------
// The summary is rendered generically: known sections first (model, task/preset, parameters, prompt,
// workflow, inputs), then every other field, so new settings show up without frontend changes.

function fmt(v) {
  if (v === null || v === undefined || v === "") return "–";
  if (typeof v === "boolean") return v ? "ja" : "nein";
  if (Array.isArray(v) && v.every((x) => x === null || typeof x !== "object")) return v.map(String).join(", ");
  if (typeof v === "object") return h("pre", { class: "gen__json" }, JSON.stringify(v, null, 2));
  return String(v);                // numbers verbatim (a seed is not 1.234.567)
}

function kvTable(obj) {
  return h("table", { class: "gen__table" }, h("tbody", {},
    Object.entries(obj).map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, fmt(v))))));
}

function workflowBlock(wf) {
  const nodes = wf && typeof wf === "object" ? (wf.nodes ?? wf) : null;
  const list = Array.isArray(nodes) ? nodes.map((n, i) => [n?.id ?? i, n]) : nodes && typeof nodes === "object" ? Object.entries(nodes) : null;
  const graph = list && list.every(([, n]) => n && typeof n === "object")
    ? h("ol", { class: "gen__nodes" }, list.map(([id, n]) => {
      const { inputs, class_type: ct, type, ...rest } = n;
      const params = { ...(inputs && typeof inputs === "object" && !Array.isArray(inputs) ? inputs : inputs !== undefined ? { inputs } : {}), ...rest };
      return h("li", { class: "gen__node" },
        h("div", { class: "gen__nodehead" }, h("span", { class: "gen__nodeid" }, `#${id}`), h("b", {}, ct || type || "Knoten")),
        Object.keys(params).length ? kvTable(params) : null);
    }))
    : fmt(wf);
  const count = list ? list.length : null;
  return h("details", { class: "more gen__workflow" },
    h("summary", {}, count ? `Workflow (${plural(count, "Knoten", "Knoten")})` : "Workflow"), graph);
}

function inputItem(i) {
  if (!i || typeof i !== "object") return h("li", {}, fmt(i));
  const { role, kind, type, label, post_id: postId, public_id: publicId, private: priv, generated, ...rest } = i;
  const what = [role, kind || type].filter(Boolean).join(" · ");
  let body;
  if (priv) body = h("span", { class: "gen__private" }, label || "privater Input");
  else if (postId != null) {
    body = [h("a", { href: `#/p/${postId}` }, label || `Post ${postId}`),
      publicId ? [" · ", h("a", { href: `#/g/${encodeURIComponent(publicId)}` }, "Generierung")] : null];
  } else if (generated) body = "in diesem Lauf erzeugt";
  else body = label || "–";
  return h("li", { class: "gen__input" }, what ? h("span", { class: "gen__role" }, what) : null, h("span", {}, body),
    Object.keys(rest).length ? kvTable(rest) : null);
}

export async function generationView(root, publicId) {
  const g = await api.genSummary(publicId);
  const { public_id: pid, post_id: postId, model_label: label, model, version, license, task, preset,
    parameters, prompt, negative_prompt: negative, workflow, inputs, start_source_sim: sim, credits, ...other } = g;
  const modelInfo = model && typeof model === "object" ? { ...model } : { ...(model != null ? { name: model } : {}) };
  if (version != null) modelInfo.version = version;
  if (license != null) modelInfo.license = license;
  const sections = [];
  if (Object.keys(modelInfo).length) sections.push(h("section", { class: "block" }, h("h2", {}, "Modell"), kvTable(modelInfo)));
  if (task != null || preset != null) {
    sections.push(h("section", { class: "block" }, h("h2", {}, "Aufgabe"),
      kvTable({ ...(task != null ? { task } : {}), ...(preset != null ? { preset } : {}) })));
  }
  if (parameters && typeof parameters === "object") sections.push(h("section", { class: "block" }, h("h2", {}, "Parameter"), kvTable(parameters)));
  if (prompt != null || negative != null) {
    sections.push(h("section", { class: "block" }, h("h2", {}, "Prompt"),
      prompt != null ? h("p", { class: "gen__prompt" }, typeof prompt === "string" ? prompt : fmt(prompt)) : null,
      negative != null ? [h("h3", { class: "gen__sub" }, "Negativ-Prompt"), h("p", { class: "gen__prompt gen__prompt--neg" }, typeof negative === "string" ? negative : fmt(negative))] : null));
  }
  if (inputs !== undefined) {
    const list = Array.isArray(inputs) ? inputs : [inputs];
    sections.push(h("section", { class: "block" }, h("h2", {}, "Eingaben"),
      list.length ? h("ul", { class: "gen__inputs" }, list.map(inputItem)) : h("p", { class: "empty" }, "Keine.")));
  }
  if (Object.keys(other).length) sections.push(h("section", { class: "block" }, h("h2", {}, "Weitere Angaben"), kvTable(other)));
  if (workflow !== undefined) sections.push(h("section", { class: "block" }, workflowBlock(workflow)));
  mount(root,
    backLink(),
    h("article", { class: "genpage" },
      h("h1", { class: "view-title" }, "Generierung"),
      h("p", { class: "view-sub" }, label ? `Erstellt mit ${label}` : "Generierungsdetails",
        sim === true ? [" · ", simNote(true)] : null,
        postId != null ? [" · ", h("a", { href: `#/p/${postId}` }, "Zum Post")] : null),
      genCredits(credits, { detail: true }),
      h("p", { class: "gen__id" }, h("span", { class: "tag" }, pid)),
      ...sections));
}

// --- profile -----------------------------------------------------------------------------------

export async function profileView(root, slug) {
  const a = await api.account(slug);
  const posts = Array.isArray(a.posts) ? a.posts : [];
  const sections = [];
  if (a.about?.length || a.facts) {
    sections.push(h("section", { class: "block" }, h("h2", {}, "Über mich"),
      (a.about || []).map((t) => h("p", { class: "prose" }, t)),
      a.facts ? h("p", { class: "facts-inline" }, a.facts) : null));
  }
  if (a.values?.length) {
    sections.push(h("section", { class: "block" }, h("h2", {}, "Das ist mir wichtig"),
      h("ul", { class: "chips" }, a.values.map((v) => h("li", {}, v)))));
  }
  if (a.day?.length) {
    sections.push(h("section", { class: "block" }, h("h2", {}, "Mein Tag"),
      h("ol", { class: "day" }, a.day.map((d) => h("li", {}, h("time", {}, d.time || ""), h("p", {}, d.text))))));
  }
  if (a.people?.length) {
    sections.push(h("section", { class: "block" }, h("h2", {}, "Meine Leute"),
      h("ul", { class: "rels" }, a.people.map((r) => {
        const acc = r.account || { slug: r.slug, display_name: r.slug, handle: "@" + r.slug, kind: "follower" };
        const inner = [avatar(acc, 34, false), h("div", {}, h("div", { class: "rel__name" }, acc.display_name), h("p", { class: "rel__line" }, r.text))];
        return h("li", {}, r.account ? h("a", { class: "rel", href: `#/u/${encodeURIComponent(r.slug)}` }, inner) : h("div", { class: "rel" }, inner));
      }))));
  }
  // 3-column square grid: poster (video/image) or the text itself; tap opens the post
  const grid = h("ul", { class: "grid" }, posts.map((p) => h("li", {}, h("a", {
    class: `grid__item${p.media_kind === "video" ? " grid__item--video" : ""}`, href: `#/p/${encodeURIComponent(p.id)}`,
    "aria-label": p.caption ? `Post: ${p.caption.slice(0, 80)}` : "Post",
  },
  p.poster_url ? h("img", { src: p.poster_url, alt: "", loading: "lazy" }) : h("span", { class: "grid__text" }, p.caption || ""),
  p.media_kind === "video" ? h("span", { class: "grid__kind", "aria-hidden": "true" }, icons.play()) : null,
  h("span", { class: "grid__count" }, "😂 ", compact(p.reactions?.lol || 0))))));
  const emptyPosts = a.kind === "house"
    ? h("div", { class: "empty-card" },
      h("p", {}, "Noch keine Sammlung. Die erste kommt am Sonntag."),
      h("a", { class: "pill", href: "#/woche" }, "Bis dahin: Diese Woche", icons.next()))
    : h("p", { class: "empty" }, "Noch keine Posts.");
  // counts: posts always; followers/following only where the data has them (never invented here)
  const count = (n, label) => (typeof n === "number" ? h("li", {}, h("b", {}, compact(n)), h("span", {}, label)) : null);
  const av = avatar(a, 88, false);
  av.className += " profile__avatar";
  mount(root,
    h("div", { class: "profilepage" },
      h("header", { class: "profile" },
        h("div", { class: "profile__top" },
          h("span", { class: "profile__ring" }, av),
          h("ul", { class: "stats" },
            count(typeof a.post_count === "number" ? a.post_count : posts.length, "Posts"),
            count(a.followers ?? a.follower_count, "Follower"),
            count(a.following ?? a.following_count, "folgt"))),
        h("div", { class: "profile__id" },
          h("h1", { class: "profile__name" }, a.display_name), earTag(a, { link: false })),
        a.bio ? h("p", { class: "profile__bio" }, a.bio) : null,
        a.followers_note ? h("p", { class: "stats__note" }, a.followers_note) : null,
        h("div", { class: "profile__actions" },
          h("button", { class: "pill", type: "button", onclick: () => shareProfile(a) }, icons.share(), "Profil teilen"),
          h("a", { class: "pill", href: "#/leute" }, "Leute"))),
      h("section", { class: "profilepage__posts", "aria-label": "Posts" },
        h("h2", { class: "profilepage__tab" }, icons.grid(), h("span", {}, "Posts")),
        posts.length ? grid : h("div", { class: "profilepage__empty" }, emptyPosts)),
      sections.length ? h("details", { class: "profile__more" },
        h("summary", {}, `Mehr über ${firstName(a.display_name)}`), ...sections) : null),
  );
}

function shareProfile(a) {
  const url = `${location.href.split("#")[0]}#/u/${encodeURIComponent(a.slug)}`;
  if (navigator.share) { navigator.share({ url, title: a.display_name }).catch(() => {}); return; }
  copy(url, "Link kopiert");
}

// --- leaderboards --------------------------------------------------------------------------------

function shiftDay(iso, n) {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function isoWeekOf(d) {
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  t.setUTCDate(t.getUTCDate() - ((t.getUTCDay() + 6) % 7) + 3);           // Thursday of that week
  const jan4 = new Date(Date.UTC(t.getUTCFullYear(), 0, 4));
  const thu1 = new Date(jan4);
  thu1.setUTCDate(jan4.getUTCDate() - ((jan4.getUTCDay() + 6) % 7) + 3);  // Thursday of week 1
  const week = 1 + Math.round((t - thu1) / (7 * 86400000));
  return `${t.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

function weekMonday(key) {
  const [y, w] = key.split("-W").map(Number);
  const jan4 = new Date(Date.UTC(y, 0, 4));
  const mon = new Date(jan4);
  mon.setUTCDate(jan4.getUTCDate() - ((jan4.getUTCDay() + 6) % 7) + (w - 1) * 7);
  return mon;
}

function shiftWeek(key, n) {
  const mon = weekMonday(key);
  mon.setUTCDate(mon.getUTCDate() + 7 * n);
  return isoWeekOf(mon);
}

function weekRange(key) {
  const mon = weekMonday(key);
  const sun = new Date(mon);
  sun.setUTCDate(mon.getUTCDate() + 6);
  const f = (d, opts) => d.toLocaleDateString("de-DE", { timeZone: "UTC", ...opts });
  const end = f(sun, { day: "numeric", month: "long", year: "numeric" });
  if (mon.getUTCMonth() === sun.getUTCMonth()) return `${f(mon, { day: "numeric" })}.–${end}`;
  return `${f(mon, { day: "numeric", month: "long", ...(mon.getUTCFullYear() !== sun.getUTCFullYear() ? { year: "numeric" } : {}) })} – ${end}`;
}

function scoreMeter(s, big = false) {
  const max = state.meta?.ranking?.score_max || 25;
  return h("span", { class: `meter${big ? " meter--big" : ""}`, title: `Score ${score(s)} von ${max}` },
    h("span", { class: "meter__num" }, score(s), h("small", {}, `/${max}`)),
    h("span", { class: "meter__bar", "aria-hidden": "true" }, h("span", { style: { width: `${Math.max(2, Math.min(100, (s / max) * 100))}%` } })));
}

function rankingNote() {
  const r = state.meta.ranking;
  const w = r.weights;
  return h("details", { class: "howto" }, h("summary", {}, "So wird gezählt"),
    h("p", {}, `Posts mit Pointe-Wertung aus dem Review (0–25): Score = ${dec(r.curated_weight)} × Pointe + ${dec(1 - r.curated_weight)} × Reaktionen. `,
      `Posts ohne Pointe-Wertung zählen nur über Reaktionen und kommen höchstens auf ${dec(r.uncurated_max)} von ${dec(r.score_max)}.`),
    h("p", {}, `Reaktionen: 😂 × ${dec(w.lol)}, 🔥 × ${dec(w.fire)}, 🌾 × ${dec(w.chew)}, ❤️ × ${dec(w.heart)}, Kommentar × ${dec(r.comment_weight)}; `,
      `daraus 0–25 Punkte (bei ${num(r.engagement_ref)} gewichteten Reaktionen etwa 16).`));
}

export async function topView(root, mode, key) {
  const meta = state.meta;
  const isWeek = mode === "woche";
  const nowKey = isWeek ? meta.week : meta.today;
  key = key || nowKey;
  if (isWeek ? !/^\d{4}-W\d{2}$/.test(key) : !/^\d{4}-\d{2}-\d{2}$/.test(key)) {
    const e = new Error("bad key");
    e.status = 404;
    throw e;
  }
  const data = await api.top(isWeek ? { week: key, limit: 10 } : { day: key, limit: 10 });
  const prev = isWeek ? shiftWeek(key, -1) : shiftDay(key, -1);
  const next = isWeek ? shiftWeek(key, 1) : shiftDay(key, 1);
  const base = isWeek ? "#/woche" : "#/heute";
  // relative titles carry no date, so the date appears exactly once (in the sub line)
  let title;
  let sub;
  if (isWeek) {
    const kw = Number(key.split("W")[1]);
    title = key === nowKey ? "Diese Woche" : key === shiftWeek(nowKey, -1) ? "Letzte Woche" : `KW ${kw}`;
    sub = `${title.startsWith("KW") ? "" : `KW ${kw} · `}${weekRange(key)}`;
  } else {
    title = key === nowKey ? "Heute" : key === shiftDay(nowKey, -1) ? "Gestern" : dayLabel(key);
    sub = title === "Heute" || title === "Gestern" ? longDay(key) : "Die lustigsten Posts des Tages";
  }
  const atEnd = key >= nowKey;    // no stepping into the future
  const board = data.items.length
    ? h("ol", { class: "board" }, data.items.map((it, i) => h("li", {},
      h("a", { class: `entry${i === 0 ? " entry--first" : ""}`, href: `#/p/${it.id}` },
        h("span", { class: "ranktag", "aria-label": `Platz ${it.rank}` }, String(it.rank)),
        h("div", { class: "entry__thumb" }, it.poster_url ? h("img", { src: it.poster_url, alt: "", loading: "lazy" }) : null),
        h("div", { class: "entry__body" },
          h("div", { class: "entry__who" }, avatar(it.account, 22, false), h("span", { class: "entry__name" }, it.account.display_name)),
          h("p", { class: "entry__cap" }, it.caption || it.title || ""),
          scoreMeter(it.score, i === 0),
          h("div", { class: "entry__nums" },
            h("span", {}, `😂 ${compact(it.reactions.lol)}`),
            h("span", {}, `💬 ${num(it.comment_count)}`),
            it.funny_score != null ? h("span", { class: "entry__pointe" }, `Pointe ${score(it.funny_score)}`) : null))))))
    : h("p", { class: "empty empty--pad" },
      atEnd && key > nowKey ? "Das liegt noch in der Zukunft."
        : isWeek ? "In dieser Woche wurde nichts gepostet." : "An diesem Tag wurde nichts gepostet.");
  mount(root,
    h("div", { class: "toppage" },
      h("div", { class: "toppage__head" },
        h("div", {}, h("h1", { class: "view-title" }, title), h("p", { class: "view-sub" }, sub)),
        h("div", { class: "stepper" },
          h("a", { href: `${base}/${prev}`, "aria-label": isWeek ? "Woche davor" : "Tag davor" }, icons.back()),
          atEnd
            ? h("span", { class: "stepper__off", "aria-disabled": "true", "aria-label": isWeek ? "Nächste Woche" : "Nächster Tag" }, icons.next())
            : h("a", { href: `${base}/${next}`, "aria-label": isWeek ? "Nächste Woche" : "Nächster Tag" }, icons.next()))),
      board,
      data.items.length && data.commands?.length ? h("section", { class: "export" },
        h("h2", {}, "Daraus eine Sammlung bauen"),
        h("p", {}, "Der Entwurf listet die besten Momente für die nächste Sammel-Story."),
        data.commands.map((c) => h("div", { class: "cmd" },
          h("div", { class: "cmd__text" }, h("span", { class: "cmd__label" }, c.label), h("code", { title: c.cmd }, cmdTokens(`weide.py ${c.tail}`))),
          h("button", { type: "button", onclick: () => copy(c.cmd), "aria-label": `${c.label}: Befehl kopieren` }, "Kopieren"))),
        rankingNote()) : null),
  );
}

// A command line wraps only between whole arguments: every word, and a flag together with its value
// ("--week 2026-W47"), is one no-wrap unit, so a narrow phone never splits "--json" into "-" / "-json".
function cmdTokens(line) {
  const words = line.split(/\s+/).filter(Boolean);
  const units = [];
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (w.startsWith("-") && words[i + 1] && !words[i + 1].startsWith("-")) units.push(`${w} ${words[++i]}`);
    else units.push(w);
  }
  return units.flatMap((u, i) => [i ? " " : null, h("span", { class: "cmd__tok" }, u)]);
}

// --- desktop rail ----------------------------------------------------------------------------------

function miniBoard(title, href, data, foot) {
  return h("section", { class: "railcard" },
    h("div", { class: "railcard__head" }, h("h2", {}, title), h("a", { href }, "Alle")),
    data.items.length
      ? h("ol", { class: "mini" }, data.items.map((it) => h("li", {}, h("a", { href: `#/p/${it.id}` },
        h("span", { class: "ranktag ranktag--s" }, String(it.rank)),
        h("span", { class: "mini__thumb" }, it.poster_url ? h("img", { src: it.poster_url, alt: "", loading: "lazy" }) : null),
        h("span", { class: "mini__body" },
          h("span", { class: "mini__name" }, it.account.display_name),
          h("span", { class: "mini__cap" }, it.caption || it.title || "")),
        h("span", { class: "mini__score" }, score(it.score))))))
      : h("p", { class: "empty" }, "Noch nichts gepostet."),
    foot || null);
}

export async function railView(root) {
  const meta = state.meta;
  const [day, week] = await Promise.all([api.top({ day: meta.today, limit: 3 }), api.top({ week: meta.week, limit: 3 })]);
  mount(root,
    miniBoard("Heute", "#/heute", day),
    miniBoard("Diese Woche", "#/woche", week,
      h("p", { class: "railcard__foot" }, "Am Sonntag wird daraus die Sammlung der Woche von ",
        h("a", { href: "#/u/highlights" }, "Highlights"), ".")));
}

// --- people ------------------------------------------------------------------------------------------

export async function peopleView(root) {
  const { items } = await api.accounts();
  // creators: every animal, robot or other character, and humans once they post; regulars: the rest
  const creators = items.filter((a) => ["animal", "robot", "other"].includes(a.kind) || (a.kind === "human" && a.posts > 0));
  const humans = items.filter((a) => (a.kind === "human" && !(a.posts > 0)) || a.kind === "house");
  const followers = items.filter((a) => a.kind === "follower");
  const card = (a) => h("li", {}, h("a", { class: "person", href: `#/u/${encodeURIComponent(a.slug)}` },
    avatar(a, 56, false), h("span", { class: "person__name" }, a.display_name), earTag(a, { link: false }),
    h("span", { class: "person__n" }, `${compact(a.followers)} Follower · ${plural(a.posts, "Post", "Posts")}`)));
  mount(root,
    h("h1", { class: "view-title" }, "Leute"),
    h("p", { class: "view-sub" }, "Creator, Stammgäste und alle, die mitlesen."),
    h("section", { class: "block block--flush" }, h("h2", {}, "Creator"), h("ul", { class: "people" }, creators.map(card))),
    humans.length ? h("section", { class: "block block--flush" }, h("h2", {}, "Stammgäste"), h("ul", { class: "people" }, humans.map(card))) : null,
    followers.length ? h("section", { class: "block block--flush" }, h("h2", {}, "Follower"),
      h("ul", { class: "followers" }, followers.map((a) => h("li", {}, h("a", { href: `#/u/${encodeURIComponent(a.slug)}` },
        avatar(a, 34, false), h("div", {}, h("div", { class: "comment__name" }, a.display_name), h("div", { class: "sub" }, a.handle))))))) : null,
  );
}
