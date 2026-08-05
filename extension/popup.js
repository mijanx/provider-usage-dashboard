'use strict';

const statusNode = document.getElementById('status');
const summaryNode = document.getElementById('summary');
const providersNode = document.getElementById('providers');
const setupNode = document.getElementById('setup');
const setupMessageNode = document.getElementById('setupMessage');
let endpoint = DEFAULT_ENDPOINT;

const PROVIDER_NAMES = {
  minimax: 'MiniMax',
  zai: 'Z.ai',
  'openai-codex': 'OpenAI Codex',
  anthropic: 'Claude',
  'kimi-coding': 'Kimi',
  'xai-oauth': 'xAI / Grok'
};

function text(value, fallback = '—') {
  return value === null || value === undefined || value === '' ? fallback : String(value);
}

function percent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}%` : '—';
}

function windowNode(window) {
  const row = document.createElement('div');
  row.className = 'window';

  const heading = document.createElement('div');
  heading.className = 'window-heading';
  const label = document.createElement('span');
  label.textContent = text(window.label).replaceAll('-', ' ');
  const remaining = document.createElement('strong');
  remaining.textContent = window.remaining_text || `${percent(window.percent_remaining)} left`;
  heading.append(label, remaining);

  const used = Math.max(0, Math.min(100, Number(window.percent_used) || 0));
  const meter = document.createElement('div');
  meter.className = 'meter';
  const fill = document.createElement('span');
  fill.style.width = `${used}%`;
  if (used >= 95) fill.className = 'critical';
  else if (used >= 75) fill.className = 'warning';
  meter.append(fill);

  const reset = document.createElement('div');
  reset.className = 'reset';
  reset.textContent = window.reset_text ? `Resets ${window.reset_text}` : '';
  row.append(heading, meter, reset);
  return row;
}

function providerNode(provider) {
  const card = document.createElement('article');
  card.className = `provider provider-${text(provider.status, 'error')}`;

  const heading = document.createElement('div');
  heading.className = 'provider-heading';
  const nameWrap = document.createElement('div');
  const name = document.createElement('h2');
  name.textContent = PROVIDER_NAMES[provider.provider] || provider.provider;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = [provider.plan, provider.email].filter(Boolean).join(' · ');
  nameWrap.append(name, meta);
  const badge = document.createElement('span');
  badge.className = `badge badge-${text(provider.status, 'error')}`;
  badge.textContent = text(provider.status).replaceAll('_', ' ');
  heading.append(nameWrap, badge);
  card.append(heading);

  const windows = (provider.windows || []).filter(item => item && typeof item === 'object');
  if (windows.length) windows.slice(0, 3).forEach(item => card.append(windowNode(item)));
  else {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = provider.error || 'No usage windows returned.';
    card.append(empty);
  }
  if (provider.error && windows.length) {
    const error = document.createElement('p');
    error.className = 'provider-error';
    error.textContent = provider.error;
    card.append(error);
  }
  return card;
}

function render(data) {
  statusNode.textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()}`;
  statusNode.className = 'status';
  summaryNode.hidden = false;
  summaryNode.replaceChildren();
  [['Healthy', data.summary.ok], ['Degraded', data.summary.degraded], ['Errors', data.summary.errors]].forEach(([label, value]) => {
    const item = document.createElement('span');
    item.textContent = `${label} ${value}`;
    summaryNode.append(item);
  });
  providersNode.replaceChildren(...data.providers.map(providerNode));
  setupNode.hidden = true;
}

function showSetup(message, canGrant = true) {
  statusNode.textContent = 'Not connected';
  statusNode.className = 'status status-error';
  summaryNode.hidden = true;
  providersNode.replaceChildren();
  setupMessageNode.textContent = message;
  document.getElementById('grant').hidden = !canGrant;
  setupNode.hidden = false;
}

async function load() {
  statusNode.textContent = 'Loading…';
  statusNode.className = 'status';
  try {
    endpoint = await storedEndpoint();
    if (!(await hasEndpointPermission(endpoint))) {
      showSetup(`Allow access to ${new URL(endpoint).origin} to read usage data.`);
      return;
    }
    render(await fetchUsage(endpoint));
  } catch (error) {
    showSetup(`Could not load ${endpoint}: ${error.message}`, false);
  }
}

document.getElementById('refresh').addEventListener('click', load);
document.getElementById('grant').addEventListener('click', async () => {
  if (await requestEndpointPermission(endpoint)) load();
});
document.getElementById('options').addEventListener('click', () => chrome.runtime.openOptionsPage());
document.getElementById('settings').addEventListener('click', () => chrome.runtime.openOptionsPage());
document.getElementById('dashboard').addEventListener('click', () => chrome.tabs.create({ url: endpoint }));

load();
