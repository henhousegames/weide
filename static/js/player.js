// Full-screen feed playback: the video that is >= 60 % visible plays muted (one at a time), everything
// else pauses. Tap toggles sound; the choice is remembered for the session. Sources are only attached
// to videos near the viewport (lazy). prefers-reduced-motion: no autoplay, tap to play.
import { h, icons, reducedMotion } from "./util.js";

const SOUND = "app:sound";
function loadSound() {
  try { return sessionStorage.getItem(SOUND) === "1"; } catch { return false; }
}
function saveSound(on) {
  try { sessionStorage.setItem(SOUND, on ? "1" : "0"); } catch { /* storage unavailable */ }
}

let soundOn = loadSound();
let current = null;
const players = new Set();

function attach(v) {
  if (!v.getAttribute("src") && v.dataset.src) {
    v.preload = "metadata";
    v.src = v.dataset.src;
  }
}

const io = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const v = e.target;
    if (e.isIntersecting && e.intersectionRatio >= 0.6) {
      if (!reducedMotion()) activate(v);
    } else if (v === current) { v.pause(); current = null; refresh(); }
  }
}, { threshold: [0, 0.6, 0.85] });

// one screen ahead and behind: attach the source so the next swipe starts quickly
const near = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) attach(e.target);
}, { rootMargin: "100% 0px" });

function activate(v) {
  if (current && current !== v) current.pause();
  current = v;
  attach(v);
  v.muted = !soundOn;
  const p = v.play();
  if (p) p.catch(() => { v.muted = true; v.play().catch(() => {}); refresh(); });
  refresh();
}

function refresh() {
  for (const entry of players) {
    const { video, button } = entry;
    if (!video.isConnected) { io.unobserve(video); near.unobserve(video); players.delete(entry); continue; }
    const on = soundOn && video === current && !video.muted;
    button.replaceChildren(on ? icons.soundOn() : icons.soundOff(), on ? "Ton an" : "Ton aus");
    button.setAttribute("aria-pressed", String(on));
    button.setAttribute("aria-label", on ? "Ton ausschalten" : "Ton einschalten");
  }
}

export function videoPlayer(src, poster, { autoplay = true } = {}) {
  const video = h("video", {
    playsinline: true, "webkit-playsinline": true, loop: true, muted: true, preload: "none",
    poster: poster || null, "data-src": src, "aria-label": "Video",
  });
  video.muted = true;
  const button = h("button", { class: "media__sound", type: "button", "aria-pressed": "false", "aria-label": "Ton einschalten" });
  const tap = h("button", { class: "media__tap", type: "button", "aria-label": "Abspielen, Ton an oder aus" },
    h("span", { class: "media__play", "aria-hidden": "true" }, icons.play()));
  const toggle = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (current !== video || video.paused) {
      if (ev.currentTarget === button || current === video) soundOn = true;
      saveSound(soundOn);
      activate(video);
      return;
    }
    soundOn = !soundOn;
    saveSound(soundOn);
    video.muted = !soundOn;
    refresh();
  };
  button.addEventListener("click", toggle);
  tap.addEventListener("click", toggle);
  const state = (playing) => () => video.parentNode?.classList?.toggle("is-playing", playing);
  video.addEventListener("play", state(true));
  video.addEventListener("pause", state(false));
  // landscape / square sources: show whole frame (contain) instead of cropping to the portrait slide
  video.addEventListener("loadedmetadata", () => {
    if (video.videoHeight && video.videoWidth / video.videoHeight > 0.75) video.parentNode?.classList?.add("media--wide");
  });
  const entry = { video, button };
  players.add(entry);
  if (autoplay) { io.observe(video); near.observe(video); }
  queueMicrotask(refresh);
  return [video, tap, button];
}

export function resetPlayers() {
  for (const { video } of players) { video.pause(); io.unobserve(video); near.unobserve(video); }
  players.clear();
  current = null;
}
