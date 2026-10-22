const API = window.API_BASE || "/api";
const CATEGORY = String(window.CATEGORY_NAME || "").trim();
const FAVORITES_MODE = Boolean(window.FAVORITES_MODE);
const RECENTS_MODE = Boolean(window.RECENTS_MODE);
const SECRET_MODE = Boolean(window.SECRET_MODE);
const SAVED_MODE = Boolean(window.SAVED_MODE);
const RANDOM_IMAGES_MODE = Boolean(window.RANDOM_IMAGES_MODE);
const IMAGES_MODE = Boolean(window.IMAGES_MODE) || RANDOM_IMAGES_MODE;
// Opened from the catalog: skip the grid, play the first item and return to the catalog on close.
const PLAY_MODE = Boolean(window.PLAY_MODE);
const PAGE_PARAMS = new URLSearchParams(window.location.search);
// , and . hops inside play mode replace history and carry from=catalog, so Esc still goes back to the catalog.
const CAME_FROM_CATALOG = PLAY_MODE && (PAGE_PARAMS.get("from") === "catalog" || (() => {
  try {
    return new URL(document.referrer).pathname === "/admin/catalog";
  } catch (_) {
    return false;
  }
})());
// Set by , and . from a regular gallery: open the first item but keep the grid behind it.
const AUTOPLAY_REQUESTED = PAGE_PARAMS.get("autoplay") === "1";
// Written by category_catalog.js when a cover is clicked.
const CATALOG_ORDER_KEY = "media_catalog_order_v1";
const IS_CATEGORY_VIEW = (
  CATEGORY.length > 0
  && !FAVORITES_MODE
  && !RECENTS_MODE
  && !SECRET_MODE
  && !SAVED_MODE
  && !RANDOM_IMAGES_MODE
);
const IS_SAVED_VIEW = SAVED_MODE;
const IS_USER_CATEGORY = CATEGORY.toLowerCase().startsWith("u_");

const PAGE_SIZE = 60;
const IMAGE_PAGE_SIZE = 25;
const IMAGE_AUTO_ADVANCE_BASE_MS = 3000;
const IMAGE_SLIDESHOW_SPEED_STEP_MIN = -3;
const IMAGE_SLIDESHOW_SPEED_STEP_MAX = 4;
const PREWARM_RANGE = "bytes=0-262143";
const PREWARM_TIMEOUT_MS = 2200;
const GALLERY_CATEGORIES_CACHE_KEY = "galleryCategoriesCacheV1";
const GALLERY_CATEGORIES_CACHE_TTL_MS = 5 * 60 * 1000;
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
let coverRequestInFlight = false;
let coverToastTimer = null;
let advanceForwardTaskInFlight = false;
let bulkCategoryDeleteInFlight = false;
let suppressFurtherGalleryUpdates = false;
let redditDownloadPollTimer = null;
let selectedPlaybackRate = 1;
let isImageModalOpen = false;
let imageAutoAdvanceTimer = null;
let imageSlideshowSpeedStep = 0;
let imageScrollObserver = null;
let imageFeedTotal = null;
const categoryMediaCounts = {
  loaded: false,
  videos: 0,
  images: 0,
};

const watchedMarkedInSession = new Set();
const favoritedInSession = new Set();
let activePlayback = null;
let activeImagePlayback = null;
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
  if (RANDOM_IMAGES_MODE) return "Imagens aleatorias";
  if (FAVORITES_MODE) return "Favoritos";
  if (RECENTS_MODE) return "Recentes";
  if (SECRET_MODE) return "Nao vistos";
  if (SAVED_MODE) return IMAGES_MODE ? "Meus Salvos - Imagens" : "Meus Salvos";
  if (!CATEGORY) return "Categoria";
  const suffix = IMAGES_MODE ? " - Imagens" : "";
  return `${IS_USER_CATEGORY ? "Usuario" : "Categoria"}: ${CATEGORY}${suffix}`;
}

function getMediaItemLabel() {
  return IMAGES_MODE ? "imagens" : "videos";
}

function isUserCategoryName(name) {
  return normalizeCategoryKey(name).startsWith("u_");
}

function buildCategoryGalleryUrl(imagesMode = false, categoryName = CATEGORY) {
  const params = new URLSearchParams();
  if (SAVED_MODE) {
    params.set("saved", "1");
    params.set("name", "Meus Salvos");
  } else {
    params.set("name", String(categoryName || "").trim());
  }
  if (imagesMode) {
    params.set("images", "1");
  }
  return `/admin/category?${params.toString()}`;
}

function filterNavigableCategories(items, imagesMode = IMAGES_MODE) {
  const list = Array.isArray(items) ? items : [];
  return list.filter((item) => {
    const name = String(item?.category || "").trim();
    if (!name) return false;
    if (isUserCategoryName(name) !== IS_USER_CATEGORY) return false;
    if (imagesMode) {
      return Number(item?.image_count || 0) > 0;
    }
    return Number(item?.video_count || 0) > 0;
  });
}

// A galeria pode abrir em modo video mesmo quando o conteudo dominante e imagem
// (subs u_ com um ou dois videos soltos). Nesse caso a navegacao segue a trilha
// de imagens para nao cair em galerias sem nenhuma imagem.
function resolveNavigationImagesMode(items) {
  if (IMAGES_MODE) return true;
  const currentKey = normalizeCategoryKey(CATEGORY);
  const current = (Array.isArray(items) ? items : []).find(
    (item) => normalizeCategoryKey(item?.category) === currentKey,
  );
  if (!current) return false;
  return Number(current.image_count || 0) > Number(current.video_count || 0);
}

const GALLERY_IMAGE_NAV_BACK_STACK_KEY = "galleryImageNavBackStack";
const GALLERY_IMAGE_NAV_MAX_ITEMS = 10;

function getCategoryImageCount(item) {
  return Number(item?.image_count || 0);
}

function pickRandomNextImageGallery(navigable, currentKey, maxItems = GALLERY_IMAGE_NAV_MAX_ITEMS) {
  const candidates = (Array.isArray(navigable) ? navigable : []).filter((item) => {
    const key = normalizeCategoryKey(item?.category);
    if (!key || key === currentKey) return false;
    const count = getCategoryImageCount(item);
    return count > 0 && count <= maxItems;
  });
  if (candidates.length === 0) return null;
  return candidates[Math.floor(Math.random() * candidates.length)];
}

function readGalleryNavBackStack() {
  try {
    const raw = sessionStorage.getItem(GALLERY_IMAGE_NAV_BACK_STACK_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((url) => typeof url === "string" && url.trim()) : [];
  } catch (_error) {
    return [];
  }
}

function writeGalleryNavBackStack(stack) {
  sessionStorage.setItem(GALLERY_IMAGE_NAV_BACK_STACK_KEY, JSON.stringify(stack));
}

function pushGalleryNavBackStack(url) {
  const normalized = String(url || "").trim();
  if (!normalized) return;
  const stack = readGalleryNavBackStack();
  stack.push(normalized);
  writeGalleryNavBackStack(stack);
}

function peekGalleryNavBackStack() {
  const stack = readGalleryNavBackStack();
  return stack.length > 0 ? stack[stack.length - 1] : null;
}

function popGalleryNavBackStack() {
  const stack = readGalleryNavBackStack();
  if (stack.length === 0) return null;
  const url = stack.pop();
  writeGalleryNavBackStack(stack);
  return url || null;
}

let galleryNavClickHandlersReady = false;
let galleryDownloadListHandlersReady = false;
let addDownloadListInFlight = false;
let currentGalleryInDownloadList = false;

function getCurrentGalleryDownloadListKey() {
  if (!CATEGORY) return null;
  const mediaType = IMAGES_MODE ? "images" : "videos";
  return `${normalizeCategoryKey(CATEGORY)}::${mediaType}`;
}

function isGalleryInDownloadList(items) {
  const currentKey = getCurrentGalleryDownloadListKey();
  if (!currentKey) return false;
  return (Array.isArray(items) ? items : []).some((item) => {
    const nameKey = normalizeCategoryKey(item?.name);
    const mediaType = String(item?.media_type || "").trim().toLowerCase();
    if (!nameKey || (mediaType !== "images" && mediaType !== "videos")) return false;
    return `${nameKey}::${mediaType}` === currentKey;
  });
}

async function refreshGalleryDownloadListMembership() {
  if (!IS_CATEGORY_VIEW || !CATEGORY) {
    currentGalleryInDownloadList = false;
    updateGalleryDownloadListButtonVisibility();
    return;
  }
  try {
    const payload = await api("/gallery/download-list");
    const items = Array.isArray(payload?.items) ? payload.items : [];
    currentGalleryInDownloadList = isGalleryInDownloadList(items);
  } catch (_) {
    currentGalleryInDownloadList = false;
  }
  updateGalleryDownloadListButtonVisibility();
}

function configureGalleryNavClickHandlers() {
  if (galleryNavClickHandlersReady) return;
  const prevLink = document.getElementById("gallery-prev-link");
  const nextLink = document.getElementById("gallery-next-link");
  if (!prevLink || !nextLink) return;
  galleryNavClickHandlersReady = true;

  nextLink.addEventListener("click", () => {
    if (nextLink.dataset.navImagesMode !== "1") return;
    if (nextLink.classList.contains("is-disabled")) return;
    pushGalleryNavBackStack(`${window.location.pathname}${window.location.search}`);
  });

  prevLink.addEventListener("click", (event) => {
    if (prevLink.dataset.navImagesMode !== "1") return;
    if (prevLink.classList.contains("is-disabled")) return;
    event.preventDefault();
    const url = popGalleryNavBackStack();
    if (url) {
      window.location.href = url;
    }
  });
}

function updateGalleryDownloadListButtonVisibility() {
  const addBtn = document.getElementById("gallery-add-download-list-btn");
  if (!addBtn) return;
  addBtn.hidden = !IS_CATEGORY_VIEW;
  addBtn.disabled = addDownloadListInFlight || !CATEGORY || currentGalleryInDownloadList;
  addBtn.title = currentGalleryInDownloadList
    ? "Esta galeria ja esta na lista de downloads."
    : "Adicionar esta galeria a lista de downloads do admin.";
}

async function addCurrentGalleryToDownloadList() {
  if (!IS_CATEGORY_VIEW || !CATEGORY || addDownloadListInFlight) return;

  addDownloadListInFlight = true;
  updateGalleryDownloadListButtonVisibility();
  try {
    const payload = await api("/gallery/download-list", {
      method: "POST",
      body: JSON.stringify({
        name: CATEGORY,
        media_type: IMAGES_MODE ? "images" : "videos",
      }),
    });
    const message = String(payload?.message || "").trim();
    currentGalleryInDownloadList = true;
    if (message) {
      updateStatus(message, false);
      return;
    }
    updateStatus(
      payload?.added
        ? "Galeria adicionada a lista de downloads."
        : "Galeria ja estava na lista de downloads.",
      false,
    );
  } catch (error) {
    updateStatus(`Falha ao adicionar a lista de downloads: ${safe(error.message)}`, true);
  } finally {
    addDownloadListInFlight = false;
    updateGalleryDownloadListButtonVisibility();
  }
}

function configureGalleryDownloadListButton() {
  if (galleryDownloadListHandlersReady) return;
  const addBtn = document.getElementById("gallery-add-download-list-btn");
  if (!addBtn) return;
  galleryDownloadListHandlersReady = true;
  addBtn.addEventListener("click", () => {
    addCurrentGalleryToDownloadList();
  });
  updateGalleryDownloadListButtonVisibility();
}

function setGalleryNavLink(linkEl, target) {
  if (!linkEl) return;
  if (!target) {
    linkEl.href = "#";
    linkEl.classList.add("is-disabled");
    linkEl.setAttribute("aria-disabled", "true");
    linkEl.removeAttribute("title");
    return;
  }
  linkEl.href = target.href;
  linkEl.classList.remove("is-disabled");
  linkEl.setAttribute("aria-disabled", "false");
  if (target.title) {
    linkEl.title = target.title;
  } else {
    linkEl.removeAttribute("title");
  }
  if (target.label) {
    linkEl.textContent = target.label;
  }
}

function renderGalleryNavigation(items) {
  const navEl = document.getElementById("gallery-nav");
  const prevLink = document.getElementById("gallery-prev-link");
  const nextLink = document.getElementById("gallery-next-link");
  if (!navEl || !prevLink || !nextLink) return;

  if (!IS_CATEGORY_VIEW) {
    navEl.hidden = true;
    return;
  }

  const navImagesMode = resolveNavigationImagesMode(items);
  const navigable = filterNavigableCategories(items, navImagesMode);
  const currentKey = normalizeCategoryKey(CATEGORY);
  const idx = navigable.findIndex((item) => normalizeCategoryKey(item?.category) === currentKey);
  const mediaSuffix = navImagesMode === IMAGES_MODE ? "" : " (imagens)";

  let prevTarget = null;
  let nextTarget = null;
  if (navImagesMode) {
    const nextItem = pickRandomNextImageGallery(navigable, currentKey);
    if (nextItem) {
      const name = String(nextItem.category || "").trim();
      nextTarget = {
        href: buildCategoryGalleryUrl(navImagesMode, name),
        label: `Proxima: ${name}${mediaSuffix}`,
        title: `${name}${mediaSuffix}`,
      };
    }
    const backUrl = peekGalleryNavBackStack();
    if (backUrl) {
      prevTarget = {
        href: backUrl,
        label: "Anterior",
        title: backUrl,
      };
    }
  } else if (idx >= 0) {
    const prevItem = navigable[idx - 1];
    const nextItem = navigable[idx + 1];
    if (prevItem) {
      const name = String(prevItem.category || "").trim();
      prevTarget = {
        href: buildCategoryGalleryUrl(navImagesMode, name),
        label: "Anterior",
        title: `${name}${mediaSuffix}`,
      };
    }
    if (nextItem) {
      const name = String(nextItem.category || "").trim();
      nextTarget = {
        href: buildCategoryGalleryUrl(navImagesMode, name),
        label: `Proxima: ${name}${mediaSuffix}`,
        title: `${name}${mediaSuffix}`,
      };
    }
  }

  navEl.hidden = false;
  if (!prevTarget) {
    prevLink.textContent = "Anterior";
  }
  if (!nextTarget) {
    nextLink.textContent = "Proxima galeria";
  }
  setGalleryNavLink(prevLink, prevTarget);
  setGalleryNavLink(nextLink, nextTarget);
  prevLink.dataset.navImagesMode = navImagesMode ? "1" : "0";
  nextLink.dataset.navImagesMode = navImagesMode ? "1" : "0";
  updateGalleryDownloadListButtonVisibility();
}

async function loadCategoryMediaCounts() {
  if (IS_SAVED_VIEW) {
    try {
      const [videosPayload, imagesPayload] = await Promise.all([
        api("/feed?only_reddit_saved=true&page_size=1&order_by=detected_desc"),
        api("/feed/images?only_reddit_saved=true&page_size=1&order_by=detected_desc"),
      ]);
      categoryMediaCounts.loaded = true;
      // Video feed has no total; approximate presence via first page / cursor.
      categoryMediaCounts.videos = Array.isArray(videosPayload?.items) && videosPayload.items.length > 0
        ? Math.max(1, Number(videosPayload.items.length || 0))
        : 0;
      if (videosPayload?.next_cursor) {
        categoryMediaCounts.videos = Math.max(categoryMediaCounts.videos, 2);
      }
      categoryMediaCounts.images = Number(imagesPayload?.total || 0);
    } catch (_error) {
      categoryMediaCounts.loaded = true;
    }
    renderGalleryMediaTabs();
    return;
  }
  if (!IS_CATEGORY_VIEW || !CATEGORY) return;
  try {
    const payload = await api(`/feed/categories/${encodeURIComponent(CATEGORY)}/media-counts`);
    categoryMediaCounts.loaded = true;
    categoryMediaCounts.videos = Number(payload?.videos || 0);
    categoryMediaCounts.images = Number(payload?.images || 0);
  } catch (_error) {
    categoryMediaCounts.loaded = true;
  }
  renderGalleryMediaTabs();
}

function renderGalleryMediaTabs() {
  const tabsEl = document.getElementById("gallery-media-tabs");
  const videosTab = document.getElementById("gallery-tab-videos");
  const imagesTab = document.getElementById("gallery-tab-images");
  if (!tabsEl || !videosTab || !imagesTab) return;

  if ((!IS_CATEGORY_VIEW && !IS_SAVED_VIEW)) {
    tabsEl.hidden = true;
    return;
  }

  if (!categoryMediaCounts.loaded && IMAGES_MODE && IS_CATEGORY_VIEW) {
    tabsEl.hidden = false;
    videosTab.href = buildCategoryGalleryUrl(false);
    imagesTab.href = buildCategoryGalleryUrl(true);
    videosTab.classList.toggle("is-active", false);
    imagesTab.classList.toggle("is-active", true);
    return;
  }

  if (!categoryMediaCounts.loaded) {
    tabsEl.hidden = true;
    return;
  }

  const showTabs = categoryMediaCounts.videos > 0 || categoryMediaCounts.images > 0 || IS_SAVED_VIEW;
  tabsEl.hidden = !showTabs;
  if (!showTabs) return;

  videosTab.href = buildCategoryGalleryUrl(false);
  imagesTab.href = buildCategoryGalleryUrl(true);
  videosTab.classList.toggle("is-active", !IMAGES_MODE);
  imagesTab.classList.toggle("is-active", IMAGES_MODE);
}

function getScopedEntityText() {
  if (!CATEGORY) return "categoria";
  return `${IS_USER_CATEGORY ? "usuario" : "categoria"} ${CATEGORY}`;
}

function getEmptyContextMessage() {
  if (RANDOM_IMAGES_MODE) return "Nenhuma imagem baixada encontrada.";
  if (SAVED_MODE) {
    return IMAGES_MODE
      ? "Nenhuma imagem salva sincronizada ainda."
      : "Nenhum video salvo sincronizado ainda.";
  }
  if (IMAGES_MODE) {
    return IS_USER_CATEGORY
      ? "Nenhuma imagem encontrada para este usuario."
      : "Nenhuma imagem encontrada para esta categoria.";
  }
  if (SECRET_MODE) return "Nenhum video pendente encontrado.";
  return IS_USER_CATEGORY
    ? "Nenhum video encontrado para este usuario."
    : "Nenhum video encontrado para esta categoria.";
}

function getLoadingContextMessage() {
  if (RANDOM_IMAGES_MODE) return "Filtro: Imagens aleatorias";
  if (FAVORITES_MODE) return "Filtro: Favoritos";
  if (RECENTS_MODE) return "Filtro: Recentes";
  if (SECRET_MODE) return "Filtro: Nao vistos";
  if (SAVED_MODE) return "Filtro: Meus Salvos";
  return getPageTitle();
}

function getLoadedContextMessage() {
  if (RANDOM_IMAGES_MODE) return `Imagens aleatorias carregadas: ${loadedCount}`;
  if (FAVORITES_MODE) return `Favoritos carregados: ${loadedCount} videos`;
  if (RECENTS_MODE) return `Recentes carregados: ${loadedCount} videos`;
  if (SECRET_MODE) return `Nao vistos carregados: ${loadedCount} videos`;
  if (SAVED_MODE) return `Meus Salvos carregados: ${loadedCount} ${getMediaItemLabel()}`;
  return `${IS_USER_CATEGORY ? "Usuario carregado" : "Categoria carregada"}: ${loadedCount} ${getMediaItemLabel()}`;
}

function configureGalleryTools() {
  const toolsEl = document.getElementById("gallery-tools");
  const deleteBtn = document.getElementById("gallery-delete-category-btn");
  const periodBtn = document.getElementById("gallery-period-download-btn");
  if (!toolsEl) return;

  toolsEl.hidden = !IS_CATEGORY_VIEW;
  if (!IS_CATEGORY_VIEW) return;

  if (deleteBtn) {
    deleteBtn.textContent = "Remover Galeria";
    deleteBtn.title = IS_USER_CATEGORY
      ? "Remove permanentemente todos os videos e imagens deste usuario, sem blacklist."
      : "Remove permanentemente todos os videos e imagens desta categoria, sem blacklist.";
  }

  if (periodBtn) {
    periodBtn.textContent = IMAGES_MODE ? "Baixar periodo imagens" : "Baixar periodo videos";
    periodBtn.title = IMAGES_MODE
      ? (IS_USER_CATEGORY
        ? "Baixar imagens antigas deste usuario por periodo."
        : "Baixar imagens antigas desta categoria por periodo.")
      : (IS_USER_CATEGORY
        ? "Baixar videos antigos deste usuario por periodo."
        : "Baixar videos antigos desta categoria por periodo.");
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
  if (!IS_CATEGORY_VIEW) {
    renderGalleryNavigation([]);
    return;
  }

  const cachedNav = readGalleryCategoriesCache();
  if (cachedNav) {
    renderGalleryNavigation(cachedNav);
  }

  try {
    const progress = await api(`/feed/categories/${encodeURIComponent(CATEGORY)}/progress`);
    setCategoryProgress(progress?.total || 0, progress?.watched || 0);
  } catch (_) {
    // Keep the gallery usable even if the aggregate progress lookup fails.
  }

  void refreshGalleryNavigationList(Boolean(cachedNav));
}

async function refreshGalleryNavigationList(hasCachedNav) {
  try {
    const payload = await api("/feed/categories");
    const items = Array.isArray(payload) ? payload : [];
    writeGalleryCategoriesCache(items);
    renderGalleryNavigation(items);
    const current = items.find(
      (item) => normalizeCategoryKey(item?.category) === normalizeCategoryKey(CATEGORY),
    );
    if (current) {
      setCategoryProgress(current?.count || 0, current?.watched_count || 0);
    }
  } catch (_) {
    if (!hasCachedNav) {
      renderGalleryNavigation([]);
    }
  }
}

function readGalleryCategoriesCache() {
  try {
    const raw = sessionStorage.getItem(GALLERY_CATEGORIES_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.items)) return null;
    if (Date.now() - Number(parsed.savedAt || 0) > GALLERY_CATEGORIES_CACHE_TTL_MS) return null;
    return parsed.items;
  } catch (_) {
    return null;
  }
}

function writeGalleryCategoriesCache(items) {
  try {
    sessionStorage.setItem(
      GALLERY_CATEGORIES_CACHE_KEY,
      JSON.stringify({ savedAt: Date.now(), items }),
    );
  } catch (_) {
    // Ignore storage quota or privacy mode errors.
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

  const mediaType = IMAGES_MODE ? "images" : "videos";
  const mediaLabel = IMAGES_MODE ? "imagens" : "videos";

  openRedditDownloadModal();
  renderRedditDownloadStatus({
    state: "running",
    media_type: mediaType,
    total_subreddits: 1,
    completed_subreddits: 0,
    total_videos_planned: 0,
    processed_videos: 0,
    failed_videos: 0,
    items: [{ name: CATEGORY, status: "pending", planned: 0, completed: 0, failed: 0 }],
    logs: [`Iniciando download de ${mediaLabel} do periodo de ${periodMonths} mes(es)...`],
  });

  try {
    await api("/reddit/download/start", {
      method: "POST",
      body: JSON.stringify({
        names: [CATEGORY],
        period_months: periodMonths,
        per_subreddit_limit: null,
        media_type: mediaType,
      }),
    });
    updateStatus(
      `Download de ${mediaLabel} por periodo iniciado para ${getScopedEntityText()} (${periodMonths} mes(es)).`,
    );
    await pollRedditDownloadStatusOnce();
    startRedditDownloadPolling();
  } catch (error) {
    updateStatus(`Falha ao iniciar download por periodo: ${safe(error.message)}`, true);
    renderRedditDownloadStatus({
      state: "failed",
      media_type: mediaType,
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
    `Remover permanentemente todos os videos e imagens deste ${entityLabel}?\n\nIsso nao adiciona ${entityLabel} a blacklist e nao bloqueia novos downloads.`,
  );
  if (!confirmed) return;

  const deleteBtn = document.getElementById("gallery-delete-category-btn");
  bulkCategoryDeleteInFlight = true;
  suppressFurtherGalleryUpdates = true;
  if (deleteBtn) {
    deleteBtn.disabled = true;
  }
  closeImageModal();
  closeVideoModal();

  try {
    const payload = await api("/videos/category/delete", {
      method: "POST",
      body: JSON.stringify({ category: CATEGORY }),
    });
    clearGalleryGrid();
    setCategoryProgress(0, 0);
    categoryMediaCounts.videos = 0;
    categoryMediaCounts.images = 0;
    categoryMediaCounts.loaded = true;
    renderGalleryMediaTabs();
    const deletedVideos = Number(payload?.deleted_videos || 0);
    const deletedImages = Number(payload?.deleted_images || 0);
    const parts = [];
    if (deletedVideos > 0) parts.push(`${deletedVideos} video(s)`);
    if (deletedImages > 0) parts.push(`${deletedImages} imagem(ns)`);
    if (parts.length > 0) {
      updateStatus(`Remocao de ${parts.join(" e ")} de ${getScopedEntityText()} iniciada.`);
    } else {
      updateStatus(`Nenhum video ou imagem encontrado em ${getScopedEntityText()}.`);
    }
  } catch (error) {
    suppressFurtherGalleryUpdates = false;
    updateStatus(`Falha ao remover galeria: ${safe(error.message)}`, true);
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

function renderImageThumbCard(item) {
  const imageId = Number(item.id) || 0;
  const fileUrl = safe(item.file_url);
  const category = safe(item.path_category);
  const date = safe(item.path_date);
  const isWatched = Boolean(item.is_watched);
  const isFavorite = Boolean(item.is_favorite);
  const caption = `Imagem #${safe(item.id)} | ${category || "-"} | ${date || "-"}`;
  const thumbHtml = fileUrl
    ? `<img src="${escapeHtml(fileUrl)}" loading="lazy" alt="Imagem ${safe(item.id)}" />`
    : `<div class="thumb-placeholder">Sem imagem</div>`;
  const mediaAttrs = fileUrl
    ? `href="${escapeHtml(fileUrl)}" data-image-url="${escapeHtml(fileUrl)}" data-caption="${escapeHtml(caption)}" data-image-id="${imageId}" data-is-watched="${isWatched ? "true" : "false"}" data-is-favorite="${isFavorite ? "true" : "false"}"`
    : "href=\"#\"";
  const watchedBadge = isWatched ? renderWatchedBadge() : "";
  const favoriteBadge = isFavorite ? renderFavoriteBadge() : "";

  return `
    <article class="thumb-card thumb-card-image${isWatched ? " is-watched" : ""}${isFavorite ? " is-favorite" : ""}" data-image-id="${imageId}" data-is-watched="${isWatched ? "true" : "false"}" data-is-favorite="${isFavorite ? "true" : "false"}">
      <a class="thumb-media ${fileUrl ? "" : "is-disabled"}" ${mediaAttrs}>
        ${thumbHtml}
      </a>
      <div class="thumb-info">
        <div class="thumb-title-row">
          <div class="thumb-title">Imagem #${safe(item.id)}</div>
          <div class="thumb-badges">${favoriteBadge}${watchedBadge}</div>
        </div>
        <div class="thumb-meta">
          <span>${escapeHtml(category || "-")}</span>
          <span>${escapeHtml(date || "-")}</span>
        </div>
      </div>
    </article>
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

function syncImageFavoriteUi(imageId, isFavorite) {
  if (!imageId) return;

  const cards = document.querySelectorAll(`.thumb-card-image[data-image-id="${imageId}"]`);
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

  const links = document.querySelectorAll(`.thumb-media[data-image-id="${imageId}"]`);
  links.forEach((linkEl) => {
    if (linkEl instanceof HTMLElement) {
      linkEl.dataset.isFavorite = isFavorite ? "true" : "false";
    }
  });
}

async function markImageFavorite(imageId) {
  const normalizedId = Number(imageId) || 0;
  if (!normalizedId) return;

  const sessionKey = `image:${normalizedId}`;
  if (favoritedInSession.has(sessionKey)) {
    updateStatus(`Imagem #${normalizedId} ja esta favoritada.`);
    syncImageFavoriteUi(normalizedId, true);
    triggerFavoriteSplash();
    return;
  }

  await api(`/images/${normalizedId}/favorite`, {
    method: "PUT",
    body: JSON.stringify({
      is_favorite: true,
    }),
  });

  favoritedInSession.add(sessionKey);
  syncImageFavoriteUi(normalizedId, true);
  triggerFavoriteSplash();
  updateStatus(`Imagem #${normalizedId} favoritada.`);
}

function triggerFavoriteSplash() {
  const splashId = isImageModalOpen ? "image-favorite-splash" : "favorite-splash";
  const splashEl = document.getElementById(splashId) || document.getElementById("favorite-splash");
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

function ensureGalleryToastEl() {
  let toastEl = document.getElementById("gallery-cover-toast");
  if (toastEl) return toastEl;
  toastEl = document.createElement("div");
  toastEl.id = "gallery-cover-toast";
  toastEl.className = "gallery-cover-toast";
  toastEl.setAttribute("aria-live", "polite");
  document.body.appendChild(toastEl);
  return toastEl;
}

function showGalleryToast(message, isError = false) {
  const toastEl = ensureGalleryToastEl();
  toastEl.textContent = message;
  toastEl.classList.toggle("is-error", Boolean(isError));
  toastEl.classList.add("is-visible");
  window.clearTimeout(coverToastTimer);
  coverToastTimer = window.setTimeout(() => {
    toastEl.classList.remove("is-visible");
  }, 1800);
}

function apiErrorDetail(error) {
  const message = error && typeof error.message === "string" ? error.message : "";
  const jsonStart = message.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const detail = JSON.parse(message.slice(jsonStart))?.detail;
      if (typeof detail === "string" && detail.trim()) return detail.trim();
    } catch (_) {
      // fall through to the raw message
    }
  }
  return message || "erro desconhecido";
}

async function setCategoryCoverFromActiveMedia() {
  if (coverRequestInFlight) return;

  let endpoint = "";
  if (isImageModalOpen) {
    const imageId = Number(activeImagePlayback?.imageId || 0) || 0;
    if (imageId) endpoint = `/images/${imageId}/category-cover`;
  } else {
    const videoId = resolveActiveVideoId();
    if (videoId) endpoint = `/videos/${videoId}/category-cover`;
  }
  if (!endpoint) {
    showGalleryToast("Falha ao definir capa: midia atual sem id.", true);
    return;
  }

  coverRequestInFlight = true;
  try {
    const result = await api(endpoint, { method: "POST" });
    const message = `Capa de ${safe(result?.category).trim() || "categoria"} definida.`;
    showGalleryToast(message);
    updateStatus(message);
  } catch (error) {
    const message = `Falha ao definir capa: ${apiErrorDetail(error)}`;
    showGalleryToast(message, true);
    updateStatus(message, true);
  } finally {
    coverRequestInFlight = false;
  }
}

function isCoverShortcut(event, code, keyLower) {
  if (event.repeat || event.ctrlKey || event.metaKey || event.altKey) return false;
  return code === "KeyC" || keyLower === "c";
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

function getImageLinks() {
  const gridEl = document.getElementById("thumb-grid");
  if (!gridEl) return [];

  const links = Array.from(gridEl.querySelectorAll(".thumb-media[data-image-url]"));
  return links.filter((linkEl) => {
    if (!(linkEl instanceof HTMLAnchorElement)) return false;
    return Boolean((linkEl.dataset.imageUrl || "").trim());
  });
}

function isImageLinkWatched(linkEl) {
  if (!(linkEl instanceof HTMLAnchorElement)) return false;
  const imageId = Number(linkEl.dataset.imageId || "0") || 0;
  if (imageId > 0 && watchedMarkedInSession.has(`image:${imageId}`)) {
    return true;
  }
  return (linkEl.dataset.isWatched || "").toLowerCase() === "true";
}

function findNextSlideshowImageIndex(fromIndex, step, links = getImageLinks()) {
  if (!Array.isArray(links) || !links.length) return -1;
  if (!step) return -1;

  let index = fromIndex + step;
  while (index >= 0 && index < links.length) {
    if (!isImageLinkWatched(links[index])) {
      return index;
    }
    index += step;
  }
  return -1;
}

function findImageIndex(imageId, imageUrl) {
  const links = getImageLinks();
  if (!links.length) return -1;

  if (imageId > 0) {
    const byId = links.findIndex((linkEl) => Number(linkEl.dataset.imageId || "0") === imageId);
    if (byId >= 0) return byId;
  }

  const normalizedUrl = (imageUrl || "").trim();
  if (normalizedUrl) {
    const byUrl = links.findIndex((linkEl) => (linkEl.dataset.imageUrl || "") === normalizedUrl);
    if (byUrl >= 0) return byUrl;
  }

  return -1;
}

function clampScroll(stageEl, scrollLeft, scrollTop) {
  const maxScrollX = Math.max(0, stageEl.scrollWidth - stageEl.clientWidth);
  const maxScrollY = Math.max(0, stageEl.scrollHeight - stageEl.clientHeight);
  stageEl.scrollLeft = Math.max(0, Math.min(maxScrollX, scrollLeft));
  stageEl.scrollTop = Math.max(0, Math.min(maxScrollY, scrollTop));
}

function centerZoomedImageInStage() {
  const stageEl = document.getElementById("image-modal-stage");
  if (!stageEl) return;
  const maxScrollX = Math.max(0, stageEl.scrollWidth - stageEl.clientWidth);
  const maxScrollY = Math.max(0, stageEl.scrollHeight - stageEl.clientHeight);
  clampScroll(stageEl, maxScrollX / 2, maxScrollY / 2);
}

function focusZoomedImageAtPoint(focusPoint) {
  const stageEl = document.getElementById("image-modal-stage");
  const viewerEl = document.getElementById("image-modal-viewer");
  if (!stageEl || !viewerEl || !focusPoint) {
    centerZoomedImageInStage();
    return;
  }

  const ratioX = Math.min(1, Math.max(0, Number(focusPoint.ratioX) || 0));
  const ratioY = Math.min(1, Math.max(0, Number(focusPoint.ratioY) || 0));
  const targetClientX = Number(focusPoint.clientX);
  const targetClientY = Number(focusPoint.clientY);
  if (!Number.isFinite(targetClientX) || !Number.isFinite(targetClientY)) {
    centerZoomedImageInStage();
    return;
  }

  const viewerRect = viewerEl.getBoundingClientRect();
  if (viewerRect.width <= 0 || viewerRect.height <= 0) {
    centerZoomedImageInStage();
    return;
  }

  const pointClientX = viewerRect.left + ratioX * viewerRect.width;
  const pointClientY = viewerRect.top + ratioY * viewerRect.height;
  clampScroll(
    stageEl,
    stageEl.scrollLeft + (pointClientX - targetClientX),
    stageEl.scrollTop + (pointClientY - targetClientY),
  );
}

function captureImageZoomFocusPoint(event, viewerEl) {
  if (!(event instanceof MouseEvent) || !(viewerEl instanceof HTMLElement)) {
    return null;
  }
  const rect = viewerEl.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  return {
    ratioX: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
    ratioY: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    clientX: event.clientX,
    clientY: event.clientY,
  };
}

function isImageModalZoomed() {
  const modalEl = document.getElementById("image-modal");
  return Boolean(modalEl && modalEl.classList.contains("is-zoomed"));
}

function getImageSlideshowRate() {
  return 2 ** imageSlideshowSpeedStep;
}

function getImageAutoAdvanceMs() {
  return Math.max(250, Math.round(IMAGE_AUTO_ADVANCE_BASE_MS / getImageSlideshowRate()));
}

function formatImageSlideshowRate(rate = getImageSlideshowRate()) {
  if (Number.isInteger(rate)) return `${rate}x`;
  const rounded = Math.round(rate * 100) / 100;
  return `${String(rounded).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1")}x`;
}

function formatImageAutoAdvanceLabel(ms = getImageAutoAdvanceMs()) {
  if (ms >= 1000) {
    const seconds = Math.round((ms / 1000) * 10) / 10;
    return `${seconds}s`;
  }
  return `${ms}ms`;
}

function updateImageSlideshowSpeedIndicator() {
  const indicatorEl = document.getElementById("image-speed-indicator");
  if (!indicatorEl) return;
  indicatorEl.textContent = formatImageSlideshowRate();
}

function adjustImageSlideshowSpeed(factor) {
  if (!isImageModalOpen || !factor) return;

  const delta = factor > 1 ? 1 : factor < 1 ? -1 : 0;
  if (!delta) return;

  const nextStep = Math.max(
    IMAGE_SLIDESHOW_SPEED_STEP_MIN,
    Math.min(IMAGE_SLIDESHOW_SPEED_STEP_MAX, imageSlideshowSpeedStep + delta),
  );
  if (nextStep === imageSlideshowSpeedStep) {
    updateStatus(
      `Slideshow no limite: ${formatImageSlideshowRate()} · ${formatImageAutoAdvanceLabel()}`,
    );
    return;
  }

  imageSlideshowSpeedStep = nextStep;
  updateImageSlideshowSpeedIndicator();
  updateStatus(
    `Slideshow ${formatImageSlideshowRate()} · ${formatImageAutoAdvanceLabel()}`,
  );
  scheduleImageAutoAdvance();
}

function clearImageAutoAdvance() {
  if (imageAutoAdvanceTimer == null) return;
  clearTimeout(imageAutoAdvanceTimer);
  imageAutoAdvanceTimer = null;
}

function scheduleImageAutoAdvance() {
  clearImageAutoAdvance();
  if (!isImageModalOpen || isImageModalZoomed()) return;

  imageAutoAdvanceTimer = setTimeout(() => {
    imageAutoAdvanceTimer = null;
    if (!isImageModalOpen || isImageModalZoomed()) return;
    navigateImageModal(1);
  }, getImageAutoAdvanceMs());
}

function setImageModalZoom(zoomed, focusPoint = null) {
  const modalEl = document.getElementById("image-modal");
  const stageEl = document.getElementById("image-modal-stage");
  if (!modalEl) return;
  modalEl.classList.toggle("is-zoomed", Boolean(zoomed));

  if (zoomed) {
    clearImageAutoAdvance();
  } else if (isImageModalOpen) {
    scheduleImageAutoAdvance();
  }

  if (!stageEl) return;

  if (!zoomed) {
    stageEl.scrollTop = 0;
    stageEl.scrollLeft = 0;
    return;
  }

  // Wait for layout after natural-size image expands the scroll area.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (focusPoint) {
        focusZoomedImageAtPoint(focusPoint);
      } else {
        centerZoomedImageInStage();
      }
    });
  });
}

function toggleImageModalZoom(focusPoint = null) {
  const modalEl = document.getElementById("image-modal");
  if (!modalEl || !isImageModalOpen) return;
  const willZoom = !modalEl.classList.contains("is-zoomed");
  setImageModalZoom(willZoom, willZoom ? focusPoint : null);
}

function closeImageModal() {
  const modalEl = document.getElementById("image-modal");
  const viewerEl = document.getElementById("image-modal-viewer");
  const captionEl = document.getElementById("image-modal-caption");
  if (!modalEl || !viewerEl || !captionEl) return;

  isImageModalOpen = false;
  activeImagePlayback = null;
  clearImageAutoAdvance();
  imageSlideshowSpeedStep = 0;
  updateImageSlideshowSpeedIndicator();
  viewerEl.removeAttribute("src");
  captionEl.textContent = "";
  setImageModalZoom(false);
  modalEl.classList.remove("open");
  modalEl.setAttribute("aria-hidden", "true");
}

function markImageWatchedLocal(imageId) {
  const normalizedId = Number(imageId) || 0;
  if (!normalizedId) return;

  watchedMarkedInSession.add(`image:${normalizedId}`);
  const cardEl = document.querySelector(`.thumb-card-image[data-image-id="${normalizedId}"]`);
  if (!(cardEl instanceof Element)) return;

  cardEl.classList.add("is-watched");
  cardEl.dataset.isWatched = "true";
  const linkEl = cardEl.querySelector(".thumb-media");
  if (linkEl instanceof HTMLAnchorElement) {
    linkEl.dataset.isWatched = "true";
  }
  ensureWatchedBadge(cardEl);
}

async function markImageWatched(imageId) {
  const normalizedId = Number(imageId) || 0;
  if (!normalizedId) return;

  markImageWatchedLocal(normalizedId);
  await api(`/images/${normalizedId}/watched`, {
    method: "PUT",
    body: JSON.stringify({
      is_watched: true,
    }),
  });
}

function openImageModal(imageUrl, caption, imageId, alreadyWatched = false, currentIndex = -1) {
  if (!imageUrl) return;

  const modalEl = document.getElementById("image-modal");
  const viewerEl = document.getElementById("image-modal-viewer");
  const captionEl = document.getElementById("image-modal-caption");
  if (!modalEl || !viewerEl || !captionEl) return;

  const resolvedIndex = Number.isInteger(currentIndex) && currentIndex >= 0
    ? currentIndex
    : findImageIndex(Number(imageId) || 0, imageUrl);

  viewerEl.src = imageUrl;
  captionEl.textContent = caption || "";
  modalEl.classList.add("open");
  modalEl.setAttribute("aria-hidden", "false");
  isImageModalOpen = true;
  activeImagePlayback = {
    imageId: Number(imageId) || 0,
    imageUrl: String(imageUrl || ""),
    currentIndex: resolvedIndex,
  };
  setImageModalZoom(false);
  updateImageSlideshowSpeedIndicator();

  if (imageId) {
    if (alreadyWatched) {
      markImageWatchedLocal(imageId);
    } else {
      markImageWatched(imageId).catch(() => {
        // Keep lightbox usable even if watched persistence fails.
      });
    }
  }
}

function openImageModalFromLink(linkEl) {
  if (!(linkEl instanceof HTMLAnchorElement)) return;
  const imageUrl = String(linkEl.dataset.imageUrl || "").trim();
  if (!imageUrl) return;
  const links = getImageLinks();
  const currentIndex = links.indexOf(linkEl);
  openImageModal(
    imageUrl,
    String(linkEl.dataset.caption || "").trim(),
    Number(linkEl.dataset.imageId) || 0,
    (linkEl.dataset.isWatched || "").toLowerCase() === "true",
    currentIndex,
  );
}

async function loadUntilImageIndexAvailable(targetIndex) {
  let links = getImageLinks();
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

    links = getImageLinks();
  }

  return links;
}

async function loadUntilUnwatchedSlideshowImage(fromIndex, step) {
  let links = getImageLinks();
  let targetIndex = findNextSlideshowImageIndex(fromIndex, step, links);
  if (targetIndex >= 0) {
    return { links, index: targetIndex };
  }

  if (step <= 0) {
    return { links, index: -1 };
  }

  let guard = 0;
  while (guard < 40 && (nextCursor || isLoading)) {
    guard += 1;
    if (isLoading) {
      await waitForFeedIdle();
    } else if (nextCursor) {
      await loadMore(false, true);
      await waitForFeedIdle();
    } else {
      break;
    }

    links = getImageLinks();
    targetIndex = findNextSlideshowImageIndex(fromIndex, step, links);
    if (targetIndex >= 0) {
      return { links, index: targetIndex };
    }
  }

  links = getImageLinks();
  return {
    links,
    index: findNextSlideshowImageIndex(fromIndex, step, links),
  };
}

function navigateImageModal(step) {
  if (!isImageModalOpen || !activeImagePlayback) return;

  const links = getImageLinks();
  if (!links.length) return;

  let currentIndex = Number(activeImagePlayback.currentIndex);
  if (!Number.isInteger(currentIndex) || currentIndex < 0 || currentIndex >= links.length) {
    currentIndex = findImageIndex(activeImagePlayback.imageId || 0, activeImagePlayback.imageUrl || "");
  }

  const targetIndex = findNextSlideshowImageIndex(currentIndex, step, links);
  if (targetIndex >= 0) {
    openImageModalFromLink(links[targetIndex]);
    return;
  }

  if (step > 0 && (nextCursor || isLoading)) {
    if (advanceForwardTaskInFlight) return;

    advanceForwardTaskInFlight = true;
    updateStatus("Carregando proximas imagens nao vistas...");

    loadUntilUnwatchedSlideshowImage(currentIndex, step)
      .then(({ links: refreshed, index }) => {
        if (index >= 0 && index < refreshed.length) {
          openImageModalFromLink(refreshed[index]);
          return;
        }
        clearImageAutoAdvance();
        updateStatus("Slideshow: nao ha mais imagens nao vistas nesta lista.");
      })
      .catch(() => {
        updateStatus("Falha ao carregar proximas imagens.", true);
      })
      .finally(() => {
        advanceForwardTaskInFlight = false;
      });
    return;
  }

  if (step > 0) {
    clearImageAutoAdvance();
    updateStatus("Slideshow: nao ha mais imagens nao vistas nesta lista.");
  }
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
  setVideoModalZoom(false);
  modalEl.classList.remove("open");
  modalEl.setAttribute("aria-hidden", "true");

  isModalOpen = false;
  activePlayback = null;
  abortPrewarm();
}

function isVideoModalZoomed() {
  const modalEl = document.getElementById("video-modal");
  return Boolean(modalEl && modalEl.classList.contains("is-zoomed"));
}

function applyVideoNaturalSize(playerEl) {
  if (!(playerEl instanceof HTMLVideoElement)) return;
  const width = Number(playerEl.videoWidth) || 0;
  const height = Number(playerEl.videoHeight) || 0;
  if (width > 0 && height > 0) {
    playerEl.style.width = `${width}px`;
    playerEl.style.height = `${height}px`;
    return;
  }
  playerEl.style.width = "";
  playerEl.style.height = "";
}

function clearVideoNaturalSize(playerEl) {
  if (!(playerEl instanceof HTMLVideoElement)) return;
  playerEl.style.width = "";
  playerEl.style.height = "";
}

function centerZoomedVideoInStage() {
  const stageEl = document.getElementById("video-modal-stage");
  if (!stageEl) return;
  const maxScrollX = Math.max(0, stageEl.scrollWidth - stageEl.clientWidth);
  const maxScrollY = Math.max(0, stageEl.scrollHeight - stageEl.clientHeight);
  clampScroll(stageEl, maxScrollX / 2, maxScrollY / 2);
}

function focusZoomedVideoAtPoint(focusPoint) {
  const stageEl = document.getElementById("video-modal-stage");
  const playerEl = document.getElementById("video-modal-player");
  if (!stageEl || !playerEl || !focusPoint) {
    centerZoomedVideoInStage();
    return;
  }

  const ratioX = Math.min(1, Math.max(0, Number(focusPoint.ratioX) || 0));
  const ratioY = Math.min(1, Math.max(0, Number(focusPoint.ratioY) || 0));
  const targetClientX = Number(focusPoint.clientX);
  const targetClientY = Number(focusPoint.clientY);
  if (!Number.isFinite(targetClientX) || !Number.isFinite(targetClientY)) {
    centerZoomedVideoInStage();
    return;
  }

  const playerRect = playerEl.getBoundingClientRect();
  if (playerRect.width <= 0 || playerRect.height <= 0) {
    centerZoomedVideoInStage();
    return;
  }

  const pointClientX = playerRect.left + ratioX * playerRect.width;
  const pointClientY = playerRect.top + ratioY * playerRect.height;
  clampScroll(
    stageEl,
    stageEl.scrollLeft + (pointClientX - targetClientX),
    stageEl.scrollTop + (pointClientY - targetClientY),
  );
}

function captureVideoZoomFocusPoint(event, playerEl) {
  if (!(event instanceof MouseEvent) || !(playerEl instanceof HTMLElement)) {
    return null;
  }
  const rect = playerEl.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  return {
    ratioX: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
    ratioY: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    clientX: event.clientX,
    clientY: event.clientY,
  };
}

function setVideoModalZoom(zoomed, focusPoint = null) {
  const modalEl = document.getElementById("video-modal");
  const stageEl = document.getElementById("video-modal-stage");
  const playerEl = document.getElementById("video-modal-player");
  if (!modalEl) return;

  modalEl.classList.toggle("is-zoomed", Boolean(zoomed));

  if (playerEl instanceof HTMLVideoElement) {
    if (zoomed) {
      applyVideoNaturalSize(playerEl);
    } else {
      clearVideoNaturalSize(playerEl);
    }
  }

  if (!stageEl) return;

  if (!zoomed) {
    stageEl.scrollTop = 0;
    stageEl.scrollLeft = 0;
    return;
  }

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (focusPoint) {
        focusZoomedVideoAtPoint(focusPoint);
      } else {
        centerZoomedVideoInStage();
      }
    });
  });
}

function toggleVideoModalZoom(focusPoint = null) {
  const modalEl = document.getElementById("video-modal");
  if (!modalEl || !isModalOpen) return;
  const willZoom = !modalEl.classList.contains("is-zoomed");
  setVideoModalZoom(willZoom, willZoom ? focusPoint : null);
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
  setVideoModalZoom(false);
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
  window.location.href = PLAY_MODE ? "/admin/catalog" : "/admin";
}

function leavePlayMode() {
  // Going back keeps the catalog's scroll position and already loaded cards.
  if (CAME_FROM_CATALOG && window.history.length > 1) {
    window.history.back();
    return;
  }
  window.location.href = "/admin/catalog";
}

function closeVideoModalByUser() {
  closeVideoModal();
  if (PLAY_MODE) leavePlayMode();
}

function closeImageModalByUser() {
  closeImageModal();
  if (PLAY_MODE) leavePlayMode();
}

function autoplayFirstItem() {
  if (!PLAY_MODE && !AUTOPLAY_REQUESTED) return;
  const firstLink = IMAGES_MODE ? getImageLinks()[0] : getPlayableLinks()[0];
  if (!firstLink) {
    // Nothing to play: show the regular gallery with its empty/error status.
    document.body.classList.remove("gallery-play-mode");
    return;
  }
  if (IMAGES_MODE) {
    openImageModalFromLink(firstLink);
  } else {
    openModalFromLink(firstLink);
  }
}

// Returns null when the current category is not in the saved catalog order (use the gallery's own
// order instead), or { target } where target is null at either end of the list.
function readCatalogOrderNeighbor(step) {
  let order = null;
  try {
    order = JSON.parse(window.sessionStorage.getItem(CATALOG_ORDER_KEY) || "null");
  } catch (_) {
    order = null;
  }
  if (!order || typeof order !== "object") return null;

  const currentKey = normalizeCategoryKey(CATEGORY);
  for (const list of [order.regular, order.users]) {
    if (!Array.isArray(list)) continue;
    const idx = list.findIndex((entry) => normalizeCategoryKey(entry?.category) === currentKey);
    if (idx < 0) continue;
    const neighbor = list[idx + step];
    const href = safe(neighbor?.href).trim();
    return { target: href ? { name: safe(neighbor.category).trim(), href } : null };
  }
  return null;
}

// Same targets as the "Anterior" / "Proxima galeria" links, including their image-trail back stack.
function readGalleryNavNeighbor(step) {
  const linkEl = document.getElementById(step < 0 ? "gallery-prev-link" : "gallery-next-link");
  if (!(linkEl instanceof HTMLAnchorElement) || linkEl.classList.contains("is-disabled")) return null;
  const imagesTrail = linkEl.dataset.navImagesMode === "1";
  if (step < 0 && imagesTrail) {
    const backUrl = popGalleryNavBackStack();
    return backUrl ? { name: "", href: backUrl } : null;
  }
  const href = safe(linkEl.getAttribute("href")).trim();
  if (!href || href === "#") return null;
  if (step > 0 && imagesTrail) {
    pushGalleryNavBackStack(`${window.location.pathname}${window.location.search}`);
  }
  return { name: safe(linkEl.title).trim(), href };
}

function withPlaybackParams(href) {
  const url = new URL(href, window.location.origin);
  url.searchParams.delete("autoplay");
  if (PLAY_MODE) {
    url.searchParams.set("play", "1");
    if (CAME_FROM_CATALOG) {
      url.searchParams.set("from", "catalog");
    }
  } else {
    url.searchParams.delete("play");
    url.searchParams.set("autoplay", "1");
  }
  return `${url.pathname}${url.search}`;
}

function navigateCategoryByKey(step) {
  if (!IS_CATEGORY_VIEW) {
    showGalleryToast("Navegacao entre categorias indisponivel nesta colecao.");
    return;
  }
  const catalogNeighbor = PLAY_MODE ? readCatalogOrderNeighbor(step) : null;
  const neighbor = catalogNeighbor ? catalogNeighbor.target : readGalleryNavNeighbor(step);
  if (!neighbor) {
    const noun = IS_USER_CATEGORY ? "usuario" : "categoria";
    showGalleryToast(`Nao ha ${noun} ${step < 0 ? "anterior" : "seguinte"}.`);
    return;
  }

  showGalleryToast(neighbor.name ? `Indo para ${neighbor.name}...` : "Voltando...");
  const targetUrl = withPlaybackParams(neighbor.href);
  if (PLAY_MODE) {
    window.location.replace(targetUrl);
  } else {
    window.location.href = targetUrl;
  }
}

function categoryStepFromShortcut(event, code, key) {
  if (event.repeat || event.ctrlKey || event.metaKey || event.altKey) return 0;
  if (code === "NumpadDecimal" || code === "NumpadComma") return 0;
  if (code === "Comma" || key === "," || key === "<") return -1;
  if (code === "Period" || key === "." || key === ">") return 1;
  return 0;
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
    if (PLAY_MODE) {
      goToDashboard();
      return;
    }
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

  if (!isModalOpen && !isImageModalOpen) return;

  if (code === "Escape" || key === "Escape") {
    event.preventDefault();
    if (isImageModalOpen) {
      closeImageModalByUser();
    } else {
      closeVideoModalByUser();
    }
    return;
  }

  if (isImageModalOpen) {
    if (code === "ArrowRight" || key === "ArrowRight") {
      event.preventDefault();
      navigateImageModal(1);
      return;
    }
    if (code === "ArrowLeft" || key === "ArrowLeft") {
      event.preventDefault();
      navigateImageModal(-1);
      return;
    }
    if ((code === "ArrowUp" || key === "ArrowUp") && !event.repeat) {
      event.preventDefault();
      adjustImageSlideshowSpeed(2);
      return;
    }
    if ((code === "ArrowDown" || key === "ArrowDown") && !event.repeat) {
      event.preventDefault();
      adjustImageSlideshowSpeed(0.5);
      return;
    }
    if ((code === "KeyV" || keyLower === "v") && !event.repeat) {
      event.preventDefault();
      const imageId = Number(activeImagePlayback?.imageId || 0) || 0;
      if (!imageId) {
        updateStatus("Falha ao favoritar: imagem atual sem id.", true);
        return;
      }
      markImageFavorite(imageId).catch((error) => {
        const message = error && typeof error.message === "string"
          ? error.message
          : "erro desconhecido";
        updateStatus(`Falha ao favoritar imagem #${imageId}: ${message}`, true);
      });
      return;
    }
    if (isCoverShortcut(event, code, keyLower)) {
      event.preventDefault();
      setCategoryCoverFromActiveMedia();
      return;
    }
    const imageCategoryStep = categoryStepFromShortcut(event, code, key);
    if (imageCategoryStep) {
      event.preventDefault();
      navigateCategoryByKey(imageCategoryStep);
      return;
    }
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

  if (isCoverShortcut(event, code, keyLower)) {
    event.preventDefault();
    setCategoryCoverFromActiveMedia();
    return;
  }

  const categoryStep = categoryStepFromShortcut(event, code, key);
  if (categoryStep) {
    event.preventDefault();
    navigateCategoryByKey(categoryStep);
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
    ? `${loadedCount} ${getMediaItemLabel()} | ${progressText}`
    : `${loadedCount} ${getMediaItemLabel()}`;
}

async function loadMore(reset = false, silentStatus = false) {
  if (isLoading) return;
  if (!CATEGORY && !FAVORITES_MODE && !RECENTS_MODE && !SECRET_MODE && !SAVED_MODE && !RANDOM_IMAGES_MODE) {
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
    imageFeedTotal = null;
    updateMeta();
  }

  try {
    const params = new URLSearchParams();
    params.set("page_size", String(IMAGES_MODE ? IMAGE_PAGE_SIZE : PAGE_SIZE));
    params.set("random_order", SECRET_MODE || RANDOM_IMAGES_MODE ? "true" : "false");
    params.set("order_by", FAVORITES_MODE ? "favorite_desc" : "detected_desc");

    if (RANDOM_IMAGES_MODE) {
      // Global random images gallery: no category filter.
    } else if (SAVED_MODE) {
      params.set("only_reddit_saved", "true");
    } else if (FAVORITES_MODE) {
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

    if (IMAGES_MODE) {
      params.set("include_total", "false");
    }

    const feedPath = IMAGES_MODE ? "/feed/images" : "/feed";
    const payload = await api(`${feedPath}?${params.toString()}`);
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

    gridEl.insertAdjacentHTML(
      "beforeend",
      items.map((item) => (IMAGES_MODE ? renderImageThumbCard(item) : renderThumbCard(item))).join(""),
    );
    loadedCount += items.length;

    for (const item of items) {
      const itemId = Number(item.id) || 0;
      if (itemId <= 0) continue;
      if (IMAGES_MODE) {
        if (item.is_watched) {
          watchedMarkedInSession.add(`image:${itemId}`);
        }
        if (item.is_favorite) {
          favoritedInSession.add(`image:${itemId}`);
        }
        continue;
      }
      if (item.is_watched) {
        watchedMarkedInSession.add(itemId);
      }
      if (item.is_favorite) {
        favoritedInSession.add(itemId);
      }
    }

    nextCursor = payload?.next_cursor || null;
    if (IMAGES_MODE && Number.isFinite(Number(payload?.total))) {
      imageFeedTotal = Math.max(0, Math.floor(Number(payload.total)));
    }
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
  if (IMAGES_MODE) {
    await loadImageGalleryPage(reset);
    return;
  }
  try {
    if (reset) updateStatus("Carregando videos...");
    await loadMore(reset, true);
    updateStatus(getLoadedContextMessage());
  } catch (error) {
    const message = error && typeof error.message === "string"
      ? error.message
      : "Falha ao carregar videos.";
    updateStatus(message, true);
  }
}

function resolveImageFeedTotal() {
  if (Number.isFinite(imageFeedTotal) && imageFeedTotal >= 0) {
    return imageFeedTotal;
  }
  if (IS_CATEGORY_VIEW && categoryMediaCounts.loaded) {
    return Math.max(0, Number(categoryMediaCounts.images) || 0);
  }
  return null;
}

function updateImageGalleryStatus() {
  if (loadedCount === 0 && !nextCursor) {
    updateStatus(getEmptyContextMessage());
    return;
  }
  const total = resolveImageFeedTotal();
  const totalPart = total == null ? "" : ` de ${total}`;
  if (nextCursor) {
    updateStatus(`Carregadas ${loadedCount} imagens${totalPart}. Role para carregar mais.`);
    return;
  }
  if (total != null) {
    updateStatus(`Carregadas ${loadedCount} imagens${totalPart}.`);
    return;
  }
  updateStatus(getLoadedContextMessage());
}

async function loadImageGalleryPage(reset = false) {
  try {
    if (reset) {
      updateStatus("Carregando imagens...");
    }
    await loadMore(reset, true);
    updateImageGalleryStatus();
  } catch (error) {
    const message = error && typeof error.message === "string"
      ? error.message
      : "Falha ao carregar imagens.";
    updateStatus(message, true);
  }
}

function setupImageInfiniteScroll() {
  if (!IMAGES_MODE) return;
  const sentinel = document.getElementById("gallery-infinite-sentinel");
  if (!sentinel) return;

  if (imageScrollObserver) {
    imageScrollObserver.disconnect();
    imageScrollObserver = null;
  }

  imageScrollObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        if (!nextCursor || isLoading || suppressFurtherGalleryUpdates) continue;
        loadImageGalleryPage(false);
      }
    },
    {
      root: null,
      rootMargin: "600px 0px",
      threshold: 0,
    },
  );
  imageScrollObserver.observe(sentinel);
}

function initGalleryNavigationFromCache() {
  if (!IS_CATEGORY_VIEW) return;
  const cachedNav = readGalleryCategoriesCache();
  if (cachedNav) {
    renderGalleryNavigation(cachedNav);
  }
}

function deferGallerySecondaryLoads() {
  const run = () => {
    loadCategoryMediaCounts();
    loadCategoryProgress();
    window.setTimeout(() => {
      loadCurrentRedditDownloadStatus();
    }, 0);
  };
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(run, { timeout: 1200 });
  } else {
    window.setTimeout(run, 0);
  }
}

function init() {
  if (AUTOPLAY_REQUESTED) {
    // Keep a reload or a later back-stack entry from autoplaying again.
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete("autoplay");
    window.history.replaceState(window.history.state, "", `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
  }
  configureGalleryNavClickHandlers();
  configureGalleryDownloadListButton();
  updatePlaybackSpeedIndicator(selectedPlaybackRate);
  const title = document.getElementById("category-title");
  if (title) {
    title.textContent = getPageTitle();
  }
  configureGalleryTools();
  initGalleryNavigationFromCache();
  if (IMAGES_MODE && IS_CATEGORY_VIEW) {
    renderGalleryMediaTabs();
  }

  const grid = document.getElementById("thumb-grid");
  if (grid) {
    grid.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const link = target.closest(".thumb-media");
      if (!link || !(link instanceof HTMLAnchorElement)) return;

      if (IMAGES_MODE) {
        const imageUrl = String(link.dataset.imageUrl || "").trim();
        if (!imageUrl) return;
        event.preventDefault();
        openImageModalFromLink(link);
        return;
      }

      if (!(link.dataset.stream || "").trim()) return;

      event.preventDefault();
      openModalFromLink(link);
    });
  }

  const imageModalCloseBtn = document.getElementById("image-modal-close");
  if (imageModalCloseBtn) {
    imageModalCloseBtn.addEventListener("click", closeImageModalByUser);
  }

  const imageViewerEl = document.getElementById("image-modal-viewer");
  if (imageViewerEl) {
    imageViewerEl.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const modalEl = document.getElementById("image-modal");
      const willZoom = Boolean(modalEl && !modalEl.classList.contains("is-zoomed"));
      toggleImageModalZoom(willZoom ? captureImageZoomFocusPoint(event, imageViewerEl) : null);
    });
  }

  const imageModal = document.getElementById("image-modal");
  if (imageModal) {
    imageModal.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof Element && target.getAttribute("data-close") === "true") {
        closeImageModalByUser();
      }
    });
  }

  const closeBtn = document.getElementById("video-modal-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", closeVideoModalByUser);
  }

  const modal = document.getElementById("video-modal");
  if (modal) {
    modal.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof Element && target.getAttribute("data-close") === "true") {
        closeVideoModalByUser();
      }
    });
  }

  const playerEl = document.getElementById("video-modal-player");
  if (playerEl instanceof HTMLVideoElement) {
    let playbackClickTimer = null;
    playerEl.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (playbackClickTimer != null) {
        clearTimeout(playbackClickTimer);
        playbackClickTimer = null;
        return;
      }
      playbackClickTimer = setTimeout(() => {
        playbackClickTimer = null;
        toggleModalPlayback();
      }, 220);
    });
    playerEl.addEventListener("dblclick", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (playbackClickTimer != null) {
        clearTimeout(playbackClickTimer);
        playbackClickTimer = null;
      }
      const modalEl = document.getElementById("video-modal");
      const willZoom = Boolean(modalEl && !modalEl.classList.contains("is-zoomed"));
      toggleVideoModalZoom(willZoom ? captureVideoZoomFocusPoint(event, playerEl) : null);
    });
    playerEl.addEventListener("loadedmetadata", () => {
      if (!isVideoModalZoomed()) return;
      applyVideoNaturalSize(playerEl);
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          centerZoomedVideoInStage();
        });
      });
    });
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

  setupImageInfiniteScroll();
  loadAllPages(true).then(autoplayFirstItem);
  void refreshGalleryDownloadListMembership();
  deferGallerySecondaryLoads();
}

init();
