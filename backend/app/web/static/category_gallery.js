const API = window.API_BASE || "/api";
const CATEGORY = String(window.CATEGORY_NAME || "").trim();
const FAVORITES_MODE = Boolean(window.FAVORITES_MODE);
const RECENTS_MODE = Boolean(window.RECENTS_MODE);
const SECRET_MODE = Boolean(window.SECRET_MODE);
const IS_CATEGORY_VIEW = CATEGORY.length > 0 && !FAVORITES_MODE && !RECENTS_MODE && !SECRET_MODE;
const IS_USER_CATEGORY = CATEGORY.toLowerCase().startsWith("u_");

const PAGE_SIZE = 60;
const PREWARM_RANGE = "bytes=0-262143";
const PREWARM_TIMEOUT_MS = 2200;
const MAX_AUTO_LOAD_PAGES = 1200;
const PLAYBACK_SPEED_BY_KEY = {
  Digit1: 1,
  Numpad1: 1,
  Digit2: 2,
  Numpad2: 2,
  Digit3: 3,
  Numpad3: 3,
  Digit5: 5,
  Numpad5: 5,
};

let nextCursor = null;
let isLoading = false;
let loadedCount = 0;
let isModalOpen = false;
let prewarmController = null;
let deletingInFlight = false;
let blacklistingInFlight = false;
let advanceForwardTaskInFlight = false;
let autoLoadTaskInFlight = false;
let bulkCategoryDeleteInFlight = false;
let suppressFurtherGalleryUpdates = false;
let redditDownloadPollTimer = null;
let selectedPlaybackRate = 1;

const watchedMarkedInSession = new Set();
const favoritedInSession = new Set();
let activePlayback = null;
const categoryProgress = {
  loaded: false,
  total: 0,
  watched: 0,
  pendingTotalDelta: 0,
  pendingWatchedDelta: 0,
};

function safe(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function normalizeCategoryKey(value) {
  return safe(value).trim().toLowerCase();
}

function escapeHtml(value) {
  return safe(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDuration(seconds) {
  if (!seconds) return "-";
  const total = Math.max(0, Math.floor(seconds));
  const min = Math.floor(total / 60);
  const sec = total % 60;
  return `${min}:${String(sec).padStart(2, "0")}`;
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text}`);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function updateStatus(message, isError = false) {
  const statusEl = document.getElementById("gallery-status");
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.className = isError ? "gallery-status status-fail" : "gallery-status";
}

function getPageTitle() {
  if (FAVORITES_MODE) return "Favoritos";
  if (RECENTS_MODE) return "Recentes";
  if (SECRET_MODE) return "Nao vistos";
  if (!CATEGORY) return "Categoria";
  return `${IS_USER_CATEGORY ? "Usuario" : "Categoria"}: ${CATEGORY}`;
}

function getScopedEntityText() {
  if (!CATEGORY) return "categoria";
  return `${IS_USER_CATEGORY ? "usuario" : "categoria"} ${CATEGORY}`;
}

function getEmptyContextMessage() {
  if (SECRET_MODE) return "Nenhum video pendente encontrado.";
  return IS_USER_CATEGORY
    ? "Nenhum video encontrado para este usuario."
    : "Nenhum video encontrado para esta categoria.";
}

function getLoadingContextMessage() {
  if (FAVORITES_MODE) return "Filtro: Favoritos";
  if (RECENTS_MODE) return "Filtro: Recentes";
  if (SECRET_MODE) return "Filtro: Nao vistos";
  return getPageTitle();
}

function getLoadedContextMessage() {
  if (FAVORITES_MODE) return `Favoritos carregados: ${loadedCount} videos`;
  if (RECENTS_MODE) return `Recentes carregados: ${loadedCount} videos`;
  if (SECRET_MODE) return `Nao vistos carregados: ${loadedCount} videos`;
  return `${IS_USER_CATEGORY ? "Usuario carregado" : "Categoria carregada"}: ${loadedCount} videos`;
}

function configureGalleryTools() {
  const toolsEl = document.getElementById("gallery-tools");
  const deleteBtn = document.getElementById("gallery-delete-category-btn");
  const periodBtn = document.getElementById("gallery-period-download-btn");
  if (!toolsEl) return;

  toolsEl.hidden = !IS_CATEGORY_VIEW;
  if (!IS_CATEGORY_VIEW) return;

  if (deleteBtn) {
    deleteBtn.textContent = IS_USER_CATEGORY
      ? "Remover todos os videos do usuario"
      : "Remover todos os videos da categoria";
    deleteBtn.title = IS_USER_CATEGORY
      ? "Remove permanentemente todos os videos deste usuario, sem blacklist."
      : "Remove permanentemente todos os videos desta categoria, sem blacklist.";
  }

  if (periodBtn) {
    periodBtn.title = IS_USER_CATEGORY
      ? "Baixar videos antigos deste usuario por periodo."
      : "Baixar videos antigos desta categoria por periodo.";
  }
}

function clearGalleryGrid() {
  const gridEl = document.getElementById("thumb-grid");
  if (gridEl) {
    gridEl.innerHTML = "";
  }
  loadedCount = 0;
  nextCursor = null;
  watchedMarkedInSession.clear();
  favoritedInSession.clear();
  updateMeta();
  setLoadMoreState();
}

function hasCategoryProgress() {
  return IS_CATEGORY_VIEW && categoryProgress.loaded;
}

function getCategoryProgressText() {
  if (!hasCategoryProgress()) return "";
  return `vistos ${categoryProgress.watched} / ${categoryProgress.total}`;
}

function applyCategoryProgressDelta(totalDelta = 0, watchedDelta = 0) {
  if (!IS_CATEGORY_VIEW) return;

  if (!categoryProgress.loaded) {
    categoryProgress.pendingTotalDelta += totalDelta;
    categoryProgress.pendingWatchedDelta += watchedDelta;
    return;
  }

  categoryProgress.total = Math.max(0, categoryProgress.total + totalDelta);
  categoryProgress.watched = Math.max(
    0,
    Math.min(categoryProgress.total, categoryProgress.watched + watchedDelta),
  );
  updateMeta();
  updateModalCaption();
}

function setCategoryProgress(total, watched) {
  if (!IS_CATEGORY_VIEW) return;

  categoryProgress.loaded = true;
  categoryProgress.total = Math.max(0, (Number(total) || 0) + categoryProgress.pendingTotalDelta);
  categoryProgress.watched = Math.max(0, (Number(watched) || 0) + categoryProgress.pendingWatchedDelta);
  categoryProgress.watched = Math.min(categoryProgress.watched, categoryProgress.total);
  categoryProgress.pendingTotalDelta = 0;
  categoryProgress.pendingWatchedDelta = 0;
  updateMeta();
  updateModalCaption();
}

function buildModalCaption(baseCaption = "") {
  const progressText = getCategoryProgressText();
  if (!progressText) return baseCaption;
  if (!baseCaption) return progressText;
  return `${baseCaption} | ${progressText}`;
}

function updateModalCaption() {
  const captionEl = document.getElementById("video-modal-caption");
  if (!captionEl) return;

  const baseCaption = activePlayback && typeof activePlayback.caption === "string"
    ? activePlayback.caption
    : "";
  captionEl.textContent = buildModalCaption(baseCaption);
}

function formatPlaybackRate(rate) {
  return `${Number(rate) || 1}x`;
}

function isActivePlaybackFavorited() {
  if (!activePlayback || Number(activePlayback.videoId) <= 0) return false;
  return Boolean(activePlayback.isFavorite) || favoritedInSession.has(Number(activePlayback.videoId));
}

function updatePlaybackSpeedIndicator(rate = selectedPlaybackRate) {
  const indicatorEl = document.getElementById("video-speed-indicator");
  if (!indicatorEl) return;
  const speedLabel = formatPlaybackRate(rate);
  const favoriteLabel = isActivePlaybackFavorited()
    ? '<span class="video-speed-favorite" aria-hidden="true">&#10084;</span>'
    : "";
  indicatorEl.innerHTML = `<span class="video-speed-value">${speedLabel}</span>${favoriteLabel}`;
}

function applyPlaybackRate(rate, announce = false) {
  const normalizedRate = Number(rate);
  if (![1, 2, 3, 5].includes(normalizedRate)) return;

  selectedPlaybackRate = normalizedRate;
  updatePlaybackSpeedIndicator(selectedPlaybackRate);

  const playerEl = document.getElementById("video-modal-player");
  if (playerEl instanceof HTMLVideoElement) {
    playerEl.defaultPlaybackRate = selectedPlaybackRate;
    playerEl.playbackRate = selectedPlaybackRate;
  }

  if (announce && isModalOpen) {
    updateStatus(`Velocidade selecionada: ${formatPlaybackRate(selectedPlaybackRate)}.`);
  }
}

async function loadCategoryProgress() {
  if (!IS_CATEGORY_VIEW) return;

  try {
    const payload = await api("/feed/categories");
    const items = Array.isArray(payload) ? payload : [];
    const current = items.find((item) => normalizeCategoryKey(item?.category) === normalizeCategoryKey(CATEGORY));
    setCategoryProgress(current?.count || 0, current?.watched_count || 0);
  } catch (_) {
    // Keep the gallery usable even if the aggregate progress lookup fails.
  }
}

function getRedditDownloadElements() {
  return {
    periodSelectEl: document.getElementById("gallery-period-select"),
    periodDownloadBtn: document.getElementById("gallery-period-download-btn"),
    modalEl: document.getElementById("reddit-download-modal"),
    modalStopBtn: document.getElementById("reddit-download-stop-btn"),
    modalCloseBtn: document.getElementById("reddit-download-close-btn"),
    modalSummaryEl: document.getElementById("reddit-download-summary"),
    modalProgressFillEl: document.getElementById("reddit-download-progress-fill"),
    modalItemsEl: document.getElementById("reddit-download-items"),
    modalLogsEl: document.getElementById("reddit-download-logs"),
  };
}

function isRedditDownloadRunningState(state) {
  return String(state || "").toLowerCase() === "running";
}

function humanizeRedditDownloadStatus(value) {
  const normalized = safe(value).toLowerCase();
  if (normalized === "pending") return "Pendente";
  if (normalized === "loading") return "Preparando";
  if (normalized === "scanning") return "Buscando no periodo";
  if (normalized === "downloading") return "Baixando";
  if (normalized === "auto_waiting") return "Aguardando ociosidade";
  if (normalized === "auto_disabled") return "Auto-download desativado";
  if (normalized === "completed") return "Concluido";
  if (normalized === "completed_with_errors") return "Concluido com erros";
  if (normalized === "failed") return "Falhou";
  if (normalized === "skipped_blacklisted") return "Ignorado (blacklist)";
  if (normalized === "idle") return "Parado";
  if (normalized === "stopped") return "Interrompido";
  return safe(value) || "Desconhecido";
}

function renderRedditDownloadStatusBlock(summaryEl, progressFillEl, itemsEl, logsEl, status) {
  if (!summaryEl || !progressFillEl || !itemsEl || !logsEl) return;

  const state = safe(status?.state).toLowerCase() || "idle";
  const totalSubreddits = Number(status?.total_subreddits || 0);
  const completedSubreddits = Number(status?.completed_subreddits || 0);
  const planned = Number(status?.total_videos_planned || 0);
  const processed = Number(status?.processed_videos || 0);
  const failed = Number(status?.failed_videos || 0);
  const message = safe(status?.message).trim();
  const progressBase = planned > 0 ? planned : totalSubreddits > 0 ? totalSubreddits : 1;
  const progressCurrent = planned > 0 ? (processed + failed) : completedSubreddits;
  const percent = Math.max(0, Math.min(100, (progressCurrent * 100) / progressBase));

  summaryEl.textContent = `Estado: ${humanizeRedditDownloadStatus(state)} | Subreddits ${completedSubreddits}/${totalSubreddits} | Videos ${processed}/${planned} | Falhas ${failed}${message ? ` | ${message}` : ""}`;
  progressFillEl.style.width = `${percent.toFixed(2)}%`;

  const items = Array.isArray(status?.items) ? status.items : [];
  if (items.length === 0) {
    itemsEl.innerHTML = `<div class="reddit-empty">Sem itens de download ainda.</div>`;
  } else {
    itemsEl.innerHTML = items.map((item) => `
      <div class="reddit-download-item">
        <span class="name">${escapeHtml(item?.name || "")}</span>
        <span class="meta">${safe(item?.completed || 0)}/${safe(item?.planned || 0)} (${safe(item?.failed || 0)} falha)</span>
        <span class="status">${escapeHtml(humanizeRedditDownloadStatus(item?.status || "pending"))}</span>
      </div>
    `).join("");
  }

  const logs = Array.isArray(status?.logs) ? status.logs : [];
  logsEl.textContent = logs.length > 0 ? logs.join("\n") : "Sem logs ainda.";
  logsEl.scrollTop = logsEl.scrollHeight;
}

function openRedditDownloadModal() {
  const { modalEl } = getRedditDownloadElements();
  if (!modalEl) return;
  modalEl.classList.add("open");
  modalEl.setAttribute("aria-hidden", "false");
}

function closeRedditDownloadModal() {
  const { modalEl } = getRedditDownloadElements();
  if (!modalEl) return;
  modalEl.classList.remove("open");
  modalEl.setAttribute("aria-hidden", "true");
}

function renderRedditDownloadStatus(status) {
  const {
    periodSelectEl,
    periodDownloadBtn,
    modalSummaryEl,
    modalProgressFillEl,
    modalItemsEl,
    modalLogsEl,
    modalStopBtn,
    modalCloseBtn,
  } = getRedditDownloadElements();

  renderRedditDownloadStatusBlock(
    modalSummaryEl,
    modalProgressFillEl,
    modalItemsEl,
    modalLogsEl,
    status,
  );

  const state = safe(status?.state).toLowerCase() || "idle";
  const isRunning = isRedditDownloadRunningState(state);
  if (periodDownloadBtn) {
    periodDownloadBtn.disabled = isRunning;
  }
  if (periodSelectEl) {
    periodSelectEl.disabled = isRunning;
  }
  if (modalStopBtn) {
    modalStopBtn.disabled = !isRunning;
  }
  if (modalCloseBtn) {
    modalCloseBtn.disabled = isRunning;
  }
}

function stopRedditDownloadPolling() {
  if (redditDownloadPollTimer) {
    clearInterval(redditDownloadPollTimer);
    redditDownloadPollTimer = null;
  }
}

async function pollRedditDownloadStatusOnce() {
  try {
    const status = await api("/reddit/download/status");
    renderRedditDownloadStatus(status);
    if (!isRedditDownloadRunningState(status?.state)) {
      stopRedditDownloadPolling();
    }
  } catch (error) {
    stopRedditDownloadPolling();
    renderRedditDownloadStatus({
      state: "failed",
      items: [],
      logs: [`Falha ao consultar status de download: ${safe(error.message)}`],
      message: "Nao foi possivel consultar o status atual.",
    });
  }
}

function startRedditDownloadPolling() {
  stopRedditDownloadPolling();
  redditDownloadPollTimer = setInterval(() => {
    pollRedditDownloadStatusOnce();
  }, 1200);
}

async function loadCurrentRedditDownloadStatus() {
  try {
    const status = await api("/reddit/download/status");
    renderRedditDownloadStatus(status);
    if (isRedditDownloadRunningState(status?.state)) {
      if (!redditDownloadPollTimer) {
        startRedditDownloadPolling();
      }
    } else {
      stopRedditDownloadPolling();
    }
  } catch (_) {
    stopRedditDownloadPolling();
  }
}

async function startCategoryPeriodDownload() {
  if (!IS_CATEGORY_VIEW) {
    updateStatus("Download por periodo disponivel apenas em categoria/usuario.", true);
    return;
  }

  const { periodSelectEl } = getRedditDownloadElements();
  const periodMonths = Number(periodSelectEl?.value || 1);
  if (![1, 2, 3, 6, 12].includes(periodMonths)) {
    updateStatus("Periodo invalido.", true);
    return;
  }

  openRedditDownloadModal();
  renderRedditDownloadStatus({
    state: "running",
    total_subreddits: 1,
    completed_subreddits: 0,
    total_videos_planned: 0,
    processed_videos: 0,
    failed_videos: 0,
    items: [{ name: CATEGORY, status: "pending", planned: 0, completed: 0, failed: 0 }],
    logs: [`Iniciando download do periodo de ${periodMonths} mes(es)...`],
  });

  try {
    await api("/reddit/download/start", {
      method: "POST",
      body: JSON.stringify({
        names: [CATEGORY],
        period_months: periodMonths,
        per_subreddit_limit: null,
      }),
    });
    updateStatus(`Download por periodo iniciado para ${getScopedEntityText()} (${periodMonths} mes(es)).`);
    await pollRedditDownloadStatusOnce();
    startRedditDownloadPolling();
  } catch (error) {
    updateStatus(`Falha ao iniciar download por periodo: ${safe(error.message)}`, true);
    renderRedditDownloadStatus({
      state: "failed",
      items: [{ name: CATEGORY, status: "failed", planned: 0, completed: 0, failed: 0 }],
      logs: [`Falha ao iniciar download por periodo: ${safe(error.message)}`],
    });
  }
}

async function stopRedditDownloadProcess() {
  try {
    const status = await api("/reddit/download/stop", { method: "POST" });
    renderRedditDownloadStatus(status);
    updateStatus("Processo de download Reddit interrompido.");
  } catch (error) {
    updateStatus(`Falha ao interromper download: ${safe(error.message)}`, true);
  }
}

async function deleteCurrentCategoryVideos() {
  if (!IS_CATEGORY_VIEW) {
    updateStatus("Remocao em lote disponivel apenas em categoria/usuario.", true);
    return;
  }
  if (bulkCategoryDeleteInFlight) return;

  const entityLabel = IS_USER_CATEGORY ? "usuario" : "categoria";
  const confirmed = window.confirm(
    `Remover permanentemente todos os videos deste ${entityLabel}?\n\nIsso nao adiciona ${entityLabel} a blacklist e nao bloqueia novos downloads.`,
  );
  if (!confirmed) return;

  const deleteBtn = document.getElementById("gallery-delete-category-btn");
  bulkCategoryDeleteInFlight = true;
  suppressFurtherGalleryUpdates = true;
  if (deleteBtn) {
    deleteBtn.disabled = true;
  }
  closeVideoModal();

  try {
    const payload = await api("/videos/category/delete", {
      method: "POST",
      body: JSON.stringify({ category: CATEGORY }),
    });
    clearGalleryGrid();
    setCategoryProgress(0, 0);
    const deletedVideos = Number(payload?.deleted_videos || 0);
    if (deletedVideos > 0) {
      updateStatus(`Remocao de ${deletedVideos} video(s) de ${getScopedEntityText()} iniciada.`);
    } else {
      updateStatus(`Nenhum video encontrado em ${getScopedEntityText()}.`);
    }
  } catch (error) {
    suppressFurtherGalleryUpdates = false;
    updateStatus(`Falha ao remover videos: ${safe(error.message)}`, true);
  } finally {
    bulkCategoryDeleteInFlight = false;
    if (deleteBtn) {
      deleteBtn.disabled = false;
    }
  }
}

function renderWatchedBadge() {
  return `
    <span class="watched-badge" title="Visualizado">
      <span class="watched-icon" aria-hidden="true">&#10003;</span>
      Visualizado
    </span>
  `;
}

function renderFavoriteBadge() {
  return `
    <span class="favorite-badge" title="Favorito" aria-label="Favorito">
      <span class="favorite-icon" aria-hidden="true">&#10084;</span>
    </span>
  `;
}

function renderThumbCard(item) {
  const videoId = Number(item.id) || 0;
  const thumb = safe(item.thumbnail_url);
  const stream = safe(item.stream_url);
  const category = safe(item.path_category);
  const date = safe(item.path_date);
  const duration = formatDuration(item.duration_seconds);
  const isWatched = Boolean(item.is_watched);
  const isFavorite = Boolean(item.is_favorite);
  const caption = `Video #${safe(item.id)} | ${category || "-"} | ${date || "-"} | ${duration}`;
  const thumbHtml = thumb
    ? `<img src="${escapeHtml(thumb)}" loading="lazy" alt="Video ${safe(item.id)} thumbnail" />`
    : `<div class="thumb-placeholder">Sem thumbnail</div>`;
  const streamAttrs = stream
    ? `href="${escapeHtml(stream)}" data-stream="${escapeHtml(stream)}" data-caption="${escapeHtml(caption)}" data-video-id="${videoId}" data-is-watched="${isWatched ? "true" : "false"}" data-is-favorite="${isFavorite ? "true" : "false"}"`
    : "href=\"#\"";
  const watchedBadge = isWatched ? renderWatchedBadge() : "";
  const favoriteBadge = isFavorite ? renderFavoriteBadge() : "";

  return `
    <article class="thumb-card${isWatched ? " is-watched" : ""}${isFavorite ? " is-favorite" : ""}" data-video-id="${videoId}" data-is-watched="${isWatched ? "true" : "false"}" data-is-favorite="${isFavorite ? "true" : "false"}">
      <a class="thumb-media ${stream ? "" : "is-disabled"}" ${streamAttrs}>
        ${thumbHtml}
      </a>
      <div class="thumb-info">
        <div class="thumb-title-row">
          <div class="thumb-title">Video #${safe(item.id)}</div>
          <div class="thumb-badges">${favoriteBadge}${watchedBadge}</div>
        </div>
        <div class="thumb-meta">
          <span>${escapeHtml(category || "-")}</span>
          <span>${escapeHtml(date || "-")}</span>
          <span>${escapeHtml(duration)}</span>
        </div>
      </div>
    </article>
  `;
}

function ensureWatchedBadge(cardEl) {
  if (!cardEl || !(cardEl instanceof Element)) return;
  const badges = cardEl.querySelector(".thumb-badges");
  if (!badges) return;
  if (badges.querySelector(".watched-badge")) return;
  badges.insertAdjacentHTML("beforeend", renderWatchedBadge());
}

function ensureFavoriteBadge(cardEl) {
  if (!cardEl || !(cardEl instanceof Element)) return;
  const badges = cardEl.querySelector(".thumb-badges");
  if (!badges) return;
  if (badges.querySelector(".favorite-badge")) return;
  badges.insertAdjacentHTML("afterbegin", renderFavoriteBadge());
}

function syncFavoriteUi(videoId, isFavorite) {
  if (!videoId) return;

  const cards = document.querySelectorAll(`.thumb-card[data-video-id="${videoId}"]`);
  cards.forEach((cardEl) => {
    if (!(cardEl instanceof HTMLElement)) return;
    cardEl.dataset.isFavorite = isFavorite ? "true" : "false";
    if (isFavorite) {
      cardEl.classList.add("is-favorite");
      ensureFavoriteBadge(cardEl);
    } else {
      cardEl.classList.remove("is-favorite");
      const badge = cardEl.querySelector(".favorite-badge");
      if (badge) {
        badge.remove();
      }
    }
  });

  const links = document.querySelectorAll(`.thumb-media[data-video-id="${videoId}"]`);
  links.forEach((linkEl) => {
    if (linkEl instanceof HTMLElement) {
      linkEl.dataset.isFavorite = isFavorite ? "true" : "false";
    }
  });

  if (activePlayback && Number(activePlayback.videoId) === Number(videoId)) {
    activePlayback.isFavorite = Boolean(isFavorite);
    updatePlaybackSpeedIndicator(selectedPlaybackRate);
  }
}

function triggerFavoriteSplash() {
  const splashEl = document.getElementById("favorite-splash");
  if (!splashEl) return;
  splashEl.classList.remove("show");
  void splashEl.offsetWidth;
  splashEl.classList.add("show");
  window.setTimeout(() => {
    splashEl.classList.remove("show");
  }, 760);
}

function syncWatchedUi(videoId, watched) {
  if (!videoId) return;

  const cards = document.querySelectorAll(`.thumb-card[data-video-id="${videoId}"]`);
  cards.forEach((cardEl) => {
    if (!(cardEl instanceof HTMLElement)) return;
    cardEl.dataset.isWatched = watched ? "true" : "false";
    if (watched) {
      cardEl.classList.add("is-watched");
      ensureWatchedBadge(cardEl);
    } else {
      cardEl.classList.remove("is-watched");
      const badge = cardEl.querySelector(".watched-badge");
      if (badge) {
        badge.remove();
      }
    }
  });

  const links = document.querySelectorAll(`.thumb-media[data-video-id="${videoId}"]`);
  links.forEach((linkEl) => {
    if (linkEl instanceof HTMLElement) {
      linkEl.dataset.isWatched = watched ? "true" : "false";
    }
  });
}

async function markVideoWatched(videoId, watchedRatio) {
  if (!videoId) return;
  if (watchedMarkedInSession.has(videoId)) return;

  await api(`/videos/${videoId}/watched`, {
    method: "PUT",
    body: JSON.stringify({
      is_watched: true,
      watched_ratio: watchedRatio,
    }),
  });

  watchedMarkedInSession.add(videoId);
  syncWatchedUi(videoId, true);
  applyCategoryProgressDelta(0, 1);
}

async function markVideoFavorite(videoId) {
  if (!videoId) return;
  if (favoritedInSession.has(videoId)) {
    updateStatus(`Video #${videoId} ja esta favoritado.`);
    syncFavoriteUi(videoId, true);
    triggerFavoriteSplash();
    return;
  }

  await api(`/videos/${videoId}/favorite`, {
    method: "PUT",
    body: JSON.stringify({
      is_favorite: true,
    }),
  });

  favoritedInSession.add(videoId);
  syncFavoriteUi(videoId, true);
  triggerFavoriteSplash();
  updateStatus(`Video #${videoId} favoritado.`);
}

function removeVideoCardsFromGrid(videoId) {
  if (!videoId) return 0;

  const cards = Array.from(document.querySelectorAll(`.thumb-card[data-video-id="${videoId}"]`));
  if (!cards.length) return 0;
  const removedWatchedCount = cards.reduce((count, cardEl) => {
    const isWatched = (cardEl.dataset.isWatched || "").toLowerCase() === "true";
    return count + (isWatched ? 1 : 0);
  }, 0);

  cards.forEach((cardEl) => {
    cardEl.remove();
  });

  loadedCount = Math.max(0, loadedCount - cards.length);
  applyCategoryProgressDelta(-cards.length, -removedWatchedCount);
  updateMeta();
  return cards.length;
}

async function deleteVideoPermanently(videoId) {
  if (!videoId) return false;
  if (deletingInFlight) return false;

  deletingInFlight = true;
  try {
    await api(`/videos/${videoId}?permanent=true`, {
      method: "DELETE",
    });

    favoritedInSession.delete(videoId);
    watchedMarkedInSession.delete(videoId);
    removeVideoCardsFromGrid(videoId);
    updateStatus(`Video #${videoId} excluido permanentemente.`);
    return true;
  } catch (_) {
    updateStatus("Falha ao excluir video.", true);
    return false;
  } finally {
    deletingInFlight = false;
    setLoadMoreState();
  }
}

function blacklistCategoryFromVideoAndGoHome(videoId) {
  if (!videoId) {
    updateStatus("Falha ao blacklistar: video atual sem id.", true);
    return;
  }
  if (blacklistingInFlight) {
    goToDashboard();
    return;
  }

  blacklistingInFlight = true;
  closeVideoModal();
  fetch(`${API}/videos/${videoId}/blacklist-category`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    keepalive: true,
  }).catch(() => {
    // The user is already leaving the page; ignore late request failures here.
  });
  goToDashboard();
}

function getPlayableLinks() {
  const gridEl = document.getElementById("thumb-grid");
  if (!gridEl) return [];

  const links = Array.from(gridEl.querySelectorAll(".thumb-media[data-stream]"));
  return links.filter((linkEl) => {
    if (!(linkEl instanceof HTMLAnchorElement)) return false;
    return Boolean((linkEl.dataset.stream || "").trim());
  });
}

function findPlayableIndex(videoId, streamUrl) {
  const links = getPlayableLinks();
  if (!links.length) return -1;

  if (videoId > 0) {
    const byId = links.findIndex((linkEl) => Number(linkEl.dataset.videoId || "0") === videoId);
    if (byId >= 0) return byId;
  }

  const normalizedStream = (streamUrl || "").trim();
  if (normalizedStream) {
    const byStream = links.findIndex((linkEl) => (linkEl.dataset.stream || "") === normalizedStream);
    if (byStream >= 0) return byStream;
  }

  return -1;
}

function resolveActiveVideoId() {
  if (activePlayback && Number(activePlayback.videoId) > 0) {
    return Number(activePlayback.videoId);
  }

  if (activePlayback && Number.isInteger(activePlayback.currentIndex) && activePlayback.currentIndex >= 0) {
    const linksByIndex = getPlayableLinks();
    if (activePlayback.currentIndex < linksByIndex.length) {
      const idxVideoId = Number(linksByIndex[activePlayback.currentIndex].dataset.videoId || "0") || 0;
      if (idxVideoId > 0) return idxVideoId;
    }
  }

  const playerEl = document.getElementById("video-modal-player");
  if (playerEl instanceof HTMLVideoElement) {
    const currentSrc = (playerEl.currentSrc || playerEl.src || "").trim();
    if (currentSrc) {
      const linksByStream = getPlayableLinks();
      const matched = linksByStream.find((linkEl) => (linkEl.dataset.stream || "").trim() === currentSrc);
      if (matched) {
        const streamVideoId = Number(matched.dataset.videoId || "0") || 0;
        if (streamVideoId > 0) return streamVideoId;
      }
    }
  }

  return 0;
}

function abortPrewarm() {
  if (!prewarmController) return;
  try {
    prewarmController.abort();
  } catch (_) {
    // no-op
  }
  prewarmController = null;
}

function prewarmStream(streamUrl) {
  if (!streamUrl) return;

  abortPrewarm();
  const controller = new AbortController();
  prewarmController = controller;

  const timeoutId = window.setTimeout(() => {
    try {
      controller.abort();
    } catch (_) {
      // no-op
    }
  }, PREWARM_TIMEOUT_MS);

  fetch(streamUrl, {
    method: "GET",
    headers: { Range: PREWARM_RANGE },
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.body) return;
      const reader = response.body.getReader();
      try {
        await reader.read();
      } catch (_) {
        // no-op
      }
      try {
        await reader.cancel();
      } catch (_) {
        // no-op
      }
    })
    .catch(() => {
      // Keep UI responsive if prewarm fails.
    })
    .finally(() => {
      window.clearTimeout(timeoutId);
      if (prewarmController === controller) {
        prewarmController = null;
      }
    });
}

function prewarmNextFromIndex(currentIndex) {
  if (!Number.isInteger(currentIndex) || currentIndex < 0) {
    abortPrewarm();
    return;
  }

  const links = getPlayableLinks();
  const nextIndex = currentIndex + 1;
  if (nextIndex < 0 || nextIndex >= links.length) {
    abortPrewarm();
    return;
  }

  const nextStream = (links[nextIndex].dataset.stream || "").trim();
  if (!nextStream) {
    abortPrewarm();
    return;
  }

  prewarmStream(nextStream);
}

function sleep(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

async function waitForFeedIdle(timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (isLoading && Date.now() < deadline) {
    await sleep(90);
  }
}

async function loadUntilIndexAvailable(targetIndex) {
  let links = getPlayableLinks();
  let guard = 0;

  while (
    links.length <= targetIndex &&
    guard < 12 &&
    (nextCursor || isLoading)
  ) {
    guard += 1;

    if (isLoading) {
      await waitForFeedIdle();
    } else if (nextCursor) {
      await loadMore(false);
      await waitForFeedIdle();
    }

    links = getPlayableLinks();
  }

  return links;
}

function closeVideoModal() {
  const modalEl = document.getElementById("video-modal");
  const playerEl = document.getElementById("video-modal-player");
  const captionEl = document.getElementById("video-modal-caption");
  if (!modalEl || !playerEl || !captionEl) return;

  playerEl.pause();
  playerEl.removeAttribute("src");
  playerEl.load();
  captionEl.textContent = "";
  modalEl.classList.remove("open");
  modalEl.setAttribute("aria-hidden", "true");

  isModalOpen = false;
  activePlayback = null;
  abortPrewarm();
}

function toggleModalPlayback() {
  const playerEl = document.getElementById("video-modal-player");
  if (!(playerEl instanceof HTMLVideoElement) || !isModalOpen) return;

  if (playerEl.paused) {
    playerEl.play().catch(() => {
      // Ignore play interruptions from browser policies.
    });
    return;
  }

  playerEl.pause();
}

function openVideoModal(streamUrl, caption, videoId, alreadyWatched, alreadyFavorite = false) {
  if (!streamUrl) return;

  const modalEl = document.getElementById("video-modal");
  const playerEl = document.getElementById("video-modal-player");
  const captionEl = document.getElementById("video-modal-caption");
  if (!modalEl || !playerEl || !captionEl) return;

  const normalizedVideoId = Number(videoId) || 0;
  const isAlreadyWatched = Boolean(alreadyWatched) || watchedMarkedInSession.has(normalizedVideoId);
  if (isAlreadyWatched && normalizedVideoId > 0) {
    watchedMarkedInSession.add(normalizedVideoId);
  }

  const currentIndex = findPlayableIndex(normalizedVideoId, streamUrl);
  activePlayback = {
    videoId: normalizedVideoId,
    streamUrl,
    caption: caption || "",
    currentIndex,
    alreadyWatched: isAlreadyWatched,
    isFavorite: Boolean(alreadyFavorite) || favoritedInSession.has(normalizedVideoId),
    markedThisOpen: false,
    markRequestInFlight: false,
    markRequestPromise: null,
  };

  updateModalCaption();
  playerEl.src = streamUrl;
  playerEl.defaultPlaybackRate = selectedPlaybackRate;
  playerEl.playbackRate = selectedPlaybackRate;
  modalEl.classList.add("open");
  modalEl.setAttribute("aria-hidden", "false");
  isModalOpen = true;
  updatePlaybackSpeedIndicator(selectedPlaybackRate);
  void maybeMarkPlaybackAsWatched(activePlayback, playerEl);

  prewarmNextFromIndex(currentIndex);

  playerEl.play().catch(() => {
    // Autoplay can be blocked by browser policies. Controls remain available.
  });
}

function openModalFromLink(linkEl) {
  if (!(linkEl instanceof HTMLAnchorElement)) return;
  const stream = (linkEl.dataset.stream || "").trim();
  if (!stream) return;

  const videoId = Number(linkEl.dataset.videoId || "0") || 0;
  const isWatched = (linkEl.dataset.isWatched || "").toLowerCase() === "true";
  const isFavorite = (linkEl.dataset.isFavorite || "").toLowerCase() === "true";
  openVideoModal(stream, linkEl.dataset.caption || "", videoId, isWatched, isFavorite);
}

function navigateModal(step) {
  if (!isModalOpen || !activePlayback) return;

  const links = getPlayableLinks();
  if (!links.length) return;

  let currentIndex = Number(activePlayback.currentIndex);
  if (!Number.isInteger(currentIndex) || currentIndex < 0 || currentIndex >= links.length) {
    currentIndex = findPlayableIndex(activePlayback.videoId || 0, activePlayback.streamUrl || "");
  }

  const targetIndex = currentIndex + step;
  if (targetIndex >= 0 && targetIndex < links.length) {
    openModalFromLink(links[targetIndex]);
    return;
  }

  if (step > 0 && targetIndex >= links.length && (nextCursor || isLoading)) {
    if (advanceForwardTaskInFlight) return;

    advanceForwardTaskInFlight = true;
    updateStatus("Carregando proximos videos...");

    loadUntilIndexAvailable(targetIndex)
      .then((refreshed) => {
        if (targetIndex >= 0 && targetIndex < refreshed.length) {
          openModalFromLink(refreshed[targetIndex]);
          return;
        }
        if (refreshed.length > 0) {
          updateStatus("Fim da lista carregada.");
        }
      })
      .catch(() => {
        updateStatus("Falha ao carregar proximos videos.", true);
      })
      .finally(() => {
        advanceForwardTaskInFlight = false;
      });
  }
}

function goToDashboard() {
  window.location.href = "/admin";
}

async function deleteActivePlaybackAndAdvance() {
  if (!activePlayback || !activePlayback.videoId) return;

  const videoId = Number(activePlayback.videoId) || 0;
  if (!videoId) return;

  let currentIndex = Number(activePlayback.currentIndex);
  if (!Number.isInteger(currentIndex) || currentIndex < 0) {
    currentIndex = findPlayableIndex(videoId, activePlayback.streamUrl || "");
  }

  const deleted = await deleteVideoPermanently(videoId);
  if (!deleted) return;

  let links = getPlayableLinks();
  if (!links.length && nextCursor && !isLoading) {
    try {
      await loadMore(false);
      links = getPlayableLinks();
    } catch (_) {
      // ignore
    }
  }

  if (!links.length) {
    closeVideoModal();
    updateStatus("Video excluido. Nao ha mais itens nessa lista.");
    return;
  }

  let targetIndex = currentIndex;
  if (!Number.isInteger(targetIndex) || targetIndex < 0) {
    targetIndex = 0;
  }
  if (targetIndex >= links.length) {
    targetIndex = links.length - 1;
  }

  openModalFromLink(links[targetIndex]);
}

function handleModalKeydown(event) {
  const key = String(event.key || "");
  const keyLower = key.toLowerCase();
  const code = String(event.code || "");

  if (!isModalOpen) return;

  if (code === "Escape" || key === "Escape") {
    event.preventDefault();
    closeVideoModal();
    return;
  }

  if (code === "ArrowRight" || key === "ArrowRight") {
    event.preventDefault();
    navigateModal(1);
    return;
  }

  if (code === "ArrowLeft" || key === "ArrowLeft") {
    event.preventDefault();
    navigateModal(-1);
    return;
  }

  if ((code === "Enter" || key === "Enter" || code === "KeyK" || keyLower === "k") && !event.repeat) {
    event.preventDefault();
    toggleModalPlayback();
    return;
  }

  if ((code === "KeyV" || keyLower === "v") && !event.repeat) {
    event.preventDefault();
    const videoId = resolveActiveVideoId();
    if (!videoId) {
      updateStatus("Falha ao favoritar: video atual sem id.", true);
      return;
    }
    markVideoFavorite(videoId).catch((error) => {
      const message = error && typeof error.message === "string"
        ? error.message
        : "erro desconhecido";
      updateStatus(`Falha ao favoritar video #${videoId}: ${message}`, true);
    });
    return;
  }

  const requestedPlaybackRate = PLAYBACK_SPEED_BY_KEY[code];
  if (requestedPlaybackRate && !event.repeat) {
    event.preventDefault();
    applyPlaybackRate(requestedPlaybackRate, true);
    return;
  }

  if ((code === "KeyD" || keyLower === "d") && !event.repeat) {
    event.preventDefault();
    deleteActivePlaybackAndAdvance().catch(() => {
      updateStatus("Falha ao excluir video.", true);
    });
    return;
  }

  if ((code === "KeyQ" || keyLower === "q") && !event.repeat) {
    event.preventDefault();
    const videoId = resolveActiveVideoId();
    blacklistCategoryFromVideoAndGoHome(videoId);
    return;
  }

  if (code === "Space" || key === " " || key === "Spacebar") {
    event.preventDefault();
    goToDashboard();
  }
}

function maybeMarkPlaybackAsWatched(playbackState = activePlayback, playerEl = null) {
  if (!playbackState || playbackState.videoId <= 0) return Promise.resolve();
  if (playbackState.alreadyWatched || playbackState.markedThisOpen) return Promise.resolve();
  if (playbackState.markRequestInFlight) {
    return playbackState.markRequestPromise || Promise.resolve();
  }

  const resolvedPlayerEl = playerEl instanceof HTMLVideoElement
    ? playerEl
    : document.getElementById("video-modal-player");
  let watchedRatio = 0;
  if (resolvedPlayerEl instanceof HTMLVideoElement) {
    const duration = resolvedPlayerEl.duration;
    if (Number.isFinite(duration) && duration > 0) {
      watchedRatio = Math.max(0, Math.min(resolvedPlayerEl.currentTime / duration, 1));
    }
  }

  playbackState.markRequestInFlight = true;
  const markPromise = markVideoWatched(playbackState.videoId, watchedRatio)
    .then(() => {
      playbackState.alreadyWatched = true;
      playbackState.markedThisOpen = true;
    })
    .catch(() => {
      // Keep playback smooth even if watched update fails.
    })
    .finally(() => {
      playbackState.markRequestInFlight = false;
      playbackState.markRequestPromise = null;
    });
  playbackState.markRequestPromise = markPromise;
  return markPromise;
}

async function handlePlaybackEnded() {
  const playerEl = document.getElementById("video-modal-player");
  if (!(playerEl instanceof HTMLVideoElement)) return;

  const finishedPlayback = activePlayback;
  if (!isModalOpen || !finishedPlayback) return;

  await maybeMarkPlaybackAsWatched(finishedPlayback, playerEl);

  if (!isModalOpen || activePlayback !== finishedPlayback) return;
  navigateModal(1);
}

function setLoadMoreState() {
  const btn = document.getElementById("gallery-load-more");
  if (!btn) return;

  if (isLoading) {
    btn.disabled = true;
    btn.textContent = "Carregando...";
    return;
  }

  if (!nextCursor) {
    btn.disabled = true;
    btn.textContent = "Fim da lista";
    return;
  }

  btn.disabled = false;
  btn.textContent = "Carregar mais";
}

function updateMeta() {
  const metaEl = document.getElementById("gallery-meta");
  if (!metaEl) return;
  const progressText = getCategoryProgressText();
  metaEl.textContent = progressText
    ? `${loadedCount} videos | ${progressText}`
    : `${loadedCount} videos`;
}

async function loadMore(reset = false, silentStatus = false) {
  if (isLoading) return;
  if (!CATEGORY && !FAVORITES_MODE && !RECENTS_MODE && !SECRET_MODE) {
    updateStatus("Categoria invalida.", true);
    return;
  }
  if (suppressFurtherGalleryUpdates) {
    nextCursor = null;
    setLoadMoreState();
    return;
  }
  if (!reset && !nextCursor) {
    setLoadMoreState();
    return;
  }

  isLoading = true;
  setLoadMoreState();

  const gridEl = document.getElementById("thumb-grid");
  if (!gridEl) {
    isLoading = false;
    return;
  }

  if (reset) {
    gridEl.innerHTML = "";
    nextCursor = null;
    loadedCount = 0;
    updateMeta();
  }

  try {
    const params = new URLSearchParams();
    params.set("page_size", String(PAGE_SIZE));
    params.set("random_order", SECRET_MODE ? "true" : "false");
    params.set("order_by", FAVORITES_MODE ? "favorite_desc" : "detected_desc");

    if (FAVORITES_MODE) {
      params.set("only_favorites", "true");
      params.set("exclude_favorites", "false");
    } else if (RECENTS_MODE) {
      params.set("only_favorites", "false");
      params.set("exclude_favorites", "true");
      params.set("exclude_watched", "false");
    } else if (SECRET_MODE) {
      params.set("only_favorites", "false");
      params.set("exclude_favorites", "true");
      params.set("exclude_watched", "true");
    } else {
      params.set("category", CATEGORY);
    }

    if (!reset && nextCursor) {
      params.set("cursor", nextCursor);
    }

    const payload = await api(`/feed?${params.toString()}`);
    const items = Array.isArray(payload?.items) ? payload.items : [];

    if (suppressFurtherGalleryUpdates) {
      nextCursor = null;
      setLoadMoreState();
      return;
    }

    if (items.length === 0 && loadedCount === 0) {
      updateStatus(getEmptyContextMessage());
      nextCursor = null;
      setLoadMoreState();
      return;
    }

    gridEl.insertAdjacentHTML("beforeend", items.map(renderThumbCard).join(""));
    loadedCount += items.length;

    for (const item of items) {
      const videoId = Number(item.id) || 0;
      if (videoId > 0 && item.is_watched) {
        watchedMarkedInSession.add(videoId);
      }
      if (videoId > 0 && item.is_favorite) {
        favoritedInSession.add(videoId);
      }
    }

    nextCursor = payload?.next_cursor || null;
    updateMeta();
    if (!silentStatus) {
      updateStatus(getLoadingContextMessage());
    }

    if (isModalOpen && activePlayback) {
      const refreshedIndex = findPlayableIndex(activePlayback.videoId || 0, activePlayback.streamUrl || "");
      if (refreshedIndex >= 0) {
        activePlayback.currentIndex = refreshedIndex;
        prewarmNextFromIndex(refreshedIndex);
      }
    }
  } catch (error) {
    const message = error && typeof error.message === "string"
      ? error.message
      : "Falha ao carregar thumbnails.";
    updateStatus(message, true);
  } finally {
    isLoading = false;
    setLoadMoreState();
  }
}

async function loadAllPages(reset = true) {
  if (autoLoadTaskInFlight) return;
  autoLoadTaskInFlight = true;
  try {
    updateStatus("Carregando galeria completa...");
    await loadMore(reset, true);
    let pagesLoaded = 1;

    while (nextCursor && pagesLoaded < MAX_AUTO_LOAD_PAGES) {
      if (suppressFurtherGalleryUpdates) {
        nextCursor = null;
        break;
      }
      await loadMore(false, true);
      pagesLoaded += 1;
      if (pagesLoaded % 5 === 0) {
        updateStatus(`Carregando galeria... ${loadedCount} videos`);
      }
      await sleep(0);
    }

    if (suppressFurtherGalleryUpdates) {
      return;
    }

    if (nextCursor) {
      updateStatus(`Carga parcial: ${loadedCount} videos carregados.`);
    } else {
      updateStatus(getLoadedContextMessage());
    }
  } catch (error) {
    const message = error && typeof error.message === "string"
      ? error.message
      : "Falha ao carregar galeria completa.";
    updateStatus(message, true);
  } finally {
    autoLoadTaskInFlight = false;
  }
}

function init() {
  updatePlaybackSpeedIndicator(selectedPlaybackRate);
  const title = document.getElementById("category-title");
  if (title) {
    title.textContent = getPageTitle();
  }
  configureGalleryTools();

  const grid = document.getElementById("thumb-grid");
  if (grid) {
    grid.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const link = target.closest(".thumb-media");
      if (!link || !(link instanceof HTMLAnchorElement)) return;
      if (!(link.dataset.stream || "").trim()) return;

      event.preventDefault();
      openModalFromLink(link);
    });
  }

  const closeBtn = document.getElementById("video-modal-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", closeVideoModal);
  }

  const modal = document.getElementById("video-modal");
  if (modal) {
    modal.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof Element && target.getAttribute("data-close") === "true") {
        closeVideoModal();
      }
    });
  }

  const playerEl = document.getElementById("video-modal-player");
  if (playerEl instanceof HTMLVideoElement) {
    playerEl.addEventListener("click", toggleModalPlayback);
    playerEl.addEventListener("timeupdate", maybeMarkPlaybackAsWatched);
    playerEl.addEventListener("ended", handlePlaybackEnded);
  }

  const deleteCategoryBtn = document.getElementById("gallery-delete-category-btn");
  if (deleteCategoryBtn) {
    deleteCategoryBtn.addEventListener("click", deleteCurrentCategoryVideos);
  }

  const periodDownloadBtn = document.getElementById("gallery-period-download-btn");
  if (periodDownloadBtn) {
    periodDownloadBtn.addEventListener("click", startCategoryPeriodDownload);
  }

  const redditDownloadCloseBtn = document.getElementById("reddit-download-close-btn");
  if (redditDownloadCloseBtn) {
    redditDownloadCloseBtn.addEventListener("click", closeRedditDownloadModal);
  }

  const redditDownloadStopBtn = document.getElementById("reddit-download-stop-btn");
  if (redditDownloadStopBtn) {
    redditDownloadStopBtn.addEventListener("click", stopRedditDownloadProcess);
  }

  const redditDownloadModal = document.getElementById("reddit-download-modal");
  const redditDownloadBackdrop = redditDownloadModal
    ? redditDownloadModal.querySelector(".reddit-download-backdrop")
    : null;
  if (redditDownloadBackdrop) {
    redditDownloadBackdrop.addEventListener("click", () => {
      if (redditDownloadCloseBtn && redditDownloadCloseBtn.disabled) return;
      closeRedditDownloadModal();
    });
  }

  window.addEventListener("keydown", handleModalKeydown, { capture: true });

  loadCurrentRedditDownloadStatus();
  loadCategoryProgress();
  loadAllPages(true);
}

init();
