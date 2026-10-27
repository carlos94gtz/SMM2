const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');

function render(payload) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        value: 'all', innerHTML: '', textContent: '',
        querySelectorAll: () => [], addEventListener() {},
      });
      return elements.get(id);
    },
    querySelectorAll: () => [], addEventListener() {},
  };
  const context = vm.createContext({ window: { SMM2_DASHBOARD_DATA: payload }, document, Intl });
  vm.runInContext(source, context);
  return { elements, context };
}

test('legacy mixed payload uses complete Expert rankings and filters recent levels', () => {
  const expert = { courseId: 'EXPERT123', name: 'Expert course', difficulty: 'Expert' };
  const other = { courseId: 'SUPER1234', name: 'Excluded course', difficulty: 'Super expert' };
  const payload = { date: '2026-10-25', timezone: 'America/Mexico_City', stats: { totalLevels: 999 },
    recentlyPlayed: [other, expert], difficulties: ['Normal', 'Expert', 'Super expert'] };
  for (const board of ['topLiked', 'topLongest', 'leastCleared']) {
    payload[board] = [other];
    payload[`${board}ByDifficulty`] = { Expert: [expert], 'Super expert': [other] };
  }
  const { elements, context } = render(payload);
  for (const board of ['topLiked', 'topLongest', 'leastCleared', 'recentlyPlayed']) {
    assert.match(elements.get(board).innerHTML, /Expert course/);
    assert.doesNotMatch(elements.get(board).innerHTML, /Excluded course/);
  }
  assert.equal(elements.get('countLabel').textContent, 'Solo niveles Expert');
  assert.equal(vm.runInContext('coursesById.has("SUPER1234")', context), false);
  for (const id of ['likedDifficultyFilter', 'longestDifficultyFilter', 'clearedDifficultyFilter']) {
    assert.equal(elements.get(id).value, 'Expert');
    assert.equal(elements.get(id).disabled, true);
    assert.doesNotMatch(elements.get(id).innerHTML, /Todas|Normal|Super/);
  }
});

test('new Expert payload displays its exact daily count, including zero', () => {
  for (const count of [0, 123]) {
    const { elements } = render({ date: '2026-10-25', timezone: 'America/Mexico_City',
      publicationDifficulty: 'Expert', stats: { totalLevels: count } });
    assert.equal(elements.get('countLabel').textContent, `${count} niveles Expert`);
  }
});
