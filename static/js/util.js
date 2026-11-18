// DOM + formatting helpers. Everything user-generated goes through textContent (never innerHTML).

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith("--")) el.style.setProperty(sk, sv);
        else el.style[sk] = sv;
      }
    }
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

// replaceChildren() that skips null/false (the native one prints "null")
export function mount(root, ...children) {
  root.replaceChildren();
  append(root, children);
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(paths, viewBox = "0 0 24 24") {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", viewBox);
  s.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
}

export const icons = {
  back: () => svg(["M15 5l-7 7 7 7"]),
  next: () => svg(["M9 5l7 7-7 7"]),
  comment: () => svg(["M4 5h16v11H9l-5 4z"]),
  soundOff: () => svg(["M4 9h4l5-4v14l-5-4H4z", "M17 9l4 6M21 9l-4 6"]),
  soundOn: () => svg(["M4 9h4l5-4v14l-5-4H4z", "M16.5 8.5a5 5 0 0 1 0 7", "M19 6a8.5 8.5 0 0 1 0 12"]),
};

const nf = new Intl.NumberFormat("de-DE");
export const num = (n) => nf.format(n ?? 0);

export function compact(n) {
  n = n ?? 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toLocaleString("de-DE", { maximumFractionDigits: 1 })} Mio.`;
  if (n >= 10_000) return `${Math.round(n / 1000).toLocaleString("de-DE")} Tsd.`;
  return nf.format(n);
}

export const score = (s) => (s ?? 0).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
export const dec = (n) => (n ?? 0).toLocaleString("de-DE", { maximumFractionDigits: 2 });
export const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;

const DAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
export function when(iso) {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff >= 0 && diff < 60) return "gerade eben";
  if (diff >= 0 && diff < 3600) return `vor ${Math.floor(diff / 60)} Min.`;
  if (diff >= 0 && diff < 86400) return `vor ${Math.floor(diff / 3600)} Std.`;
  const hm = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  if (diff >= 0 && diff < 6 * 86400) return `${DAYS[d.getDay()]}, ${hm}`;
  return d.toLocaleDateString("de-DE", { day: "numeric", month: "short", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
}

export function dayLabel(isoDate) {
  const d = new Date(isoDate + "T12:00:00");
  return `${DAYS[d.getDay()]}, ${d.toLocaleDateString("de-DE", { day: "numeric", month: "long" })}`;
}

export function longDay(isoDate) {
  return new Date(isoDate + "T12:00:00").toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

export function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage unavailable: ignore */ }
  return undefined;
}

export function toast(text) {
  const t = h("div", { class: "toast", role: "status" }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 1800);
}

export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Befehl kopiert");
  } catch {
    toast("Kopieren nicht möglich – bitte markieren");
  }
}
