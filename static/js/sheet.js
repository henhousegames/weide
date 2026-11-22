// Comment sheet: the rail's comment button opens the post's comments in a bottom sheet over the feed
// (read-only, nested replies; the same data as the post page, so curated preview posts work too).
// Comments load lazily when the sheet opens.
//
// History: opening pushes one history entry with the same URL (no route change, no hashchange), so the
// phone's Back button closes the sheet instead of leaving the feed. Closing by the button, Escape, the
// backdrop or a drag down goes back over that entry. A link inside the sheet ("Zum Beitrag", a
// commenter's profile) first closes the sheet (back over its entry), then navigates, so history stays
// feed -> target with no stale sheet entry in between.
//
// While open: focus is trapped in the sheet and restored to the opener on close, the rest of the app is
// inert and the page does not scroll.
import { api } from "./api.js";
import { commentNode } from "./ui.js";
import { h, icons, plural, reducedMotion } from "./util.js";

let open = null;      // { root, sheet, opener, pushed, pending, onKey, onPop }

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

// the markup (no listeners, no data): also used by the frontend render test
export function sheetMarkup(post) {
  const n = post.comment_count || 0;
  const href = `#/p/${encodeURIComponent(post.id)}`;
  const title = h("h2", { class: "sheet__title", id: "sheet-title" }, n ? plural(n, "Kommentar", "Kommentare") : "Kommentare");
  const close = h("button", { class: "sheet__close", type: "button", "aria-label": "Kommentare schließen" }, icons.close());
  const handle = h("div", { class: "sheet__handle", "aria-hidden": "true" });
  const list = h("div", { class: "sheet__list", "aria-live": "polite", "aria-busy": "true" }, h("p", { class: "loading" }, "Lädt …"));
  const link = h("a", { class: "sheet__link pill", href }, "Zum Beitrag", icons.next());
  const sheet = h("section", {
    class: "sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "sheet-title", tabindex: "-1",
    "data-post-id": String(post.id),
  },
  h("header", { class: "sheet__head" }, handle, title, close),
  list,
  h("footer", { class: "sheet__foot" }, link));
  const backdrop = h("div", { class: "sheet__backdrop", "aria-hidden": "true" });
  const root = h("div", { class: "sheet-layer" }, backdrop, sheet);
  return { root, sheet, backdrop, close, list, link, handle, title };
}

// fill the list from /api/posts/<id> (data/posts/<id>.json in the static export)
export async function loadComments(post, list) {
  try {
    const p = await api.post(post.id);
    const comments = Array.isArray(p.comments) ? p.comments : [];
    list.replaceChildren(comments.length
      ? h("ul", { class: "comments" }, comments.map(commentNode))
      : h("p", { class: "empty" }, "Noch keine Kommentare. Uwe ist bestimmt gleich da."));
  } catch {
    list.replaceChildren(h("p", { class: "empty" }, "Die Kommentare laden gerade nicht. Bitte gleich nochmal versuchen."));
  }
  list.setAttribute("aria-busy", "false");
}

function teardown() {
  if (!open) return;
  const { root, opener, onKey, onPop } = open;
  open = null;
  document.removeEventListener("keydown", onKey, true);
  window.removeEventListener("popstate", onPop);
  root.remove();
  const app = document.getElementById("app");
  if (app) app.inert = false;
  document.documentElement.classList.remove("sheet-open");
  if (opener?.isConnected) opener.focus({ preventScroll: true });
}

function close() {
  if (!open) return;
  if (open.pushed && history.state?.sheet !== undefined) history.back();    // onPop tears down
  else teardown();
}

// a route change elsewhere (main.js render): drop the sheet without touching history
export function closeSheetNow() {
  teardown();
}

export function openComments(post, opener) {
  if (open) teardown();
  const m = sheetMarkup(post);
  const state = { root: m.root, sheet: m.sheet, opener, pushed: false, pending: null };
  state.onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;
    const items = [...m.sheet.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (!items.length) { e.preventDefault(); m.sheet.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (!m.sheet.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && (document.activeElement === first || document.activeElement === m.sheet)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  state.onPop = () => {
    const target = state.pending;
    teardown();
    if (target) location.hash = target;
  };
  open = state;

  m.close.addEventListener("click", close);
  m.backdrop.addEventListener("click", close);
  // links inside (Zum Beitrag, profiles): close over the sheet's history entry first, then navigate
  m.sheet.addEventListener("click", (e) => {
    const a = e.target.closest?.("a[href^='#']");
    if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    state.pending = a.getAttribute("href");
    if (state.pushed && history.state?.sheet !== undefined) history.back();
    else { teardown(); location.hash = state.pending; }
  });
  dragToClose(m);

  document.body.append(m.root);
  document.documentElement.classList.add("sheet-open");
  const app = document.getElementById("app");
  if (app) app.inert = true;
  document.addEventListener("keydown", state.onKey, true);
  try {
    history.pushState({ ...(history.state || {}), sheet: String(post.id) }, "");
    state.pushed = true;
    window.addEventListener("popstate", state.onPop);
  } catch { /* history unavailable: the sheet still closes by button / Escape */ }
  if (!reducedMotion()) requestAnimationFrame(() => m.root.classList.add("is-open"));
  else m.root.classList.add("is-open");
  m.close.focus({ preventScroll: true });
  loadComments(post, m.list);
}

// drag the handle / header down: past 90 px (or a quick flick) closes, else it snaps back
function dragToClose(m) {
  const head = m.sheet.querySelector(".sheet__head");
  let y0 = null;
  let t0 = 0;
  let dy = 0;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    y0 = e.clientY; t0 = e.timeStamp; dy = 0;
    head.setPointerCapture?.(e.pointerId);
    m.sheet.classList.add("is-dragging");
  });
  head.addEventListener("pointermove", (e) => {
    if (y0 === null) return;
    dy = Math.max(0, e.clientY - y0);
    m.sheet.style.transform = `translateY(${dy}px)`;
  });
  const end = (e) => {
    if (y0 === null) return;
    const fast = dy > 30 && dy / Math.max(1, e.timeStamp - t0) > 0.6;
    y0 = null;
    m.sheet.classList.remove("is-dragging");
    m.sheet.style.transform = "";
    if (dy > 90 || fast) close();
  };
  head.addEventListener("pointerup", end);
  head.addEventListener("pointercancel", end);
}
