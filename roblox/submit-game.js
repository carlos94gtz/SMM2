const form = document.getElementById("gameSubmitForm");
const gameInput = document.getElementById("gameInput");
const commentInput = document.getElementById("commentInput");
const issueUrl = "https://github.com/carlos94gtz/SMM2/issues/new";

function normalizeGame(value) {
  const trimmed = value.trim();
  const match = trimmed.match(/roblox\.com\/games\/(\d+)/i) || trimmed.match(/^(\d+)$/);
  return match ? match[1] : trimmed;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const game = normalizeGame(gameInput.value);
  const comment = commentInput.value.trim();
  const title = `Juego Roblox: ${game}`;
  const body = [
    "## Juego Roblox",
    "",
    `URL o ID: ${gameInput.value.trim()}`,
    "",
    "## Comentario",
    "",
    comment,
  ].join("\n");

  const params = new URLSearchParams({ title, body });
  window.location.href = `${issueUrl}?${params.toString()}`;
});
