const API = window.API_BASE || "/api";
const CHANNEL_KEY = String(window.CHANNEL_KEY || "").trim();
const CHANNEL_NAME = String(window.CHANNEL_NAME || "").trim();
const PAGE_SIZE = 100;
const MAX_ITEMS = 300;
const MAX_PAGES = 10;

let playlistItems = [];
let currentIndex = -1;
let shuffleEnabled = false;

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return response.json();
}

function safe(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function escapeHtml(value) {
  return safe(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setStatus(message, type = "") {
  const statusEl = document.getElementById("channel-status");
  if (!statusEl) return;
  statusEl.textContent = safe(message);
  statusEl.classList.remove("reddit-ready", "reddit-error");
  if (type === "ready") statusEl.classList.add("reddit-ready");
  if (type === "error") statusEl.classList.add("reddit-error");
}

function formatDuration(seconds) {
  const totalSeconds = Number(seconds);
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return "-";
  const rounded = Math.floor(totalSeconds);
  const minutes = Math.floor(rounded / 60);
  const secs = rounded % 60;
  const hours = Math.floor(minutes / 60);
  if (hours > 0) {
    return `${hours}:${String(minutes % 60).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function fileLabel(item) {
  const originalPath = safe(item?.original_path).replace(/\\/g, "/");
  const leaf = originalPath.split("/").filter(Boolean).pop() || `Video ${safe(item?.id)}`;
  const pathDate = safe(item?.path_date).trim();
  return pathDate ? `${pathDate} | ${leaf}` : leaf;
}

function renderPlaylist() {
  const listEl = document.getElementById("channel-playlist");
  if (!listEl) return;

  if (!Array.isArray(playlistItems) || playlistItems.length === 0) {
    listEl.innerHTML = `<div class="category-empty">Sem videos neste canal.</div>`;
    return;
  }

  listEl.innerHTML = playlistItems.map((item, index) => {
    const activeClass = index === currentIndex ? " is-active" : "";
    return `
      <button type="button" class="channel-playlist-item${activeClass}" data-index="${index}">
        <span class="channel-playlist-item-title">${escapeHtml(fileLabel(item))}</span>
        <span class="channel-playlist-item-meta">#${escapeHtml(item.id)} | ${escapeHtml(formatDuration(item.duration_seconds))}</span>
      </button>
    `;
  }).join("");
}

function updateNowPlaying() {
  const nowPlayingEl = document.getElementById("channel-now-playing");
  const metaEl = document.getElementById("channel-meta");
  if (!nowPlayingEl || !metaEl) return;

  if (currentIndex < 0 || currentIndex >= playlistItems.length) {
    nowPlayingEl.textContent = "Nenhum item carregado.";
    metaEl.textContent = `${playlistItems.length} videos`;
    return;
  }

  const item = playlistItems[currentIndex];
  nowPlayingEl.textContent = `Tocando ${currentIndex + 1}/${playlistItems.length}: ${fileLabel(item)}`;
  metaEl.textContent = `${playlistItems.length} videos | ${shuffleEnabled ? "shuffle ligado" : "ordem normal"} | HLS real disponivel`;
}

function resolveNextIndex(direction = 1) {
  if (!playlistItems.length) return -1;
  if (shuffleEnabled && playlistItems.length > 1) {
    let next = currentIndex;
    while (next === currentIndex) {
      next = Math.floor(Math.random() * playlistItems.length);
    }
    return next;
  }
  if (currentIndex < 0) return 0;
  const size = playlistItems.length;
  return (currentIndex + direction + size) % size;
}

async function playIndex(index) {
  const playerEl = document.getElementById("channel-player");
  if (!(playerEl instanceof HTMLVideoElement)) return;
  if (index < 0 || index >= playlistItems.length) return;

  currentIndex = index;
  const item = playlistItems[index];
  playerEl.src = safe(item.stream_url);
  playerEl.load();
  updateNowPlaying();
  renderPlaylist();

  try {
    await playerEl.play();
    setStatus(`Canal carregado: ${CHANNEL_NAME || CHANNEL_KEY}.`, "ready");
  } catch (error) {
    setStatus(`Playback pronto, mas o navegador exigiu interacao manual: ${safe(error.message)}`, "error");
  }
}

async function loadChannelPlaylist() {
  if (!CHANNEL_NAME) {
    setStatus("Canal invalido: categoria ausente.", "error");
    return;
  }

  const titleEl = document.getElementById("channel-title");
  if (titleEl) {
    titleEl.textContent = CHANNEL_NAME;
  }

  const galleryLinkEl = document.getElementById("channel-gallery-link");
  if (galleryLinkEl) {
    galleryLinkEl.href = `/admin/category?name=${encodeURIComponent(CHANNEL_NAME)}`;
  }

  const m3uLinkEl = document.getElementById("channel-m3u-link");
  if (m3uLinkEl) {
    m3uLinkEl.href = `${API}/channels/playlist.m3u?category=${encodeURIComponent(CHANNEL_NAME)}&order_by=detected_desc&limit=300`;
  }

  const liveLinkEl = document.getElementById("channel-live-link");
  if (liveLinkEl) {
    liveLinkEl.href = `${API}/channels/${encodeURIComponent(CHANNEL_KEY)}/hls/index.m3u8`;
  }

  setStatus("Carregando videos do canal...");
  let cursor = null;
  let page = 0;
  const items = [];

  while (page < MAX_PAGES && items.length < MAX_ITEMS) {
    const params = new URLSearchParams();
    params.set("category", CHANNEL_NAME);
    params.set("page_size", String(PAGE_SIZE));
    params.set("order_by", "detected_desc");
    if (cursor) {
      params.set("cursor", cursor);
    }

    const payload = await api(`/feed?${params.toString()}`);
    const batch = Array.isArray(payload?.items) ? payload.items.filter((item) => safe(item?.stream_url).trim()) : [];
    items.push(...batch);
    cursor = safe(payload?.next_cursor).trim() || null;
    page += 1;
    if (!cursor || batch.length === 0) {
      break;
    }
  }

  playlistItems = items.slice(0, MAX_ITEMS);
  currentIndex = playlistItems.length > 0 ? 0 : -1;
  renderPlaylist();
  updateNowPlaying();

  if (!playlistItems.length) {
    setStatus("Nenhum video com stream disponivel neste canal.", "error");
    return;
  }

  await playIndex(0);
}

document.getElementById("channel-prev-btn")?.addEventListener("click", () => {
  const nextIndex = resolveNextIndex(-1);
  if (nextIndex >= 0) {
    playIndex(nextIndex);
  }
});

document.getElementById("channel-next-btn")?.addEventListener("click", () => {
  const nextIndex = resolveNextIndex(1);
  if (nextIndex >= 0) {
    playIndex(nextIndex);
  }
});

document.getElementById("channel-shuffle-toggle")?.addEventListener("change", (event) => {
  shuffleEnabled = Boolean(event?.target?.checked);
  updateNowPlaying();
});

document.getElementById("channel-playlist")?.addEventListener("click", (event) => {
  const target = event.target?.closest?.("[data-index]");
  if (!target) return;
  const index = Number(target.getAttribute("data-index"));
  if (Number.isFinite(index)) {
    playIndex(index);
  }
});

document.getElementById("channel-player")?.addEventListener("ended", () => {
  const nextIndex = resolveNextIndex(1);
  if (nextIndex >= 0) {
    playIndex(nextIndex);
  }
});

document.getElementById("channel-player")?.addEventListener("error", () => {
  const nextIndex = resolveNextIndex(1);
  if (nextIndex >= 0) {
    setStatus("Falha no item atual. Avancando para o proximo.", "error");
    playIndex(nextIndex);
  }
});

loadChannelPlaylist().catch((error) => {
  setStatus(`Falha ao carregar canal: ${safe(error.message)}`, "error");
});
