'use strict';

const FIELDS = ['apiBaseUrl', 'apiKey', 'userId'];
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get([...FIELDS, 'trackingEnabled']);
  $('apiBaseUrl').value = s.apiBaseUrl || 'http://localhost:8080';
  $('apiKey').value = s.apiKey || '';
  $('userId').value = s.userId || 'ext-' + crypto.randomUUID();
  $('trackingEnabled').checked = Boolean(s.trackingEnabled);
}

$('save').addEventListener('click', async () => {
  const apiBaseUrl = $('apiBaseUrl').value.trim().replace(/\/+$/, '');
  try {
    new URL(apiBaseUrl);
  } catch (_) {
    $('saved').textContent = 'Invalid URL';
    return;
  }
  const userId = $('userId').value.trim().slice(0, 64);
  if (!userId) {
    $('saved').textContent = 'User ID required';
    return;
  }
  await chrome.storage.local.set({
    apiBaseUrl, userId, apiKey: $('apiKey').value, trackingEnabled: $('trackingEnabled').checked,
  });
  $('saved').textContent = 'Saved.';
});

load();
