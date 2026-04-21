let favorites = JSON.parse(localStorage.getItem("flixoraFavorites")) || [];
let homePayload = null;
let categoryPayload = null;
let detailPayload = null;
let heroIntervalId = null;
let activeSearchToken = 0;
const PAGE_MOVIE_LIMIT = 100;
const FLIXORA_CONFIG = window.FLIXORA_CONFIG || {};
const API_BASE = resolveApiBase();
const VIEW_TRACK_TTL_MS = 12 * 60 * 60 * 1000;

const categoryDescriptions = {
  Trending: "Fresh picks pulled from the latest Moviebox homepage feed.",
  Action: "High-energy stories with momentum, spectacle, and sharp conflict.",
  Comedy: "Lighter, funnier picks drawn from the live catalog.",
  Drama: "Character-heavy titles with tension, emotion, and depth.",
};

const sitePages = [
  { page: "about", href: "about.html", label: "About Us" },
  { page: "contact", href: "contact.html", label: "Contact Us" },
  { page: "privacy", href: "privacy.html", label: "Privacy Policy" },
  { page: "terms", href: "terms.html", label: "Terms & Conditions" },
];

function normalizeBaseUrl(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function isAbsoluteUrl(value) {
  return /^https?:\/\//i.test(String(value || ""));
}

function resolveApiBase() {
  const configuredBase = normalizeBaseUrl(FLIXORA_CONFIG.apiBase);
  const hostname = window.location.hostname;
  const isLocalHost = hostname === "localhost" || hostname === "127.0.0.1";

  if (isLocalHost && !FLIXORA_CONFIG.preferConfiguredApiInLocalDev) {
    return window.location.origin;
  }

  return configuredBase || window.location.origin;
}

function toApiUrl(path) {
  if (!path) {
    return API_BASE;
  }

  if (isAbsoluteUrl(path)) {
    return path;
  }

  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

function normalizeBackendAssetUrl(url) {
  if (!url || isAbsoluteUrl(url) || !String(url).startsWith("/api/")) {
    return url;
  }

  return toApiUrl(url);
}

function debounce(fn, delay) {
  let timeoutId;
  return (...args) => {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => fn(...args), delay);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function fetchJson(path) {
  const requestUrl = toApiUrl(path);
  const response = await fetch(requestUrl, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`Request failed for ${requestUrl} with status ${response.status}`);
  }
  return response.json();
}

function formatViewCount(count) {
  const numericCount = Math.max(0, Number(count) || 0);
  const compact = new Intl.NumberFormat("en", {
    notation: numericCount >= 1000 ? "compact" : "standard",
    maximumFractionDigits: numericCount >= 1000 ? 1 : 0,
  }).format(numericCount);
  return `${compact} view${numericCount === 1 ? "" : "s"}`;
}

function buildMovieMetaText(movie, { includeRating = false, includeType = true } = {}) {
  const parts = [
    movie?.category || "Trending",
    movie?.year || "Unknown",
  ];

  if (includeRating) {
    parts.push(`Rating ${movie?.rating || "N/A"}`);
  }

  if (includeType) {
    parts.push(movie?.mediaType === "series" ? "Series" : "Movie");
  }

  parts.push(formatViewCount(movie?.viewCount));
  return parts.join(" | ");
}

function updateDetailMetaText(movie) {
  const rating = document.getElementById("movieRating");
  if (!rating || !movie) {
    return;
  }

  rating.textContent = buildMovieMetaText(movie, { includeRating: true, includeType: true });
}

function getMovieViewStorageKey(movieId) {
  return `flixoraMovieView:${movieId}`;
}

function shouldTrackMovieView(movieId) {
  if (!movieId) {
    return false;
  }

  try {
    const lastTrackedAt = Number(localStorage.getItem(getMovieViewStorageKey(movieId)) || "0");
    return !lastTrackedAt || (Date.now() - lastTrackedAt) > VIEW_TRACK_TTL_MS;
  } catch {
    return true;
  }
}

function markMovieViewTracked(movieId) {
  if (!movieId) {
    return;
  }

  try {
    localStorage.setItem(getMovieViewStorageKey(movieId), String(Date.now()));
  } catch {
    // Ignore storage failures and keep the page usable.
  }
}

function updateMovieViewInList(items, movieId, viewCount) {
  if (!Array.isArray(items)) {
    return;
  }

  items.forEach((item) => {
    if (item?.id === movieId) {
      item.viewCount = viewCount;
    }
  });
}

function syncMovieViewCaches(movieId, viewCount) {
  if (!movieId) {
    return;
  }

  if (homePayload) {
    updateMovieViewInList(homePayload.hero, movieId, viewCount);
    updateMovieViewInList(homePayload.catalog, movieId, viewCount);
    (homePayload.sections || []).forEach((section) => updateMovieViewInList(section?.movies, movieId, viewCount));
  }

  if (categoryPayload) {
    updateMovieViewInList(categoryPayload.items, movieId, viewCount);
    updateMovieViewInList(categoryPayload.movies, movieId, viewCount);
    updateMovieViewInList(categoryPayload.keepBrowsing, movieId, viewCount);
    if (categoryPayload.featured?.id === movieId) {
      categoryPayload.featured.viewCount = viewCount;
    }
  }

  if (detailPayload?.movie?.id === movieId) {
    detailPayload.movie.viewCount = viewCount;
  }

  if (detailPayload) {
    updateMovieViewInList(detailPayload.related, movieId, viewCount);
    updateMovieViewInList(detailPayload.trending, movieId, viewCount);
  }
}

async function registerMovieView(movieId) {
  if (!movieId || !shouldTrackMovieView(movieId)) {
    return null;
  }

  try {
    const response = await fetch(toApiUrl(`/api/movie-view?id=${encodeURIComponent(movieId)}`), {
      method: "POST",
    });
    if (!response.ok) {
      throw new Error(`View tracking failed with status ${response.status}`);
    }

    const payload = await response.json();
    const viewCount = Number(payload?.viewCount) || 0;
    markMovieViewTracked(movieId);
    syncMovieViewCaches(movieId, viewCount);
    return viewCount;
  } catch (error) {
    console.error("Unable to register movie view", error);
    return null;
  }
}

async function getHomeData(force = false) {
  if (!homePayload || force) {
    const refreshParam = force ? "?refresh=1" : "";
    homePayload = await fetchJson(`/api/home${refreshParam}`);
  }
  return homePayload;
}

function getSearchQueryFromUrl() {
  const urlParams = new URLSearchParams(window.location.search);
  return urlParams.get("q")?.trim() || "";
}

function getUrlCategory() {
  const urlParams = new URLSearchParams(window.location.search);
  return urlParams.get("category") || "Trending";
}

function getMovieIdFromUrl() {
  const urlParams = new URLSearchParams(window.location.search);
  return urlParams.get("id");
}

function getCurrentPageId() {
  return document.body?.dataset.page || "";
}

function buildCategoryHref(categoryName) {
  return `category.html?category=${encodeURIComponent(categoryName)}`;
}

function buildSearchHref(query) {
  return `category.html?q=${encodeURIComponent(query)}`;
}

function isSearchResultsPage() {
  return Boolean(document.querySelector(".page-category") && getSearchQueryFromUrl());
}

function goToMoviePage(movieId) {
  if (!movieId) {
    return;
  }
  window.location.href = `movie.html?id=${encodeURIComponent(movieId)}`;
}

function openExternalLink(url) {
  if (!url) {
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

function bindOpenDetailControl(control, movieId) {
  if (!control || !movieId) {
    return;
  }

  control.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    goToMoviePage(movieId);
  });

  control.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      goToMoviePage(movieId);
    }
  });
}

function getBestExternalLink(movie) {
  return (
    movie?.resourceOptions?.[0]?.qualities?.[0]?.playUrl ||
    movie?.downloadUrl ||
    movie?.trailer ||
    movie?.detailUrl ||
    movie?.resourceOptions?.[0]?.downloadUrl ||
    movie?.resourceOptions?.[0]?.resourceLink ||
    ""
  );
}

function isDirectMediaUrl(url) {
  return Boolean(url) && /(\.mp4|\.m3u8|\.webm|\.mkv)(\?|$)|macdn\.aoneroom\.com|bcdn\.hakunaymatata\.com|\/resource\//i.test(url);
}

function formatPlayerTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "00:00";
  }

  const totalSeconds = Math.floor(seconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;

  if (hours > 0) {
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
  }

  return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function triggerDownload(url, filename = "") {
  if (!url) {
    return;
  }

  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (filename) {
    link.download = filename;
  }
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function buildMediaProxyUrl(url, filename = "", download = false) {
  if (!url) {
    return "";
  }

  const params = new URLSearchParams({ url });
  if (download) {
    params.set("download", "1");
  }
  if (filename) {
    params.set("filename", filename);
  }
  return toApiUrl(`/api/media?${params.toString()}`);
}

function buildSubtitleProxyUrl(url, label = "", language = "") {
  if (!url) {
    return "";
  }

  const params = new URLSearchParams({ url });
  if (label) {
    params.set("label", label);
  }
  if (language) {
    params.set("lang", language);
  }
  return toApiUrl(`/api/subtitle?${params.toString()}`);
}

function buildPlaybackChoices(movie) {
  const choices = [];
  const seen = new Set();

  (movie.resourceOptions || []).forEach((option) => {
    const optionQualities = Array.isArray(option.qualities) && option.qualities.length
      ? option.qualities
      : [{
        label: option.resolutions?.[0] ? `${option.resolutions[0]}P` : "Source",
        resolution: option.resolutions?.[0] || 0,
        playUrl: option.downloadUrl || option.resourceLink || "",
        downloadUrl: option.downloadUrl || option.resourceLink || "",
        sourceUrl: option.resourceLink || option.downloadUrl || "",
        streamType: option.streamType || "file",
        manifestUrl: option.manifestUrl || "",
      }];

    optionQualities.forEach((quality) => {
      const playUrl = quality.playUrl || quality.downloadUrl || quality.sourceUrl || "";
      const streamType = quality.streamType || option.streamType || (playUrl.toLowerCase().includes(".mpd") ? "dash" : "file");
      const manifestUrl = quality.manifestUrl || option.manifestUrl || "";
      const key = `${option.source}::${quality.resolution || 0}::${streamType}::${manifestUrl || playUrl}`;
      if (!playUrl || seen.has(key)) {
        return;
      }

      seen.add(key);
      choices.push({
        source: option.source,
        label: quality.label || (quality.resolution ? `${quality.resolution}P` : "Source"),
        resolution: quality.resolution || 0,
        playUrl,
        downloadUrl: quality.downloadUrl || playUrl,
        sourceUrl: quality.sourceUrl || option.resourceLink || playUrl,
        direct: streamType !== "dash" && isDirectMediaUrl(playUrl),
        streamType,
        manifestUrl: normalizeBackendAssetUrl(manifestUrl),
      });
    });
  });

  if (movie.trailer) {
    const trailerKey = `trailer::${movie.trailer}`;
    if (!seen.has(trailerKey)) {
      choices.push({
        source: "Trailer",
        label: "Trailer",
        resolution: 0,
        playUrl: movie.trailer,
        downloadUrl: movie.trailer,
        sourceUrl: movie.trailer,
        direct: isDirectMediaUrl(movie.trailer),
        streamType: "file",
        manifestUrl: "",
      });
    }
  }

  return choices;
}

function getCategories() {
  return homePayload?.categories || [];
}

function createSidebarLink({ href, label, active = false }) {
  const link = document.createElement("a");
  link.className = `sidebar-link ${active ? "active" : ""}`;
  link.href = href;
  link.textContent = label;
  return link;
}

function createSidebarGroupLabel(label) {
  const groupLabel = document.createElement("p");
  groupLabel.className = "sidebar-group-label";
  groupLabel.textContent = label;
  return groupLabel;
}

function renderSidebarNav(categories = []) {
  const nav = document.querySelector("[data-sidebar-nav]");
  if (!nav) {
    return;
  }

  const page = getCurrentPageId();
  const activeCategory = isSearchResultsPage() ? "" : getUrlCategory();
  const visibleCategories = ["Trending", ...categories.filter((category) => category !== "Trending")];
  nav.innerHTML = "";

  nav.appendChild(createSidebarLink({
    href: "index.html",
    label: "Home",
    active: page === "home",
  }));

  visibleCategories.forEach((categoryName) => {
    const isActiveCategory =
      page === "category" &&
      activeCategory &&
      activeCategory.toLowerCase() === categoryName.toLowerCase();
    nav.appendChild(createSidebarLink({
      href: buildCategoryHref(categoryName),
      label: categoryName,
      active: isActiveCategory,
    }));
  });

  nav.appendChild(createSidebarGroupLabel("About & Support"));

  sitePages.forEach((item) => {
    nav.appendChild(createSidebarLink({
      href: item.href,
      label: item.label,
      active: page === item.page,
    }));
  });
}

function updateFeaturedLinks(homeData) {
  const featuredId = homeData?.hero?.[0]?.id || homeData?.catalog?.[0]?.id;
  if (!featuredId) {
    return;
  }

  document.querySelectorAll('.topbar-actions a[href^="movie.html?id="]').forEach((link) => {
    link.href = `movie.html?id=${encodeURIComponent(featuredId)}`;
  });
}

function toggleFavorite(event, movieId) {
  event.stopPropagation();
  event.preventDefault();

  const heart = event.currentTarget;
  const id = String(movieId);
  const index = favorites.indexOf(id);

  if (index > -1) {
    favorites.splice(index, 1);
    heart.classList.remove("active");
  } else {
    favorites.push(id);
    heart.classList.add("active");
  }

  localStorage.setItem("flixoraFavorites", JSON.stringify(favorites));
}

function renderCards(container, data = [], count = PAGE_MOVIE_LIMIT) {
  if (!container) {
    return;
  }

  const items = data.slice(0, count);
  container.innerHTML = "";

  if (!items.length) {
    container.innerHTML = '<p class="movie-meta">No titles available right now.</p>';
    return;
  }

  items.forEach((movie) => {
    const movieId = String(movie.id);
    const card = document.createElement("article");
    card.className = "movie-card";
    card.dataset.id = movieId;
    card.tabIndex = 0;
    card.setAttribute("role", "link");
    card.setAttribute("aria-label", `Open ${movie.title}`);
    card.innerHTML = `
      <a class="movie-card-link" href="movie.html?id=${encodeURIComponent(movieId)}" aria-label="Open ${escapeHtml(movie.title)}">
        <img src="${escapeHtml(movie.poster || movie.backdrop)}" alt="${escapeHtml(movie.title)}" loading="lazy">
        <div class="card-overlay">
          <div class="play-btn" role="button" tabindex="0" aria-label="Open details for ${escapeHtml(movie.title)}">&#9654;</div>
        </div>
        <h3>${escapeHtml(movie.title)}</h3>
        <p class="movie-meta">${escapeHtml(buildMovieMetaText(movie))}</p>
      </a>
      <button class="heart favorite-button ${favorites.includes(movieId) ? "active" : ""}" type="button" aria-label="Toggle favorite">&#10084;</button>
    `;

    card.addEventListener("click", (event) => {
      if (event.target.closest(".favorite-button")) {
        return;
      }
      goToMoviePage(movieId);
    });

    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        goToMoviePage(movieId);
      }
    });

    card.querySelector(".favorite-button")?.addEventListener("click", (event) => toggleFavorite(event, movieId));
    bindOpenDetailControl(card.querySelector(".play-btn"), movieId);
    container.appendChild(card);
  });
}

function renderSeriesItems(container, movie) {
  if (!container) {
    return;
  }

  const items = movie?.seriesItems || [];
  container.innerHTML = "";

  if (!items.length) {
    return;
  }

  items.forEach((item) => {
    const detailId = String(item.detailId || movie.id || "");
    const card = document.createElement("article");
    card.className = "movie-card";
    card.tabIndex = 0;
    card.setAttribute("role", "link");
    card.setAttribute("aria-label", `Open ${item.title}`);
    card.innerHTML = `
      <a class="movie-card-link" href="movie.html?id=${encodeURIComponent(detailId)}" aria-label="Open ${escapeHtml(item.title)}">
        <img src="${escapeHtml(item.poster || item.backdrop || movie.poster || movie.backdrop)}" alt="${escapeHtml(item.title)}" loading="lazy">
        <div class="card-overlay">
          <div class="play-btn" role="button" tabindex="0" aria-label="Open details for ${escapeHtml(item.title)}">&#9654;</div>
        </div>
        <h3>${escapeHtml(item.episodeCode ? `${item.episodeCode} • ${item.title}` : item.title)}</h3>
        <p class="movie-meta">${escapeHtml(item.meta || movie.category || "Series")}</p>
      </a>
      <button class="watch-btn series-download-btn" type="button">View Details</button>
    `;

    card.addEventListener("click", (event) => {
      if (event.target.closest(".series-download-btn")) {
        return;
      }
      goToMoviePage(detailId);
    });

    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        goToMoviePage(detailId);
      }
    });

    card.querySelector(".series-download-btn")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      goToMoviePage(detailId);
    });

    bindOpenDetailControl(card.querySelector(".play-btn"), detailId);

    container.appendChild(card);
  });
}

function updateSection(sectionElement, sectionData) {
  if (!sectionElement || !sectionData) {
    return;
  }

  sectionElement.querySelector(".section-heading h2").textContent = sectionData.title;
  sectionElement.querySelector(".section-heading span").textContent = sectionData.description;
  renderCards(sectionElement.querySelector(".slider-container"), sectionData.movies || [], PAGE_MOVIE_LIMIT);
}

function pickDisplayGenres(movies = [], preferredGenres = []) {
  const counts = new Map();
  movies.forEach((movie) => {
    (movie.genres || []).forEach((genre) => {
      counts.set(genre, (counts.get(genre) || 0) + 1);
    });
  });

  const rankedGenres = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map((entry) => entry[0]);

  const picked = [];
  preferredGenres.forEach((genre) => {
    if (counts.has(genre) && !picked.includes(genre)) {
      picked.push(genre);
    }
  });

  rankedGenres.forEach((genre) => {
    if (picked.length < 3 && !picked.includes(genre)) {
      picked.push(genre);
    }
  });

  return picked.slice(0, 3);
}

function buildSectionsFromMovies(movies = [], preferredGenres = getCategories()) {
  const trending = [...movies].sort((a, b) => (b.rating || 0) - (a.rating || 0));
  const genres = pickDisplayGenres(movies, preferredGenres);
  const sections = [
    {
      title: "Trending Now",
      description: "",
      movies: trending,
    },
  ];

  genres.forEach((genre) => {
    sections.push({
      title: genre,
      description: "",
      movies: movies.filter((movie) => (movie.genres || []).includes(genre)),
    });
  });

  return sections;
}

function initSlider(slider) {
  if (!slider || slider.dataset.sliderInit === "1") {
    return;
  }

  const container = slider.querySelector(".slider-container");
  const prev = slider.querySelector(".prev");
  const next = slider.querySelector(".next");

  if (!container || !prev || !next) {
    return;
  }

  slider.dataset.sliderInit = "1";
  container.tabIndex = 0;
  container.setAttribute("aria-label", "Scrollable movie list");

  function getScrollAmount() {
    const firstCard = container.querySelector(".movie-card");
    if (firstCard) {
      const styles = window.getComputedStyle(container);
      const gap = Number.parseFloat(styles.columnGap || styles.gap || "0") || 0;
      return firstCard.getBoundingClientRect().width + gap;
    }

    return Math.max(container.clientWidth * 0.85, 280);
  }

  function updateControls() {
    const maxScrollLeft = Math.max(container.scrollWidth - container.clientWidth, 0);
    prev.disabled = container.scrollLeft <= 8;
    next.disabled = container.scrollLeft >= maxScrollLeft - 8;
  }

  next.addEventListener("click", () => {
    container.scrollBy({ left: getScrollAmount(), behavior: "smooth" });
  });

  prev.addEventListener("click", () => {
    container.scrollBy({ left: -getScrollAmount(), behavior: "smooth" });
  });

  let isDown = false;
  let startX = 0;
  let scrollLeft = 0;

  container.addEventListener("wheel", (event) => {
    const mostlyVertical = Math.abs(event.deltaY) > Math.abs(event.deltaX);
    if (!mostlyVertical) {
      return;
    }

    const canScrollHorizontally = container.scrollWidth > container.clientWidth;
    if (!canScrollHorizontally) {
      return;
    }

    event.preventDefault();
    container.scrollBy({
      left: event.deltaY,
      behavior: "auto",
    });
  }, { passive: false });

  container.addEventListener("scroll", updateControls, { passive: true });
  container.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      container.scrollBy({ left: getScrollAmount(), behavior: "smooth" });
    }

    if (event.key === "ArrowLeft") {
      event.preventDefault();
      container.scrollBy({ left: -getScrollAmount(), behavior: "smooth" });
    }
  });
  window.addEventListener("resize", updateControls);
  updateControls();
}

function initPageSliders(scope = document) {
  scope.querySelectorAll(".slider-section").forEach((slider) => initSlider(slider));
}

function renderHomeHero(heroMovies = []) {
  const hero = document.getElementById("hero");
  if (!hero || !heroMovies.length) {
    return;
  }

  const heroTitle = document.getElementById("heroTitle");
  const heroMeta = document.getElementById("heroMeta");
  const heroDesc = document.getElementById("heroDesc");
  const heroPlayButton = document.getElementById("heroPlayButton");
  const dotsContainer = document.getElementById("heroDots");
  const previousButton = document.querySelector(".hero-prev");
  const nextButton = document.querySelector(".hero-next");
  let currentIndex = 0;

  function renderDots() {
    if (!dotsContainer) {
      return;
    }

    dotsContainer.innerHTML = "";
    heroMovies.forEach((_, index) => {
      const dot = document.createElement("button");
      dot.className = `hero-dot ${index === currentIndex ? "active" : ""}`;
      dot.type = "button";
      dot.setAttribute("aria-label", `Go to feature ${index + 1}`);
      dot.addEventListener("click", () => {
        currentIndex = index;
        paintHero();
        restartAutoPlay();
      });
      dotsContainer.appendChild(dot);
    });
  }

  function paintHero() {
    const movie = heroMovies[currentIndex];
    hero.style.backgroundImage = `url(${movie.backdrop || movie.poster})`;
    heroTitle.textContent = movie.title;
    heroMeta.textContent = buildMovieMetaText(movie, { includeRating: true, includeType: false });
    heroDesc.textContent = movie.desc || "No description available yet.";
    heroPlayButton.onclick = () => goToMoviePage(movie.id);
    renderDots();
  }

  function moveHero(step) {
    currentIndex = (currentIndex + step + heroMovies.length) % heroMovies.length;
    paintHero();
  }

  function restartAutoPlay() {
    window.clearInterval(heroIntervalId);
    heroIntervalId = window.setInterval(() => moveHero(1), 5000);
  }

  previousButton?.addEventListener("click", () => {
    moveHero(-1);
    restartAutoPlay();
  });

  nextButton?.addEventListener("click", () => {
    moveHero(1);
    restartAutoPlay();
  });

  paintHero();
  restartAutoPlay();
}

function renderHomeSections(sections = []) {
  const targets = [
    document.getElementById("trending"),
    document.getElementById("action"),
    document.getElementById("comedy"),
    document.getElementById("drama"),
  ];

  targets.forEach((target, index) => {
    const sectionData = sections[index] || sections[0];
    if (target && sectionData) {
      updateSection(target, sectionData);
    }
  });
}

function renderDetailGenreSections(sections = []) {
  const targets = [
    document.getElementById("detailTrending"),
    document.getElementById("detailAction"),
    document.getElementById("detailComedy"),
    document.getElementById("detailDrama"),
  ];

  targets.forEach((target, index) => {
    const sectionData = sections[index] || sections[0];
    if (target && sectionData) {
      updateSection(target, sectionData);
    }
  });
}

function renderCategoryPageData(payload) {
  const featured = payload.featured || payload.movies?.[0] || {};
  const requestedCategory = payload.requestedCategory || "Trending";
  const hero = document.getElementById("categoryHero");
  const title = document.getElementById("categoryTitle");
  const meta = document.getElementById("categoryMeta");
  const desc = document.getElementById("categoryDesc");
  const sectionTitle = document.getElementById("categorySectionTitle");
  const count = document.getElementById("categoryCount");

  if (hero) {
    hero.style.backgroundImage = `url(${featured.backdrop || featured.poster || ""})`;
  }

  if (title) {
    title.textContent = requestedCategory;
  }

  if (meta) {
    meta.textContent = "";
  }

  if (desc) {
    desc.textContent = "";
  }

  if (sectionTitle) {
    sectionTitle.textContent = `${requestedCategory} Titles`;
  }

  if (count) {
    count.textContent = "";
  }

  renderCards(document.querySelector(".category-container"), payload.movies || [], PAGE_MOVIE_LIMIT);

  const keepBrowsingSection = document.getElementById("moreCategories");
  if (keepBrowsingSection) {
    keepBrowsingSection.querySelector(".section-heading h2").textContent = "Keep Browsing";
    keepBrowsingSection.querySelector(".section-heading span").textContent = "";
  }
  renderCards(document.querySelector(".page-category .trending-container"), payload.keepBrowsing || [], PAGE_MOVIE_LIMIT);
}

function renderSearchPageData(payload) {
  const query = payload?.query || getSearchQueryFromUrl();
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const featured = items[0] || homePayload?.hero?.[0] || homePayload?.catalog?.[0] || {};
  const hero = document.getElementById("categoryHero");
  const title = document.getElementById("categoryTitle");
  const meta = document.getElementById("categoryMeta");
  const desc = document.getElementById("categoryDesc");
  const sectionTitle = document.getElementById("categorySectionTitle");
  const count = document.getElementById("categoryCount");
  const eyebrow = hero?.querySelector(".eyebrow");
  const heroPrimaryAction = hero?.querySelector(".watch-btn");
  const heroSecondaryAction = hero?.querySelector(".hero-secondary");
  const keepBrowsing = homePayload?.sections?.[0]?.movies || homePayload?.catalog || [];
  const resultsContainer = document.querySelector(".category-container");

  if (hero) {
    hero.style.backgroundImage = featured?.backdrop || featured?.poster
      ? `url(${featured.backdrop || featured.poster})`
      : "";
  }

  if (eyebrow) {
    eyebrow.textContent = "Search Results";
  }

  if (title) {
    title.textContent = query ? `Results for "${query}"` : "Search Results";
  }

  if (meta) {
    meta.textContent = items.length
      ? `${items.length} match${items.length === 1 ? "" : "es"} found`
      : "No matches found";
  }

  if (desc) {
    desc.textContent = items.length
      ? "Pick a title below to open its details page."
      : query
        ? `No match found for "${query}". Try another title.`
        : "Search for a movie or TV show to see matching results.";
  }

  if (sectionTitle) {
    sectionTitle.textContent = items.length
      ? `Matches for "${query}"`
      : "No Match";
  }

  if (count) {
    count.textContent = items.length
      ? `${items.length} result${items.length === 1 ? "" : "s"}`
      : "";
  }

  if (heroPrimaryAction) {
    heroPrimaryAction.textContent = items.length ? "Explore Matches" : "Back To Home";
    heroPrimaryAction.href = items.length ? "#categoryGrid" : "index.html";
  }

  if (heroSecondaryAction) {
    heroSecondaryAction.textContent = "Back To Home";
    heroSecondaryAction.href = "index.html";
  }

  renderCards(resultsContainer, items, PAGE_MOVIE_LIMIT);
  if (resultsContainer && !items.length) {
    resultsContainer.innerHTML = `
      <article class="movie-card empty-search-state" aria-live="polite">
        <div class="card-overlay"></div>
        <h3>No Match</h3>
        <p class="movie-meta">No result found for ${escapeHtml(query || "that search")}.</p>
      </article>
    `;
  }

  const keepBrowsingSection = document.getElementById("moreCategories");
  if (keepBrowsingSection) {
    keepBrowsingSection.querySelector(".section-heading h2").textContent = items.length ? "Keep Browsing" : "Popular Right Now";
    keepBrowsingSection.querySelector(".section-heading span").textContent = "";
  }

  renderCards(document.querySelector(".page-category .trending-container"), keepBrowsing, PAGE_MOVIE_LIMIT);
}

function renderMovieDetailPayload(payload, homeData) {
  const movie = payload.movie;
  const backdrop = document.getElementById("movieBackdrop");
  const moviePlayer = document.getElementById("moviePlayer");
  const title = document.getElementById("movieTitle");
  const description = document.getElementById("movieDesc");
  const rating = document.getElementById("movieRating");
  const topWatchLink = document.getElementById("topWatchLink");
  const playerWatchButton = document.getElementById("playerWatchButton");
  const playerRewindButton = document.getElementById("playerRewindButton");
  const playerToggleButton = document.getElementById("playerToggleButton");
  const playerForwardButton = document.getElementById("playerForwardButton");
  const playerMuteButton = document.getElementById("playerMuteButton");
  const playerFullscreenButton = document.getElementById("playerFullscreenButton");
  const playerLanguageLabel = document.getElementById("playerLanguageLabel");
  const playerSourceLabel = document.getElementById("playerSourceLabel");
  const playerQualityLabel = document.getElementById("playerQualityLabel");
  const playerCaptionLabel = document.getElementById("playerCaptionLabel");
  const downloadButton = document.getElementById("downloadButton");
  const refreshDetailButton = document.getElementById("refreshDetailButton");
  const prevMovieButton = document.getElementById("prevMovieButton");
  const nextMovieButton = document.getElementById("nextMovieButton");
  const seriesCollection = document.getElementById("seriesCollection");
  const seriesSectionTitle = document.getElementById("seriesSectionTitle");
  const seriesSectionMeta = document.getElementById("seriesSectionMeta");
  const seriesList = document.querySelector(".series-list");
  const resourceSelect = document.querySelector(".resource-select");
  const subtitleSelect = document.querySelector(".subtitle-select");
  const resourceSourceCopy = document.querySelector(".resource-box p");
  const supportButton = document.querySelector(".support-btn");
  const playerProgress = document.querySelector(".player-progress");
  const progressFill = document.querySelector(".progress-fill");
  const playerTime = document.getElementById("playerTime");
  const playbackChoices = buildPlaybackChoices(movie);
  const subtitleChoices = Array.isArray(movie.subtitleOptions) ? movie.subtitleOptions : [];
  let isScrubbing = false;
  let lastCenterTapAt = 0;
  let currentSubtitle = null;
  let currentPlayback = playbackChoices[0] || {
    source: "Moviebox",
    label: "Source",
    resolution: 0,
    playUrl: getBestExternalLink(movie),
    downloadUrl: getBestExternalLink(movie),
    sourceUrl: movie.detailUrl || getBestExternalLink(movie),
    direct: isDirectMediaUrl(getBestExternalLink(movie)),
    streamType: "file",
    manifestUrl: "",
  };

  function destroyDashPlayer() {
    if (!moviePlayer?._dashPlayer) {
      return;
    }

    try {
      moviePlayer._dashPlayer.reset();
    } catch (error) {
      console.error("Unable to reset DASH player", error);
    }
    moviePlayer._dashPlayer = null;
  }

  if (backdrop) {
    backdrop.style.backgroundImage = `url(${movie.backdrop || movie.poster || ""})`;
  }

  if (moviePlayer) {
    destroyDashPlayer();
    moviePlayer.pause();
    moviePlayer.removeAttribute("src");
    delete moviePlayer.dataset.playbackActive;
    moviePlayer.querySelectorAll('track[data-flixora-subtitle="1"]').forEach((track) => track.remove());
    moviePlayer.load();
    moviePlayer.hidden = true;
  }

  if (title) {
    title.textContent = movie.title;
  }

  if (description) {
    description.textContent = movie.desc || "No description available yet.";
  }

  if (rating) {
    rating.textContent = buildMovieMetaText(movie, { includeRating: true, includeType: true });
  }

  function updatePlaybackMeta(choice) {
    if (playerLanguageLabel) {
      playerLanguageLabel.textContent = movie.dubOptions?.[0] || movie.language?.[0] || "Original Audio";
    }

    if (playerSourceLabel) {
      playerSourceLabel.textContent = choice?.source || "Moviebox";
    }

    if (playerQualityLabel) {
      playerQualityLabel.textContent = choice?.resolution ? `${choice.resolution}P` : choice?.label || "Auto";
    }

    if (resourceSourceCopy) {
      resourceSourceCopy.textContent = `Source: ${choice?.source || "Moviebox"}`;
    }

    if (playerCaptionLabel) {
      playerCaptionLabel.textContent = currentSubtitle?.label || "Captions Off";
    }
  }

  function hasLoadedVideo() {
    return Boolean(moviePlayer && !moviePlayer.hidden && (moviePlayer.src || moviePlayer._dashPlayer || moviePlayer.dataset.playbackActive === "1"));
  }

  function togglePlayerPlayback() {
    if (!moviePlayer) {
      return;
    }

    if (!hasLoadedVideo()) {
      playCurrentSelection();
      return;
    }

    if (moviePlayer.paused) {
      moviePlayer.play().catch(() => {
        openExternalLink(currentPlayback.playUrl);
      });
      return;
    }

    moviePlayer.pause();
  }

  function clearSubtitleTracks() {
    if (!moviePlayer) {
      return;
    }

    moviePlayer.querySelectorAll('track[data-flixora-subtitle="1"]').forEach((track) => track.remove());
    Array.from(moviePlayer.textTracks || []).forEach((track) => {
      track.mode = "disabled";
    });
  }

  function applySubtitleChoice(choice) {
    currentSubtitle = choice || null;
    clearSubtitleTracks();

    if (subtitleSelect) {
      subtitleSelect.value = currentSubtitle?.url || "";
    }

    if (playerCaptionLabel) {
      playerCaptionLabel.textContent = currentSubtitle?.label || "Captions Off";
    }

    if (!moviePlayer || !currentSubtitle?.url) {
      return;
    }

    const track = document.createElement("track");
    track.dataset.flixoraSubtitle = "1";
    track.kind = "subtitles";
    track.label = currentSubtitle.label || "Caption";
    track.srclang = currentSubtitle.code || "und";
    track.src = buildSubtitleProxyUrl(
      currentSubtitle.url,
      currentSubtitle.label || "Caption",
      currentSubtitle.code || "und",
    );
    track.default = true;
    track.addEventListener("load", () => {
      Array.from(moviePlayer.textTracks || []).forEach((textTrack) => {
        textTrack.mode = "disabled";
      });
      if (track.track) {
        track.track.mode = "showing";
      }
    });
    moviePlayer.appendChild(track);
  }

  function syncFullscreenButton() {
    if (!playerFullscreenButton) {
      return;
    }

    const fullscreenActive = document.fullscreenElement === backdrop || document.fullscreenElement === backdrop?.closest(".player-panel");
    playerFullscreenButton.innerHTML = fullscreenActive ? "&#11199;" : "&#9974;";
    playerFullscreenButton.setAttribute("aria-label", fullscreenActive ? "Exit fullscreen" : "Toggle fullscreen");
  }

  function seekPlayer(offsetSeconds) {
    if (!moviePlayer) {
      return;
    }

    if (!hasLoadedVideo()) {
      playCurrentSelection();
      return;
    }

    if (!Number.isFinite(moviePlayer.duration) || moviePlayer.duration <= 0) {
      return;
    }

    const nextTime = Math.min(
      Math.max(moviePlayer.currentTime + offsetSeconds, 0),
      moviePlayer.duration,
    );
    moviePlayer.currentTime = nextTime;
  }

  function seekToRatio(ratio) {
    if (!moviePlayer) {
      return;
    }

    if (!hasLoadedVideo()) {
      playCurrentSelection();
      return;
    }

    if (!Number.isFinite(moviePlayer.duration) || moviePlayer.duration <= 0) {
      return;
    }

    const boundedRatio = Math.min(Math.max(ratio, 0), 1);
    moviePlayer.currentTime = boundedRatio * moviePlayer.duration;
  }

  function seekFromClientPosition(clientX) {
    if (!playerProgress) {
      return;
    }

    const rect = playerProgress.getBoundingClientRect();
    const ratio = rect.width ? (clientX - rect.left) / rect.width : 0;
    seekToRatio(ratio);
  }

  function isCenterInteraction(event) {
    if (!backdrop) {
      return false;
    }

    const rect = backdrop.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      return false;
    }

    const withinHorizontalCenter =
      event.clientX >= rect.left + rect.width * 0.3 &&
      event.clientX <= rect.left + rect.width * 0.7;
    const withinVerticalCenter =
      event.clientY >= rect.top + rect.height * 0.25 &&
      event.clientY <= rect.top + rect.height * 0.75;

    return withinHorizontalCenter && withinVerticalCenter;
  }

  async function toggleFullscreen() {
    const fullscreenTarget = backdrop?.closest(".player-panel") || backdrop;
    if (!fullscreenTarget) {
      return;
    }

    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await fullscreenTarget.requestFullscreen();
      }
    } catch (error) {
      console.error("Fullscreen toggle failed", error);
    } finally {
      syncFullscreenButton();
    }
  }

  function playCurrentSelection() {
    if (!currentPlayback?.playUrl) {
      return;
    }

    if (moviePlayer && currentPlayback.streamType === "dash" && currentPlayback.manifestUrl) {
      if (!window.dashjs) {
        window.alert("This browser player needs DASH support for this movie, but the DASH library is not available.");
        return;
      }

      destroyDashPlayer();
      moviePlayer.pause();
      moviePlayer.removeAttribute("src");
      moviePlayer.load();
      moviePlayer.hidden = false;
      moviePlayer.dataset.playbackActive = "1";
      moviePlayer._dashPlayer = window.dashjs.MediaPlayer().create();
      moviePlayer._dashPlayer.initialize(moviePlayer, currentPlayback.manifestUrl, true);
      if (currentSubtitle) {
        window.setTimeout(() => applySubtitleChoice(currentSubtitle), 100);
      }
      return;
    }

    if (moviePlayer && currentPlayback.direct) {
      destroyDashPlayer();
      const proxiedUrl = buildMediaProxyUrl(currentPlayback.playUrl, `${movie.title}.mp4`);
      moviePlayer.hidden = false;
      moviePlayer.dataset.playbackActive = "1";
      if (moviePlayer.src !== proxiedUrl) {
        moviePlayer.src = proxiedUrl;
      }
      moviePlayer.play().catch(() => {
        window.location.href = proxiedUrl;
      });
      return;
    }

    window.alert("A direct full-movie stream is not available for this title right now.");
  }

  function applyPlaybackChoice(choice, shouldAutoplay = false) {
    const previousPlayback = currentPlayback;
    currentPlayback = choice || currentPlayback;
    updatePlaybackMeta(currentPlayback);

    const playbackChanged =
      previousPlayback?.playUrl !== currentPlayback?.playUrl ||
      previousPlayback?.manifestUrl !== currentPlayback?.manifestUrl ||
      previousPlayback?.streamType !== currentPlayback?.streamType;

    if (playbackChanged && moviePlayer) {
      destroyDashPlayer();
      moviePlayer.pause();
      moviePlayer.removeAttribute("src");
      delete moviePlayer.dataset.playbackActive;
      moviePlayer.load();
      moviePlayer.hidden = true;
    }

    if (resourceSelect) {
      const selectedIndex = playbackChoices.findIndex((entry) => entry.playUrl === currentPlayback.playUrl && entry.label === currentPlayback.label);
      if (selectedIndex >= 0) {
        resourceSelect.selectedIndex = selectedIndex;
      }
    }

    if (topWatchLink) {
      topWatchLink.href = currentPlayback.direct
        ? buildMediaProxyUrl(currentPlayback.playUrl, `${movie.title}.mp4`)
        : "#";
      topWatchLink.target = currentPlayback.direct ? "_self" : "_self";
      topWatchLink.rel = "noreferrer";
      topWatchLink.onclick = (event) => {
        event.preventDefault();
        playCurrentSelection();
      };
    }

    if (shouldAutoplay) {
      playCurrentSelection();
    }
  }

  playerWatchButton.onclick = () => {
    playCurrentSelection();
  };

  if (playerToggleButton) {
    playerToggleButton.onclick = () => {
      togglePlayerPlayback();
    };
  }

  if (playerRewindButton) {
    playerRewindButton.onclick = () => {
      seekPlayer(-10);
    };
  }

  if (playerForwardButton) {
    playerForwardButton.onclick = () => {
      seekPlayer(10);
    };
  }

  if (playerMuteButton && moviePlayer) {
    playerMuteButton.onclick = () => {
      moviePlayer.muted = !moviePlayer.muted;
      playerMuteButton.innerHTML = moviePlayer.muted ? "&#128266;" : "&#128263;";
    };
  }

  if (playerFullscreenButton) {
    playerFullscreenButton.onclick = () => {
      toggleFullscreen();
    };
  }

  downloadButton.onclick = () => {
    const targetUrl = currentPlayback.downloadUrl || currentPlayback.playUrl;
    if (currentPlayback.direct && targetUrl) {
      triggerDownload(buildMediaProxyUrl(targetUrl, `${movie.title}.mp4`, true), `${movie.title}.mp4`);
      return;
    }

    window.alert("A direct downloadable movie file is not available for this title right now.");
  };

  refreshDetailButton.onclick = () => {
    goToMoviePage(movie.id);
  };

  prevMovieButton.onclick = () => {
    goToMoviePage(payload.prevId);
  };

  nextMovieButton.onclick = () => {
    goToMoviePage(payload.nextId);
  };

  if (resourceSelect) {
    resourceSelect.innerHTML = "";

    if (playbackChoices.length) {
      playbackChoices.forEach((choice) => {
        const selectOption = document.createElement("option");
        selectOption.value = choice.playUrl || choice.sourceUrl || "";
        selectOption.textContent = `${choice.label} - ${choice.source}`;
        resourceSelect.appendChild(selectOption);
      });

      resourceSelect.onchange = () => {
        const activeChoice = playbackChoices[resourceSelect.selectedIndex] || playbackChoices[0];
        applyPlaybackChoice(activeChoice);
      };
    } else {
      const fallbackOption = document.createElement("option");
      fallbackOption.value = currentPlayback.playUrl;
      fallbackOption.textContent = "Open Source";
      resourceSelect.appendChild(fallbackOption);
      resourceSelect.onchange = null;
    }
  }

  if (subtitleSelect) {
    subtitleSelect.innerHTML = "";

    const offOption = document.createElement("option");
    offOption.value = "";
    offOption.textContent = subtitleChoices.length ? "Captions Off" : "No Captions Available";
    subtitleSelect.appendChild(offOption);

    subtitleChoices.forEach((choice) => {
      const option = document.createElement("option");
      option.value = choice.url || "";
      option.textContent = choice.label || choice.code || "Caption";
      subtitleSelect.appendChild(option);
    });

    subtitleSelect.disabled = subtitleChoices.length === 0;
    subtitleSelect.onchange = () => {
      const activeChoice = subtitleChoices.find((choice) => choice.url === subtitleSelect.value) || null;
      applySubtitleChoice(activeChoice);
    };
  }

  supportButton.onclick = () => {
    const fallbackUrl = currentPlayback.direct
      ? buildMediaProxyUrl(currentPlayback.playUrl, `${movie.title}.mp4`)
      : "";

    if (fallbackUrl) {
      window.location.href = fallbackUrl;
      return;
    }

    if (currentPlayback.streamType === "dash" && currentPlayback.manifestUrl) {
      playCurrentSelection();
      return;
    }

    window.alert("No direct Moviebox media file is available from this title on the site right now.");
  };

  if (moviePlayer && playerTime && progressFill) {
    if (playerProgress) {
      const stopScrubbing = () => {
        isScrubbing = false;
        playerProgress.classList.remove("scrubbing");
      };

      playerProgress.onpointerdown = (event) => {
        isScrubbing = true;
        playerProgress.classList.add("scrubbing");
        if (typeof playerProgress.setPointerCapture === "function") {
          playerProgress.setPointerCapture(event.pointerId);
        }
        seekFromClientPosition(event.clientX);
      };

      playerProgress.onpointermove = (event) => {
        if (!isScrubbing) {
          return;
        }

        seekFromClientPosition(event.clientX);
      };

      playerProgress.onpointerup = (event) => {
        if (!isScrubbing) {
          return;
        }

        seekFromClientPosition(event.clientX);
        stopScrubbing();
      };

      playerProgress.onpointercancel = stopScrubbing;
      playerProgress.onlostpointercapture = stopScrubbing;
    }

    if (backdrop) {
      backdrop.ondblclick = (event) => {
        const targetElement = event.target instanceof Element ? event.target : null;
        if (targetElement?.closest("button")) {
          return;
        }

        if (isCenterInteraction(event)) {
          event.preventDefault();
          togglePlayerPlayback();
        }
      };

      backdrop.onpointerup = (event) => {
        const targetElement = event.target instanceof Element ? event.target : null;
        if (event.pointerType !== "touch" || targetElement?.closest("button")) {
          return;
        }

        if (!isCenterInteraction(event)) {
          lastCenterTapAt = 0;
          return;
        }

        const now = Date.now();
        if (now - lastCenterTapAt <= 320) {
          event.preventDefault();
          togglePlayerPlayback();
          lastCenterTapAt = 0;
          return;
        }

        lastCenterTapAt = now;
      };
    }

    moviePlayer.onloadedmetadata = () => {
      playerTime.textContent = `00:00 / ${formatPlayerTime(moviePlayer.duration)}`;
      progressFill.style.width = "0%";
    };

    moviePlayer.ontimeupdate = () => {
      const current = formatPlayerTime(moviePlayer.currentTime);
      const total = formatPlayerTime(moviePlayer.duration);
      playerTime.textContent = `${current} / ${total}`;
      const progress = moviePlayer.duration ? (moviePlayer.currentTime / moviePlayer.duration) * 100 : 0;
      progressFill.style.width = `${progress}%`;
    };

    moviePlayer.onplay = () => {
      if (playerToggleButton) {
        playerToggleButton.innerHTML = "&#10074;&#10074;";
      }
    };

    moviePlayer.onpause = () => {
      if (playerToggleButton) {
        playerToggleButton.innerHTML = "&#9654;";
      }
    };

    moviePlayer.onended = () => {
      if (playerToggleButton) {
        playerToggleButton.innerHTML = "&#9654;";
      }
    };
  }

  document.onfullscreenchange = () => {
    syncFullscreenButton();
  };

  applyPlaybackChoice(currentPlayback);
  applySubtitleChoice(null);
  syncFullscreenButton();

  if (seriesCollection && seriesList) {
    const hasSeriesItems = movie.mediaType === "series" && Array.isArray(movie.seriesItems) && movie.seriesItems.length > 0;
    seriesCollection.hidden = !hasSeriesItems;

    if (hasSeriesItems) {
      if (seriesSectionTitle) {
        seriesSectionTitle.textContent = `${movie.title} Episodes`;
      }

      if (seriesSectionMeta) {
        seriesSectionMeta.textContent = `${movie.seriesItems.length} episode${movie.seriesItems.length === 1 ? "" : "s"}`;
      }

      renderSeriesItems(seriesList, movie);
    } else {
      seriesList.innerHTML = "";
    }
  }

  renderCards(document.querySelector(".related-movies"), payload.related || [], PAGE_MOVIE_LIMIT);
  renderCards(document.querySelector(".page-detail .trending-container"), payload.trending || [], PAGE_MOVIE_LIMIT);
  renderDetailGenreSections(buildSectionsFromMovies(homeData.catalog || [], homeData.categories || []));
}

function closeModal() {
  document.getElementById("movieModal")?.classList.remove("active");
}

function initAnimations() {
  const elements = document.querySelectorAll(".animate-on-scroll");
  if (!elements.length) {
    return;
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("animate");
      }
    });
  }, { threshold: 0.15 });

  elements.forEach((element) => observer.observe(element));
}

function hideLoader() {
  const loader = document.getElementById("loader");
  if (!loader) {
    return;
  }

  loader.style.opacity = "0";
  window.setTimeout(() => {
    loader.style.display = "none";
  }, 300);
}

function restoreCurrentPage() {
  if (document.querySelector(".page-home") && homePayload) {
    renderHomeHero(homePayload.hero || []);
    renderHomeSections(homePayload.sections || []);
    initPageSliders(document);
    return;
  }

  if (document.querySelector(".page-category") && categoryPayload) {
    if (categoryPayload.mode === "search") {
      renderSearchPageData(categoryPayload);
    } else {
      renderCategoryPageData(categoryPayload);
    }
    initPageSliders(document.querySelector(".page-category"));
    return;
  }

  if (document.querySelector(".page-detail") && detailPayload && homePayload) {
    renderMovieDetailPayload(detailPayload, homePayload);
    initPageSliders(document.querySelector(".page-detail"));
  }
}

function applySearchResults(items, term) {
  categoryPayload = { mode: "search", query: term, items };
  renderSearchPageData(categoryPayload);
  initPageSliders(document.querySelector(".page-category") || document);
}

function initSearch() {
  const searchInput = document.getElementById("searchInput");
  if (!searchInput) {
    return;
  }

  const initialQuery = getSearchQueryFromUrl();
  if (initialQuery) {
    searchInput.value = initialQuery;
  }

  const searchForm = searchInput.closest("form");
  if (!searchForm) {
    return;
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const term = searchInput.value.trim();
    if (!term) {
      return;
    }
    window.location.href = buildSearchHref(term);
  });
}

function initContactForm() {
  const contactForm = document.querySelector("[data-contact-form]");
  if (!contactForm) {
    return;
  }

  const status = document.querySelector("[data-contact-status]");
  const emailTarget = document.querySelector("[data-contact-email]");
  const copyButton = document.querySelector("[data-copy-email]");

  function setStatus(message) {
    if (status) {
      status.textContent = message;
    }
  }

  contactForm.addEventListener("submit", (event) => {
    event.preventDefault();

    const formData = new FormData(contactForm);
    const name = String(formData.get("name") || "").trim();
    const email = String(formData.get("email") || "").trim();
    const subject = String(formData.get("subject") || "").trim() || "FeemX inquiry";
    const message = String(formData.get("message") || "").trim();

    const bodyLines = [
      name ? `Name: ${name}` : "",
      email ? `Email: ${email}` : "",
      "",
      message || "Hello Serge,",
    ].filter(Boolean);

    const mailtoUrl = `mailto:serge.wiseabijuru5@gmail.com?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(bodyLines.join("\n"))}`;
    window.location.href = mailtoUrl;
    setStatus("Your email app should open with a drafted message.");
  });

  copyButton?.addEventListener("click", async () => {
    const email = emailTarget?.textContent?.trim() || "serge.wiseabijuru5@gmail.com";
    try {
      await navigator.clipboard.writeText(email);
      setStatus("Email address copied to your clipboard.");
    } catch (error) {
      console.error("Unable to copy email address", error);
      setStatus("Copy failed. Please use the email address shown above.");
    }
  });
}

async function loadHomePage() {
  const homeData = await getHomeData();
  renderHomeHero(homeData.hero || []);
  renderHomeSections(homeData.sections || []);
}

async function loadCategoryPage() {
  const searchQuery = getSearchQueryFromUrl();
  if (searchQuery) {
    const searchPayload = await fetchJson(`/api/search?q=${encodeURIComponent(searchQuery)}`);
    categoryPayload = {
      mode: "search",
      query: searchQuery,
      items: searchPayload.items || [],
    };
    renderSearchPageData(categoryPayload);
    return;
  }

  categoryPayload = await fetchJson(`/api/category?category=${encodeURIComponent(getUrlCategory())}`);
  categoryPayload.mode = "category";
  renderCategoryPageData(categoryPayload);
}

async function loadMovieDetail() {
  const movieId = getMovieIdFromUrl() || homePayload?.hero?.[0]?.id || homePayload?.catalog?.[0]?.id;
  detailPayload = await fetchJson(`/api/movie?id=${encodeURIComponent(movieId)}`);
  renderMovieDetailPayload(detailPayload, homePayload);
  const trackedViewCount = await registerMovieView(movieId);
  if (trackedViewCount !== null && detailPayload?.movie) {
    detailPayload.movie.viewCount = trackedViewCount;
    updateDetailMetaText(detailPayload.movie);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  initAnimations();
  initSearch();
  initContactForm();

  try {
    homePayload = await getHomeData();
    updateFeaturedLinks(homePayload);
  } catch (error) {
    console.error("Failed to load shared navigation data", error);
  }

  renderSidebarNav(homePayload?.categories || []);

  try {
    if (document.querySelector(".page-home") && homePayload) {
      await loadHomePage();
    }

    if (document.querySelector(".page-category")) {
      await loadCategoryPage();
    }

    if (document.querySelector(".page-detail") && homePayload) {
      await loadMovieDetail();
    }

    initPageSliders(document);
  } catch (error) {
    console.error("Failed to initialize FeemX", error);
  } finally {
    window.setTimeout(hideLoader, 300);
  }

  document.getElementById("modalClose")?.addEventListener("click", closeModal);
  document.getElementById("movieModal")?.addEventListener("click", (event) => {
    if (event.target.id === "movieModal") {
      closeModal();
    }
  });
});
