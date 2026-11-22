// Full-screen feed playback: the video that is >= 60 % visible plays muted (one at a time), everything
// else pauses. A tap on the video plays/pauses it (a brief centred icon confirms); the speaker button
// (top right of the post) toggles sound, remembered for the session. A video the user paused stays
// paused while it is in view; scrolled away and back it autoplays again. Sources are only attached to
// videos near the viewport (lazy). prefers-reduced-motion: no autoplay, a tap plays.
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
const userPaused = new WeakSet();      // paused by a tap: no auto-resume while it stays in view

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
      if (!reducedMotion() && !userPaused.has(v)) activate(v);
    } else {
      userPaused.delete(v);              // scrolled away: may autoplay again when it comes back
      if (v === current) { v.pause(); current = null; refresh(); }
    }
  }
}, { threshold: [0, 0.6, 0.85] });

// one screen ahead and behind: attach the source so the next swipe starts quickly
const near = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) attach(e.target);
}, { rootMargin: "100% 0px" });

function activate(v) {
  if (current && current !== v) current.pause();
  current = v;
  userPaused.delete(v);
  attach(v);
  v.muted = !soundOn;
  const p = v.play();
  if (p) p.catch(() => { v.muted = true; v.play().catch(() => {}); refresh(); });
  refresh();
}

const soundIsOn = (video) => soundOn && video === current && !video.muted;

function refresh() {
  for (const entry of players) {
    const { video, button, tap } = entry;
    if (!video.isConnected) { io.unobserve(video); near.unobserve(video); players.delete(entry); continue; }
    const on = soundIsOn(video);
    // muted: icon + "Ton an" (a visible invitation); with sound on: the icon alone
    button.replaceChildren(on ? icons.soundOn() : icons.soundOff(),
      on ? null : h("span", { class: "media__soundtext", "aria-hidden": "true" }, "Ton an"));
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-pressed", String(on));
    button.setAttribute("aria-label", on ? "Ton ausschalten" : "Ton einschalten");
    tap.setAttribute("aria-label", video === current && !video.paused ? "Video anhalten" : "Video abspielen");
  }
}

// brief centred icon after a tap: the new state (play / pause); fades out (app.css)
function flash(box, playing) {
  box.querySelector(".media__flash")?.remove();
  const f = h("span", { class: "media__flash", "aria-hidden": "true" }, playing ? icons.play() : icons.pause());
  box.append(f);
  setTimeout(() => f.remove(), 700);
}

export function videoPlayer(src, poster, { autoplay = true } = {}) {
  const video = h("video", {
    playsinline: true, "webkit-playsinline": true, loop: true, muted: true, preload: "none",
    poster: poster || null, "data-src": src, "aria-label": "Video",
  });
  video.muted = true;
  const button = h("button", { class: "media__sound", type: "button", "aria-pressed": "false", "aria-label": "Ton einschalten" });
  const tap = h("button", { class: "media__tap", type: "button", "aria-label": "Video abspielen" },
    h("span", { class: "media__play", "aria-hidden": "true" }, icons.play()));
  // a tap on the video: play / pause (the sound setting stays as it is)
  tap.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const playing = current === video && !video.paused;
    if (playing) {
      userPaused.add(video);
      video.pause();
    } else {
      activate(video);
    }
    if (video.parentNode) flash(video.parentNode, !playing);
    refresh();
  });
  // the speaker: sound on / off for the session; turning it on also starts this video
  button.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    soundOn = !soundIsOn(video);
    saveSound(soundOn);
    if (soundOn && (current !== video || video.paused)) activate(video);
    else video.muted = !soundOn;
    refresh();
  });
  const state = (playing) => () => { video.parentNode?.classList?.toggle("is-playing", playing); refresh(); };
  video.addEventListener("play", state(true));
  video.addEventListener("pause", state(false));
  // landscape / square sources: show whole frame (contain) instead of cropping to the portrait slide
  video.addEventListener("loadedmetadata", () => {
    if (video.videoHeight && video.videoWidth / video.videoHeight > 0.75) video.parentNode?.classList?.add("media--wide");
  });
  const entry = { video, button, tap };
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
