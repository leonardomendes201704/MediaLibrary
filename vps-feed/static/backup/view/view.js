const stage = document.getElementById("stage");
const statusEl = document.getElementById("status");
const refreshButton = document.getElementById("refresh");
const countEl = document.getElementById("count");
const SEEN_STORAGE = "secretfeed.backup.seen";
const AUTO_MS = 5000;

const state = {
  seed: null,
  cursor: 0,
  items: [],
  loading: false,
  done: false,
  active: 0,
  generation: 0,
  catalogTotal: null,
};

const seen = loadSeen();
let persistTimer = 0;
let autoTimer = 0;

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

function activeType() {
  return state.items[state.active]?.type || "image";
}

function clearAutoAdvance() {
  window.clearTimeout(autoTimer);
  autoTimer = 0;
}

function advanceToNext() {
  if (state.items.length === 0) return;
  const next = stage.querySelector(`.slide[data-index="${state.active + 1}"]`);
  if (!next) {
    maybeLoadMore();
    scheduleAutoAdvance();
    return;
  }
  stage.scrollTo({ top: next.offsetTop, behavior: "smooth" });
}

function scheduleAutoAdvance() {
  clearAutoAdvance();
  if (state.items.length === 0 || state.loading) return;
  if (activeType() === "video") return;
  autoTimer = window.setTimeout(advanceToNext, AUTO_MS);
}

const observer = new IntersectionObserver(onIntersect, {
  root: stage,
  threshold: [0.6, 0.8],
});

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
  scheduleAutoAdvance();
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
    slide.dataset.type = item.type || "image";
    slide.dataset.key = item.key || "";
    slide.dataset.url = item.url;

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
        const slideIndex = Number(slide.dataset.index);
        if (slideIndex !== state.active) return;
        advanceToNext();
      });
      slide.append(progress, video);
    } else {
      const image = document.createElement("img");
      image.alt = "";
      image.draggable = false;
      slide.append(image);
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
    if (index === state.active) {
      if (media.ended) media.currentTime = 0;
      media.muted = true;
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
  return seen.size ? "Tudo já visto." : "Nenhuma midia de backup ainda.";
}

async function loadMore() {
  if (state.loading || state.done) return;
  const generation = state.generation;
  state.loading = true;
  if (state.items.length === 0) setStatus("Carregando…");
  try {
    const response = await fetch("/api/backup/feed", {
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
    scheduleAutoAdvance();
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
  clearAutoAdvance();
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

loadMore();
window.setInterval(() => {
  if (!state.done || state.loading || !state.seed) return;
  state.done = false;
  loadMore();
}, 20000);
