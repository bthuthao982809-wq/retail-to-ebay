const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync('web/index.html', 'utf8');
const source = html.slice(html.indexOf('async function poll()'), html.indexOf('// ---------- xem trước listing'));
const nodes = new Map();
const element = selector => {
  if (!nodes.has(selector)) nodes.set(selector, { style: {}, dataset: {}, hidden: false, disabled: false, textContent: '' });
  return nodes.get(selector);
};
const state = {
  running: false, ai_running: false, progress: [7, 165], logs: [], log_total: 0,
  running_tabs: [], phase: '', busy_other: '',
  results: [{ id: 'r8', status: 'error', error: 'Chưa chạy — chờ xác minh' },
            { id: 'r9', status: 'error', error: 'An earlier product error' }],
  wait: { reason: 'prAna đang chờ xác minh', until: Date.now() / 1000 + 75, active: true },
};
const context = {
  tab: 'prana', logSince: 0, lastSig: '', lastStatus: null,
  api: async () => state, appendLogs() {}, renderResults() {},
  updateAiTitleButton() {}, updateExportState() {},
  $: element, document: { querySelectorAll: () => [] },
};
vm.createContext(context);
vm.runInContext(source, context);

(async () => {
  await context.poll();
  assert.equal(element('#run').disabled, true);
  assert.equal(element('#resumeBtn').disabled, true);
  assert.equal(element('#retryBtn').disabled, true);
  assert.equal(element('#waitBanner').hidden, false);
  assert.match(element('#waitBanner').textContent, /prAna đang chờ xác minh/);
  assert.match(element('#waitBanner').textContent, /1:1[45]/);
  assert.equal(element('#resumeBtn').textContent, '▶ Chạy tiếp 1 link');
  state.wait.active = false;
  await context.poll();
  assert.equal(element('#run').disabled, false);
  assert.equal(element('#resumeBtn').disabled, false);
  assert.equal(element('#retryBtn').disabled, false);
  assert.equal(element('#waitBanner').hidden, true);
  state.running = true;
  state.wait = { reason: 'Xác minh trong cửa sổ prAna', until: 0, active: true };
  await context.poll();
  assert.equal(element('#run').disabled, true);
  assert.equal(element('#stop').disabled, false);
  assert.equal(element('#waitBanner').textContent, state.wait.reason);
  console.log('PASS: waiting countdown, blocked start/retry, pending counts, expiry, responsive Stop');
})().catch(error => { console.error(error); process.exitCode = 1; });
