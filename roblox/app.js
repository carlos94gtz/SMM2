const data = window.ROBLOX_DASHBOARD_DATA;

const boards = [
  {
    listId: "topActive",
    filterId: "activeFilter",
    dataKey: "topActiveByMaturity",
    mode: "players",
  },
  {
    listId: "topRated",
    filterId: "ratedFilter",
    dataKey: "topRatedByMaturity",
    mode: "rating",
  },
  {
    listId: "topFavorites",
    filterId: "favoritesFilter",
    dataKey: "topFavoritesByMaturity",
    mode: "favorites",
  },
];

const detailOverlay = document.getElementById("detailOverlay");
const closeDetail = document.getElementById("closeDetail");
let courseLookup = new Map();

function fmt(value) {
  return new Intl.NumberFormat("es-MX").format(value || 0);
}

function shortDate(value) {
  if (!value) return "Sin fecha";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Sin fecha";
  return new Intl.DateTimeFormat("es-MX", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}

function generatedDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("es-MX", {
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function score(game, mode) {
  if (mode === "rating") {
    return `<div class="primary-score"><strong>${esc(game.ratingPretty)}</strong><span>rating</span></div>`;
  }
  if (mode === "favorites") {
    return `<div class="primary-score"><strong>${fmt(game.favorites)}</strong><span>favoritos</span></div>`;
  }
  return `<div class="primary-score"><strong>${fmt(game.playerCount)}</strong><span>activos</span></div>`;
}

function gameCard(game, index, mode) {
  const icon = game.icon
    ? `<img src="${esc(game.icon)}" alt="${esc(game.name)}" loading="lazy" referrerpolicy="no-referrer" />`
    : `<div class="icon-fallback">sin icono</div>`;

  return `
    <article class="game-card" data-universe-id="${esc(game.universeId)}" tabindex="0" role="button" aria-label="Ver detalles de ${esc(game.name)}">
      <div class="rank">${index + 1}</div>
      <div class="icon">${icon}</div>
      <div class="game-body">
        <div class="game-top">
          <div class="game-title">
            <h3>${esc(game.name)}</h3>
            <span class="game-id">${esc(game.rootPlaceId)}</span>
          </div>
          ${score(game, mode)}
        </div>
        <div class="pill-row">
          <span class="pill">${esc(game.maturityLabel)}</span>
          <span class="pill genre">${esc(game.genre)}</span>
        </div>
        <div class="metrics">
          <span>${fmt(game.visits)} visitas</span>
          <span>${fmt(game.voteCount)} votos</span>
          <span>${fmt(game.favorites)} favoritos</span>
          <span>${fmt(game.maxPlayers)} max players</span>
        </div>
      </div>
    </article>
  `.trim();
}

function fillFilters() {
  boards.forEach((board) => {
    const filter = document.getElementById(board.filterId);
    filter.innerHTML = data.maturityOptions
      .map((option) => `<option value="${esc(option.id)}">${esc(option.label)}</option>`)
      .join("");
    filter.addEventListener("change", () => renderBoard(board));
    filter.closest(".filter-box").addEventListener("click", () => filter.focus());
  });
}

function renderBoard(board) {
  const list = document.getElementById(board.listId);
  const filter = document.getElementById(board.filterId);
  const selected = filter.value || "all";
  const games = data[board.dataKey][selected] || [];
  list.innerHTML =
    games.length > 0
      ? games.map((game, index) => gameCard(game, index, board.mode)).join("")
      : `<div class="empty-state">No hay experiencias en este filtro.</div>`;

  list.querySelectorAll(".game-card").forEach((card) => {
    card.addEventListener("click", () => openDetail(card.dataset.universeId));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDetail(card.dataset.universeId);
      }
    });
  });
}

function openDetail(universeId) {
  const game = courseLookup.get(String(universeId));
  if (!game) return;

  document.getElementById("detailIcon").src = game.icon || "";
  document.getElementById("detailIcon").alt = game.name;
  document.getElementById("detailCreator").textContent = game.creatorName
    ? `${game.creatorType}: ${game.creatorName}`
    : "Roblox";
  document.getElementById("detailTitle").textContent = game.name;
  document.getElementById("detailDescription").textContent =
    game.description || "Sin descripción pública.";
  document.getElementById("detailPlayers").textContent = fmt(game.playerCount);
  document.getElementById("detailRating").textContent = game.ratingPretty;
  document.getElementById("detailVisits").textContent = fmt(game.visits);
  document.getElementById("detailFavorites").textContent = fmt(game.favorites);
  document.getElementById("detailUniverse").textContent = `Universe ${game.universeId}`;
  document.getElementById("detailMaturity").textContent = game.maturityLabel;
  document.getElementById("detailUpdated").textContent = `Actualizado ${shortDate(game.updated)}`;
  document.getElementById("detailLink").href = game.robloxUrl;

  detailOverlay.classList.add("is-open");
  detailOverlay.setAttribute("aria-hidden", "false");
  closeDetail.focus();
}

function hideDetail() {
  detailOverlay.classList.remove("is-open");
  detailOverlay.setAttribute("aria-hidden", "true");
}

function allGames() {
  const map = new Map();
  ["topActive", "topRated", "topFavorites"].forEach((key) => {
    data[key].forEach((game) => map.set(String(game.universeId), game));
  });
  Object.values(data.topActiveByMaturity).flat().forEach((game) => map.set(String(game.universeId), game));
  Object.values(data.topRatedByMaturity).flat().forEach((game) => map.set(String(game.universeId), game));
  Object.values(data.topFavoritesByMaturity).flat().forEach((game) => map.set(String(game.universeId), game));
  return map;
}

function init() {
  if (!data) return;
  courseLookup = allGames();
  document.getElementById("dateLabel").textContent = generatedDate(data.generatedAt);
  document.getElementById("countLabel").textContent = `${fmt(data.stats.sampleSize)} experiencias`;
  fillFilters();
  boards.forEach(renderBoard);

  closeDetail.addEventListener("click", hideDetail);
  detailOverlay.addEventListener("click", (event) => {
    if (event.target === detailOverlay) hideDetail();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideDetail();
  });
}

init();
