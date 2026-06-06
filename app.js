const data = window.SMM2_DASHBOARD_DATA;

const number = new Intl.NumberFormat("es-MX");
const dateFormatter = new Intl.DateTimeFormat("es-MX", {
  dateStyle: "long",
  timeZone: data.timezone,
});
const coursesById = new Map();
for (const courses of dashboardCourseLists()) {
  for (const course of courses) coursesById.set(String(course.courseId), course);
}
let lastActiveCard = null;

function dashboardCourseLists() {
  const lists = [data.topLiked || [], data.leastCleared || []];
  for (const courses of Object.values(data.leastClearedByDifficulty || {})) {
    lists.push(courses || []);
  }
  return lists;
}

function byId(id) {
  return document.getElementById(id);
}

function text(value) {
  return value == null || value === "" ? "Sin dato" : String(value);
}

function metric(value) {
  return number.format(value || 0);
}

function formatCourseId(value) {
  const raw = text(value).replace(/-/g, "").toUpperCase();
  if (raw.length !== 9) return text(value);
  return `${raw.slice(0, 3)}-${raw.slice(3, 6)}-${raw.slice(6, 9)}`;
}

function escapeHtml(value) {
  return text(value).replace(/[&<>"']/g, (character) => {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character];
  });
}

function img(url, alt) {
  if (!url) return `<div class="thumb-fallback">sin imagen</div>`;
  return `<img src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy" referrerpolicy="no-referrer" />`;
}

function scoreMarkup(course, mode) {
  if (mode === "likes") {
    return `
      <div class="primary-score">
        <strong>${metric(course.likes)}</strong>
        <span>likes</span>
      </div>
    `;
  }

  return `
    <div class="primary-score">
      <strong>${escapeHtml(course.clearRatePretty)}</strong>
      <span>clear rate</span>
    </div>
  `;
}

function card(course, index, mode) {
  const title = text(course.name);
  const creator = text(course.uploaderName);
  const courseId = formatCourseId(course.courseId);
  return `
    <article class="level-card" data-course-id="${escapeHtml(course.courseId)}" tabindex="0" role="button" aria-label="Ver detalles de ${escapeHtml(title)}, ID ${escapeHtml(courseId)}">
      <div class="rank">${index + 1}</div>
      <div class="thumb">${img(course.thumbnail, title)}</div>
      <div class="level-body">
        <div class="level-top">
          <div class="level-title">
            <h3>${escapeHtml(title)}</h3>
            <span class="course-id">${escapeHtml(courseId)}</span>
          </div>
          ${scoreMarkup(course, mode)}
        </div>
        <div class="level-meta">
          <span class="pill difficulty">${escapeHtml(course.difficulty)}</span>
          <span class="pill">${escapeHtml(course.style)}</span>
          <span class="pill">${escapeHtml(course.theme)}</span>
          <span class="pill">${escapeHtml(creator)}</span>
        </div>
        <div class="metrics">
          <span>${metric(course.plays)} plays</span>
          <span>${metric(course.clears)} clears</span>
          <span>${metric(course.attempts)} intentos</span>
          <span>${escapeHtml(course.uploadTimePretty)} clear-check</span>
        </div>
      </div>
    </article>
  `;
}

function emptyState() {
  return `
    <div class="empty-state">
      No hay niveles con suficientes intentos para esta dificultad.
    </div>
  `;
}

function detailStat(label, value) {
  return `
    <div class="detail-stat">
      <span>${label}</span>
      <strong>${value}</strong>
    </div>
  `;
}

function detailMarkup(course) {
  const title = text(course.name);
  const description = text(course.description);
  const uploaded = text(course.uploadedPretty);
  const image = img(course.thumbnail, title);
  const courseId = formatCourseId(course.courseId);

  return `
    <div class="detail-layout">
      <div class="detail-media">${image}</div>
      <div class="detail-copy">
        <p class="kicker">Detalle del nivel</p>
        <h2 id="detailTitle">${escapeHtml(title)}</h2>
        <span class="detail-course-id">${escapeHtml(courseId)}</span>
        <div class="level-meta detail-tags">
          <span class="pill difficulty">${escapeHtml(course.difficulty)}</span>
          <span class="pill">${escapeHtml(course.style)}</span>
          <span class="pill">${escapeHtml(course.theme)}</span>
          <span class="pill">${escapeHtml(course.uploaderName)}</span>
          <span class="pill">${escapeHtml(course.uploaderCountry)}</span>
        </div>
        <div class="detail-grid">
          ${detailStat("Likes", metric(course.likes))}
          ${detailStat("Plays", metric(course.plays))}
          ${detailStat("Clears", metric(course.clears))}
          ${detailStat("Intentos", metric(course.attempts))}
          ${detailStat("Clear rate", escapeHtml(course.clearRatePretty))}
          ${detailStat("Clear-check", escapeHtml(course.uploadTimePretty))}
        </div>
      </div>
    </div>
    <div class="detail-description">
      <span>Descripción</span>
      <p>${escapeHtml(description)}</p>
    </div>
    <div class="detail-footer">
      <span>Publicado: ${escapeHtml(uploaded)}</span>
      <span>Data ID: ${metric(course.dataId)}</span>
    </div>
  `;
}

function closestCard(target) {
  if (!(target instanceof Element)) return null;
  return target.closest(".level-card[data-course-id]");
}

function openDetail(courseId, trigger) {
  const course = coursesById.get(String(courseId));
  const overlay = byId("detailOverlay");
  const content = byId("detailContent");
  if (!course || !overlay || !content) return;

  lastActiveCard = trigger || document.querySelector(`[data-course-id="${courseId}"]`);
  content.innerHTML = detailMarkup(course);
  overlay.hidden = false;
  document.body.classList.add("detail-open");
  byId("detailClose")?.focus();
}

function closeDetail() {
  const overlay = byId("detailOverlay");
  if (!overlay || overlay.hidden) return;

  overlay.hidden = true;
  document.body.classList.remove("detail-open");
  byId("detailContent").innerHTML = "";
  lastActiveCard?.focus();
  lastActiveCard = null;
}

function bindDetailEvents() {
  document.addEventListener("click", (event) => {
    const overlay = byId("detailOverlay");
    if (event.target === overlay) {
      closeDetail();
      return;
    }

    const cardElement = closestCard(event.target);
    if (cardElement) openDetail(cardElement.dataset.courseId, cardElement);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDetail();

    const cardElement = closestCard(event.target);
    if (!cardElement || (event.key !== "Enter" && event.key !== " ")) return;

    event.preventDefault();
    openDetail(cardElement.dataset.courseId, cardElement);
  });

  byId("detailClose")?.addEventListener("click", closeDetail);
}

function renderList(id, courses, mode) {
  byId(id).innerHTML = courses.length
    ? courses.map((course, index) => card(course, index, mode)).join("")
    : emptyState();
}

function difficultyOptions() {
  return [
    { value: "all", label: "Todas" },
    ...(data.difficulties || []).map((difficulty) => ({
      value: difficulty,
      label: difficulty,
    })),
  ];
}

function renderDifficultyFilter() {
  const select = byId("difficultyFilter");
  if (!select) return;

  const currentValue = select.value || "all";
  const options = difficultyOptions();
  select.innerHTML = options
    .map((option) => {
      return `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`;
    })
    .join("");

  const allowedValues = new Set(options.map((option) => option.value));
  select.value = allowedValues.has(currentValue) ? currentValue : "all";
}

function selectedLeastClearedCourses() {
  const selectedDifficulty = byId("difficultyFilter")?.value || "all";
  if (selectedDifficulty === "all") return data.leastCleared || [];
  return data.leastClearedByDifficulty?.[selectedDifficulty] || [];
}

function renderLeastCleared() {
  renderList("leastCleared", selectedLeastClearedCourses(), "clear");
}

function bindDifficultyFilter() {
  byId("difficultyFilter")?.addEventListener("change", renderLeastCleared);
}

function render() {
  const localDate = new Date(`${data.date}T12:00:00`);
  byId("dateLabel").textContent = dateFormatter.format(localDate);
  byId("countLabel").textContent = `${metric(data.stats.totalLevels)} niveles`;

  renderDifficultyFilter();
  renderList("topLiked", data.topLiked, "likes");
  renderLeastCleared();
}

render();
bindDifficultyFilter();
bindDetailEvents();
