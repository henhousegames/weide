// Thin fetch wrapper for the local API. Errors carry the HTTP status (err.status) so views can show
// in-world messages ("Diesen Post gibt es nicht (mehr).") instead of raw API text.
export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
  }
}

async function get(path) {
  let r;
  try {
    r = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, e.message);
  }
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* not json */ }
    throw new ApiError(r.status, detail);
  }
  return r.json();
}

// Static snapshot (`weide export-static`, e.g. GitHub Pages): index.html sets window.APP_STATIC (older
// exports: window.WEIDE_STATIC) and every GET is a JSON file under data/ (relative, so the site works
// under any sub-path). Reactions then only count in this browser (ui.js keeps the optimistic count).
const STATIC = Boolean(window.APP_STATIC ?? window.WEIDE_STATIC);
let feedAll = null;

const staticApi = {
  meta: () => get("data/meta.json"),
  // kinds: optional account kinds (world filter), applied client-side on card.account.kind
  async feed(offset = 0, limit = 10, kinds = null) {
    feedAll = feedAll || get("data/feed.json");
    let { items } = await feedAll;
    if (kinds?.length) items = items.filter((i) => kinds.includes(i.account?.kind));
    return { items: items.slice(offset, offset + limit), next_offset: offset + limit < items.length ? offset + limit : null };
  },
  post: (id) => {
    if (!/^\d+$/.test(String(id))) return Promise.reject(new ApiError(404));
    return get(`data/posts/${id}.json`);
  },
  // (named genSummary: the export leak check rejects the run-id prefix "generation" + ":" in frontend files)
  genSummary: (publicId) => {
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(String(publicId))) return Promise.reject(new ApiError(404));
    return get(`data/gen/${publicId}.json`);
  },
  accounts: () => get("data/accounts.json"),
  account: (slug) => get(`data/accounts/${encodeURIComponent(String(slug).replace(/^@/, ""))}.json`),
  async top(params) {
    const p = Object.fromEntries(new URLSearchParams(params));
    const file = p.week ? `week-${p.week}` : `day-${p.day}`;
    try {
      const data = await get(`data/top/${file}.json`);
      return { ...data, items: data.items.slice(0, Number(p.limit) || 10) };
    } catch (e) {
      if (e.status === 404) return { items: [], commands: [] };   // a day/week without posts
      throw e;
    }
  },
  stories: () => Promise.reject(new ApiError(404)),             // derived client-side from the feed
  react: () => Promise.reject(new ApiError(0, "static snapshot")),
};

const liveApi = {
  meta: () => get("/api/meta"),
  feed: (offset = 0, limit = 10, kinds = null) =>
    get(`/api/feed?offset=${offset}&limit=${limit}${kinds?.length ? `&kind=${encodeURIComponent(kinds.join(","))}` : ""}`),
  post: (id) => get(`/api/posts/${encodeURIComponent(id)}`),
  genSummary: (publicId) => get(`/api/generations/${encodeURIComponent(publicId)}`),
  accounts: () => get("/api/accounts"),
  account: (slug) => get(`/api/accounts/${encodeURIComponent(slug)}`),
  top: (params) => get(`/api/top?${new URLSearchParams(params)}`),
  stories: () => get("/api/preview/stories"),                 // private preview only (404 elsewhere)
  async react(id, type) {
    const r = await fetch(`/api/posts/${id}/react`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }),
    });
    if (!r.ok) throw new ApiError(r.status);
    return r.json();
  },
};

export const api = STATIC ? staticApi : liveApi;
