const ISSUE_URL = "https://github.com/carlos94gtz/SMM2/issues/new";
const COURSE_ID_CHARS = "0123456789BCDFGHJKLMNPQRSTVWXY";

const form = document.getElementById("levelSubmitForm");
const courseIdInput = document.getElementById("courseIdInput");
const commentInput = document.getElementById("commentInput");
const formStatus = document.getElementById("formStatus");
const previewCourseId = document.getElementById("previewCourseId");
const previewComment = document.getElementById("previewComment");
const copyLevelButton = document.getElementById("copyLevelButton");

function normalizeCourseId(value) {
  return String(value || "")
    .toUpperCase()
    .replace(/[^0-9A-Z]/g, "")
    .split("")
    .filter((character) => COURSE_ID_CHARS.includes(character))
    .slice(0, 9)
    .join("");
}

function formatCourseId(value) {
  const raw = normalizeCourseId(value);
  const groups = [raw.slice(0, 3), raw.slice(3, 6), raw.slice(6, 9)].filter(Boolean);
  return groups.join("-");
}

function fieldValue(input) {
  return input.value.trim();
}

function currentSubmission() {
  return {
    courseId: formatCourseId(courseIdInput.value),
    rawCourseId: normalizeCourseId(courseIdInput.value),
    comment: fieldValue(commentInput),
  };
}

function issueBody(submission) {
  return [
    "## Nivel",
    `ID: ${submission.courseId}`,
    "",
    "## Comentario",
    submission.comment || "Sin comentario",
  ].join("\n");
}

function issueUrl(submission) {
  const params = new URLSearchParams({
    title: `Nivel SMM2 - ${submission.courseId}`,
    body: issueBody(submission),
  });
  return `${ISSUE_URL}?${params.toString()}`;
}

function setStatus(message, type = "") {
  formStatus.textContent = message;
  formStatus.dataset.type = type;
}

function updatePreview() {
  const submission = currentSubmission();
  previewCourseId.textContent = submission.courseId || "XXX-XXX-XXX";
  previewComment.textContent = submission.comment || "Sin comentario";
}

function validateSubmission(submission) {
  if (submission.rawCourseId.length !== 9) {
    setStatus("El ID debe tener 9 caracteres.", "error");
    courseIdInput.focus();
    return false;
  }
  setStatus("");
  return true;
}

courseIdInput.addEventListener("input", () => {
  courseIdInput.value = formatCourseId(courseIdInput.value);
  updatePreview();
});

commentInput.addEventListener("input", updatePreview);

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const submission = currentSubmission();
  if (!validateSubmission(submission)) return;
  window.location.href = issueUrl(submission);
});

copyLevelButton.addEventListener("click", async () => {
  const submission = currentSubmission();
  if (!validateSubmission(submission)) return;

  try {
    await navigator.clipboard.writeText(issueBody(submission));
    setStatus("Mensaje copiado.", "success");
  } catch {
    setStatus("No se pudo copiar el mensaje.", "error");
  }
});

updatePreview();
