/*
 * Content script for leetcode.com/problems/*.
 * Observes: problem identity, static difficulty, topic tags, time on page, hint and
 * solution/editorial use, and the verdict shown after a submission. DOM selectors are
 * FRAGILE by nature (LeetCode can change markup at any time); each has a fallback and
 * failures degrade to "no event" (or to hintUsed=false, the backend's default).
 */
(function () {
  'use strict';
  const X = globalThis.KTExtract;
  const sessionId = 'sess-' + Math.random().toString(36).slice(2, 10);
  let currentSlug = X.parseSlug(location.href);
  let pageOpenedAt = Date.now();
  let lastVerdictKey = null;
  let hintUsed = false;
  let solutionViewed = X.isSolutionPath(location.pathname);

  function problemInfo() {
    const titleEl = document.querySelector('[data-cy="question-title"], div.text-title-large a, div.text-title-large');
    const parsed = X.parseProblemTitle(titleEl ? titleEl.textContent : '') ||
      X.parseProblemTitle(document.title.replace(/\s+-\s+LeetCode$/, ''));
    if (!parsed) return null;
    const diffEl = document.querySelector('[class*="text-difficulty-"], [diff]');
    const tags = Array.from(document.querySelectorAll('a[href^="/tag/"]')).map((a) => a.textContent);
    return {
      ...parsed,
      url: 'https://leetcode.com/problems/' + (currentSlug || '') + '/',
      staticDifficulty: X.normalizeDifficulty(diffEl ? diffEl.textContent : ''),
      concepts: X.mapTags(tags),
    };
  }

  function verdictElement() {
    return document.querySelector('[data-e2e-locator="submission-result"]');
  }

  // Assistance flags and the timer belong to one problem: switching between the
  // description, solutions and submissions tabs of the same problem keeps them.
  function trackNavigation() {
    const slug = X.parseSlug(location.href);
    if (slug !== currentSlug) {
      currentSlug = slug;
      pageOpenedAt = Date.now();
      lastVerdictKey = null;
      hintUsed = false;
      solutionViewed = false;
    }
    if (X.isSolutionPath(location.pathname)) solutionViewed = true;
  }

  document.addEventListener('click', (e) => {
    let el = e.target instanceof Element ? e.target : null;
    for (let depth = 0; el && depth < 4; depth++, el = el.parentElement) {
      if (X.isHintToggle(el.textContent)) {
        hintUsed = true;
        return;
      }
    }
  }, true);

  function onMutation() {
    trackNavigation();
    const el = verdictElement();
    if (!el) return;
    const result = X.mapSubmissionResult(el.textContent);
    if (!result) return;
    const key = location.pathname + '|' + el.textContent.trim();
    if (key === lastVerdictKey) return;         // same verdict node re-rendered
    lastVerdictKey = key;

    const problem = problemInfo();
    if (!problem) return;
    chrome.runtime.sendMessage({
      type: 'kt/submission',
      problem,
      result,
      timestamp: new Date().toISOString(),
      timeSpentSeconds: Math.round((Date.now() - pageOpenedAt) / 1000),
      hintUsed,
      solutionViewed,
      sessionId,
    });
  }

  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => { scheduled = false; onMutation(); }, 400);
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
})();
