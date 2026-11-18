// Muted autoplay on scroll (one video at a time), tap for sound.
import { h, icons } from "./util.js";

let soundOn = false;
let current = null;
const players = new Set();

const io = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const v = e.target;
    if (e.isIntersecting && e.intersectionRatio >= 0.6) activate(v);
    else if (v === current) { v.pause(); current = null; }
  }
}, { threshold: [0, 0.6, 0.85] });

function activate(v) {
  if (current && current !== v) current.pause();
  current = v;
  if (!v.getAttribute("src") && v.dataset.src) v.src = v.dataset.src;
  v.muted = !soundOn;
  const p = v.play();
  if (p) p.catch(() => { v.muted = true; soundOn = false; refresh(); v.play().catch(() => {}); });
  refresh();
}

function refresh() {
  for (const entry of players) {
    const { video, button } = entry;
    if (!video.isConnected) { io.unobserve(video); players.delete(entry); continue; }
    const on = soundOn && video === current;
    button.replaceChildren(on ? icons.soundOn() : icons.soundOff(), on ? "Ton an" : "Ton aus");
    button.setAttribute("aria-pressed", String(on));
  }
}

export function videoPlayer(src, poster, { autoplay = true } = {}) {
  const video = h("video", {
    playsinline: true, loop: true, muted: true, preload: "none", poster: poster || null,
    "data-src": src, "aria-label": "Video",
  });
  video.muted = true;
  const button = h("button", { class: "media__sound", type: "button", "aria-pressed": "false" });
  const tap = h("button", { class: "media__tap", type: "button", "aria-label": "Ton an oder aus" });
  const toggle = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (current !== video) { soundOn = true; activate(video); return; }
    soundOn = !soundOn;
    video.muted = !soundOn;
    if (video.paused) video.play().catch(() => {});
    refresh();
  };
  button.addEventListener("click", toggle);
  tap.addEventListener("click", toggle);
  const entry = { video, button };
  players.add(entry);
  if (autoplay) io.observe(video);
  queueMicrotask(refresh);
  return [video, tap, button];
}

export function resetPlayers() {
  for (const { video } of players) { video.pause(); io.unobserve(video); }
  players.clear();
  current = null;
}
