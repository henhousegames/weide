// Shared building blocks: ear tags, avatars, media, reactions, post cards.
import { api } from "./api.js";
import { videoPlayer } from "./player.js";
import { DISCLAIMER, compact, h, icons, num, sharePost, store, svg, when } from "./util.js";

export const state = { meta: null };

// Account kinds as shown to people. "other" (identity.md kind "thing": a deposit bottle, a cap …) reads
// as an object: a small "Ding" badge with a box icon on the tag; the raw kind is never shown.
export const KIND_LABELS = { animal: "Tier", human: "Mensch", robot: "Roboter", other: "Ding", house: "Haus", follower: "Follower" };
export const isThing = (account) => account?.kind === "other" || account?.kind === "thing";

export function kindBadge(account) {
  if (!isThing(account)) return null;
  return h("span", { class: "tag__kind", title: "Ding – ein Gegenstand mit Account" },
    svg(["M4 8l8-4 8 4v8l-8 4-8-4z", "M4 8l8 4 8-4", "M12 12v8"]), "Ding");
}

export function earTag(account, { link = true } = {}) {
  const mod = { human: "human", robot: "robot", follower: "follower", house: "house", other: "thing", thing: "thing" }[account.kind];
  const cls = `tag${mod ? ` tag--${mod}` : ""}`;
  return link
    ? h("a", { class: cls, href: `#/u/${encodeURIComponent(account.slug)}` }, account.handle, kindBadge(account))
    : h("span", { class: cls }, account.handle, kindBadge(account));
}

function initials(name) {
  const words = String(name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/);
  return (words[0]?.[0] || "?").toUpperCase() + (words[1]?.[0] || "").toUpperCase();
}

// The house account's avatar: the ear tag itself (the app's one signature device).
const SVG_NS = "http://www.w3.org/2000/svg";
function tagMark() {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 32 32");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = '<path class="mark__tag" d="M9 4h14a5 5 0 0 1 5 5v12a7 7 0 0 1-7 7H11a7 7 0 0 1-7-7V9a5 5 0 0 1 5-5z"/>'
    + '<circle class="mark__hole" cx="16" cy="10" r="2.6"/><path class="mark__line" d="M11 18.5h10M11 22.5h6.5"/>';
  return s;
}

export function avatar(account, size = 36, link = true) {
  const style = { "--s": `${size}px` };
  const house = account.kind === "house" && !account.avatar_url;
  if (!account.avatar_url && !house) style.background = account.color || "#9BC53D";
  const inner = account.avatar_url
    ? h("img", { src: `${account.avatar_url}?size=${size > 64 ? 256 : 128}`, alt: "", loading: "lazy" })
    : house ? tagMark() : initials(account.display_name);
  const attrs = { class: `avatar${house ? " avatar--house" : ""}`, style, title: account.display_name };
  return link
    ? h("a", { ...attrs, href: `#/u/${encodeURIComponent(account.slug)}`, "aria-label": `Profil von ${account.display_name}` }, inner)
    : h("span", attrs, inner);
}

// Every post fills the same frame (a full-screen slide in the feed, 9:16 on the post page). Videos fill
// it (landscape sources are contained, see player.js); images keep their own shape (object-fit: contain)
// over a blurred, dimmed copy of themselves, so any aspect ratio looks intended; text posts are the text.
export function media(post, { autoplay = true } = {}) {
  if (post.media_kind === "video") {
    const box = h("div", { class: "media media--video" });
    if (!post.media_exists) {
      if (post.poster_url) box.append(h("img", { src: post.poster_url, alt: "" }));
      box.append(h("div", { class: "media__missing" }, "Video wird noch gerendert."));
      return box;
    }
    if (post.poster_url) box.append(h("img", { class: "media__backdrop", src: post.poster_url, alt: "", "aria-hidden": "true", loading: "lazy" }));
    box.append(...videoPlayer(post.media_url, post.poster_url, { autoplay }));
    if (post.kind === "collection") box.append(h("span", { class: "media__badge" }, "Sammlung"));
    else if (post.kind === "story") box.append(h("span", { class: "media__badge" }, "Story"));
    return box;
  }
  if (post.media_kind === "image") {
    const src = post.media_url || post.poster_url;
    return h("div", { class: "media media--image" },
      h("img", { class: "media__backdrop", src: post.poster_url || src, alt: "", "aria-hidden": "true", loading: "lazy" }),
      h("img", { class: "media__img", src, alt: post.caption ? `Bild: ${post.caption.slice(0, 80)}` : "Bild", loading: "lazy" }));
  }
  if (post.media_kind === "audio" && post.media_url) {      // audio-only (private preview): a plain player
    return h("div", { class: "media media--audio" },
      h("p", { class: "media__audiocap" }, post.caption || ""),
      h("audio", { class: "media__audio", src: post.media_url, controls: true, preload: "none" }));
  }
  const t = post.caption || "";
  return h("div", { class: `textpost${t.length > 260 ? " textpost--long" : t.length > 120 ? " textpost--mid" : ""}` },
    h("p", { class: "textpost__text" }, t));
}

function captionNode(post, clamp) {
  return h("p", { class: `caption${clamp ? " caption--clamp" : ""}` },
    post.kind === "text" ? null : post.caption || "",
    post.hashtags ? [post.kind === "text" ? "" : " ", h("span", { class: "caption__tags" }, post.hashtags)] : null);
}

// Pressed reactions per post live in localStorage under "<app slug>:r:<id>" (slug derived from the app
// name like config.APP_SLUG). Keys written under the previous name ("weide:r:<id>") are still read and
// merged in, so returning visitors keep their reactions after the rename.
const LEGACY_SLUG = "weide";
const appSlug = () => String(state.meta?.app_name || "").toLowerCase().replace(/[^a-z0-9]+/g, "") || "app";
const pressedKey = (id) => `${appSlug()}:r:${id}`;

function loadPressed(id) {
  const now = store(pressedKey(id)) || [];
  const old = appSlug() === LEGACY_SLUG ? [] : store(`${LEGACY_SLUG}:r:${id}`) || [];
  const all = [...new Set([...now, ...old])];
  if (all.length > now.length) store(pressedKey(id), all);     // migrate once to the new key
  return new Set(all);
}

// The action rail: one button per reaction (pressed state per browser), comments, share.
// rail: the vertical column on a feed slide (emoji above the count); else a row (post page).
export function reactionBar(post, { showComments = true, rail = false, share = true } = {}) {
  const pressed = loadPressed(post.id);
  const bar = h("div", { class: rail ? "reacts reacts--rail" : "reacts", role: "group", "aria-label": "Aktionen" });
  for (const r of state.meta?.reactions || []) {
    const count = h("span", { class: "react__n" }, compact(post.reactions?.[r.key] || 0));
    const btn = h("button", {
      class: `react react--${r.key}`, type: "button", "aria-pressed": String(pressed.has(r.key)),
      "aria-label": `${r.label}: ${num(post.reactions?.[r.key] || 0)}`, title: r.label,
    }, h("span", { class: "react__e", "aria-hidden": "true" }, r.emoji), count);
    btn.addEventListener("click", async () => {
      if (pressed.has(r.key)) return;
      pressed.add(r.key);
      store(pressedKey(post.id), [...pressed]);
      btn.setAttribute("aria-pressed", "true");
      btn.classList.remove("bump"); void btn.offsetWidth; btn.classList.add("bump");
      post.reactions = post.reactions || {};
      post.reactions[r.key] = (post.reactions[r.key] || 0) + 1;
      count.textContent = compact(post.reactions[r.key]);
      try {
        const res = await api.react(post.id, r.key);
        post.reactions = res.reactions;
        count.textContent = compact(res.reactions[r.key]);
      } catch { /* keep optimistic count */ }
    });
    bar.append(btn);
  }
  const n = post.comment_count || 0;
  if (showComments) {
    // opens the comment sheet over the feed (main.js listens); the link still works without it
    // (new tab, modifier keys): #/p/<id> has the same comments
    bar.append(h("a", { class: "react react--comments", href: `#/p/${encodeURIComponent(post.id)}`,
      "aria-label": `${num(n)} ${n === 1 ? "Kommentar" : "Kommentare"} – Kommentare öffnen`, "aria-haspopup": "dialog",
      onclick: (e) => {
        if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        document.dispatchEvent(new CustomEvent("app:comments", { detail: { post, opener: e.currentTarget } }));
      } },
    h("span", { class: "react__e", "aria-hidden": "true" }, icons.comment()), h("span", { class: "react__n" }, compact(n))));
  }
  if (share) {
    bar.append(h("button", {
      class: "react react--share", type: "button", "aria-label": "Teilen oder Link kopieren", title: "Teilen",
      onclick: () => sharePost(post.id, post.account?.display_name),
    }, h("span", { class: "react__e", "aria-hidden": "true" }, icons.share()), h("span", { class: "react__n" }, "Teilen")));
  }
  return bar;
}

export const SIM_NOTE = "Startbild aus eigener Simulation";
// small note next to the model label when the start frame came from our own simulation
export const simNote = (on) => (on === true ? h("span", { class: "gen-note" }, SIM_NOTE) : null);

// "Erstellt mit <model>" links to the public generation summary (#/g/<public_id>). Legacy posts have
// no generation data; only the post page says so (withMissing), feed cards stay quiet.
export function genBadge(post, { withMissing = false } = {}) {
  const g = post.generation;
  if (g?.public_id) {
    const badge = h("a", { class: "gen-badge", href: `#/g/${encodeURIComponent(g.public_id)}` },
      `Erstellt mit ${typeof g.model_label === "string" && g.model_label ? g.model_label : "einem unbenannten Modell"}`);
    return g.start_source_sim === true ? h("span", { class: "gen-line" }, badge, simNote(true)) : badge;
  }
  // curated preview feed: a model label without a public summary -> the same line, not a link
  if (typeof g?.model_label === "string" && g.model_label) {
    const badge = h("span", { class: "gen-badge" }, `Erstellt mit ${g.model_label}`);
    return g.start_source_sim === true ? h("span", { class: "gen-line" }, badge, simNote(true)) : badge;
  }
  return withMissing ? h("span", { class: "gen-badge gen-badge--none" }, "Keine Generierungsdetails verfügbar") : null;
}

// Credits under the model line: every credit text (licence notices such as "Powered by Stability AI",
// CC0 audio sources), small. Text via textContent only; a licence URL (http/https) links the text.
// `detail` adds model and licence id (generation page). No credits -> null (no empty element).
export const safeUrl = (u) => (typeof u === "string" && /^https?:\/\/[^\s"'<>]+$/i.test(u) ? u : null);

export function genCredits(credits, { detail = false } = {}) {
  const list = Array.isArray(credits) ? credits.filter((c) => c && typeof c.text === "string" && c.text) : [];
  if (!list.length) return null;
  return h("ul", { class: "gen-credits" }, list.map((c) => {
    const url = safeUrl(c.license?.url);
    const text = url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, c.text) : c.text;
    const extra = detail ? [typeof c.model === "string" ? c.model : null,
      typeof c.license?.id === "string" ? c.license.id : null].filter(Boolean).join(" · ") : "";
    return h("li", { class: `gen-credit${typeof c.kind === "string" ? ` gen-credit--${c.kind.replace(/[^a-z0-9_-]/gi, "")}` : ""}` },
      text, extra ? h("span", { class: "gen-credit__meta" }, ` · ${extra}`) : null);
  }));
}

export const disclaimer = () => (typeof state.meta?.disclaimer === "string" && state.meta.disclaimer) || DISCLAIMER;

// robot accounts: the flag from the API, or the kind for data exported before the flag existed
export const isRobot = (account) => account?.robot === true || (account?.robot !== false && account?.kind === "robot");

export function robotNote(post) {
  return isRobot(post.account) ? h("p", { class: "robot-note" }, disclaimer()) : null;
}

export function siteFooter() {
  return h("footer", { class: "site-foot" }, h("p", {}, disclaimer()));
}

// one comment with its nested replies (post page, comment sheet)
export function commentNode(c) {
  return h("li", { class: "comment" },
    avatar(c.account, 30),
    h("div", { class: "comment__body" },
      h("div", { class: "comment__who" },
        h("a", { class: "comment__name", href: `#/u/${encodeURIComponent(c.account.slug)}` }, c.account.display_name),
        h("time", { class: "comment__time", datetime: c.created_at }, when(c.created_at))),
      h("p", { class: "comment__text" }, c.text),
      c.replies?.length ? h("ul", { class: "comment__replies" }, c.replies.map(commentNode)) : null));
}

export function postHead(post, { size = 40 } = {}) {
  return h("div", { class: "post__head" },
    avatar(post.account, size),
    h("div", { class: "post__who" },
      h("a", { class: "post__name", href: `#/u/${encodeURIComponent(post.account.slug)}` }, post.account.display_name),
      h("div", { class: "post__meta" }, earTag(post.account), h("time", { class: "post__time", datetime: post.posted_at }, when(post.posted_at)))));
}

// Feed slide: one post per screen. Video, image or the text itself fills the frame; author, caption,
// generation badge + credits and the robot disclaimer sit on a gradient at the bottom, the action
// rail on the right (short-video feed).
export function postCard(post) {
  const href = `#/p/${encodeURIComponent(post.id)}`;
  const label = `Post von ${post.account.display_name}`;
  const text = post.kind === "text" || !post.media_kind;
  const cap = !text && (post.caption || post.hashtags)
    ? h("a", { class: "post__caption", href, "aria-label": `${label} öffnen` }, captionNode(post, true))
    : text && post.hashtags ? captionNode(post, false) : null;
  return h("article", { class: `post slide${text ? " post--text" : ""}`, "data-post-id": String(post.id), "aria-label": label },
    h("div", { class: "post__frame" },
      media(post),
      post.media_kind !== "video" ? h("a", { class: "post__open", href, "aria-label": `${label} öffnen`, tabindex: "-1" }) : null,
      h("div", { class: "post__overlay" },
        postHead(post),
        cap,
        genBadge(post),
        genCredits(post.generation?.credits),
        robotNote(post)),
      reactionBar(post, { showComments: true, rail: true })));
}
