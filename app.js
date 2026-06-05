const data = window.SMM2_DASHBOARD_DATA;

const number = new Intl.NumberFormat("es-MX");
const dateFormatter = new Intl.DateTimeFormat("es-MX", {
  dateStyle: "long",
  timeZone: data.timezone,
});

function byId(id) {
  return document.getElementById(id);
}

function text(value) {
  return value == null || value === "" ? "Sin dato" : String(value);
}

function metric(value) {
  return number.format(value || 0);
}

function img(url, alt) {
  if (!url) return `<div class="thumb-fallback">sin imagen</div>`;
  return `<img src="${url}" alt="${alt}" loading="lazy" referrerpolicy="no-referrer" />`;
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
      <strong>${course.clearRatePretty}</strong>
      <span>clear rate</span>
    </div>
  `;
}

function card(course, index, mode) {
  const title = text(course.name);
  const creator = text(course.uploaderName);
  return `
    <article class="level-card">
      <div class="rank">${index + 1}</div>
      <div class="thumb">${img(course.thumbnail, title)}</div>
      <div class="level-body">
        <div class="level-top">
          <div class="level-title">
            <h3>${title}</h3>
            <span class="course-id">${course.courseId}</span>
          </div>
          ${scoreMarkup(course, mode)}
        </div>
        <div class="level-meta">
          <span class="pill difficulty">${text(course.difficulty)}</span>
          <span class="pill">${text(course.style)}</span>
          <span class="pill">${text(course.theme)}</span>
          <span class="pill">${creator}</span>
        </div>
        <div class="metrics">
          <span>${metric(course.plays)} plays</span>
          <span>${metric(course.clears)} clears</span>
          <span>${metric(course.attempts)} intentos</span>
          <span>${text(course.uploadTimePretty)} clear-check</span>
        </div>
      </div>
    </article>
  `;
}

function renderList(id, courses, mode) {
  byId(id).innerHTML = courses.map((course, index) => card(course, index, mode)).join("");
}

function render() {
  const localDate = new Date(`${data.date}T12:00:00`);
  byId("dateLabel").textContent = dateFormatter.format(localDate);
  byId("countLabel").textContent = `${metric(data.stats.totalLevels)} niveles`;

  renderList("topLiked", data.topLiked, "likes");
  renderList("leastCleared", data.leastCleared, "clear");
}

render();
