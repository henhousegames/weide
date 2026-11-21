// Private preview only (served by `weide serve --preview`, never part of the static export).
// Adds the slim PREVIEW bar, a small status pill on preview cards (approved-not-live / drafts) and turns
// the post page of a preview card (#/p/vs-<id>) into the draft detail: large media, prompt, model, task,
// output facts, created date and the Lab link. Cards themselves are rendered by the normal frontend; this
// script remembers which ids are previews (from the API responses) and decorates those cards.
// Reactions are disabled: the preview is read-only.
(function () {
  "use strict";
  const previews = new Map();          // card id -> preview info
  const remember = (card) => { if (card && card.preview) previews.set(String(card.id), card.preview); };
  const TASKS = { t2i: "Text → Bild", i2i: "Bild → Bild", t2v: "Text → Video", i2v: "Bild → Video" };
  const SHORT = { draft: "Entwurf", approved: "Freigegeben" };

  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await origFetch(input, init);
    const url = typeof input === "string" ? input : input && input.url;
    if (res.status === 401) { location.href = "/login"; return res; }
    if (res.ok && url && /^\/api\/(feed|posts\/|accounts\/)/.test(new URL(url, location.href).pathname)) {
      try {
        const data = await res.clone().json();
        (data.items || data.posts || []).forEach(remember);
        remember(data);
      } catch { /* not json */ }
      queueMicrotask(decorateAll);
    }
    return res;
  };

  function el(tag, cls, ...children) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    for (const c of children) if (c !== null && c !== undefined && c !== "") e.append(c);
    return e;
  }

  function labLink(p, cls, text) {
    if (!p.lab_url) return null;
    const a = el("a", cls, text);
    a.href = p.lab_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  }

  // small pill on the media: "● Entwurf" / "● Freigegeben" (full text in the title)
  function pill(p) {
    const m = el("span", `pv-pill pv-pill--${p.state}`, el("span", "pv-pill__dot"), SHORT[p.state] || p.label);
    m.title = p.label;
    return m;
  }

  function num(n, digits, min = 0) {
    return Number(n).toLocaleString("de-DE", { minimumFractionDigits: min, maximumFractionDigits: digits });
  }

  function date(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? d.toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" }) : null;
  }

  function facts(p) {
    const rows = [
      ["Modell", p.model],
      ["Aufgabe", p.task ? (TASKS[p.task] ? `${TASKS[p.task]} (${p.task})` : p.task) : null],
      ["Auflösung", p.width && p.height ? `${p.width} × ${p.height}` : null],
      ["Bildrate", p.fps ? `${num(p.fps, 2)} fps` : null],
      ["Dauer", p.duration_s ? `${num(p.duration_s, 1, 1)} s` : null],
      ["Ton", p.has_audio === true ? "ja" : p.has_audio === false && p.fps ? "nein" : null],
      ["Erstellt", date(p.created_at)],
    ].filter(([, v]) => v);
    const dl = el("dl", "pv-facts");
    for (const [k, v] of rows) dl.append(el("dt", null, k), el("dd", null, v));
    return dl;
  }

  function detailPanel(p) {
    const lab = labLink(p, "pv-lab", "Im Lab öffnen");
    if (lab) lab.append(el("span", "pv-lab__arrow", "↗"));
    const status = el("div", "pv-panel__status", pill(p),
      el("span", "pv-panel__hint", p.state === "draft" ? "wartet auf Freigabe" : "noch nicht live"));
    const prompt = p.prompt
      ? el("section", "pv-panel__block", el("h2", null, "Prompt"), el("p", "pv-prompt", p.prompt))
      : null;
    return el("section", "pv-panel", status, prompt,
      el("section", "pv-panel__block", el("h2", null, "Details"), facts(p)), lab);
  }

  function idOf(article) {
    if (article.dataset.postId) return article.dataset.postId;
    const m = /^#\/p\/(.+)$/.exec(article.classList.contains("postpage") ? location.hash : "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  function decorate(article) {
    if (article.dataset.pv) return;
    const id = idOf(article);
    const p = id && previews.get(id);
    if (!p) return;
    article.dataset.pv = p.state;
    article.classList.add("pv-card", `pv-card--${p.state}`);
    const media = article.querySelector(".post__frame, .postpage__media .media, .media, .textpost");
    if (media) media.append(el("div", "pv-tags", pill(p)));
    if (article.classList.contains("postpage")) {
      article.classList.add("pv-detail");
      if (p.caption_is_fallback) article.classList.add("pv-detail--nocaption");
      const body = article.querySelector(".postpage__body");
      if (body) body.append(detailPanel(p));
    }
  }

  function decorateAll() {
    document.querySelectorAll("article.post, article.postpage").forEach(decorate);
  }

  function banner() {
    const b = el("div", "pv-banner");
    b.setAttribute("role", "status");
    const st = el("span", "pv-banner__status");
    b.append(el("strong", "pv-banner__tag", "Preview"), el("span", "pv-banner__text", "privat"), st);
    document.body.prepend(b);
    document.documentElement.classList.add("pv");
    origFetch("/api/preview/status", { headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        if (!s) return;
        // notice: one line from the server when drafts/approvals are unavailable or stale
        st.textContent = s.notice || (!s.connected ? "Studio-Verbindung fehlt – Entwürfe und Freigaben nicht sichtbar"
          : `${s.generations} aus dem Studio`);
        if (s.notice) b.classList.add("pv-banner--notice");
      }).catch(() => {});
  }

  document.addEventListener("DOMContentLoaded", () => {
    banner();
    new MutationObserver(decorateAll).observe(document.getElementById("app") || document.body,
      { childList: true, subtree: true });
    decorateAll();
  });
})();
