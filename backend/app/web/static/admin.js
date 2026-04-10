const API = window.API_BASE || "/api";
const COLLAPSE_STORAGE_KEY = "media_admin_collapsed_sections_v1";
const ADMIN_NOTES_KEY = "admin_notes";
const REDDIT_MIN_DOWNLOAD_FREE_GB_KEY = "reddit_min_download_free_gb";
const TRANSCODE_QUALITY_PERCENT_KEY = "transcode_quality_percent";
const DEFAULT_MIN_DOWNLOAD_FREE_GB = 8;
const DEFAULT_TRANSCODE_CRF = Number.isFinite(Number(window.DEFAULT_TRANSCODE_CRF))
  ? Number(window.DEFAULT_TRANSCODE_CRF)
  : 22;
const TRANSCODE_QUALITY_MIN_PERCENT = 0;
const TRANSCODE_QUALITY_MAX_PERCENT = 100;
const TRANSCODE_QUALITY_MIN_CRF = 18;
const TRANSCODE_QUALITY_MAX_CRF = 32;
let notesDirty = false;
let redditSubscriptions = [];
let redditFilter = "";
let redditDownloadPollTimer = null;
let redditAutoToggleRequestInFlight = false;

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

function loadCollapsedMap() {
  try {
    const raw = localStorage.getItem(COLLAPSE_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveCollapsedMap(map) {
  try {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Ignore storage errors.
  }
}

function folderShortName(path) {
  const raw = safe(path).trim();
  if (!raw) return "";
  const normalized = raw.replace(/\\/g, "/");
  const parts = normalized.split("/").filter((part) => part.length > 0);
  return parts.length > 0 ? parts[parts.length - 1] : raw;
}

function getDirectSectionToggle(section) {
  return Array.from(section.children).find((child) => (
    child instanceof HTMLElement && child.matches("[data-section-toggle]")
  )) || null;
}

function applySectionCollapsed(section, collapsed) {
  const toggle = getDirectSectionToggle(section);
  if (!toggle) return;
  section.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
}

function initCollapsibleSections() {
  const sections = Array.from(document.querySelectorAll(".collapsible-section"));
  if (sections.length === 0) return;
  const collapsedMap = loadCollapsedMap();
  sections.forEach((section) => {
    const id = section.getAttribute("data-section-id");
    const toggle = getDirectSectionToggle(section);
    if (!id || !toggle) return;

    const hasStoredState = Object.prototype.hasOwnProperty.call(collapsedMap, id);
    const defaultCollapsed = section.getAttribute("data-default-collapsed") === "true";
    const initialCollapsed = hasStoredState ? Boolean(collapsedMap[id]) : defaultCollapsed;
    applySectionCollapsed(section, initialCollapsed);

    toggle.addEventListener("click", () => {
      const nextCollapsed = !section.classList.contains("collapsed");
      applySectionCollapsed(section, nextCollapsed);
      collapsedMap[id] = nextCollapsed;
      saveCollapsedMap(collapsedMap);
    });
  });
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

function formatDuration(seconds) {
  const totalSeconds = Number(seconds);
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return "-";
  const s = Math.floor(totalSeconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(rem).padStart(2, "0")}`;
  }
  return `${m}:${String(rem).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const idx = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const scaled = value / (1024 ** idx);
  const fixed = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  return `${scaled.toFixed(fixed)} ${units[idx]}`;
}

function formatDateTime(value) {
  const raw = safe(value).trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString("pt-BR");
}

const STAT_TILE_CONFIG = {
  Folders: {
    tone: "slate",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5A2.5 2.5 0 0 1 5.5 6H10l2 2h6.5A2.5 2.5 0 0 1 21 10.5v7A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5z"/><path d="M3 10h18"/></svg>`,
  },
  Active: {
    tone: "green",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.3 2.2 2.2 4.8-5.2"/></svg>`,
  },
  Videos: {
    tone: "blue",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m10 9 5 3-5 3z"/><path d="M7 5v14"/><path d="M17 5v14"/></svg>`,
  },
  Ready: {
    tone: "green",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v10"/><path d="m8.5 9.5 3.5 3.5 3.5-3.5"/><rect x="4" y="15" width="16" height="5" rx="2"/></svg>`,
  },
  "Tempo total": {
    tone: "slate",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`,
  },
  "Tempo assistido": {
    tone: "cyan",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 8v4l3.5 2.5"/><path d="m9.5 12 1.6 1.6 3.9-4.1"/></svg>`,
  },
  "Com audio": {
    tone: "blue",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 10v4"/><path d="M8 8v8"/><path d="M11 6v12"/><path d="M14 9a3 3 0 0 1 0 6"/><path d="M17 7a6 6 0 0 1 0 10"/></svg>`,
  },
  "Sem audio": {
    tone: "amber",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 10v4"/><path d="M8 8v8"/><path d="M11 6v12"/><path d="m16 9 4 6"/><path d="m20 9-4 6"/></svg>`,
  },
  Visualizados: {
    tone: "cyan",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.8"/></svg>`,
  },
  "Visualizados sem favorito": {
    tone: "blue",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.8"/><path d="m16.5 7.5 3 3"/><path d="m19.5 7.5-3 3"/></svg>`,
  },
  "Ready Size": {
    tone: "amber",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M8 8h8"/><path d="M8 12h8"/><path d="M8 16h5"/></svg>`,
  },
  Processing: {
    tone: "amber",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4V1.5"/><path d="M12 22.5V20"/><path d="m17.7 6.3 1.8-1.8"/><path d="m4.5 19.5 1.8-1.8"/><path d="M20 12h2.5"/><path d="M1.5 12H4"/><path d="m17.7 17.7 1.8 1.8"/><path d="m4.5 4.5 1.8 1.8"/><circle cx="12" cy="12" r="4.5"/></svg>`,
  },
  Errors: {
    tone: "red",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5 21 19H3z"/><path d="M12 9v4.5"/><path d="M12 17h.01"/></svg>`,
  },
  "Jobs Pending": {
    tone: "amber",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`,
  },
  "Jobs Running": {
    tone: "blue",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4.5v15l14-7.5z"/></svg>`,
  },
  "Jobs Failed": {
    tone: "red",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="m9 9 6 6"/><path d="m15 9-6 6"/></svg>`,
  },
  default: {
    tone: "slate",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 8h8"/><path d="M8 12h8"/><path d="M8 16h5"/></svg>`,
  },
};

function statTile(label, value) {
  const config = STAT_TILE_CONFIG[label] || STAT_TILE_CONFIG.default;
  return `
    <div class="stat-tile stat-tone-${config.tone}">
      <div class="stat-icon" aria-hidden="true">${config.icon}</div>
      <div class="stat-copy">
        <div class="label">${escapeHtml(label)}</div>
        <div class="value">${escapeHtml(value)}</div>
      </div>
    </div>
  `;
}

function categoryChip(item, weight) {
  const name = String(item?.category || "").trim();
  const count = Number(item?.count || 0);
  const favoritesCount = Number(item?.favoritesCount ?? item?.favorites_count ?? 0);
  const href = item?.href ? String(item.href) : "";
  const isFavorite = Boolean(item?.isFavorite);
  const showFavoriteHeart = !isFavorite && Number.isFinite(favoritesCount) && favoritesCount >= 5;
  const normalized = Math.max(0, Math.min(1, Number(weight) || 0));
  const classes = `category-chip${isFavorite ? " category-chip-favorite" : ""}${href ? " category-chip-link" : ""}`;
  const countTitle = showFavoriteHeart ? ` title="${escapeAttr(`${favoritesCount} videos favoritados`)}"` : "";
  const countHeart = showFavoriteHeart ? ` <span class="count-favorite-heart" aria-hidden="true">&#10084;</span>` : "";
  const inner = `<span class="name">${escapeHtml(name)}</span><span class="count"${countTitle}>${safe(count)}${countHeart}</span>`;
  if (href) {
    return `<a class="${classes}" href="${escapeHtml(href)}" style="--cloud:${normalized.toFixed(3)}">${inner}</a>`;
  }
  return `<span class="${classes}" style="--cloud:${normalized.toFixed(3)}">${inner}</span>`;
}

function processingChip(label, value, title = "") {
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  return `<span class="processing-chip"${titleAttr}><span class="label">${escapeHtml(label)}:</span><span class="value">${escapeHtml(value)}</span></span>`;
}

function isUserCategoryName(name) {
  const normalized = String(name || "").trim().toLowerCase();
  return normalized.startsWith("u_");
}

function normalizeSubredditName(value) {
  return safe(value).trim().replace(/^r\//i, "");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

function formatAudioSummary(video) {
  const hasAudio = video?.has_audio;
  const codec = safe(video?.audio_codec).trim();
  if (hasAudio === true) {
    return codec ? codec.toUpperCase() : "Com audio";
  }
  if (hasAudio === false) {
    return "Sem audio";
  }
  return "-";
}

function setNotesStatus(message, tone = "") {
  const statusEl = document.getElementById("notes-status");
  if (!statusEl) return;
  const toneClass = tone === "ready" ? "reddit-ready" : tone === "error" ? "reddit-error" : "";
  statusEl.className = `reddit-status${toneClass ? ` ${toneClass}` : ""}`;
  statusEl.textContent = safe(message);
}

function setDownloadSpaceStatus(message, tone = "") {
  const statusEl = document.getElementById("download-space-status");
  if (!statusEl) return;
  const toneClass = tone === "ready" ? "reddit-ready" : tone === "error" ? "reddit-error" : "";
  statusEl.className = `reddit-status${toneClass ? ` ${toneClass}` : ""}`;
  statusEl.textContent = safe(message);
}

function parseDownloadSpaceGb(value) {
  const normalized = Number(value);
  if (!Number.isFinite(normalized) || normalized <= 0) {
    return DEFAULT_MIN_DOWNLOAD_FREE_GB;
  }
  return normalized;
}

function clampNumber(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function qualityPercentToCrf(value) {
  const normalized = clampNumber(Number(value) || 0, TRANSCODE_QUALITY_MIN_PERCENT, TRANSCODE_QUALITY_MAX_PERCENT);
  const baseCrf = clampNumber(DEFAULT_TRANSCODE_CRF, TRANSCODE_QUALITY_MIN_CRF, TRANSCODE_QUALITY_MAX_CRF);
  const span = TRANSCODE_QUALITY_MAX_CRF - baseCrf;
  if (span <= 0) return baseCrf;
  return Math.round(baseCrf + ((100 - normalized) / 100) * span);
}

function parseTranscodeQualityPercent(value) {
  const normalized = Number(value);
  if (!Number.isFinite(normalized)) {
    return 100;
  }
  return clampNumber(Math.round(normalized), TRANSCODE_QUALITY_MIN_PERCENT, TRANSCODE_QUALITY_MAX_PERCENT);
}

function formatTranscodeQuality(value) {
  const qualityPercent = parseTranscodeQualityPercent(value);
  return `${qualityPercent}% (CRF ${qualityPercentToCrf(qualityPercent)})`;
}

function syncTranscodeQualityPreview(value) {
  const rangeEl = document.getElementById("transcode-quality-range");
  const valueEl = document.getElementById("transcode-quality-value");
  const qualityPercent = parseTranscodeQualityPercent(value);
  if (rangeEl) {
    rangeEl.value = String(qualityPercent);
  }
  if (valueEl) {
    valueEl.textContent = formatTranscodeQuality(qualityPercent);
  }
}

function setTranscodeQualityStatus(message, tone = "") {
  const statusEl = document.getElementById("transcode-quality-status");
  if (!statusEl) return;
  const toneClass = tone === "ready" ? "reddit-ready" : tone === "error" ? "reddit-error" : "";
  statusEl.className = `reddit-status${toneClass ? ` ${toneClass}` : ""}`;
  statusEl.textContent = safe(message);
}

function getRedditElements() {
  return {
    statusEl: document.getElementById("reddit-config-status"),
    listEl: document.getElementById("reddit-subreddits-list"),
    metaEl: document.getElementById("reddit-list-meta"),
    autoToggleEl: document.getElementById("reddit-auto-download-toggle"),
    fetchBtn: document.getElementById("reddit-fetch-btn"),
    saveBtn: document.getElementById("reddit-save-btn"),
    downloadBtn: document.getElementById("reddit-download-btn"),
    downloadEmptyCategoriesBtn: document.getElementById("reddit-download-empty-categories-btn"),
    stopBtn: document.getElementById("reddit-stop-btn"),
    periodSelectEl: document.getElementById("reddit-period-select"),
    periodDownloadBtn: document.getElementById("reddit-period-download-btn"),
    filterInput: document.getElementById("reddit-filter-input"),
    modalEl: document.getElementById("reddit-download-modal"),
    modalStopBtn: document.getElementById("reddit-download-stop-btn"),
    modalCloseBtn: document.getElementById("reddit-download-close-btn"),
    modalSummaryEl: document.getElementById("reddit-download-summary"),
    modalProgressFillEl: document.getElementById("reddit-download-progress-fill"),
    modalItemsEl: document.getElementById("reddit-download-items"),
    modalLogsEl: document.getElementById("reddit-download-logs"),
    inlineSummaryEl: document.getElementById("reddit-download-inline-summary"),
    inlineProgressFillEl: document.getElementById("reddit-download-inline-progress-fill"),
    inlineItemsEl: document.getElementById("reddit-download-inline-items"),
    inlineLogsEl: document.getElementById("reddit-download-inline-logs"),
  };
}

function setRedditButtonsDisabled(disabled) {
  const { fetchBtn, saveBtn, downloadBtn, downloadEmptyCategoriesBtn, periodSelectEl, periodDownloadBtn } = getRedditElements();
  if (fetchBtn) fetchBtn.disabled = disabled;
  if (saveBtn) saveBtn.disabled = disabled;
  if (downloadBtn) downloadBtn.disabled = disabled;
  if (downloadEmptyCategoriesBtn) downloadEmptyCategoriesBtn.disabled = disabled;
  if (periodSelectEl) periodSelectEl.disabled = disabled;
  if (periodDownloadBtn) periodDownloadBtn.disabled = disabled;
}

function renderRedditSubscriptions() {
  const { listEl, metaEl } = getRedditElements();
  if (!listEl || !metaEl) return;

  if (!Array.isArray(redditSubscriptions) || redditSubscriptions.length === 0) {
    listEl.innerHTML = `<div class="reddit-empty">Nenhum subreddit carregado ainda.</div>`;
    metaEl.textContent = "0 itens";
    return;
  }

  const normalizedFilter = redditFilter.trim().toLowerCase();
  const filtered = redditSubscriptions.filter((item) => {
    if (!normalizedFilter) return true;
    const name = normalizeSubredditName(item?.name).toLowerCase();
    const title = safe(item?.title).toLowerCase();
    return name.includes(normalizedFilter) || title.includes(normalizedFilter);
  });
  filtered.sort((left, right) => {
    const subscriberDiff = Number(right?.subscribers || 0) - Number(left?.subscribers || 0);
    if (subscriberDiff !== 0) return subscriberDiff;
    return normalizeSubredditName(left?.name).localeCompare(normalizeSubredditName(right?.name), "pt-BR");
  });

  const selectedCount = redditSubscriptions.reduce((acc, item) => acc + (item?.is_selected ? 1 : 0), 0);
  metaEl.textContent = `${filtered.length}/${redditSubscriptions.length} exibidos - ${selectedCount} selecionados`;

  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="reddit-empty">Nenhum subreddit para o filtro atual.</div>`;
    return;
  }

  listEl.innerHTML = filtered
    .map((item) => {
      const name = normalizeSubredditName(item?.name);
      const isUserProfile = isUserCategoryName(name);
      const title = safe(item?.title);
      const over18 = Boolean(item?.over18);
      const subscribers = Number(item?.subscribers || 0);
      const nsfwLabel = over18 ? "NSFW" : "SFW";
      const downloaded = Boolean(item?.is_downloaded);
      const displayName = `${isUserProfile ? "u" : "r"}/${name}`;
      const audienceLabel = isUserProfile
        ? (subscribers > 0 ? `${subscribers.toLocaleString("pt-BR")} seguidores` : "seguidores indisponiveis")
        : `${subscribers.toLocaleString("pt-BR")} inscritos`;
      return `
        <label class="reddit-item">
          <input
            class="reddit-item-checkbox"
            type="checkbox"
            data-reddit-name="${escapeAttr(name)}"
            ${item?.is_selected ? "checked" : ""}
          />
          <span class="reddit-item-main">
            <span class="reddit-item-title">
              ${escapeHtml(displayName)}
              ${downloaded ? '<span class="reddit-item-pill">downloaded</span>' : ""}
            </span>
            <span class="reddit-item-meta">
              <span>${escapeHtml(title || "(sem titulo)")}</span>
              <span>${nsfwLabel}</span>
              <span>${escapeHtml(audienceLabel)}</span>
            </span>
          </span>
        </label>
      `;
    })
    .join("");
}

function updateRedditSelectionByName(name, checked) {
  const normalized = normalizeSubredditName(name).toLowerCase();
  if (!normalized) return;
  redditSubscriptions = redditSubscriptions.map((item) => {
    if (normalizeSubredditName(item?.name).toLowerCase() !== normalized) return item;
    return { ...item, is_selected: Boolean(checked) };
  });
}

function setRedditStatus(message, type = "info") {
  const { statusEl } = getRedditElements();
  if (!statusEl) return;
  statusEl.textContent = safe(message);
  statusEl.classList.remove("reddit-ready", "reddit-error");
  if (type === "ready") statusEl.classList.add("reddit-ready");
  if (type === "error") statusEl.classList.add("reddit-error");
}

function setCategoriesActionStatus(message, type = "info") {
  const statusEl = document.getElementById("categories-action-status");
  if (!statusEl) return;
  statusEl.textContent = safe(message);
  statusEl.classList.remove("reddit-ready", "reddit-error");
  if (type === "ready") statusEl.classList.add("reddit-ready");
  if (type === "error") statusEl.classList.add("reddit-error");
}

function setVideosActionStatus(message, type = "info") {
  const statusEl = document.getElementById("videos-action-status");
  if (!statusEl) return;
  statusEl.textContent = safe(message);
  statusEl.classList.remove("reddit-ready", "reddit-error");
  if (type === "ready") statusEl.classList.add("reddit-ready");
  if (type === "error") statusEl.classList.add("reddit-error");
}

async function loadRedditConfigStatus() {
  const { fetchBtn, saveBtn, downloadBtn, periodSelectEl, periodDownloadBtn } = getRedditElements();
  if (!fetchBtn || !saveBtn || !downloadBtn || !periodSelectEl || !periodDownloadBtn) return;
  try {
    const payload = await api("/reddit/config-status");
    if (payload.configured) {
      fetchBtn.disabled = false;
      saveBtn.disabled = false;
      downloadBtn.disabled = false;
      periodSelectEl.disabled = false;
      periodDownloadBtn.disabled = false;
      if (!Array.isArray(redditSubscriptions) || redditSubscriptions.length === 0) {
        setRedditStatus("Reddit configurado. Clique em 'Buscar assinaturas' para carregar.", "ready");
      }
    } else {
      const missing = Array.isArray(payload.missing) ? payload.missing.join(", ") : "";
      setRedditStatus(`Config Reddit incompleta. Variaveis faltando: ${missing}`, "error");
      fetchBtn.disabled = true;
      saveBtn.disabled = true;
      downloadBtn.disabled = true;
      periodSelectEl.disabled = true;
      periodDownloadBtn.disabled = true;
    }
  } catch (error) {
    setRedditStatus(`Falha ao validar config Reddit: ${safe(error.message)}`, "error");
    fetchBtn.disabled = true;
    saveBtn.disabled = true;
    downloadBtn.disabled = true;
    periodSelectEl.disabled = true;
    periodDownloadBtn.disabled = true;
  }
}

function getSelectedRedditNames() {
  return redditSubscriptions
    .filter((item) => Boolean(item?.is_selected))
    .map((item) => normalizeSubredditName(item?.name))
    .filter((name) => name.length > 0);
}

async function loadRedditSubscriptions(forceRefresh = false) {
  setRedditStatus(forceRefresh ? "Atualizando assinaturas no Reddit..." : "Carregando assinaturas em cache...");
  setRedditButtonsDisabled(true);
  try {
    const payload = await api(`/reddit/subscriptions${forceRefresh ? "?refresh=true" : ""}`);
    redditSubscriptions = Array.isArray(payload?.items) ? payload.items : [];
    renderRedditSubscriptions();

    const total = Number(payload?.total || redditSubscriptions.length);
    const selected = Number(payload?.selected_total || 0);
    const fromCache = Boolean(payload?.from_cache);
    const cachedAt = formatDateTime(payload?.cached_at);
    const cacheLabel = fromCache ? "cache" : "atualizado";
    const cacheAtLabel = cachedAt ? ` (${cachedAt})` : "";
    const warning = safe(payload?.warning).trim();

    if (warning) {
      setRedditStatus(`${warning}`, "error");
    } else {
      setRedditStatus(`Subreddits carregados: ${total}. Selecionados: ${selected}. Fonte: ${cacheLabel}${cacheAtLabel}.`, "ready");
    }
  } catch (error) {
    setRedditStatus(`Erro ao buscar assinaturas: ${safe(error.message)}`, "error");
  } finally {
    setRedditButtonsDisabled(false);
  }
}

async function fetchRedditSubscriptions() {
  await loadRedditSubscriptions(true);
}

async function saveRedditSelection() {
  if (!Array.isArray(redditSubscriptions) || redditSubscriptions.length === 0) {
    setRedditStatus("Nenhum subreddit carregado para salvar.", "error");
    return;
  }
  setRedditStatus("Salvando selecao...");
  setRedditButtonsDisabled(true);
  try {
    const selectedNames = redditSubscriptions
      .filter((item) => item?.is_selected)
      .map((item) => normalizeSubredditName(item?.name))
      .filter((name) => name.length > 0);
    const payload = await api("/reddit/subscriptions/selection", {
      method: "PUT",
      body: JSON.stringify({ names: selectedNames }),
    });
    const selectedTotal = Number(payload?.selected_total || selectedNames.length);
    setRedditStatus(`Selecao salva com sucesso (${selectedTotal} subreddits).`, "ready");
    renderRedditSubscriptions();
  } catch (error) {
    setRedditStatus(`Falha ao salvar selecao: ${safe(error.message)}`, "error");
  } finally {
    setRedditButtonsDisabled(false);
  }
}

function selectAllRedditSubscriptions() {
  if (!Array.isArray(redditSubscriptions) || redditSubscriptions.length === 0) return;
  redditSubscriptions = redditSubscriptions.map((item) => ({ ...item, is_selected: true }));
  renderRedditSubscriptions();
}

function clearRedditSubscriptionsSelection() {
  if (!Array.isArray(redditSubscriptions) || redditSubscriptions.length === 0) return;
  redditSubscriptions = redditSubscriptions.map((item) => ({ ...item, is_selected: false }));
  renderRedditSubscriptions();
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
        <span class="name">r/${escapeHtml(item?.name || "")}</span>
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
  const { modalEl } = getRedditElements();
  if (!modalEl) return;
  modalEl.classList.add("open");
  modalEl.setAttribute("aria-hidden", "false");
}

function closeRedditDownloadModal() {
  const { modalEl } = getRedditElements();
  if (!modalEl) return;
  modalEl.classList.remove("open");
  modalEl.setAttribute("aria-hidden", "true");
}

function renderRedditDownloadStatus(status) {
  const {
    autoToggleEl,
    modalSummaryEl,
    modalProgressFillEl,
    modalItemsEl,
    modalLogsEl,
    stopBtn,
    modalStopBtn,
    modalCloseBtn,
    downloadBtn,
    downloadEmptyCategoriesBtn,
    periodSelectEl,
    periodDownloadBtn,
    inlineSummaryEl,
    inlineProgressFillEl,
    inlineItemsEl,
    inlineLogsEl,
  } = getRedditElements();
  renderRedditDownloadStatusBlock(
    modalSummaryEl,
    modalProgressFillEl,
    modalItemsEl,
    modalLogsEl,
    status,
  );
  renderRedditDownloadStatusBlock(
    inlineSummaryEl,
    inlineProgressFillEl,
    inlineItemsEl,
    inlineLogsEl,
    status,
  );

  const state = safe(status?.state).toLowerCase() || "idle";
  const autoEnabled = status?.auto_enabled !== false;
  if (autoToggleEl) {
    autoToggleEl.checked = autoEnabled;
    autoToggleEl.disabled = redditAutoToggleRequestInFlight;
  }
  if (downloadBtn) {
    downloadBtn.disabled = isRedditDownloadRunningState(state);
  }
  if (downloadEmptyCategoriesBtn) {
    downloadEmptyCategoriesBtn.disabled = isRedditDownloadRunningState(state);
  }
  if (stopBtn) {
    stopBtn.disabled = !isRedditDownloadRunningState(state);
  }
  if (periodDownloadBtn) {
    periodDownloadBtn.disabled = isRedditDownloadRunningState(state);
  }
  if (periodSelectEl) {
    periodSelectEl.disabled = isRedditDownloadRunningState(state);
  }
  if (modalStopBtn) {
    modalStopBtn.disabled = !isRedditDownloadRunningState(state);
  }
  if (modalCloseBtn) {
    modalCloseBtn.disabled = isRedditDownloadRunningState(state);
  }
}

async function updateRedditAutoDownloadEnabled(enabled) {
  const normalized = Boolean(enabled);
  const { autoToggleEl } = getRedditElements();
  redditAutoToggleRequestInFlight = true;
  if (autoToggleEl) {
    autoToggleEl.disabled = true;
  }
  try {
    const status = await api("/reddit/download/auto-enabled", {
      method: "PUT",
      body: JSON.stringify({ enabled: normalized }),
    });
    renderRedditDownloadStatus(status);
    setRedditStatus(
      normalized
        ? "Auto-download quando ocioso ativado."
        : "Auto-download quando ocioso desativado. Downloads manuais continuam disponiveis.",
      "ready",
    );
    loadConfig().catch(() => {});
  } catch (error) {
    setRedditStatus(`Falha ao atualizar auto-download: ${safe(error.message)}`, "error");
    await loadCurrentRedditDownloadStatus();
  } finally {
    redditAutoToggleRequestInFlight = false;
    const { autoToggleEl: currentAutoToggleEl } = getRedditElements();
    if (currentAutoToggleEl) {
      currentAutoToggleEl.disabled = false;
    }
  }
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
  } catch (error) {
    const fallback = {
      state: "failed",
      items: [],
      logs: [`Falha ao carregar status do download Reddit: ${safe(error.message)}`],
      message: "Nao foi possivel consultar o status atual.",
    };
    renderRedditDownloadStatus(fallback);
    stopRedditDownloadPolling();
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
      await loadRedditSubscriptions(false);
      await loadVideos();
      await loadJobs();
      await loadHealthAndStats();
    }
  } catch (error) {
    stopRedditDownloadPolling();
    const fallback = {
      state: "failed",
      items: [],
      logs: [`Falha ao consultar status de download: ${safe(error.message)}`],
    };
    renderRedditDownloadStatus(fallback);
  }
}

function startRedditDownloadPolling() {
  stopRedditDownloadPolling();
  redditDownloadPollTimer = setInterval(() => {
    pollRedditDownloadStatusOnce();
  }, 1200);
}

async function startRedditLatestVideosDownload() {
  const selectedNames = getSelectedRedditNames();

  if (selectedNames.length === 0) {
    setRedditStatus("Selecione pelo menos 1 subreddit para baixar.", "error");
    return;
  }

  openRedditDownloadModal();
  renderRedditDownloadStatus({
    state: "running",
    total_subreddits: selectedNames.length,
    completed_subreddits: 0,
    total_videos_planned: selectedNames.length * 10,
    processed_videos: 0,
    failed_videos: 0,
    items: selectedNames.map((name) => ({ name, status: "pending", planned: 0, completed: 0, failed: 0 })),
    logs: ["Iniciando download..."],
  });

  try {
    await api("/reddit/download/start", {
      method: "POST",
      body: JSON.stringify({
        names: selectedNames,
        per_subreddit_limit: 10,
      }),
    });
    setRedditStatus(`Download iniciado para ${selectedNames.length} subreddit(s).`, "ready");
    await pollRedditDownloadStatusOnce();
    startRedditDownloadPolling();
  } catch (error) {
    setRedditStatus(`Falha ao iniciar download: ${safe(error.message)}`, "error");
    renderRedditDownloadStatus({
      state: "failed",
      items: selectedNames.map((name) => ({ name, status: "failed", planned: 0, completed: 0, failed: 0 })),
      logs: [`Falha ao iniciar download: ${safe(error.message)}`],
    });
  }
}

async function startRedditEmptyCategoriesDownload() {
  openRedditDownloadModal();
  renderRedditDownloadStatus({
    state: "running",
    total_subreddits: 0,
    completed_subreddits: 0,
    total_videos_planned: 0,
    processed_videos: 0,
    failed_videos: 0,
    items: [],
    logs: ["Selecionando categorias sem videos salvos..."],
  });

  try {
    await api("/reddit/download/start-empty-categories", {
      method: "POST",
    });
    setRedditStatus("Download iniciado para categorias sem videos salvos.", "ready");
    await pollRedditDownloadStatusOnce();
    startRedditDownloadPolling();
  } catch (error) {
    setRedditStatus(`Falha ao iniciar download de categorias vazias: ${safe(error.message)}`, "error");
    renderRedditDownloadStatus({
      state: "failed",
      items: [],
      logs: [`Falha ao iniciar download de categorias vazias: ${safe(error.message)}`],
    });
  }
}

async function startRedditPeriodDownload() {
  const { periodSelectEl } = getRedditElements();
  const selectedNames = getSelectedRedditNames();

  if (selectedNames.length !== 1) {
    setRedditStatus("Selecione exatamente 1 subreddit para baixar por periodo.", "error");
    return;
  }

  const periodMonths = Number(periodSelectEl?.value || 1);
  if (![1, 2, 3, 6, 12].includes(periodMonths)) {
    setRedditStatus("Periodo invalido.", "error");
    return;
  }

  const [selectedName] = selectedNames;
  openRedditDownloadModal();
  renderRedditDownloadStatus({
    state: "running",
    total_subreddits: 1,
    completed_subreddits: 0,
    total_videos_planned: 0,
    processed_videos: 0,
    failed_videos: 0,
    items: [{ name: selectedName, status: "pending", planned: 0, completed: 0, failed: 0 }],
    logs: [`Iniciando download do periodo de ${periodMonths} mes(es)...`],
  });

  try {
    await api("/reddit/download/start", {
      method: "POST",
      body: JSON.stringify({
        names: [selectedName],
        period_months: periodMonths,
        per_subreddit_limit: null,
      }),
    });
    setRedditStatus(`Download por periodo iniciado para r/${selectedName} (${periodMonths} mes(es)).`, "ready");
    await pollRedditDownloadStatusOnce();
    startRedditDownloadPolling();
  } catch (error) {
    setRedditStatus(`Falha ao iniciar download por periodo: ${safe(error.message)}`, "error");
    renderRedditDownloadStatus({
      state: "failed",
      items: [{ name: selectedName, status: "failed", planned: 0, completed: 0, failed: 0 }],
      logs: [`Falha ao iniciar download por periodo: ${safe(error.message)}`],
    });
  }
}

async function stopRedditDownloadProcess() {
  try {
    const status = await api("/reddit/download/stop", { method: "POST" });
    renderRedditDownloadStatus(status);
    setRedditStatus("Processo de download Reddit interrompido.", "ready");
  } catch (error) {
    setRedditStatus(`Falha ao interromper download: ${safe(error.message)}`, "error");
  }
}

function renderCategoryCloud(list) {
  if (!Array.isArray(list) || list.length === 0) {
    return `<div class="category-empty">Sem categorias detectadas.</div>`;
  }

  const normalized = list.map((item) => {
    const count = Number(item?.count);
    const favoritesCount = Number(item?.favoritesCount ?? item?.favorites_count);
    const category = String(item?.category || "").trim();
    const hasCustomHref = Object.prototype.hasOwnProperty.call(item || {}, "href");
    return {
      category,
      count: Number.isFinite(count) ? Math.max(0, count) : 0,
      favoritesCount: Number.isFinite(favoritesCount) ? Math.max(0, favoritesCount) : 0,
      href: hasCustomHref
        ? String(item?.href || "")
        : (category ? `/admin/category?name=${encodeURIComponent(category)}` : ""),
      isFavorite: Boolean(item?.isFavorite),
    };
  }).filter((item) => item.category.length > 0);

  if (normalized.length === 0) {
    return `<div class="category-empty">Sem categorias detectadas.</div>`;
  }

  const counts = normalized.map((item) => item.count);
  const minCount = Math.min(...counts);
  const maxCount = Math.max(...counts);

  return normalized
    .map((item) => {
      const weight = maxCount === minCount
        ? 1
        : (item.count - minCount) / (maxCount - minCount);
      return categoryChip(item, weight);
    })
    .join("");
}

function renderChannelsList(list) {
  if (!Array.isArray(list) || list.length === 0) {
    return `<div class="category-empty">Sem canais disponiveis.</div>`;
  }

  return list.map((item) => {
    const category = safe(item?.category).trim();
    const channelKey = safe(item?.channel_key ?? item?.channelKey).trim();
    const isUserCategory = Boolean(item?.is_user_category ?? item?.isUserCategory);
    const videoCount = Math.max(0, Number(item?.video_count ?? item?.videoCount ?? 0));
    const watchedCount = Math.max(0, Number(item?.watched_count ?? item?.watchedCount ?? 0));
    const favoritesCount = Math.max(0, Number(item?.favorites_count ?? item?.favoritesCount ?? 0));
    const galleryUrl = safe(item?.gallery_url ?? item?.galleryUrl).trim();
    const playUrl = safe(item?.play_url ?? item?.playUrl).trim();
    const playlistUrl = safe(item?.playlist_url ?? item?.playlistUrl).trim();
    const liveUrl = safe(item?.live_url ?? item?.liveUrl).trim();
    const badge = isUserCategory ? `<span class="channel-card-pill">usuario</span>` : `<span class="channel-card-pill">categoria</span>`;

    return `
      <article class="channel-card" data-channel-key="${escapeAttr(channelKey)}">
        <div class="channel-card-main">
          <div class="channel-card-title">
            <span class="channel-card-name">${escapeHtml(category)}</span>
            ${badge}
          </div>
          <div class="channel-card-meta">
            <span>${videoCount.toLocaleString("pt-BR")} videos</span>
            <span>${watchedCount.toLocaleString("pt-BR")} vistos</span>
            <span>${favoritesCount.toLocaleString("pt-BR")} favoritos</span>
          </div>
        </div>
        <div class="channel-card-actions">
          <a class="channel-link" href="${escapeAttr(playUrl)}">Play Channel</a>
          <a class="channel-link" href="${escapeAttr(galleryUrl)}">Galeria</a>
          <a class="channel-link" href="${escapeAttr(playlistUrl)}" download>Baixar M3U</a>
          <a class="channel-link channel-link-secondary" href="${escapeAttr(liveUrl)}" target="_blank" rel="noopener">Canal HLS</a>
        </div>
      </article>
    `;
  }).join("");
}

function normalizePipelineState(state) {
  const value = String(state || "").toLowerCase();
  if (value === "running" || value === "paused" || value === "stopped") {
    return value;
  }
  return "unknown";
}

function renderPipelineState(control) {
  const stateEl = document.getElementById("pipeline-state");
  const state = normalizePipelineState(control?.state);
  stateEl.className = `pipeline-state state-${state}`;
  const worker = control?.worker_running
    ? control?.worker_paused
      ? "paused"
      : "running"
    : "stopped";
  const watcher = control?.watcher_running
    ? control?.watcher_paused
      ? "paused"
      : "running"
    : "stopped";
  stateEl.textContent = `Pipeline: ${state} (worker ${worker}, watcher ${watcher})`;
}

async function loadControlStatus() {
  try {
    const status = await api("/control/status");
    renderPipelineState(status);
  } catch (error) {
    const stateEl = document.getElementById("pipeline-state");
    stateEl.className = "pipeline-state state-unknown";
    stateEl.textContent = `Pipeline: offline (${safe(error.message)})`;
  }
}

async function pipelineAction(action) {
  await api(`/control/${action}`, { method: "POST" });
  await refreshAll();
}

async function loadHealthAndStats() {
  const healthEl = document.getElementById("health-state");
  const statsEl = document.getElementById("stats-grid");
  const processingEl = document.getElementById("processing-now");
  const categoriesEl = document.getElementById("categories-chips");
  const channelsEl = document.getElementById("channels-list");
  const usersEl = document.getElementById("users-chips");
  const categoriesMetaEl = document.getElementById("categories-meta");
  const channelsMetaEl = document.getElementById("channels-meta");
  const usersMetaEl = document.getElementById("users-meta");
  const usersSectionEl = document.getElementById("users-section");
  if (!healthEl || !statsEl || !processingEl || !categoriesEl || !categoriesMetaEl) {
    return;
  }
  try {
    const [health, stats, categories, channelsPayload] = await Promise.all([
      api("/health"),
      api("/stats"),
      api("/feed/categories"),
      api("/channels").catch((error) => ({ load_error: safe(error.message) })),
    ]);
    healthEl.innerHTML = `<span class="status-ok">ONLINE</span> (${health.status})`;
    statsEl.innerHTML = [
      statTile("Folders", stats.folders_total),
      statTile("Active", stats.folders_active),
      statTile("Videos", stats.videos_total),
      statTile("Ready", stats.videos_ready),
      statTile("Tempo total", formatDuration(stats.videos_total_duration_seconds)),
      statTile("Tempo assistido", formatDuration(stats.videos_total_watched_seconds)),
      statTile("Com audio", stats.videos_with_audio),
      statTile("Sem audio", stats.videos_without_audio),
      statTile("Visualizados", stats.videos_watched),
      statTile("Visualizados sem favorito", stats.videos_watched_not_favorited),
      statTile("Ready Size", formatBytes(stats.ready_storage_bytes)),
      statTile("Processing", stats.videos_processing),
      statTile("Errors", stats.videos_error),
      statTile("Jobs Pending", stats.jobs_pending),
      statTile("Jobs Running", stats.jobs_running),
      statTile("Jobs Failed", stats.jobs_failed),
    ].join("");
    const categoriesList = Array.isArray(categories) ? categories : [];
    const userCategories = categoriesList.filter((item) => isUserCategoryName(item?.category));
    const regularCategories = categoriesList.filter((item) => !isUserCategoryName(item?.category));
    const favoriteCount = Number.isFinite(Number(stats?.videos_favorites)) ? Number(stats.videos_favorites) : 0;
    const readyCount = Number.isFinite(Number(stats?.videos_ready)) ? Number(stats.videos_ready) : 0;
    const watchedNotFavoritedCount = Number.isFinite(Number(stats?.videos_watched_not_favorited))
      ? Number(stats.videos_watched_not_favorited)
      : 0;
    const recentCount = Math.max(0, readyCount - favoriteCount);
    const secretCount = Math.max(0, readyCount - favoriteCount - watchedNotFavoritedCount);
    const currentCategory = safe(stats?.processing_current_category).trim();
    const currentFolder = safe(stats?.processing_current_folder).trim();
    const currentVideoId = Number.isFinite(Number(stats?.processing_current_video_id))
      ? Number(stats.processing_current_video_id)
      : null;

    if (!currentCategory && !currentFolder && currentVideoId === null) {
      processingEl.innerHTML = `<span class="processing-empty">Sem job em processamento no momento.</span>`;
    } else {
      const processingItems = [];
      if (currentCategory) {
        processingItems.push(processingChip("Categoria", currentCategory));
      }
      if (currentFolder) {
        processingItems.push(processingChip("Pasta", folderShortName(currentFolder), currentFolder));
      }
      if (currentVideoId !== null) {
        processingItems.push(processingChip("Video", String(currentVideoId)));
      }
      processingEl.innerHTML = processingItems.join("");
    }

    const cloudItems = [
      {
        category: "Favoritos",
        count: Math.max(0, favoriteCount),
        favoritesCount: Math.max(0, favoriteCount),
        href: "/admin/category?favorites=1&name=Favoritos",
        isFavorite: true,
      },
      {
        category: "Nao vistos",
        count: secretCount,
        href: "/admin/category?secret=1&name=Nao%20vistos",
      },
      {
        category: "Recentes",
        count: recentCount,
        href: "/admin/category?recent=1&name=Recentes",
      },
      ...regularCategories,
    ];
    if (cloudItems.length > 0) {
      categoriesEl.innerHTML = renderCategoryCloud(cloudItems);
      categoriesMetaEl.textContent = `${regularCategories.length} categorias`;
    } else {
      categoriesEl.innerHTML = `<div class="category-empty">Sem categorias detectadas.</div>`;
      categoriesMetaEl.textContent = "";
    }

    if (usersEl && usersMetaEl) {
      if (usersSectionEl) {
        usersSectionEl.style.display = "block";
      }
      if (userCategories.length > 0) {
        usersEl.innerHTML = renderCategoryCloud(userCategories);
        usersMetaEl.textContent = `${userCategories.length} usuarios`;
      } else {
        usersEl.innerHTML = `<div class="category-empty">Sem usuarios detectados.</div>`;
        usersMetaEl.textContent = "0 usuarios";
      }
    }

    if (channelsEl && channelsMetaEl) {
      const channelLoadError = safe(channelsPayload?.load_error).trim();
      if (channelLoadError) {
        channelsEl.innerHTML = `<div class="category-empty">${escapeHtml(channelLoadError)}</div>`;
        channelsMetaEl.textContent = "";
      } else {
        const channelsList = Array.isArray(channelsPayload) ? channelsPayload : [];
        const userChannelCount = channelsList.filter((item) => Boolean(item?.is_user_category ?? item?.isUserCategory)).length;
        const regularChannelCount = Math.max(0, channelsList.length - userChannelCount);
        channelsEl.innerHTML = renderChannelsList(channelsList);
        channelsMetaEl.textContent = `${channelsList.length} canais (${regularChannelCount} categorias, ${userChannelCount} usuarios)`;
      }
    }
  } catch (error) {
    healthEl.innerHTML = `<span class="status-fail">OFFLINE</span> (${safe(error.message)})`;
    processingEl.innerHTML = `<span class="processing-empty">${escapeHtml(error.message || "Falha ao carregar status de processamento.")}</span>`;
    categoriesEl.innerHTML = `<div class="category-empty">${escapeHtml(error.message || "Falha ao carregar categorias.")}</div>`;
    categoriesMetaEl.textContent = "";
    if (channelsEl) {
      channelsEl.innerHTML = `<div class="category-empty">${escapeHtml(error.message || "Falha ao carregar canais.")}</div>`;
    }
    if (channelsMetaEl) {
      channelsMetaEl.textContent = "";
    }
    if (usersEl) {
      usersEl.innerHTML = `<div class="category-empty">${escapeHtml(error.message || "Falha ao carregar categorias de usuarios.")}</div>`;
    }
    if (usersMetaEl) {
      usersMetaEl.textContent = "";
    }
  }
}

async function loadFolders() {
  const table = document.getElementById("folders-table");
  try {
    const rows = await api("/folders");
    table.innerHTML = rows
      .map(
        (f) => `
          <tr>
            <td>${f.id}</td>
            <td>${safe(f.path)}</td>
            <td>${f.is_active ? "yes" : "no"}</td>
            <td>${f.serve_files ? "yes" : "no"}</td>
            <td>${f.video_count}</td>
            <td>${f.ready_count}</td>
            <td>${f.error_count}</td>
            <td>
              <button onclick="toggleFolder(${f.id}, ${!f.is_active})">${f.is_active ? "Disable" : "Enable"}</button>
              <button onclick="removeFolder(${f.id})">Delete</button>
            </td>
          </tr>
        `
      )
      .join("");
  } catch (error) {
    table.innerHTML = `<tr><td colspan="8">${safe(error.message)}</td></tr>`;
  }
}

async function toggleFolder(folderId, active) {
  await api(`/folders/${folderId}`, {
    method: "PUT",
    body: JSON.stringify({ is_active: active }),
  });
  await refreshAll();
}

async function removeFolder(folderId) {
  if (!window.confirm("Delete folder config?")) return;
  await api(`/folders/${folderId}`, { method: "DELETE" });
  await refreshAll();
}

async function loadVideos() {
  const table = document.getElementById("videos-table");
  const status = document.getElementById("video-status-filter").value;
  const statusQuery = status ? `&status=${encodeURIComponent(status)}` : "";
  try {
    const payload = await api(`/videos?page=1&page_size=60${statusQuery}`);
    table.innerHTML = payload.items
      .map(
        (v) => `
          <tr>
            <td>${v.id}</td>
            <td>${safe(v.status)}</td>
            <td>${safe(v.width)}x${safe(v.height)}</td>
            <td>${formatDuration(v.duration_seconds)}</td>
            <td>${escapeHtml(formatAudioSummary(v))}</td>
            <td title="${safe(v.original_path)}">${safe(v.original_path)}</td>
            <td>
              <button onclick="reprocessVideo(${v.id})">Reprocess</button>
              <button onclick="deleteVideo(${v.id})">Hide</button>
            </td>
          </tr>
        `
      )
      .join("");
  } catch (error) {
    table.innerHTML = `<tr><td colspan="7">${safe(error.message)}</td></tr>`;
  }
}

async function reprocessVideo(videoId) {
  await api(`/videos/${videoId}/reprocess`, { method: "POST" });
  await refreshAll();
}

async function deleteVideo(videoId) {
  if (!window.confirm("Remove from catalog (without deleting original)?")) return;
  await api(`/videos/${videoId}`, { method: "DELETE" });
  await refreshAll();
}

async function cleanupAllDownloadedVideos() {
  const btn = document.getElementById("cleanup-all-downloaded-videos-btn");
  if (!btn) return;
  if (!window.confirm("Excluir permanentemente os videos baixados pelo app, preservando os favoritados? Categorias e usuarios sem favoritos tambem serao limpos. Se o auto-download Reddit estiver ligado, novos downloads podem acontecer de novo depois.")) return;

  btn.disabled = true;
  setCategoriesActionStatus("Removendo videos baixados do app e preservando favoritos...");
  try {
    const payload = await api("/videos/cleanup/downloaded-all", { method: "POST" });
    const deletedVideos = Number(payload?.deleted_videos || 0);
    setCategoriesActionStatus(
      `Limpeza concluida: ${deletedVideos} video(s) baixado(s) do app removido(s). Favoritos foram preservados.`,
      "ready",
    );
    await refreshAll();
  } catch (error) {
    setCategoriesActionStatus(`Falha na limpeza total: ${safe(error.message)}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function resetLibraryPreservingConfig() {
  const btn = document.getElementById("reset-library-btn");
  if (!btn) return;

  const confirmed = window.prompt(
    "Isso vai parar o pipeline, interromper downloads Reddit em andamento, apagar todos os videos/arquivos catalogados, jobs, logs, feed e historico Reddit. As configuracoes serao preservadas. Arquivos originais ja catalogados em pastas monitoradas tambem serao apagados. Digite ZERAR para confirmar.",
    "",
  );
  if (String(confirmed || "").trim().toUpperCase() !== "ZERAR") return;

  btn.disabled = true;
  setCategoriesActionStatus("Zerando base e preservando configuracoes...");
  try {
    const payload = await api("/library/reset", { method: "POST" });
    const deletedVideos = Number(payload?.deleted_videos || 0);
    const deletedJobs = Number(payload?.deleted_jobs || 0);
    const deletedErrors = Number(payload?.deleted_error_logs || 0);
    const deletedFeedStates = Number(payload?.deleted_feed_states || 0);
    const deletedHistory = Number(payload?.deleted_reddit_history || 0);
    const deletedFiles = Number(payload?.deleted_files || 0);
    setCategoriesActionStatus(
      `Base zerada. Videos: ${deletedVideos}; arquivos: ${deletedFiles}; jobs: ${deletedJobs}; logs: ${deletedErrors}; feed: ${deletedFeedStates}; historico Reddit: ${deletedHistory}. O pipeline ficou parado.`,
      "ready",
    );
    await refreshAll();
  } catch (error) {
    setCategoriesActionStatus(`Falha ao zerar a base: ${safe(error.message)}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function cleanupWatchedUnfavoritedVideos() {
  const btn = document.getElementById("cleanup-watched-unfavorited-btn");
  if (!btn) return;
  if (!window.confirm("Excluir permanentemente todos os videos visualizados que nao foram favoritados?")) return;

  btn.disabled = true;
  setCategoriesActionStatus("Removendo videos visualizados nao favoritados...");
  try {
    const payload = await api("/videos/cleanup/watched-unfavorited", { method: "POST" });
    const deletedVideos = Number(payload?.deleted_videos || 0);
    setCategoriesActionStatus(`Remocao concluida: ${deletedVideos} video(s) excluido(s) permanentemente.`, "ready");
    await refreshAll();
  } catch (error) {
    setCategoriesActionStatus(`Falha na remocao: ${safe(error.message)}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function backfillAudioMetadata() {
  const btn = document.getElementById("videos-backfill-audio-btn");
  if (!btn) return;
  if (!window.confirm("Atualizar metadados de audio dos videos antigos sem reprocessar tudo?")) return;

  btn.disabled = true;
  const batchSize = 200;
  let totalScanned = 0;
  let totalUpdated = 0;
  let totalWithAudio = 0;
  let totalWithoutAudio = 0;
  let totalFailed = 0;
  let remaining = 0;
  let round = 0;
  try {
    do {
      round += 1;
      setVideosActionStatus(`Atualizando metadados de audio... lote ${round}`);
      const payload = await api(`/videos/backfill-audio?limit=${batchSize}`, { method: "POST" });
      const scanned = Number(payload?.scanned_videos || 0);
      const updated = Number(payload?.updated_videos || 0);
      const withAudio = Number(payload?.videos_with_audio || 0);
      const withoutAudio = Number(payload?.videos_without_audio || 0);
      const failed = Number(payload?.failed_videos || 0);
      remaining = Number(payload?.remaining_videos || 0);
      totalScanned += scanned;
      totalUpdated += updated;
      totalWithAudio += withAudio;
      totalWithoutAudio += withoutAudio;
      totalFailed += failed;
      setVideosActionStatus(
        `Lote ${round} concluido. Restantes: ${remaining}. Atualizados ate agora: ${totalUpdated}.`,
        "ready",
      );
      if (remaining > 0 && updated === 0) {
        setVideosActionStatus(
          `Backfill interrompido sem progresso. Restantes: ${remaining}. Atualizados: ${totalUpdated}. Falhas: ${totalFailed}.`,
          "error",
        );
        break;
      }
    } while (remaining > 0);

    if (remaining === 0) {
      setVideosActionStatus(
        `Audio atualizado. Escaneados: ${totalScanned}. Atualizados: ${totalUpdated}. Com audio: ${totalWithAudio}. Sem audio: ${totalWithoutAudio}. Falhas: ${totalFailed}.`,
        totalFailed > 0 ? "error" : "ready",
      );
    }
    await refreshAll();
  } catch (error) {
    setVideosActionStatus(`Falha ao atualizar audio: ${safe(error.message)}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadJobs() {
  const table = document.getElementById("jobs-table");
  try {
    const jobs = await api("/jobs?limit=80");
    table.innerHTML = jobs
      .map(
        (j) => `
          <tr>
            <td>${j.id}</td>
            <td>${safe(j.type)}</td>
            <td>${safe(j.status)}</td>
            <td>${j.attempts}/${j.max_attempts}</td>
            <td>${safe(j.video_id)}</td>
            <td>${safe(j.error_message)}</td>
          </tr>
        `
      )
      .join("");
  } catch (error) {
    table.innerHTML = `<tr><td colspan="6">${safe(error.message)}</td></tr>`;
  }
}

async function loadConfig() {
  const table = document.getElementById("config-table");
  const notesEl = document.getElementById("admin-notes");
  const downloadSpaceEl = document.getElementById("download-min-free-gb");
  const transcodeQualityEl = document.getElementById("transcode-quality-range");
  try {
    const payload = await api("/config");
    const values = payload?.values || {};
    const notesValue = typeof values[ADMIN_NOTES_KEY] === "string" ? values[ADMIN_NOTES_KEY] : "";
    const minDownloadFreeGb = parseDownloadSpaceGb(values[REDDIT_MIN_DOWNLOAD_FREE_GB_KEY]);
    const transcodeQualityPercent = parseTranscodeQualityPercent(values[TRANSCODE_QUALITY_PERCENT_KEY]);
    const entries = Object.entries(values).filter(
      ([key]) =>
        key !== ADMIN_NOTES_KEY &&
        key !== REDDIT_MIN_DOWNLOAD_FREE_GB_KEY &&
        key !== TRANSCODE_QUALITY_PERCENT_KEY,
    );
    if (notesEl && !notesDirty) {
      notesEl.value = notesValue;
    }
    if (downloadSpaceEl) {
      downloadSpaceEl.value = String(minDownloadFreeGb);
    }
    if (transcodeQualityEl) {
      syncTranscodeQualityPreview(transcodeQualityPercent);
    }
    setNotesStatus(notesDirty ? "Alteracoes locais nao salvas." : "Anotacoes carregadas do banco local.");
    setDownloadSpaceStatus(`Minimo atual carregado: ${minDownloadFreeGb} GB.`);
    setTranscodeQualityStatus(
      `Qualidade atual carregada: ${formatTranscodeQuality(transcodeQualityPercent)}. Vale para novos transcodes.`,
    );
    table.innerHTML =
      entries.length > 0
        ? entries.map(([k, v]) => `<tr><td>${safe(k)}</td><td>${safe(v)}</td></tr>`).join("")
        : `<tr><td colspan="2">Nenhuma configuracao salva.</td></tr>`;
  } catch (error) {
    table.innerHTML = `<tr><td colspan="2">${safe(error.message)}</td></tr>`;
    setNotesStatus(`Falha ao carregar anotacoes: ${safe(error.message)}`, "error");
    setDownloadSpaceStatus(`Falha ao carregar limite de espaco: ${safe(error.message)}`, "error");
    setTranscodeQualityStatus(`Falha ao carregar qualidade: ${safe(error.message)}`, "error");
  }
}

async function refreshAll() {
  await Promise.all([
    loadHealthAndStats(),
    loadControlStatus(),
    loadRedditConfigStatus(),
    loadCurrentRedditDownloadStatus(),
    loadRedditSubscriptions(false),
    loadFolders(),
    loadVideos(),
    loadJobs(),
    loadConfig(),
  ]);
}

initCollapsibleSections();

document.getElementById("folder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = document.getElementById("folder-path").value.trim();
  const serveFiles = document.getElementById("folder-serve").checked;
  const scanSubdirs = document.getElementById("folder-subdirs").checked;
  if (!path) return;
  try {
    await api("/folders", {
      method: "POST",
      body: JSON.stringify({
        path,
        serve_files: serveFiles,
        scan_subdirs: scanSubdirs,
        is_active: true,
      }),
    });
    document.getElementById("folder-path").value = "";
    await refreshAll();
  } catch (error) {
    window.alert(error.message);
  }
});

document.getElementById("config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = document.getElementById("config-key").value.trim();
  const value = document.getElementById("config-value").value.trim();
  if (!key) return;
  try {
    await api("/config", {
      method: "PUT",
      body: JSON.stringify({ values: { [key]: value } }),
    });
    document.getElementById("config-key").value = "";
    document.getElementById("config-value").value = "";
    await refreshAll();
  } catch (error) {
    window.alert(error.message);
  }
});

const notesForm = document.getElementById("notes-form");
const notesTextarea = document.getElementById("admin-notes");
const downloadSpaceForm = document.getElementById("download-space-form");
const downloadSpaceInput = document.getElementById("download-min-free-gb");
const transcodeQualityForm = document.getElementById("transcode-quality-form");
const transcodeQualityRange = document.getElementById("transcode-quality-range");

if (notesTextarea) {
  notesTextarea.addEventListener("input", () => {
    notesDirty = true;
    setNotesStatus("Alteracoes nao salvas.");
  });
}

if (notesForm && notesTextarea) {
  notesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      setNotesStatus("Salvando anotacoes...");
      await api("/config", {
        method: "PUT",
        body: JSON.stringify({
          values: {
            [ADMIN_NOTES_KEY]: notesTextarea.value.replace(/\r\n/g, "\n"),
          },
        }),
      });
      notesDirty = false;
      await loadConfig();
      setNotesStatus("Anotacoes salvas no banco local.", "ready");
    } catch (error) {
      setNotesStatus(`Falha ao salvar anotacoes: ${safe(error.message)}`, "error");
    }
  });
}

if (downloadSpaceForm && downloadSpaceInput) {
  downloadSpaceForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const normalizedGb = Number(downloadSpaceInput.value);
    if (!Number.isFinite(normalizedGb) || normalizedGb <= 0) {
      setDownloadSpaceStatus("Informe um valor positivo em GB.", "error");
      return;
    }
    try {
      setDownloadSpaceStatus("Salvando minimo de espaco...");
      await api("/config", {
        method: "PUT",
        body: JSON.stringify({
          values: {
            [REDDIT_MIN_DOWNLOAD_FREE_GB_KEY]: String(normalizedGb),
          },
        }),
      });
      await loadConfig();
      setDownloadSpaceStatus(`Minimo de espaco salvo: ${normalizedGb} GB.`, "ready");
    } catch (error) {
      setDownloadSpaceStatus(`Falha ao salvar minimo de espaco: ${safe(error.message)}`, "error");
    }
  });
}

if (transcodeQualityRange) {
  syncTranscodeQualityPreview(transcodeQualityRange.value);
  transcodeQualityRange.addEventListener("input", () => {
    syncTranscodeQualityPreview(transcodeQualityRange.value);
    setTranscodeQualityStatus("Alteracao local pronta para salvar.");
  });
}

if (transcodeQualityForm && transcodeQualityRange) {
  transcodeQualityForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const qualityPercent = parseTranscodeQualityPercent(transcodeQualityRange.value);
    try {
      setTranscodeQualityStatus("Salvando qualidade do transcode...");
      await api("/config", {
        method: "PUT",
        body: JSON.stringify({
          values: {
            [TRANSCODE_QUALITY_PERCENT_KEY]: String(qualityPercent),
          },
        }),
      });
      await loadConfig();
      setTranscodeQualityStatus(
        `Qualidade salva: ${formatTranscodeQuality(qualityPercent)}. Novos videos usarao esse ajuste.`,
        "ready",
      );
    } catch (error) {
      setTranscodeQualityStatus(`Falha ao salvar qualidade: ${safe(error.message)}`, "error");
    }
  });
}

document.getElementById("refresh-all").addEventListener("click", refreshAll);
document.getElementById("pipeline-start").addEventListener("click", () => pipelineAction("start"));
document.getElementById("pipeline-pause").addEventListener("click", () => pipelineAction("pause"));
document.getElementById("pipeline-stop").addEventListener("click", () => pipelineAction("stop"));
document.getElementById("videos-refresh-btn").addEventListener("click", loadVideos);
document.getElementById("video-status-filter").addEventListener("change", loadVideos);

const redditFetchBtn = document.getElementById("reddit-fetch-btn");
const redditSaveBtn = document.getElementById("reddit-save-btn");
const redditSelectAllBtn = document.getElementById("reddit-select-all-btn");
const redditClearBtn = document.getElementById("reddit-clear-btn");
const redditAutoDownloadToggle = document.getElementById("reddit-auto-download-toggle");
const redditDownloadBtn = document.getElementById("reddit-download-btn");
const redditDownloadEmptyCategoriesBtn = document.getElementById("reddit-download-empty-categories-btn");
const redditStopBtn = document.getElementById("reddit-stop-btn");
const redditPeriodDownloadBtn = document.getElementById("reddit-period-download-btn");
const redditFilterInput = document.getElementById("reddit-filter-input");
const redditListEl = document.getElementById("reddit-subreddits-list");
const redditDownloadModal = document.getElementById("reddit-download-modal");
const redditDownloadStopBtn = document.getElementById("reddit-download-stop-btn");
const redditDownloadCloseBtn = document.getElementById("reddit-download-close-btn");
const redditDownloadBackdrop = redditDownloadModal ? redditDownloadModal.querySelector(".reddit-download-backdrop") : null;
const resetLibraryBtn = document.getElementById("reset-library-btn");
const cleanupAllDownloadedVideosBtn = document.getElementById("cleanup-all-downloaded-videos-btn");
const cleanupWatchedUnfavoritedBtn = document.getElementById("cleanup-watched-unfavorited-btn");
const backfillAudioBtn = document.getElementById("videos-backfill-audio-btn");
const channelsMasterLink = document.getElementById("channels-master-link");
const channelsVodLink = document.getElementById("channels-vod-link");

if (channelsMasterLink) {
  channelsMasterLink.href = `${API}/channels/master.m3u`;
}
if (channelsVodLink) {
  channelsVodLink.href = `${API}/channels/vod.m3u`;
}

if (redditFetchBtn) {
  redditFetchBtn.addEventListener("click", fetchRedditSubscriptions);
}
if (redditSaveBtn) {
  redditSaveBtn.addEventListener("click", saveRedditSelection);
}
if (redditSelectAllBtn) {
  redditSelectAllBtn.addEventListener("click", selectAllRedditSubscriptions);
}
if (redditClearBtn) {
  redditClearBtn.addEventListener("click", clearRedditSubscriptionsSelection);
}
if (redditAutoDownloadToggle) {
  redditAutoDownloadToggle.addEventListener("change", (event) => {
    updateRedditAutoDownloadEnabled(Boolean(event?.target?.checked));
  });
}
if (redditDownloadBtn) {
  redditDownloadBtn.addEventListener("click", startRedditLatestVideosDownload);
}
if (redditDownloadEmptyCategoriesBtn) {
  redditDownloadEmptyCategoriesBtn.addEventListener("click", startRedditEmptyCategoriesDownload);
}
if (redditStopBtn) {
  redditStopBtn.addEventListener("click", stopRedditDownloadProcess);
}
if (redditPeriodDownloadBtn) {
  redditPeriodDownloadBtn.addEventListener("click", startRedditPeriodDownload);
}
if (redditFilterInput) {
  redditFilterInput.addEventListener("input", (event) => {
    redditFilter = safe(event?.target?.value);
    renderRedditSubscriptions();
  });
}
if (redditListEl) {
  redditListEl.addEventListener("change", (event) => {
    const target = event.target;
    if (!target || target.tagName !== "INPUT" || target.type !== "checkbox") return;
    const name = target.getAttribute("data-reddit-name");
    updateRedditSelectionByName(name, target.checked);
    renderRedditSubscriptions();
  });
}
if (redditDownloadCloseBtn) {
  redditDownloadCloseBtn.addEventListener("click", closeRedditDownloadModal);
}
if (redditDownloadStopBtn) {
  redditDownloadStopBtn.addEventListener("click", stopRedditDownloadProcess);
}
if (redditDownloadBackdrop) {
  redditDownloadBackdrop.addEventListener("click", () => {
    if (redditDownloadCloseBtn && redditDownloadCloseBtn.disabled) return;
    closeRedditDownloadModal();
  });
}
if (resetLibraryBtn) {
  resetLibraryBtn.addEventListener("click", resetLibraryPreservingConfig);
}
if (cleanupAllDownloadedVideosBtn) {
  cleanupAllDownloadedVideosBtn.addEventListener("click", cleanupAllDownloadedVideos);
}
if (cleanupWatchedUnfavoritedBtn) {
  cleanupWatchedUnfavoritedBtn.addEventListener("click", cleanupWatchedUnfavoritedVideos);
}
if (backfillAudioBtn) {
  backfillAudioBtn.addEventListener("click", backfillAudioMetadata);
}

renderRedditSubscriptions();

refreshAll();
setInterval(() => {
  loadHealthAndStats();
  loadControlStatus();
  loadRedditConfigStatus();
  loadCurrentRedditDownloadStatus();
  loadJobs();
}, 5000);

window.toggleFolder = toggleFolder;
window.removeFolder = removeFolder;
window.reprocessVideo = reprocessVideo;
window.deleteVideo = deleteVideo;
