'use strict';

const DEFAULT_ENDPOINT = 'http://127.0.0.1:8768';

function normalizeEndpoint(raw) {
  const value = String(raw || '').trim();
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error('Endpoint must use http:// or https://');
  }
  if (url.username || url.password) {
    throw new Error('Credentials are not allowed in the endpoint URL');
  }
  url.hash = '';
  url.search = '';
  url.pathname = url.pathname.replace(/\/+$/, '');
  return url.toString().replace(/\/$/, '');
}

function endpointOriginPattern(endpoint) {
  return `${new URL(endpoint).origin}/*`;
}

function usageUrl(endpoint) {
  return `${endpoint}/api/usage`;
}

function selectUsageWindows(windows, limit = 3) {
  const valid = Array.isArray(windows)
    ? windows.filter(item => item && typeof item === 'object')
    : [];
  const selected = [];

  for (const preferredLabel of ['session', 'weekly']) {
    const preferred = valid.find(item => String(item.label || '').toLowerCase() === preferredLabel);
    if (preferred && !selected.includes(preferred)) selected.push(preferred);
  }
  for (const item of valid) {
    if (selected.length >= limit) break;
    if (!selected.includes(item)) selected.push(item);
  }
  return selected.slice(0, limit);
}

function resetText(window) {
  return window && window.reset_text ? String(window.reset_text) : '';
}

async function storedEndpoint() {
  const stored = await chrome.storage.sync.get({ endpoint: DEFAULT_ENDPOINT });
  return normalizeEndpoint(stored.endpoint);
}

async function hasEndpointPermission(endpoint) {
  return chrome.permissions.contains({ origins: [endpointOriginPattern(endpoint)] });
}

async function requestEndpointPermission(endpoint) {
  return chrome.permissions.request({ origins: [endpointOriginPattern(endpoint)] });
}

async function fetchUsage(endpoint) {
  const response = await fetch(usageUrl(endpoint), {
    method: 'GET',
    headers: { Accept: 'application/json' },
    cache: 'no-store'
  });
  if (!response.ok) throw new Error(`Dashboard returned HTTP ${response.status}`);
  const data = await response.json();
  if (!data || !Array.isArray(data.providers) || !data.summary) {
    throw new Error('Endpoint returned an unexpected payload');
  }
  return data;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { fetchUsage, resetText, selectUsageWindows };
}
