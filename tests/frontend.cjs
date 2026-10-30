// Runs static/app.js against a minimal fake DOM and a fake /api/usage.
// Checks logic and texts, not the visual layout: look at the page in a browser for that.
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// --- Fake DOM ----------------------------------------------------------------

const elements = new Map();

function element(selector) {
  if (!elements.has(selector)) {
    elements.set(selector, {
      innerHTML: '',
      textContent: '',
      value: selector === '#days' ? '7' : '',
      hidden: false,
      classList: {toggle() {}, add() {}, remove() {}},
      addEventListener(name, handler) { this[name] = handler; },
      setAttribute() {},
    });
  }
  return elements.get(selector);
}

const html = selector => element(selector).innerHTML;
const text = selector => element(selector).textContent;

// --- Fake API ----------------------------------------------------------------

const provider = {
  input: 100, output: 20, cache_read: 50, cache_write: 0, total: 120,
  sessions: 1, records: 2, first: 1, last: 1, limits: null,
  activity: {
    tracked: false, completed_seconds: 0, completed_turns: 0,
    unresolved: 0, open_recent: 0, recent_event: false,
  },
  source: {status: 'ok', files: 1, scanned_at: 1, last_success: 1, skipped_total: 0},
  integration: {status: 'awaiting_statusline'},
};

const report = {
  generated_at: Date.now() / 1000,
  period: {days: 7, timezone: 'Asia/Kamchatka'},
  providers: {codex: structuredClone(provider), claude: structuredClone(provider)},
  history: [{date: '2026-10-30', codex: 120, claude: 120}],
  host: {},
};

const api = {lastUrl: '', offline: false, status: 200};

async function fakeFetch(path) {
  api.lastUrl = path;
  if (api.offline) throw Error('offline');
  return {ok: api.status === 200, status: api.status, json: async () => structuredClone(report)};
}

const context = {
  document: {querySelector: element, querySelectorAll: () => []},
  Intl, Date, Math, Number, String, Set, Error, AbortSignal,
  setInterval() {},
  fetch: fakeFetch,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context);

const refresh = () => vm.runInContext('refresh()', context);
const now = () => Date.now() / 1000;

// --- Checks ------------------------------------------------------------------

const checks = {
  async 'totals and card order'() {
    await new Promise(resolve => setImmediate(resolve));  // the first refresh runs on load
    assert.equal(text('#total'), '240');
    assert.match(html('#agents'), /Запрашиваем их из аккаунта Claude/);
    assert.match(html('#chart-table'), /2026-10-30/);
    const cards = html('#agents');
    assert.ok(cards.indexOf('<h3>Claude Code</h3>') < cards.indexOf('<h3>Codex</h3>'),
              'Claude card must be left of Codex');
  },

  async 'expired and stale quotas'() {
    report.providers.codex.limits = {
      observed_at: 1, source: 'session_log', stale: true,
      primary: {expired: true, remaining_percent: null, window_minutes: 300, resets_at: 1},
    };
    await refresh();
    assert.match(html('#agents'), /Сброс прошёл/);
    assert.match(html('#agents'), /Устаревший снимок/);
    assert.equal(text('#activity'), 'Нет данных');
  },

  async 'Claude task time'() {
    report.providers.claude.activity = {
      tracked: true, completed_seconds: 600, completed_turns: 3,
      unresolved: 1, open_recent: 0, recent_event: true,
    };
    report.activity = {tracked: true, completed_seconds: 600};
    await refresh();
    assert.equal(text('#activity'), '10 мин');
    assert.match(text('#activity-sub'), /Codex нет данных · Claude 10 мин/);
    assert.match(html('#agents'), /10 мин · 3 завершённых задач · без завершения: 1/);
    assert.match(html('#agents'), /Время задач не измеряется/);
  },

  async 'Claude limits and integration states'() {
    report.providers.claude.integration = {status: 'ok'};
    report.providers.claude.limits = {
      observed_at: now(), source: 'oauth_usage', stale: false,
      primary: {expired: false, remaining_percent: 70, used_percent: 30, window_minutes: 300, resets_at: now() + 3600},
      secondary: null,
    };
    await refresh();
    assert.match(html('#agents'), /70% осталось/);
    assert.match(html('#agents'), /Источник: аккаунт Claude/);
    assert.match(html('#source-list'), /Лимиты из аккаунта Claude/);

    const states = [
      [{status: 'auth_expired'}, /Вход Claude истёк/],
      [{status: 'endpoint_changed'}, /Адрес лимитов Claude изменился/],
      [{status: 'ok', endpoint: '/api/oauth/usage_v2'}, /новый адрес найден автоматически: \/api\/oauth\/usage_v2/],
    ];
    for (const [integration, expected] of states) {
      report.providers.claude.integration = integration;
      await refresh();
      assert.match(html('#source-list'), expected);
    }
  },

  async 'period selection'() {
    element('#days').value = '1';
    report.period.days = 1;
    await refresh();
    assert.equal(api.lastUrl, '/api/usage?days=1');
  },

  async 'server cards and empty chart'() {
    const GB = 1024 ** 3;
    report.host = {
      cpu_percent: 12.5, cpu_count: 4, cpu_sample_seconds: 15,
      memory: {used: 2 * GB, total: 8 * GB, percent: 25},
      disk: {free: 50 * GB, total: 100 * GB, percent: 50},
      uptime_seconds: 90000,
    };
    report.history = [{date: '2026-10-30', codex: 0, claude: 0}];
    await refresh();
    assert.match(html('#host'), /12,5%/);
    assert.match(html('#host'), /2 ГБ из 8 ГБ/);
    assert.match(html('#host'), /50 ГБ свободно/);
    assert.match(text('#uptime'), /25 ч 0 мин/);
    assert.match(html('#chart'), /расход не найден/);
  },

  async 'collector failure'() {
    report.collector_error = true;
    await refresh();
    assert.equal(element('#notice').hidden, false);
    assert.match(text('#notice'), /последний сохранённый/);
    assert.equal(text('#updated'), 'Сборщик не обновляет данные');
    delete report.collector_error;
  },

  async 'first collection'() {
    api.status = 503;
    await refresh();
    assert.match(text('#notice'), /первый сбор/);
    api.status = 200;
  },

  async 'network error'() {
    api.offline = true;
    await refresh();
    assert.equal(element('#notice').hidden, false);
    assert.match(text('#notice'), /Нет связи/);
    assert.equal(element('#refresh').disabled, false);
  },
};

(async () => {
  // Checks share the fake report and run in order.
  for (const [name, check] of Object.entries(checks)) {
    try {
      await check();
    } catch (error) {
      console.error(`Frontend check failed: ${name}`);
      throw error;
    }
  }
  console.log(`Frontend: ${Object.keys(checks).length} checks passed (${Object.keys(checks).join(', ')}).`);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
