// Hash router: #/  #/w/<world>  #/p/<id>  #/u/<slug>  #/heute[/<YYYY-MM-DD>]  #/woche[/<YYYY-Www>]  #/leute
import { api } from "./api.js";
import { resetPlayers } from "./player.js";
import { state } from "./ui.js";
import { dayLabel, h, mount } from "./util.js";
import { feedView, notFound, peopleView, postView, profileView, railView, topView } from "./views.js";

const app = document.getElementById("app");
const view = document.getElementById("view");
const rail = document.getElementById("rail");
const desktop = window.matchMedia("(min-width: 1024px)");
let cleanup = null;
const scrollPos = new Map();

// view: name used for layout (data-view on .app); missing: in-world text for a 404 of that view
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const [name = "", arg] = parts;
  switch (name) {
    case "": return { tab: "feed", view: "feed", run: (r) => feedView(r) };
    case "w": return { tab: "feed", view: "feed", missing: "Diese Welt gibt es nicht.", run: (r) => feedView(r, arg) };
    case "p": return { tab: null, view: "post", missing: "Diesen Post gibt es nicht (mehr).", run: (r) => postView(r, arg) };
    case "u": return { tab: arg === undefined ? "leute" : null, view: "profile", missing: "Dieses Profil gibt es nicht.", run: (r) => profileView(r, arg) };
    case "heute": return { tab: "heute", view: "heute", missing: "Diesen Tag gibt es nicht.", run: (r) => topView(r, "heute", arg) };
    case "woche": return { tab: "woche", view: "woche", missing: "Diese Woche gibt es nicht.", run: (r) => topView(r, "woche", arg) };
    case "leute": return { tab: "leute", view: "leute", run: (r) => peopleView(r) };
    default: return { tab: null, view: "error", run: (r) => notFound(r, "Diese Seite gibt es nicht.") };
  }
}

// the display name comes from config.APP_NAME (server-injected <title>, /api/meta); never hard-coded here
const appName = () => state.meta?.app_name || document.title;

let lastHash = location.hash;
async function render() {
  scrollPos.set(lastHash, window.scrollY);
  lastHash = location.hash;
  if (typeof cleanup === "function") cleanup();
  cleanup = null;
  resetPlayers();
  const r = route();
  app.dataset.view = r.view;
  for (const a of document.querySelectorAll(".tabs a")) {
    if (a.dataset.tab === r.tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  view.replaceChildren(h("p", { class: "loading" }, "Lädt …"));
  try {
    cleanup = await r.run(view);
    window.scrollTo(0, scrollPos.get(location.hash) || 0);
  } catch (e) {
    console.error(e);
    app.dataset.view = "error";
    if (e.status === 404 || e.status === 400) notFound(view, r.missing || "Diese Seite gibt es nicht.");
    else mount(view, h("p", { class: "error" }, `${appName()} hakt gerade. Bitte gleich nochmal versuchen.`));
  }
  refreshRail();
}

// right rail (desktop only): today's and this week's top 3, refreshed at most every 30 s
let railAt = 0;
async function refreshRail(force = false) {
  if (!desktop.matches) return;
  if (!force && Date.now() - railAt < 30_000) return;
  railAt = Date.now();
  try {
    await railView(rail);
  } catch (e) {
    console.error(e);
    rail.replaceChildren();
  }
}

async function start() {
  try {
    state.meta = await api.meta();
  } catch (e) {
    console.error(e);
    mount(view, h("p", { class: "error" }, `${appName()} ist gerade nicht erreichbar. Bitte gleich nochmal versuchen.`));
    return;
  }
  document.getElementById("today").textContent = dayLabel(state.meta.today);
  window.addEventListener("hashchange", render);
  desktop.addEventListener("change", () => refreshRail(true));
  render();
}

if ("scrollRestoration" in history) history.scrollRestoration = "manual";
start();
