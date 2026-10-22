const API = window.API_BASE || "/api";
const PAGE_SIZE = 24;
const FAVORITE_HEART_MIN = 5;
const SEARCH_DEBOUNCE_MS = 150;
const SORT_STORAGE_KEY = "media_catalog_sort_v1";

let collections = [];
let regularCategories = [];
let userCategories = [];
let visibleRegular = [];
let visibleUsers = [];
let renderedCount = 0;
let scrollObserver = null;
let searchTimer = null;

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

function isUserCategoryName(name) {
  return safe(name).trim().toLowerCase().startsWith("u_");
}

function toCount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function hashString(value) {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function monogram(name) {
  const parts = safe(name).replace(/^u_/i, "").split(/[\s_\-.]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  return `${parts[0].charAt(0)}${parts[1].charAt(0)}`.toUpperCase();
}

function readStoredSort() {
  try {
    return safe(window.localStorage.getItem(SORT_STORAGE_KEY));
  } catch (error) {
    return "";
  }
}

function storeSort(value) {
  try {
    window.localStorage.setItem(SORT_STORAGE_KEY, value);
  } catch (error) {
    // Sort preference is a convenience only.
  }
}

function normalizeCategory(item) {
  const category = safe(item?.category).trim();
  const videoCount = toCount(item?.video_count ?? item?.videoCount);
  const imageCount = toCount(item?.image_count ?? item?.imageCount);
  const imagesOnly = videoCount <= 0 && imageCount > 0;
  return {
    category,
    count: toCount(item?.count),
    videoCount,
    imageCount,
    favoritesCount: toCount(item?.favorites_count ?? item?.favoritesCount),
    watchedCount: toCount(item?.watched_count ?? item?.watchedCount),
    href: `/admin/category?name=${encodeURIComponent(category)}${imagesOnly ? "&images=1" : ""}`,
    coverUrl: safe(item?.cover_url ?? item?.coverUrl).trim(),
    hue: hashString(category.toLowerCase()) % 360,
  };
}

function buildCollections(stats) {
  const favorites = toCount(stats?.videos_favorites);
  const ready = toCount(stats?.videos_ready);
  const watchedNotFavorited = toCount(stats?.videos_watched_not_favorited);
  return [
    {
      category: "Favoritos",
      count: favorites,
      href: "/admin/category?favorites=1&name=Favoritos",
      icon: "fa-heart",
      hue: 356,
      isCollection: true,
    },
    {
      category: "Meus Salvos",
      count: toCount(stats?.videos_reddit_saved) + toCount(stats?.images_reddit_saved),
      href: "/admin/category?saved=1&name=Meus%20Salvos",
      icon: "fa-bookmark",
      hue: 38,
      isCollection: true,
    },
    {
      category: "Nao vistos",
      count: Math.max(0, ready - favorites - watchedNotFavorited),
      href: "/admin/category?secret=1&name=Nao%20vistos",
      icon: "fa-eye-slash",
      hue: 205,
      isCollection: true,
    },
    {
      category: "Recentes",
      count: Math.max(0, ready - favorites),
      href: "/admin/category?recent=1&name=Recentes",
      icon: "fa-clock",
      hue: 150,
      isCollection: true,
    },
  ];
}

// Single extension point for real category covers: return an image URL to replace the placeholder.
function coverUrlFor(item) {
  return item.coverUrl || null;
}

function coverPlaceholder(item) {
  const mark = item.isCollection
    ? `<i class="fa-solid ${escapeHtml(item.icon)}"></i>`
    : escapeHtml(monogram(item.category));
  return `
    <div class="catalog-cover-placeholder" aria-hidden="true">
      <span class="catalog-cover-mark">${mark}</span>
      ${item.isCollection ? "" : `<span class="catalog-cover-soon">Capa em breve</span>`}
    </div>`;
}

function cardDetails(item) {
  if (item.isCollection) return "Colecao";
  const parts = [];
  if (item.videoCount > 0) parts.push(`${item.videoCount} videos`);
  if (item.imageCount > 0) parts.push(`${item.imageCount} imagens`);
  if (item.watchedCount > 0) parts.push(`${item.watchedCount} vistos`);
  return parts.length > 0 ? parts.join(" · ") : `${item.count} itens`;
}

function posterCard(item) {
  const coverUrl = coverUrlFor(item);
  const showHeart = !item.isCollection && item.favoritesCount >= FAVORITE_HEART_MIN;
  const heart = showHeart
    ? ` <i class="fa-solid fa-heart" title="${escapeHtml(`${item.favoritesCount} favoritados`)}"></i>`
    : "";
  const cover = coverUrl
    ? `<img class="catalog-cover-img" src="${escapeHtml(coverUrl)}" alt="" loading="lazy" />`
    : "";
  return `
    <a class="catalog-card${item.isCollection ? " catalog-card-collection" : ""}" href="${escapeHtml(item.href)}" title="${escapeHtml(item.category)}" style="--poster-hue: ${item.hue}">
      <div class="catalog-poster">
        ${coverPlaceholder(item)}
        ${cover}
        <span class="catalog-card-count">${item.count}${heart}</span>
        <div class="catalog-card-info">
          <span class="catalog-card-name">${escapeHtml(item.category)}</span>
          <span class="catalog-card-details">${escapeHtml(cardDetails(item))}</span>
        </div>
      </div>
    </a>`;
}

function setStatus(message, isError = false) {
  const statusEl = document.getElementById("catalog-status");
  if (!statusEl) return;
  statusEl.textContent = safe(message);
  statusEl.hidden = !message;
  statusEl.classList.toggle("is-error", Boolean(isError));
}

function currentQuery() {
  return safe(document.getElementById("catalog-search")?.value).trim().toLowerCase();
}

function currentSorter() {
  const byName = (a, b) => a.category.localeCompare(b.category, "pt-BR", { sensitivity: "base" });
  const sort = safe(document.getElementById("catalog-sort")?.value);
  if (sort === "name") return byName;
  if (sort === "favorites") {
    return (a, b) => b.favoritesCount - a.favoritesCount || b.count - a.count || byName(a, b);
  }
  return (a, b) => b.count - a.count || byName(a, b);
}

function observeSentinel() {
  const sentinel = document.getElementById("catalog-infinite-sentinel");
  if (!sentinel) return;
  if (!scrollObserver) {
    scrollObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          renderNextPage();
        }
      },
      { rootMargin: "600px 0px" },
    );
  }
  // Re-observing delivers a fresh entry, so a tall viewport keeps filling until the sentinel leaves it.
  scrollObserver.unobserve(sentinel);
  scrollObserver.observe(sentinel);
}

function renderNextPage() {
  const total = visibleRegular.length + visibleUsers.length;
  if (renderedCount >= total) return;
  const end = Math.min(total, renderedCount + PAGE_SIZE);
  const regularHtml = [];
  const userHtml = [];
  for (let i = renderedCount; i < end; i += 1) {
    if (i < visibleRegular.length) {
      regularHtml.push(posterCard(visibleRegular[i]));
    } else {
      userHtml.push(posterCard(visibleUsers[i - visibleRegular.length]));
    }
  }
  if (regularHtml.length > 0) {
    document.getElementById("catalog-grid")?.insertAdjacentHTML("beforeend", regularHtml.join(""));
  }
  if (userHtml.length > 0) {
    const usersSection = document.getElementById("catalog-users-section");
    if (usersSection) usersSection.hidden = false;
    document.getElementById("catalog-users-grid")?.insertAdjacentHTML("beforeend", userHtml.join(""));
  }
  renderedCount = end;
  if (renderedCount < total) {
    observeSentinel();
  }
}

function renderCatalog() {
  const query = currentQuery();
  const sorter = currentSorter();
  const matches = (item) => !query || item.category.toLowerCase().includes(query);
  const visibleCollections = collections.filter(matches);
  visibleRegular = regularCategories.filter(matches).sort(sorter);
  visibleUsers = userCategories.filter(matches).sort(sorter);
  renderedCount = 0;

  const collectionsSection = document.getElementById("catalog-collections-section");
  const collectionsGrid = document.getElementById("catalog-collections-grid");
  if (collectionsSection && collectionsGrid) {
    collectionsGrid.innerHTML = visibleCollections.map(posterCard).join("");
    collectionsSection.hidden = visibleCollections.length === 0;
  }
  const categoriesSection = document.getElementById("catalog-categories-section");
  const grid = document.getElementById("catalog-grid");
  if (categoriesSection && grid) {
    grid.innerHTML = "";
    categoriesSection.hidden = visibleRegular.length === 0;
  }
  const usersSection = document.getElementById("catalog-users-section");
  const usersGrid = document.getElementById("catalog-users-grid");
  if (usersSection && usersGrid) {
    usersGrid.innerHTML = "";
    usersSection.hidden = true;
  }

  const metaEl = document.getElementById("catalog-meta");
  if (metaEl) {
    metaEl.textContent = `${visibleRegular.length} categorias · ${visibleUsers.length} usuarios`;
  }
  const nothingFound = visibleCollections.length + visibleRegular.length + visibleUsers.length === 0;
  setStatus(nothingFound ? (query ? "Nenhuma categoria encontrada." : "Sem categorias detectadas.") : "");
  renderNextPage();
}

function bindControls() {
  const searchEl = document.getElementById("catalog-search");
  const sortEl = document.getElementById("catalog-sort");
  if (sortEl) {
    const storedSort = readStoredSort();
    if (Array.from(sortEl.options).some((option) => option.value === storedSort)) {
      sortEl.value = storedSort;
    }
    sortEl.addEventListener("change", () => {
      storeSort(sortEl.value);
      renderCatalog();
    });
  }
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(renderCatalog, SEARCH_DEBOUNCE_MS);
    });
  }
  // A cover that fails to load is removed so the placeholder underneath shows through.
  document.querySelector(".catalog-main")?.addEventListener(
    "error",
    (event) => {
      if (event.target instanceof HTMLImageElement && event.target.classList.contains("catalog-cover-img")) {
        event.target.remove();
      }
    },
    true,
  );
}

async function loadCatalog() {
  setStatus("Carregando...");
  try {
    const [categories, stats] = await Promise.all([
      api("/feed/categories"),
      api("/stats"),
    ]);
    const list = (Array.isArray(categories) ? categories : [])
      .map(normalizeCategory)
      .filter((item) => item.category.length > 0);
    collections = buildCollections(stats);
    regularCategories = list.filter((item) => !isUserCategoryName(item.category));
    userCategories = list.filter((item) => isUserCategoryName(item.category));
    renderCatalog();
  } catch (error) {
    setStatus(`Falha ao carregar catalogo: ${safe(error.message)}`, true);
  }
}

bindControls();
loadCatalog();
