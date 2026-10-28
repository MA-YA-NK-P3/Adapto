'use strict';

const send = (type) => new Promise((resolve) => chrome.runtime.sendMessage({ type }, resolve));
const $ = (id) => document.getElementById(id);

function text(el, value) { el.textContent = value; return el; }

function renderRecommendation(next) {
  const box = $('rec');
  box.replaceChildren();
  const r = next.recommendation || {};
  const g = next.gap || {};
  const title = document.createElement('div');
  title.append(text(document.createElement('strong'), r.problemId ? `${r.type}: ${r.problemId}${r.topic ? ' (' + r.topic + ')' : ''}` : 'No recommendation'));
  box.append(title);
  if (r.reason) box.append(text(document.createElement('div'), r.reason));
  if (g.type && g.type !== 'NONE') {
    box.append(text(document.createElement('div'), `Gap: ${g.type}${g.concept ? ' — ' + g.concept : ''}`));
    const ul = document.createElement('ul');
    (g.evidence || []).slice(0, 3).forEach((e) => ul.append(text(document.createElement('li'), e)));
    box.append(ul);
  }
  if (next.state) {
    box.append(text(document.createElement('div'),
      `Ability ${next.state.ability} ± ${next.state.uncertainty} (${next.state.calibrated ? 'calibrated' : 'calibrating'})`));
  }
  box.hidden = false;
}

async function refresh() {
  const st = await send('kt/status');
  if (!st || !st.ok) { text($('status'), 'Extension error'); return; }
  const s = st.data;
  text($('status'), `${s.trackingEnabled ? 'Tracking ON' : 'Tracking OFF'} · user ${s.userId} · ${s.queued} queued`);
  const next = await send('kt/next-action');
  const keyRejected = Boolean(next && !next.ok && (next.status === 401 || next.status === 403));
  $('auth').hidden = !(s.authError || keyRejected);
  text($('auth'), `Backend rejected the API key — events are kept locally (${s.queued} queued). ` +
    'Check the extension options, then use "Send queued events" to retry.');
  if (next && next.ok) {
    $('err').hidden = true;
    renderRecommendation(next.data);
  } else if (keyRejected) {
    $('err').hidden = true;
  } else {
    text($('err'), next && next.status === 404
      ? 'No history yet — solve or submit a problem with tracking enabled.'
      : `Backend unreachable at ${s.apiBaseUrl} (${next ? next.error : 'no response'})`).hidden = false;
  }
}

$('flush').addEventListener('click', async () => { await send('kt/flush'); refresh(); });
$('options').addEventListener('click', () => chrome.runtime.openOptionsPage());
refresh();
