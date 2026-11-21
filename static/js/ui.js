// Shared building blocks: ear tags, avatars, media, reactions, post cards.
import { api } from "./api.js";
import { videoPlayer } from "./player.js";
import { compact, h, icons, num, plural, store, when } from "./util.js";

export const state = { meta: null };

export function earTag(account, { link = true } = {}) {
  const mod = { human: "human", robot: "robot", follower: "follower", house: "house" }[account.kind];
  const cls = `tag${mod ? ` tag--${mod}` : ""}`;
  return link
    ? h("a", { class: cls, href: `#/u/${encodeURIComponent(account.slug)}` }, account.handle)
    : h("span", { class: cls }, account.handle);
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

export function media(post, { autoplay = true } = {}) {
  if (post.media_kind === "video") {
    const box = h("div", { class: "media media--video" });
    if (!post.media_exists) {
      if (post.poster_url) box.append(h("img", { src: post.poster_url, alt: "" }));
      box.append(h("div", { class: "media__missing" }, "Video wird noch gerendert."));
      return box;
    }
    box.append(...videoPlayer(post.media_url, post.poster_url, { autoplay }));
    if (post.kind === "collection") box.append(h("span", { class: "media__badge" }, "Sammlung"));
    else if (post.kind === "story") box.append(h("span", { class: "media__badge" }, "Story"));
    return box;
  }
  if (post.media_kind === "image") {
    return h("div", { class: "media media--image" },
      h("img", { src: post.poster_url || post.media_url, alt: post.caption ? `Bild: ${post.caption.slice(0, 80)}` : "Bild", loading: "lazy" }));
  }
  return h("div", { class: "textpost" }, post.caption || "");
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

export function reactionBar(post, { showComments = true } = {}) {
  const pressed = loadPressed(post.id);
  const bar = h("div", { class: "reacts" });
  for (const r of state.meta?.reactions || []) {
    const count = h("span", {}, compact(post.reactions?.[r.key] || 0));
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
  if (showComments) {
    bar.append(h("a", { class: "reacts__comments", href: `#/p/${post.id}`, "aria-label": `${post.comment_count} Kommentare` },
      icons.comment(), num(post.comment_count)));
  }
  return bar;
}

// "Erstellt mit <model>" links to the public generation summary (#/g/<public_id>). Legacy posts have
// no generation data; only the post page says so (withMissing), feed cards stay quiet.
export function genBadge(post, { withMissing = false } = {}) {
  const g = post.generation;
  if (g?.public_id) {
    return h("a", { class: "gen-badge", href: `#/g/${encodeURIComponent(g.public_id)}` },
      `Erstellt mit ${g.model_label || "einem unbenannten Modell"}`);
  }
  return withMissing ? h("span", { class: "gen-badge gen-badge--none" }, "Keine Generierungsdetails verfügbar") : null;
}

export function postHead(post) {
  return h("div", { class: "post__head" },
    avatar(post.account, 40),
    h("div", { class: "post__who" },
      h("a", { class: "post__name", href: `#/u/${encodeURIComponent(post.account.slug)}` }, post.account.display_name),
      h("div", { class: "post__meta" }, earTag(post.account), h("time", { class: "post__time", datetime: post.posted_at }, when(post.posted_at)))));
}

export function postCard(post) {
  const long = (post.caption || "").length > 140;
  const n = post.comment_count || 0;
  return h("article", { class: "post", "aria-label": `Post von ${post.account.display_name}` },
    postHead(post),
    media(post),
    reactionBar(post, { showComments: false }),
    post.kind === "text" ? (post.hashtags ? captionNode(post, false) : null) : captionNode(post, long),
    genBadge(post),
    h("a", { class: "post__more", href: `#/p/${post.id}` },
      n ? [icons.comment(), plural(n, "Kommentar", "Kommentare")] : "Zum Post"));
}
