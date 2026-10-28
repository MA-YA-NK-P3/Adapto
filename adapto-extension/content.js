// Adapto unified content script:
// 1. DOM Filter Engine (Focus Mode for LeetCode problemsets)
// 2. Knowledge Tracing Observer (captures problem attempts, verdicts, time, hints, solutions)

(() => {
  'use strict';

  const HIDDEN_ATTR = "data-adapto-hidden";
  const BADGE_ID = "adapto-focus-badge";
  const EMPTY_ID = "adapto-empty-state";

  const X = globalThis.KTExtract;
  const sessionId = 'sess-' + Math.random().toString(36).slice(2, 10);
  let pageOpenedAt = Date.now();
  let lastVerdictKey = null;
  let hintUsed = false;
  let solutionViewed = X && typeof X.isSolutionPath === 'function' ? X.isSolutionPath(location.pathname) : false;

  let state = { slugs: [], enabled: false, adaptiveSlug: null, adaptiveTitle: null };
  let slugSet = new Set();
  let scheduled = false;

  function slugFromHref(href) {
    if (!href) return null;
    const m = href.match(/\/problems\/([a-z0-9\-]+)/i);
    return m ? m[1].toLowerCase() : null;
  }

  function currentProblemSlug() {
    return X && typeof X.parseSlug === 'function' ? X.parseSlug(location.href) : slugFromHref(location.pathname);
  }

  let currentSlug = currentProblemSlug();

  // Walk up from a problem link to the repeating "row" container
  function findRow(anchor) {
    let node = anchor;
    let best = null;
    for (let depth = 0; depth < 8 && node.parentElement; depth++) {
      const parent = node.parentElement;
      if (parent === document.body) break;
      const siblings = Array.from(parent.children).filter(
        (c) => c.tagName === node.tagName
      );
      if (siblings.length >= 5) best = node;
      if (node.getAttribute && node.getAttribute("role") === "row") return node;
      node = parent;
    }
    return best;
  }

  function isProblemListPage() {
    return (
      /\/(problemset|problem-list|study-plan|tag)\//.test(location.pathname) ||
      location.pathname === "/problemset/" ||
      location.pathname.startsWith("/problemset")
    );
  }

  function isProblemDetailPage() {
    return /\/problems\/[a-z0-9\-]+/i.test(location.pathname);
  }

  // --- 1. DOM Filter Engine ---
  function applyFilter() {
    if (!isProblemListPage()) return;

    const anchors = document.querySelectorAll('a[href*="/problems/"]');
    const active = state.enabled && slugSet.size > 0;
    let visible = 0;
    let total = 0;

    anchors.forEach((a) => {
      const slug = slugFromHref(a.getAttribute("href"));
      if (!slug) return;
      const row = findRow(a);
      if (!row) return;
      total++;
      if (active && !slugSet.has(slug)) {
        row.setAttribute(HIDDEN_ATTR, "1");
      } else {
        row.removeAttribute(HIDDEN_ATTR);
        visible++;
      }
    });

    updateBadge(active, visible);
    updateEmptyState(active && total > 0 && visible === 0);
  }

  function updateBadge(active, visible) {
    let badge = document.getElementById(BADGE_ID);
    if (!active) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("a");
      badge.id = BADGE_ID;
      document.documentElement.appendChild(badge);
    }
    if (state.adaptiveSlug) {
      badge.href = `/problems/${state.adaptiveSlug}/`;
      badge.innerHTML = `◆ <strong>AI Tutor:</strong> Solve ${state.adaptiveTitle || state.adaptiveSlug} &rarr;`;
      badge.title = "Click to jump directly to this problem";
    } else {
      badge.removeAttribute("href");
      badge.textContent = `◆ Adapto Focus Mode — ${slugSet.size} problems in syllabus`;
    }
  }

  function updateEmptyState(show) {
    let el = document.getElementById(EMPTY_ID);
    if (!show) {
      if (el) el.remove();
      return;
    }
    if (isProblemListPage()) {
      if (!el) {
        el = document.createElement("div");
        el.id = EMPTY_ID;
        document.body.appendChild(el);
      }
      if (state.adaptiveSlug) {
        const diff = state.adaptiveDifficulty !== undefined && state.adaptiveDifficulty !== null
          ? (state.adaptiveDifficulty > 6 ? 'HARD' : state.adaptiveDifficulty > 3.8 ? 'MEDIUM' : 'EASY')
          : 'MEDIUM';
        const diffColor = diff === 'HARD' ? '#ff375f' : diff === 'MEDIUM' ? '#ffc01e' : '#00b8a3';
        const reason = state.adaptiveReason || 'Assigned by Knowledge Tracer to calibrate your baseline ability.';

        el.innerHTML = `
          <div style="display:flex; align-items:center; justify-content:center; gap:8px; margin-bottom:12px;">
            <span style="font-size:11px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:#a8e619; background:rgba(168,230,25,0.15); padding:4px 10px; border-radius:999px;">
              🧠 AI Tutor Recommendation
            </span>
            <span style="font-size:10px; font-weight:700; letter-spacing:0.06em; color:${diffColor}; background:rgba(255,255,255,0.08); padding:3px 8px; border-radius:999px;">
              ${diff}
            </span>
          </div>
          <h2 style="font-size:20px; font-weight:700; margin-bottom:8px; color:#ffffff; letter-spacing:0.01em;">
            ${state.adaptiveTitle || state.adaptiveSlug}
          </h2>
          <p style="font-size:12.5px; color:#8a9486; margin-bottom:18px; line-height:1.5; max-width:360px; margin-left:auto; margin-right:auto;">
            ${reason}
          </p>
          <a href="/problems/${state.adaptiveSlug}/" class="adapto-rec-btn" style="font-size:14px; padding:11px 24px;">
            🚀 Open &amp; Solve Problem &rarr;
          </a>
        `;
      } else {
        el.innerHTML = `
          <strong>Nothing from your syllabus on this page.</strong><br/>
          Adapto hid problems because none match your active focus mode. 
          Open the Adapto popup to change your sheet or switch to AI Tutor recommendations.
        `;
      }
    }
  }

  function scheduleFilter() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      applyFilter();
    });
  }

  // --- 2. Knowledge Tracing Observation Engine ---
  function problemInfo() {
    const slug = currentProblemSlug();
    if (!slug) return null;

    const titleEl = document.querySelector('[data-cy="question-title"], div.text-title-large a, div.text-title-large, h4[data-cy="question-title"]');
    const titleText = titleEl ? titleEl.textContent.trim() : '';

    let parsed = X && X.parseProblemTitle ? X.parseProblemTitle(titleText) : null;
    if (!parsed) {
      const docClean = document.title.replace(/\s+-\s+LeetCode$/, '').trim();
      parsed = X && X.parseProblemTitle ? X.parseProblemTitle(docClean) : null;
    }

    const diffEl = document.querySelector('[class*="text-difficulty-"], [diff], [class*="text-olive"], [class*="text-yellow"], [class*="text-pink"], [class*="text-green-"], [class*="text-amber-"], [class*="text-red-"]');
    const staticDifficulty = X && X.normalizeDifficulty ? X.normalizeDifficulty(diffEl ? diffEl.textContent : '') : null;
    const tags = Array.from(document.querySelectorAll('a[href^="/tag/"]')).map((a) => a.textContent);

    return {
      problemId: parsed ? parsed.problemId : null,
      number: parsed ? parsed.number : null,
      title: parsed ? parsed.title : (state.adaptiveTitle || titleText || slug),
      slug: slug,
      url: 'https://leetcode.com/problems/' + slug + '/',
      staticDifficulty,
      concepts: X && X.mapTags ? X.mapTags(tags) : [],
    };
  }

  function findVerdict() {
    // 1. Direct e2e locators
    const e2e = document.querySelector('[data-e2e-locator="submission-result"], [data-e2e-locator="console-result"]');
    if (e2e && X && X.mapSubmissionResult) {
      const res = X.mapSubmissionResult(e2e.textContent);
      if (res) return { result: res, text: e2e.textContent.trim() };
    }

    // 2. Class and color-based locators (common in modern LeetCode)
    const candidates = document.querySelectorAll(
      '[class*="result__"], [class*="status__"], [class*="verdict"], ' +
      'div[class*="text-red"], span[class*="text-red"], ' +
      'div[class*="text-green"], span[class*="text-green"], ' +
      'div[class*="text-rose"], span[class*="text-rose"], ' +
      'div[class*="text-emerald"], span[class*="text-emerald"], ' +
      'div[class*="text-yellow"], div[class*="text-amber"]'
    );
    for (const el of candidates) {
      const text = el.textContent.trim();
      if (text.length > 0 && text.length < 80 && X && X.mapSubmissionResult) {
        const res = X.mapSubmissionResult(text);
        if (res) return { result: res, text };
      }
    }

    // 3. Tab and heading elements on /submissions/ URL
    if (location.pathname.includes('/submissions/')) {
      const headings = document.querySelectorAll('h1, h2, h3, h4, span, div, a, p');
      for (const el of headings) {
        if (el.children.length <= 1) {
          const text = el.textContent.trim();
          if (/^(Accepted|Wrong Answer|Time Limit Exceeded|Runtime Error|Compile Error)/i.test(text) && X && X.mapSubmissionResult) {
            const res = X.mapSubmissionResult(text);
            if (res) return { result: res, text };
          }
        }
      }
    }

    return null;
  }

  function trackNavigation() {
    const slug = currentProblemSlug();
    if (slug && slug !== currentSlug) {
      currentSlug = slug;
      pageOpenedAt = Date.now();
      lastVerdictKey = null;
      hintUsed = false;
      solutionViewed = false;
    }
    if (X && X.isSolutionPath && X.isSolutionPath(location.pathname)) {
      solutionViewed = true;
    }
  }

  document.addEventListener('click', (e) => {
    let el = e.target instanceof Element ? e.target : null;
    for (let depth = 0; el && depth < 4; depth++, el = el.parentElement) {
      if (X && X.isHintToggle && X.isHintToggle(el.textContent)) {
        hintUsed = true;
        return;
      }
    }
  }, true);

  function checkSubmissions() {
    if (!isProblemDetailPage()) return;
    trackNavigation();
    const v = findVerdict();
    if (!v) return;

    const key = location.pathname + '|' + v.text;
    if (key === lastVerdictKey) return; // same verdict already reported
    lastVerdictKey = key;

    const problem = problemInfo();
    if (!problem) return;

    try {
      chrome.runtime.sendMessage({
        type: 'kt/submission',
        problem,
        result: v.result,
        timestamp: new Date().toISOString(),
        timeSpentSeconds: Math.max(5, Math.round((Date.now() - pageOpenedAt) / 1000)),
        hintUsed,
        solutionViewed,
        sessionId,
      }, (res) => {
        if (!res || !res.ok) {
          lastVerdictKey = null; // allow retry if background reported error
        }
      });
    } catch (_) {
      lastVerdictKey = null;
    }
  }

  async function refreshState() {
    try {
      const data = await chrome.storage.local.get(["adapto", "kt.lastRecommendation"]);
      state = data.adapto || { slugs: [], enabled: false };
      const lastRec = data["kt.lastRecommendation"];
      if (!state.adaptiveSlug && lastRec && lastRec.resolvedSlug) {
        state.adaptiveSlug = lastRec.resolvedSlug;
        state.adaptiveTitle = lastRec.resolvedTitle;
        state.adaptiveReason = lastRec.recommendation && lastRec.recommendation.reason;
        state.adaptiveDifficulty = lastRec.recommendation && lastRec.recommendation.difficulty;
      }
      slugSet = new Set(state.slugs || (state.adaptiveSlug ? [state.adaptiveSlug] : []));
      scheduleFilter();
    } catch (e) {
      /* extension context invalidated on reload — ignore */
    }
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && (changes.adapto || changes["kt.lastRecommendation"])) {
      refreshState();
    }
  });

  // Observe DOM for SPA transitions, filter updates, and submission results
  const observer = new MutationObserver(() => {
    scheduleFilter();
    checkSubmissions();
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
  });

  let lastPath = location.pathname;
  setInterval(() => {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      trackNavigation();
      scheduleFilter();
    }
  }, 500);

  refreshState();
})();
