'use strict';

const REFRESH_MS = 15000;
const REQUEST_TIMEOUT_MS = 20000;
// The collector writes every 15 s; older data means it has stopped.
const STALE_AFTER_SECONDS = 90;

let zone = 'Asia/Kamchatka';
let inFlight = false;
let lastData = null;

// --- Labels ------------------------------------------------------------------

const LIMIT_SOURCES = {
  app_server: 'Codex app-server',
  session_log: 'журнал сессии',
  statusline: 'Claude status line',
  oauth_usage: 'аккаунт Claude',
};

const SOURCE_STATES = {
  ok: 'Сбор работает',
  missing: 'Каталог не найден',
  partial: 'Частичные данные',
  error: 'Ошибка чтения',
};

const CODEX_INTEGRATION = {
  ok: 'App-server подключён',
  unavailable: 'App-server недоступен · используем журналы',
  not_configured: 'App-server не настроен',
};

const CLAUDE_INTEGRATION = {
  endpoint_changed: 'Адрес лимитов Claude изменился · ищем новый после обновления Claude Code',
  auth_expired: 'Вход Claude истёк · откройте Claude Code, он обновит вход',
  no_credentials: 'Нет входа в Claude Code',
  unavailable: 'Сервис лимитов Claude недоступен',
  no_limits: 'Status line не передала лимиты',
  awaiting_statusline: 'Ожидаем первый снимок status line',
  invalid_snapshot: 'Ошибка снимка status line',
};

const EMPTY_LIMITS = {
  codex: 'Лимиты пока не получены. Проверяем app-server и доступные журналы.',
  claude: 'Лимиты пока не получены. Запрашиваем их из аккаунта Claude раз в 5 минут.',
};

const NOTICES = {
  initial: 'Идёт первый сбор данных. Страница обновится автоматически.',
  network: 'Нет связи с сервером. Данные на экране могли устареть; повторим запрос автоматически.',
  stale: 'Показан последний сохранённый результат. Свежие данные от сборщика пока не поступили.',
};

// --- Formatting --------------------------------------------------------------

const $ = selector => document.querySelector(selector);

const HTML_ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => HTML_ESCAPES[char]);

const compactFormat = new Intl.NumberFormat('ru-RU', {notation: 'compact', maximumFractionDigits: 1});
const exactFormat = new Intl.NumberFormat('ru-RU');
const fmt = n => compactFormat.format(n || 0);
const exact = n => exactFormat.format(n || 0);

function date(seconds) {
  if (!seconds) return 'нет данных';
  return new Date(seconds * 1000).toLocaleString('ru-RU', {
    timeZone: zone, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function ago(seconds) {
  if (!seconds) return 'Нет данных';
  const age = Math.max(0, Date.now() / 1000 - seconds);
  if (age < 60) return 'Только что';
  if (age < 3600) return `${Math.floor(age / 60)} мин назад`;
  if (age < 86400) return `${Math.floor(age / 3600)} ч назад`;
  return `${Math.floor(age / 86400)} дн назад`;
}

function duration(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)} с`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин`;
  return `${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин`;
}

const percent = n => n == null ? '—' : Number(n).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + '%';
const gb = bytes => (bytes / 1024 ** 3).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + ' ГБ';

// --- AI agents ---------------------------------------------------------------

function windowLabel(window, fallback) {
  const minutes = window?.window_minutes;
  if (!minutes) return fallback;
  if (minutes >= 1440) return `${Math.round(minutes / 1440)} дней`;
  return `${Math.round(minutes / 60 * 10) / 10} ч`;
}

function limitBlock(window, fallbackTitle, stale) {
  const label = windowLabel(window, fallbackTitle);
  const known = window && !window.expired && window.remaining_percent != null;
  const left = known ? window.remaining_percent : 0;

  let value = 'Нет данных';
  if (known) value = `${percent(left)} ${stale ? 'в снимке' : 'осталось'}`;
  else if (window?.expired) value = 'Сброс прошёл · ждём данные';

  const meter = known
    ? `role="meter" aria-label="Осталось в окне ${esc(label)}" aria-valuenow="${left}" aria-valuemin="0" aria-valuemax="100"`
    : '';
  const reset = window?.resets_at ? 'Сброс: ' + date(window.resets_at) : 'Время сброса неизвестно';
  return `
    <div class="limit">
      <div class="limit-line"><span>${esc(label)}</span><b>${value}</b></div>
      <div class="bar ${known ? '' : 'unknown'}" ${meter}><i class="${left < 15 ? 'low' : ''}" style="width:${left}%"></i></div>
      <small>${reset}</small>
    </div>`;
}

function limitsSection(name, limits) {
  const stale = limits?.stale;
  let badge = 'Нет источника';
  if (limits) badge = stale ? 'Устаревший снимок' : 'Снимок получен';

  const body = limits
    ? limitBlock(limits.primary, 'Основное окно', stale) + limitBlock(limits.secondary, 'Дополнительное окно', stale)
    : `<div class="empty-limits note">${EMPTY_LIMITS[name]}</div>`;
  const note = limits
    ? `Источник: ${LIMIT_SOURCES[limits.source] || 'сборщик'} · ${date(limits.observed_at)}`
    : 'Отсутствие данных не означает нулевой остаток.';
  return `
    <div class="limits-title"><span>Лимиты подписки</span><span class="badge ${stale ? 'warning' : ''}">${badge}</span></div>
    ${body}
    <p class="note">${note}</p>`;
}

function taskTimeLine(activity) {
  if (!activity.tracked) return 'Время задач не измеряется';
  let line = `${duration(activity.completed_seconds)} · ${activity.completed_turns} завершённых задач`;
  if (activity.unresolved) line += ` · без завершения: ${activity.unresolved}`;
  return line;
}

function agentStatus(activity) {
  if (activity.open_recent) return 'Задача без завершения';
  if (activity.recent_event) return 'Недавний расход';
  return 'Нет недавних событий';
}

function agentCard(name, p) {
  const codex = name === 'codex';
  const activity = p.activity;
  const active = activity.open_recent || activity.recent_event;
  return `
    <article class="agent ${codex ? '' : 'claude-card'}">
      <div class="agent-head">
        <div class="icon ${codex ? '' : 'cl'}" aria-hidden="true">${codex ? '✳' : '✽'}</div>
        <div><h3>${codex ? 'Codex' : 'Claude Code'}</h3><div class="desc">${p.sessions} сессий за период</div></div>
        <span class="status ${active ? '' : 'off'}">${agentStatus(activity)}</span>
      </div>
      <div class="agent-total"><strong title="${exact(p.total)}">${fmt(p.total)}</strong><span>токенов за период</span></div>
      <dl class="breakdown">
        <div><dt>Вход, всего</dt><dd>${fmt(p.input)}</dd></div>
        <div><dt>Выход</dt><dd>${fmt(p.output)}</dd></div>
        <div><dt>Кэш: чтение / запись</dt><dd>${fmt(p.cache_read)} / ${fmt(p.cache_write)}</dd><small>входит во вход</small></div>
      </dl>
      ${limitsSection(name, p.limits)}
      <div class="activity-line"><span>Время задач</span><b>${esc(taskTimeLine(activity))}</b></div>
    </article>`;
}

// --- Overview ----------------------------------------------------------------

function renderOverview(d) {
  const codex = d.providers.codex;
  const claude = d.providers.claude;
  const total = codex.total + claude.total;
  $('#total').textContent = fmt(total);
  $('#total').title = exact(total);
  $('#sessions').textContent = codex.sessions + claude.sessions;

  const all = d.activity || codex.activity;
  const part = p => p.activity.tracked ? duration(p.activity.completed_seconds) : 'нет данных';
  $('#activity').textContent = all.tracked ? duration(all.completed_seconds) : 'Нет данных';
  $('#activity-sub').textContent = `Codex ${part(codex)} · Claude ${part(claude)} · пересечения один раз`;
  $('#last').textContent = ago(Math.max(codex.last || 0, claude.last || 0));
}

// --- Chart -------------------------------------------------------------------

function renderChart(d) {
  const history = d.history;
  const max = Math.max(1, ...history.flatMap(day => [day.codex, day.claude]));
  const hasData = history.some(day => day.codex || day.claude);
  const height = value => `${value / max * 100}%`;

  const period = d.period.days === 1 ? 'Сегодня' : `Последние ${d.period.days} дней`;
  $('#chart-caption').textContent = `${period} · ${zone}`;
  $('#y-axis').innerHTML = [max, max * 2 / 3, max / 3, 0].map(value => `<span>${fmt(value)}</span>`).join('');

  $('#chart').innerHTML = hasData
    ? history.map(day => `
        <div class="column" title="${esc(day.date)}: Codex ${exact(day.codex)}, Claude ${exact(day.claude)}">
          <i class="codex" style="height:${height(day.codex)}"></i><i class="claude" style="height:${height(day.claude)}"></i>
        </div>`).join('')
    : '<span class="empty-chart">За этот период расход не найден</span>';
  $('#chart').setAttribute('aria-label', `Расход за ${d.period.days} дней. Точные значения доступны в таблице ниже.`);

  // First, middle and last dates as DD.MM.
  const ticks = [...new Set([0, Math.floor((history.length - 1) / 2), history.length - 1])];
  $('#axis').innerHTML = ticks.map(i => {
    const iso = history[i].date;
    return `<span>${esc(iso.slice(8) + '.' + iso.slice(5, 7))}</span>`;
  }).join('');

  $('#chart-table').innerHTML = history.map(day =>
    `<tr><td>${esc(day.date)}</td><td>${exact(day.codex)}</td><td>${exact(day.claude)}</td></tr>`).join('');
}

// --- Server ------------------------------------------------------------------

function hostCard(title, value, sub, load) {
  return `
    <article class="server-card">
      <div class="metric-title">${title}</div>
      <div class="number">${value}</div>
      <div class="bar ${load == null ? 'unknown' : ''}"><i style="width:${load ?? 0}%"></i></div>
      <div class="sub">${esc(sub)}</div>
    </article>`;
}

function renderHost(host) {
  const cpuSub = host.cpu_percent == null
    ? 'Ожидаем второй замер'
    : `${host.cpu_count ?? '—'} CPU · среднее за ${host.cpu_sample_seconds} с`;
  const memory = host.memory;
  const disk = host.disk;
  $('#host').innerHTML =
    hostCard('ПРОЦЕССОР', percent(host.cpu_percent), cpuSub, host.cpu_percent) +
    hostCard('ОПЕРАТИВНАЯ ПАМЯТЬ', memory ? percent(memory.percent) : '—',
             memory ? `${gb(memory.used)} из ${gb(memory.total)}` : 'Нет данных', memory?.percent) +
    hostCard('ДИСК С ДАННЫМИ PULSE', disk ? percent(disk.percent) : '—',
             disk ? `${gb(disk.free)} свободно из ${gb(disk.total)}` : 'Нет данных', disk?.percent);
  $('#uptime').textContent = host.uptime_seconds != null
    ? `Без перезагрузки: ${duration(host.uptime_seconds)}`
    : 'Uptime недоступен';
}

// --- Data sources ------------------------------------------------------------

function integrationLabel(name, p) {
  const status = p.integration.status;
  if (name === 'codex') return CODEX_INTEGRATION[status] || 'Проверяем лимиты';
  if (status === 'ok') {
    if (p.limits?.source === 'statusline') return 'Status line подключена';
    if (p.integration.endpoint) {
      return `Лимиты из аккаунта Claude · новый адрес найден автоматически: ${esc(p.integration.endpoint)}`;
    }
    return 'Лимиты из аккаунта Claude';
  }
  return CLAUDE_INTEGRATION[status] || 'Проверяем лимиты';
}

function sourceRow(name, p) {
  const source = p.source;
  let details = `Проверено: ${date(source.scanned_at)}<br>Успешный сбор: ${date(source.last_success)}`;
  if (source.read_errors) details += `<br>Ошибок чтения: ${source.read_errors}`;
  if (source.skipped_total) details += `<br>Всего пропусков при чтении: ${source.skipped_total}`;
  const title = name === 'codex' ? 'Codex' : 'Claude Code';
  const history = [
    `Файлов: ${source.files} · записей с токенами: ${p.records}`,
    `История с ${date(p.first)}`,
    `Последний расход: ${ago(p.last)}`,
  ].join('<br>');
  const state = SOURCE_STATES[source.status] || 'Неизвестно';
  return `
    <div class="source-row">
      <div class="source-name">${title}<div class="source-detail">${integrationLabel(name, p)}</div></div>
      <div class="source-detail">${history}</div>
      <div class="source-state ${source.status === 'ok' ? '' : 'warn'}">${state}<div class="source-detail">${details}</div></div>
    </div>`;
}

function renderSources(d) {
  $('#source-list').innerHTML = Object.entries(d.providers).map(([name, p]) => sourceRow(name, p)).join('');
}

// --- Page --------------------------------------------------------------------

function render(d) {
  lastData = d;
  zone = d.period.timezone;
  renderOverview(d);
  $('#agents').innerHTML = agentCard('claude', d.providers.claude) + agentCard('codex', d.providers.codex);
  renderChart(d);
  renderHost(d.host);
  renderSources(d);
  $('#footer-time').textContent = `Обновление каждые 15 с · ${zone}`;
  updateFreshness();
}

function showNotice(text) {
  $('#notice').hidden = false;
  $('#notice').textContent = text;
}

function updateFreshness() {
  if (!lastData) return;
  const stale = Date.now() / 1000 - lastData.generated_at > STALE_AFTER_SECONDS || lastData.collector_error;
  const time = new Date(lastData.generated_at * 1000).toLocaleTimeString('ru-RU', {
    timeZone: zone, hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  $('#updated').textContent = stale ? 'Сборщик не обновляет данные' : 'Обновлено ' + time;
  $('#live-dot').classList.toggle('off', !!stale);
  if (stale) showNotice(NOTICES.stale);
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  $('#refresh').disabled = true;
  const days = $('#days').value;
  try {
    const response = await fetch(`/api/usage?days=${days}`, {
      cache: 'no-store', signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) throw new Error(response.status === 503 ? 'initial' : 'network');
    const data = await response.json();
    if (days !== $('#days').value) return;  // the period changed while loading
    $('#notice').hidden = true;
    render(data);
  } catch (error) {
    showNotice(error.message === 'initial' ? NOTICES.initial : NOTICES.network);
    $('#updated').textContent = 'Ожидаем данные';
    $('#live-dot').classList.add('off');
  } finally {
    inFlight = false;
    $('#refresh').disabled = false;
    if (days !== $('#days').value) refresh();
  }
}

$('#refresh').addEventListener('click', refresh);
$('#days').addEventListener('change', refresh);
document.querySelectorAll('nav a').forEach(link => link.addEventListener('click', () => {
  document.querySelectorAll('nav a').forEach(other => other.classList.remove('active'));
  link.classList.add('active');
}));
refresh();
setInterval(refresh, REFRESH_MS);
