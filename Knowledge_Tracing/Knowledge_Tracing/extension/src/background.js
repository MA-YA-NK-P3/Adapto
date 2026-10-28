/*
 * MV3 service worker: receives observations from the content script, turns them into
 * schema-v1 events, persists them in a durable queue and delivers them to the backend.
 * No modelling happens here.
 */
importScripts('lib/extract.js', 'lib/api.js', 'lib/queue.js');

const DEFAULTS = { apiBaseUrl: 'http://localhost:8080', apiKey: '', trackingEnabled: false };

const chromeStorage = {
  async get(key) { return (await chrome.storage.local.get(key))[key]; },
  async set(key, value) { await chrome.storage.local.set({ [key]: value }); },
};

async function settings() {
  const s = await chrome.storage.local.get(['apiBaseUrl', 'apiKey', 'userId', 'trackingEnabled']);
  if (!s.userId) {
    s.userId = 'ext-' + crypto.randomUUID();
    await chrome.storage.local.set({ userId: s.userId });
  }
  return { ...DEFAULTS, ...Object.fromEntries(Object.entries(s).filter(([, v]) => v !== undefined)) };
}

async function queue() {
  const s = await settings();
  const client = new KTApi.ApiClient({ baseUrl: s.apiBaseUrl, apiKey: s.apiKey });
  return { q: new KTQueue.EventQueue({ storage: chromeStorage, client }), client, s };
}

async function nextAttemptNumber(problemId) {
  const attempts = (await chromeStorage.get('kt.attempts')) || {};
  attempts[problemId] = (attempts[problemId] || 0) + 1;
  await chromeStorage.set('kt.attempts', attempts);
  return attempts[problemId];
}

async function onSubmission(msg) {
  const { q, s } = await queue();
  if (!s.trackingEnabled) return { queued: false, reason: 'tracking disabled (see options)' };
  const event = KTExtract.buildEvent({
    userId: s.userId,
    problemId: msg.problem.problemId,
    eventId: crypto.randomUUID(),
    timestamp: msg.timestamp,
    attemptNumber: await nextAttemptNumber(msg.problem.problemId),
    result: msg.result,
    timeSpentSeconds: msg.timeSpentSeconds,
    hintUsed: msg.hintUsed,
    solutionViewed: msg.solutionViewed,
    sessionId: msg.sessionId,
  });
  const problem = msg.problem.staticDifficulty ? {
    problemKey: msg.problem.problemId,
    title: msg.problem.title,
    url: msg.problem.url,
    staticDifficulty: msg.problem.staticDifficulty,
    concepts: msg.problem.concepts || [],
  } : undefined;
  await q.enqueue({ event, problem });
  return { queued: true, report: await flush(q) };
}

async function flush(q, options) {
  const report = await q.flush(options);
  await chromeStorage.set('kt.lastFlush', { at: new Date().toISOString(), ...report });
  return report;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handlers = {
    'kt/submission': () => onSubmission(msg),
    'kt/flush': async () => flush((await queue()).q, { force: true }),
    'kt/status': async () => {
      const { q, s } = await queue();
      return { userId: s.userId, trackingEnabled: s.trackingEnabled, apiBaseUrl: s.apiBaseUrl,
        queued: await q.size(), lastFlush: await chromeStorage.get('kt.lastFlush'), authError: await q.authError() };
    },
    'kt/next-action': async () => {
      const { client, s } = await queue();
      return (await client.nextAction(s.userId)).body;
    },
  };
  const h = handlers[msg && msg.type];
  if (!h) return false;
  h().then((r) => sendResponse({ ok: true, data: r }))
    .catch((e) => sendResponse({ ok: false, error: e.message, status: e.status }));
  return true; // async response
});

chrome.alarms.create('kt-flush', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'kt-flush') await flush((await queue()).q);
});

// A new API key or base URL may fix a 401/403: lift the pause and try again right away.
chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area !== 'local' || !(changes.apiKey || changes.apiBaseUrl)) return;
  const { q } = await queue();
  if (!(await q.authError())) return;
  await q.clearAuthError();
  await flush(q);
});
