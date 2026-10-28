/*
 * Adapto Background Service Worker (Manifest V3)
 * Bridges the LeetCode Focus Mode with the Knowledge Tracing Brain (KT_python).
 */
importScripts('lib/extract.js', 'lib/api.js', 'lib/queue.js');

const DEFAULTS = {
  apiBaseUrl: 'http://127.0.0.1:8081',
  apiKey: '',
  trackingEnabled: true,
  mode: 'adaptive', // 'adaptive' | 'byos'
};

const chromeStorage = {
  async get(key) { return (await chrome.storage.local.get(key))[key]; },
  async set(key, value) { await chrome.storage.local.set({ [key]: value }); },
};

async function getSettings() {
  const s = await chrome.storage.local.get(['apiBaseUrl', 'apiKey', 'userId', 'trackingEnabled', 'mode']);
  if (!s.userId) {
    s.userId = 'adapto-' + crypto.randomUUID().slice(0, 8);
    await chrome.storage.local.set({ userId: s.userId });
  }
  return { ...DEFAULTS, ...Object.fromEntries(Object.entries(s).filter(([, v]) => v !== undefined)) };
}

async function getQueue() {
  const s = await getSettings();
  const client = new KTApi.ApiClient({ baseUrl: s.apiBaseUrl, apiKey: s.apiKey });
  return { q: new KTQueue.EventQueue({ storage: chromeStorage, client }), client, s };
}

async function nextAttemptNumber(problemId) {
  const attempts = (await chromeStorage.get('kt.attempts')) || {};
  attempts[problemId] = (attempts[problemId] || 0) + 1;
  await chromeStorage.set('kt.attempts', attempts);
  return attempts[problemId];
}

async function flushQueue(q, options) {
  const report = await q.flush(options);
  await chromeStorage.set('kt.lastFlush', { at: new Date().toISOString(), ...report });
  return report;
}

async function onSubmission(msg) {
  const { q, s, client } = await getQueue();
  if (!s.trackingEnabled) {
    return { queued: false, reason: 'tracking disabled' };
  }

  let problemId = (msg.problem && msg.problem.problemId) || null;
  const slug = (msg.problem && msg.problem.slug) || (msg.problem && KTExtract.parseSlug(msg.problem.url));

  // 1. If problemId is not already LC..., check last recommendation
  if (!problemId || !problemId.startsWith('LC')) {
    const lastRec = await chromeStorage.get('kt.lastRecommendation');
    if (lastRec && lastRec.recommendation && (lastRec.resolvedSlug === slug || !slug)) {
      problemId = lastRec.recommendation.problemId;
    }
  }

  // 2. If still missing, query KT_python for problem by slug
  if ((!problemId || !problemId.startsWith('LC')) && slug) {
    try {
      const probRes = await client.getProblem(slug);
      if (probRes && probRes.body && probRes.body.problemId) {
        problemId = probRes.body.problemId;
      }
    } catch (_) {}
  }

  // 3. Fallback: use slug or provided problemId
  if (!problemId && slug) {
    problemId = slug;
  }

  if (!problemId) {
    return { queued: false, reason: 'could not resolve problem identifier' };
  }

  const attemptNumber = await nextAttemptNumber(problemId);
  const event = KTExtract.buildEvent({
    userId: s.userId,
    problemId: problemId,
    eventId: crypto.randomUUID(),
    timestamp: msg.timestamp,
    attemptNumber,
    result: msg.result,
    timeSpentSeconds: msg.timeSpentSeconds,
    hintUsed: msg.hintUsed,
    solutionViewed: msg.solutionViewed,
    sessionId: msg.sessionId,
  });

  const problem = msg.problem && msg.problem.staticDifficulty ? {
    problemKey: problemId,
    title: msg.problem.title,
    url: msg.problem.url,
    staticDifficulty: msg.problem.staticDifficulty,
    concepts: msg.problem.concepts || [],
  } : undefined;

  await q.enqueue({ event, problem });
  const flushReport = await flushQueue(q);

  // If in adaptive mode, auto-refresh recommendation immediately
  try {
    await syncAdaptiveRecommendation(client, s);
  } catch (_) {}

  return { queued: true, report: flushReport, problemId };
}

async function syncAdaptiveRecommendation(client, settings) {
  let nextRes;
  try {
    nextRes = await client.nextAction(settings.userId);
  } catch (err) {
    if (err && (err.status === 404 || String(err.message).includes('404'))) {
      // Auto-register learner on first use, then fetch recommendation
      try {
        await client.registerLearner(settings.userId);
        nextRes = await client.nextAction(settings.userId);
      } catch (regErr) {
        throw regErr;
      }
    } else {
      throw err;
    }
  }

  if (!nextRes || !nextRes.body) return null;

  const data = nextRes.body;
  const rec = data.recommendation || {};
  let resolvedSlug = null;
  let resolvedTitle = rec.problemId || 'Next Recommended Problem';
  let resolvedUrl = null;

  if (rec.problemId) {
    try {
      const probRes = await client.getProblem(rec.problemId);
      if (probRes && probRes.body) {
        resolvedSlug = probRes.body.slug || KTExtract.parseSlug(probRes.body.url);
        resolvedTitle = probRes.body.title || resolvedTitle;
        resolvedUrl = probRes.body.url;
      }
    } catch (_) {}

    // Fallback: derive slug from title (e.g. "House Robber III" -> "house-robber-iii")
    if (!resolvedSlug && resolvedTitle && !resolvedTitle.startsWith('LC')) {
      resolvedSlug = resolvedTitle
        .toLowerCase()
        .replace(/[^\w\s-]/g, '')
        .trim()
        .replace(/\s+/g, '-');
    }

    if (!resolvedUrl && resolvedSlug) {
      resolvedUrl = `https://leetcode.com/problems/${resolvedSlug}/`;
    }
  }

  const enrichedRec = {
    ...data,
    resolvedSlug,
    resolvedTitle,
    resolvedUrl,
    syncedAt: new Date().toISOString(),
  };

  await chrome.storage.local.set({ 'kt.lastRecommendation': enrichedRec });

  // Update Adapto active syllabus if in Adaptive Mode
  if (settings.mode === 'adaptive' && resolvedSlug) {
    const { adapto } = await chrome.storage.local.get('adapto');
    const state = adapto || { slugs: [], enabled: true };
    if (!state.slugs.includes(resolvedSlug)) {
      state.slugs = [resolvedSlug, ...state.slugs.filter(s => s !== resolvedSlug)];
    }
    state.enabled = true;
    state.adaptiveSlug = resolvedSlug;
    state.adaptiveTitle = resolvedTitle;
    state.adaptiveReason = rec.reason || 'Assigned by Knowledge Tracer to calibrate your initial ability.';
    state.adaptiveDifficulty = rec.difficulty;
    state.adaptiveType = rec.type;
    state.adaptiveConcept = (data.gap && data.gap.concept) || null;
    await chrome.storage.local.set({ adapto: state });
  }

  return enrichedRec;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handlers = {
    'kt/submission': () => onSubmission(msg),
    'kt/flush': async () => flushQueue((await getQueue()).q, { force: true }),
    'kt/status': async () => {
      const { q, s } = await getQueue();
      const lastRec = await chromeStorage.get('kt.lastRecommendation');
      return {
        userId: s.userId,
        trackingEnabled: s.trackingEnabled,
        apiBaseUrl: s.apiBaseUrl,
        mode: s.mode,
        queued: await q.size(),
        lastFlush: await chromeStorage.get('kt.lastFlush'),
        authError: await q.authError(),
        lastRecommendation: lastRec,
      };
    },
    'kt/next-action': async () => {
      const { client, s } = await getQueue();
      try {
        return (await client.nextAction(s.userId)).body;
      } catch (err) {
        if (err && (err.status === 404 || String(err.message).includes('404'))) {
          await client.registerLearner(s.userId);
          return (await client.nextAction(s.userId)).body;
        }
        throw err;
      }
    },
    'adapto/sync-adaptive': async () => {
      const { client, s } = await getQueue();
      return await syncAdaptiveRecommendation(client, s);
    },
    'adapto/set-mode': async () => {
      const mode = msg.mode;
      await chrome.storage.local.set({ mode });
      if (mode === 'adaptive') {
        const { client, s } = await getQueue();
        await syncAdaptiveRecommendation(client, { ...s, mode: 'adaptive' });
      }
      return { ok: true, mode };
    },
  };

  const handler = handlers[msg && msg.type];
  if (!handler) return false;

  handler()
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: err.message, status: err.status }));

  return true; // asynchronous response
});

// Periodic flush alarm
chrome.alarms.create('adapto-flush', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'adapto-flush') {
    const { q } = await getQueue();
    await flushQueue(q);
  }
});
