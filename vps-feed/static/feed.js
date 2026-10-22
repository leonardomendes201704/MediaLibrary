const stage = document.getElementById("stage");
const statusEl = document.getElementById("status");
const refreshButton = document.getElementById("refresh");
const countEl = document.getElementById("count");
const SEEN_STORAGE = "secretfeed.seen";

const mutedIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" aria-hidden="true"><path d="M4 10v4h4l5 4V6L8 10H4z"/><path d="M17 9l4 6M21 9l-4 6"/></svg>';
const soundIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" aria-hidden="true"><path d="M4 10v4h4l5 4V6L8 10H4z"/><path d="M16 9a4 4 0 010 6"/><path d="M18.5 7a7 7 0 010 10"/></svg>';

const state = {
  seed: null,
  cursor: 0,
  items: [],
  loading: false,
  done: false,
  active: 0,
  sound: false,
  pointerY: 0,
  generation: 0,
  catalogTotal: null,
};

const seen = loadSeen();
let persistTimer = 0;

function loadSeen() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_STORAGE) || "[]");
    if (!Array.isArray(raw)) return new Set();
    return new Set(raw.filter((key) => typeof key === "string" && key));
  } catch (error) {
    return new Set();
  }
}

function persistSeen() {
  window.clearTimeout(persistTimer);
  try {
    localStorage.setItem(SEEN_STORAGE, JSON.stringify([...seen]));
  } catch (error) {}
}

function renderCount() {
  const total = state.catalogTotal == null ? "–" : String(state.catalogTotal);
  countEl.textContent = `${seen.size} / ${total}`;
}

function markSeen(key) {
  if (!key || seen.has(key)) return;
  seen.add(key);
  renderCount();
  window.clearTimeout(persistTimer);
  persistTimer = window.setTimeout(persistSeen, 300);
}

function rememberActive() {
  const item = state.items[state.active];
  if (item) markSeen(item.key);
}

const observer = new IntersectionObserver(onIntersect, {
  root: stage,
  threshold: [0.6, 0.8],
});

function subredditText(name) {
  const value = String(name || "").trim();
  if (!value) return "";
  const lower = value.toLowerCase();
  if (lower.startsWith("u_")) return `u/${value.slice(2)}`;
  if (lower.startsWith("u/") || lower.startsWith("r/")) return value;
  return `r/${value}`;
}

function setStatus(text) {
  if (!text) {
    statusEl.hidden = true;
    statusEl.textContent = "";
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = text;
}

function onIntersect(entries) {
  let best = null;
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    if (!best || entry.intersectionRatio > best.intersectionRatio) best = entry;
  }
  if (!best) return;
  const index = Number(best.target.dataset.index);
  if (Number.isNaN(index) || index === state.active) return;
  state.active = index;
  rememberActive();
  syncWindow();
  maybeLoadMore();
}

function appendSlides(items) {
  const fragment = document.createDocumentFragment();
  const created = [];
  for (const item of items) {
    const index = state.items.length;
    state.items.push(item);
    const slide = document.createElement("section");
    slide.className = "slide";
    slide.dataset.index = String(index);
    slide.dataset.type = item.type;
    slide.dataset.key = item.key || "";
    slide.dataset.url = item.url;
    const subreddit = subredditText(item.subreddit);
    slide.addEventListener("pointerdown", (event) => {
      state.pointerY = event.clientY;
    });
    slide.addEventListener("pointerup", (event) => {
      if (slide.dataset.type !== "video") return;
      if (Math.abs(event.clientY - state.pointerY) > 12) return;
      state.sound = !state.sound;
      syncWindow();
    });

    if (item.type === "video") {
      const progress = document.createElement("div");
      progress.className = "progress";
      const bar = document.createElement("span");
      progress.append(bar);
      const video = document.createElement("video");
      video.loop = false;
      video.muted = true;
      video.playsInline = true;
      video.setAttribute("playsinline", "");
      video.preload = "none";
      video.addEventListener("timeupdate", () => {
        if (!video.duration) return;
        bar.style.width = `${(video.currentTime / video.duration) * 100}%`;
      });
      video.addEventListener("ended", () => {
        const index = Number(slide.dataset.index);
        if (index !== state.active) return;
        const next = stage.querySelector(`.slide[data-index="${index + 1}"]`);
        if (!next) return;
        stage.scrollTo({ top: next.offsetTop, behavior: "smooth" });
      });
      const sound = document.createElement("div");
      sound.className = "sound";
      sound.innerHTML = mutedIcon;
      slide.append(progress, video, sound);
    } else {
      const image = document.createElement("img");
      image.alt = "";
      image.draggable = false;
      slide.append(image);
    }
    if (subreddit) {
      const label = document.createElement("p");
      label.className = "subreddit";
      label.textContent = subreddit;
      slide.append(label);
    }
    fragment.append(slide);
    created.push(slide);
  }
  stage.append(fragment);
  for (const slide of created) observer.observe(slide);
}

function mediaOf(slide) {
  return slide.querySelector("video, img");
}

function release(media) {
  if (!media || media.dataset.bound !== "1") return;
  if (media.tagName === "VIDEO") {
    media.pause();
    media.removeAttribute("src");
    media.load();
  } else {
    media.removeAttribute("src");
  }
  media.dataset.bound = "0";
}

function syncWindow() {
  const last = state.items.length - 1;
  const lo = Math.max(0, state.active - 1);
  const hi = Math.min(last, state.active + 2);
  for (const slide of stage.querySelectorAll(".slide")) {
    const index = Number(slide.dataset.index);
    const media = mediaOf(slide);
    if (!media) continue;
    if (index < lo || index > hi) {
      release(media);
      continue;
    }
    if (media.dataset.bound !== "1") {
      media.src = slide.dataset.url;
      media.dataset.bound = "1";
      if (media.tagName === "VIDEO") {
        media.preload = index === state.active + 1 ? "auto" : "metadata";
      }
    }
    if (index === state.active) rememberActive();
    if (media.tagName !== "VIDEO") continue;
    const sound = slide.querySelector(".sound");
    if (sound) sound.innerHTML = state.sound ? soundIcon : mutedIcon;
    if (index === state.active) {
      if (media.ended) media.currentTime = 0;
      media.muted = !state.sound;
      const play = media.play();
      if (play) play.catch(() => {});
    } else {
      media.pause();
    }
  }
}

function maybeLoadMore() {
  if (state.done || state.loading) return;
  if (state.items.length === 0 || state.active >= state.items.length - 3) {
    loadMore();
  }
}

function emptyMessage() {
  return seen.size ? "Tudo já visto." : "Nada para mostrar ainda.";
}

async function loadMore() {
  if (state.loading || state.done) return;
  const generation = state.generation;
  state.loading = true;
  if (state.items.length === 0) setStatus("Carregando…");
  try {
    const response = await fetch("/api/feed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cursor: state.cursor,
        limit: 6,
        seed: state.seed,
        exclude: [...seen],
      }),
    });
    if (generation !== state.generation) return;
    if (response.status === 503) {
      state.loading = false;
      setStatus("Carregando…");
      window.setTimeout(loadMore, 2000);
      return;
    }
    if (!response.ok) throw new Error(`feed ${response.status}`);
    const data = await response.json();
    if (generation !== state.generation) return;
    state.seed = data.seed || state.seed;
    if (typeof data.catalog_total === "number") state.catalogTotal = data.catalog_total;
    renderCount();
    const items = Array.isArray(data.items) ? data.items : [];
    if (items.length) appendSlides(items);
    if (typeof data.end_cursor === "number") state.cursor = data.end_cursor;
    else if (data.next_cursor != null) state.cursor = data.next_cursor;
    state.done = data.next_cursor == null;
    state.loading = false;
    if (state.items.length === 0) {
      setStatus(emptyMessage());
      return;
    }
    setStatus("");
    syncWindow();
    maybeLoadMore();
  } catch (error) {
    if (generation !== state.generation) return;
    state.loading = false;
    if (state.items.length === 0) {
      setStatus("Não foi possível carregar.");
      window.setTimeout(loadMore, 3000);
    }
  }
}

function refreshFeed() {
  persistSeen();
  state.generation += 1;
  for (const slide of stage.querySelectorAll(".slide")) observer.unobserve(slide);
  stage.replaceChildren();
  stage.scrollTop = 0;
  state.seed = null;
  state.cursor = 0;
  state.items = [];
  state.loading = false;
  state.done = false;
  state.active = 0;
  loadMore();
}

renderCount();
refreshButton.addEventListener("click", refreshFeed);
window.addEventListener("pagehide", persistSeen);

function isPhone() {
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (standalone) return false;
  return window.matchMedia("(hover: none) and (pointer: coarse)").matches;
}

function onFullscreenChange() {
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    document.removeEventListener("pointerdown", enterFullscreen, true);
  }
}

function enterFullscreen() {
  if (!isPhone()) return;
  if (document.fullscreenElement || document.webkitFullscreenElement) return;
  const root = document.documentElement;
  if (typeof root.requestFullscreen === "function") {
    root.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
    return;
  }
  if (typeof root.webkitRequestFullscreen === "function") {
    root.webkitRequestFullscreen();
  }
}

if (isPhone()) {
  document.addEventListener("fullscreenchange", onFullscreenChange);
  document.addEventListener("webkitfullscreenchange", onFullscreenChange);
  document.addEventListener("pointerdown", enterFullscreen, true);
}

loadMore();
window.setInterval(() => {
  if (!state.done || state.loading || !state.seed) return;
  state.done = false;
  loadMore();
}, 20000);
