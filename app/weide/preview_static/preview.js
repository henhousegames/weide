// Private preview only (served by `weide serve --preview`, never part of the static export).
// Adds the fixed PREVIEW banner and marks preview cards (approved-not-live / drafts). Cards themselves
// are rendered by the normal frontend; this script only remembers which ids are previews (from the API
// responses) and decorates those cards. Reactions are disabled: the preview is read-only.
(function () {
  "use strict";
  const previews = new Map();          // card id -> preview info
  const remember = (card) => { if (card && card.preview) previews.set(String(card.id), card.preview); };

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

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function marker(p) {
    const m = el("div", `pv-marker pv-marker--${p.state}`);
    m.append(el("span", "pv-marker__label", p.label));
    if (p.state === "draft" && p.lab_url) {
      const a = el("a", "pv-marker__lab", "Im Lab öffnen");
      a.href = p.lab_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      m.append(a);
    }
    if (p.model) m.append(el("span", "pv-marker__model", p.model));
    return m;
  }

  function idOf(article) {
    const more = article.querySelector("a.post__more");
    const href = more ? more.getAttribute("href") : article.classList.contains("postpage") ? location.hash : "";
    const m = /^#\/p\/(.+)$/.exec(href || "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  function decorate(article) {
    if (article.dataset.pv) return;
    const id = idOf(article);
    const p = id && previews.get(id);
    if (!p) return;
    article.dataset.pv = p.state;
    article.classList.add("pv-card", `pv-card--${p.state}`);
    const head = article.querySelector(".post__head, .postpage__head");
    (head || article).after(marker(p));
    if (article.classList.contains("postpage") && p.prompt) {
      const d = el("details", "more pv-prompt");
      d.append(el("summary", null, "Prompt"), el("p", "pv-prompt__text", p.prompt));
      const body = article.querySelector(".postpage__body");
      if (body) body.append(d);
    }
  }

  function decorateAll() {
    document.querySelectorAll("article.post, article.postpage").forEach(decorate);
  }

  function banner() {
    const b = el("div", "pv-banner");
    b.setAttribute("role", "status");
    b.append(el("strong", "pv-banner__tag", "PREVIEW"), el("span", "pv-banner__text", "privat · nicht öffentlich"));
    const st = el("span", "pv-banner__status", "");
    b.append(st);
    document.body.prepend(b);
    document.documentElement.classList.add("pv");
    origFetch("/api/preview/status", { headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        if (!s) return;
        st.textContent = !s.connected ? "Studio nicht verbunden"
          : s.error && !s.last_poll ? "Studio nicht erreichbar"
          : `${s.generations} aus dem Studio`;
      }).catch(() => {});
  }

  document.addEventListener("DOMContentLoaded", () => {
    banner();
    new MutationObserver(decorateAll).observe(document.getElementById("app") || document.body,
      { childList: true, subtree: true });
    decorateAll();
  });
})();
