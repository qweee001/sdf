const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'app', 'dashboard.py'), 'utf8'
);
const match = source.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(match, 'Dashboard inline script must exist');
const script = match[1];

async function scenario(initiallyLoggedIn) {
  const elements = new Map();
  let loggedIn = initiallyLoggedIn;
  let intervals = 0;
  // Inert sink marker: this mock never parses HTML or executes event handlers.
  const payload = '<img src=x onerror="window.__xss=1">';
  const account = {
    id: 'fixture', name: 'fixture',
    persona: JSON.stringify({ name: payload }),
    state: 'stopped', stats: {}, groups: [], setup_complete: false,
  };
  const document = {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, {
          style: {}, value: 'fixture', innerHTML: '', textContent: '',
          listeners: [],
          addEventListener(type, fn) { this.listeners.push({ type, fn }); },
          classList: { add() {}, remove() {} },
        });
      }
      return elements.get(id);
    },
    createElement() {
      return {
        set textContent(s) {
          this.innerHTML = String(s).replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        },
      };
    },
  };
  const context = vm.createContext({
    document,
    setTimeout() {},
    setInterval() { intervals++; },
    // All fetches terminate here; no real HTTP or service client is used.
    fetch: async (requestPath) => {
      assert.ok([
        '/api/login', '/api/status', '/api/live-test/status',
      ].includes(requestPath), `Unexpected mock request: ${requestPath}`);
      if (requestPath === '/api/login') loggedIn = true;
      const status = loggedIn ? 200 : 401;
      return {
        status, ok: status === 200,
        json: async () => requestPath === '/api/status'
          ? { accounts: [account], total: 1, running: 0, features: {}, reply_audit: {} }
          : { ok: true, live_test: { status: 'idle' } },
      };
    },
  });
  vm.runInContext(script, context);
  await new Promise(setImmediate);
  if (!initiallyLoggedIn) {
    await vm.runInContext('doLogin()', context);
    await new Promise(setImmediate);
  }
  return {
    initiallyLoggedIn,
    accountClickHandlers: document.getElementById('accounts').listeners
      .filter(({ type }) => type === 'click').length,
    refreshIntervals: intervals,
    unescapedEventMarkup: document.getElementById('accounts').innerHTML.includes(payload),
  };
}

(async () => {
  const freshLogin = await scenario(false);
  const restoredSession = await scenario(true);
  console.log(JSON.stringify(freshLogin));
  console.log(JSON.stringify(restoredSession));
  // These assertions reproduce current defects, not desired fixed behavior.
  assert.equal(freshLogin.unescapedEventMarkup, true);
  assert.equal(restoredSession.unescapedEventMarkup, true);
  assert.equal(freshLogin.accountClickHandlers, 0);
  assert.equal(freshLogin.refreshIntervals, 0);
  assert.equal(restoredSession.accountClickHandlers, 1);
  assert.equal(restoredSession.refreshIntervals, 1);
  console.log('REPRODUCED: unescaped HTML sink and missing fresh-login initialization');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
