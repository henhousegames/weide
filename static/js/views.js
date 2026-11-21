// Views: feed, post detail, profile, leaderboards, people, the desktop rail.
// Copy is in-world German; CLI hints live in the README (and the collapsed "Quelle und Ranking").
import { api } from "./api.js";
import { avatar, earTag, media, postCard, postHead, reactionBar, state } from "./ui.js";
import { compact, copy, dayLabel, dec, h, icons, longDay, mount, num, plural, score, when } from "./util.js";

const PAGE = 8;

export function notFound(root, text) {
  mount(root, h("div", { class: "notfound" },
    h("p", { class: "notfound__text" }, text),
    h("a", { class: "notfound__link", href: "#/" }, "Zum Feed")));
}

// Feed worlds (#/w/<world>): filter by the posting account's kind. "Alle" is the plain feed (#/).
// English and legacy names are accepted as aliases; "mixed"/"alle" mean everything.
export const WORLDS = [
  { key: "", label: "Alle", kinds: null },
  { key: "tiere", label: "Tiere", kinds: ["animal"] },
  { key: "menschen", label: "Menschen", kinds: ["human"] },
  { key: "roboter", label: "Roboter", kinds: ["robot"] },
];
const WORLD_ALIASES = { alle: "", all: "", mixed: "", animals: "tiere", weide: "tiere", humans: "menschen", robots: "roboter" };

function worldNav(current) {
  return h("nav", { class: "worlds", "aria-label": "Welten" }, WORLDS.map((w) => h("a", {
    class: "worlds__item", href: w.key ? `#/w/${w.key}` : "#/", "aria-current": w.key === current ? "page" : null,
  }, w.label)));
}

export async function feedView(root, worldArg) {
  const key = worldArg === undefined ? "" : (WORLD_ALIASES[String(worldArg).toLowerCase()] ?? String(worldArg).toLowerCase());
  const world = WORLDS.find((w) => w.key === key);
  if (!world) {
    const e = new Error("unknown world");
    e.status = 404;
    throw e;
  }
  const list = h("div", { class: "feed" });
  const sentinel = h("div", { class: "sentinel" });
  mount(root, worldNav(world.key), list, sentinel);
  let offset = 0;
  let loading = false;
  async function more() {
    if (loading || offset === null) return;
    loading = true;
    const page = await api.feed(offset, PAGE, world.kinds);
    if (!page.items.length && offset === 0) {
      list.append(h("p", { class: "empty empty--pad" }, world.key === "roboter"
        ? "Noch keine Posts. Die Roboter laden noch." : "Noch keine Posts. Die Creator schlafen noch."));
    }
    list.append(...page.items.map(postCard));
    offset = page.next_offset;
    loading = false;
    if (offset === null) sentinel.remove();
  }
  await more();
  const io = new IntersectionObserver((e) => { if (e[0].isIntersecting) more(); }, { rootMargin: "1200px 0px" });
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
      c.replies.length ? h("ul", { class: "comment__replies" }, c.replies.map(commentNode)) : null));
}

function likedBy(p) {
  const hearts = p.reactions.heart || 0;
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
        moments,
        h("section", { class: "block" },
          h("h2", {}, n ? plural(n, "Kommentar", "Kommentare") : "Kommentare"),
          p.comments.length ? h("ul", { class: "comments" }, p.comments.map(commentNode))
            : h("p", { class: "empty" }, "Noch keine Kommentare. Uwe ist bestimmt gleich da.")),
        h("details", { class: "more" }, h("summary", {}, "Quelle und Ranking"),
          h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))))),
  );
}

// --- profile -----------------------------------------------------------------------------------

export async function profileView(root, slug) {
  const a = await api.account(slug);
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
  const grid = h("div", { class: "grid" }, a.posts.map((p) => h("a", { href: `#/p/${p.id}`, "aria-label": p.caption || "Post" },
    p.poster_url ? h("img", { src: p.poster_url, alt: "", loading: "lazy" }) : h("div", { class: "grid__text" }, p.caption || ""),
    h("span", { class: "grid__count" }, "😂 ", compact(p.reactions?.lol || 0)))));
  const emptyPosts = a.kind === "house"
    ? h("div", { class: "empty-card" },
      h("p", {}, "Noch keine Sammlung. Die erste kommt am Sonntag."),
      h("a", { class: "pill", href: "#/woche" }, "Bis dahin: Diese Woche", icons.next()))
    : h("p", { class: "empty" }, "Noch keine Posts.");
  mount(root,
    h("div", { class: "profilepage" },
      h("div", { class: "profilepage__info" },
        h("div", { class: "profile" },
          h("div", { class: "profile__top" },
            avatar(a, 92, false),
            h("div", { class: "profile__id" }, h("h1", { class: "profile__name" }, a.display_name), earTag(a, { link: false }))),
          a.bio ? h("p", { class: "profile__bio" }, a.bio) : null,
          h("ul", { class: "stats" },
            h("li", {}, h("b", {}, compact(a.followers)), h("span", {}, "Follower")),
            h("li", {}, h("b", {}, num(a.posts.length)), h("span", {}, "Posts")),
            h("li", {}, h("b", {}, num(a.following)), h("span", {}, "folgt"))),
          a.followers_note ? h("p", { class: "stats__note" }, a.followers_note) : null),
        ...sections),
      h("section", { class: "block profilepage__posts" }, h("h2", {}, "Posts"), a.posts.length ? grid : emptyPosts)),
  );
  root.querySelector(".profile .avatar")?.classList.add("profile__avatar");
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
