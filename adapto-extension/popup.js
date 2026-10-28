// Adapto Popup — Focus Mode + Knowledge Tracing AI Tutor Integration

const LEETCODE_URL_RE = /https?:\/\/(?:www\.)?leetcode\.(?:com|cn)\/problems\/([a-z0-9\-]+)/gi;

const els = {
  focusToggle: document.getElementById("focusToggle"),
  focusState: document.getElementById("focusState"),
  statusMsg: document.getElementById("statusMsg"),
  activeModeLabel: document.getElementById("activeModeLabel"),

  // Tabs
  tabAdaptive: document.getElementById("tabAdaptive"),
  tabByos: document.getElementById("tabByos"),
  tabSettings: document.getElementById("tabSettings"),
  panelAdaptive: document.getElementById("panelAdaptive"),
  panelByos: document.getElementById("panelByos"),
  panelSettings: document.getElementById("panelSettings"),

  // Adaptive panel
  serverDot: document.getElementById("serverDot"),
  serverText: document.getElementById("serverText"),
  recType: document.getElementById("recType"),
  recDiff: document.getElementById("recDiff"),
  recTitle: document.getElementById("recTitle"),
  recReason: document.getElementById("recReason"),
  gapBox: document.getElementById("gapBox"),
  gapVal: document.getElementById("gapVal"),
  btnApplyRec: document.getElementById("btnApplyRec"),
  btnOpenProblem: document.getElementById("btnOpenProblem"),
  btnRefreshRec: document.getElementById("btnRefreshRec"),
  metricAbility: document.getElementById("metricAbility"),
  metricStatus: document.getElementById("metricStatus"),
  metricQueued: document.getElementById("metricQueued"),

  // BYOS panel
  dropZone: document.getElementById("dropZone"),
  fileInput: document.getElementById("fileInput"),
  cursorDot: document.getElementById("cursorDot"),
  statsCard: document.getElementById("statsCard"),
  problemCount: document.getElementById("problemCount"),
  listWrap: document.getElementById("listWrap"),
  problemList: document.getElementById("problemList"),
  clearBtn: document.getElementById("clearBtn"),

  // Settings panel
  chkTracking: document.getElementById("chkTracking"),
  inpApiUrl: document.getElementById("inpApiUrl"),
  inpUserId: document.getElementById("inpUserId"),
  btnSaveSettings: document.getElementById("btnSaveSettings"),
  btnFlushEvents: document.getElementById("btnFlushEvents"),
};

let currentRecommendation = null;

const send = (type, payload = {}) =>
  new Promise((resolve) => chrome.runtime.sendMessage({ type, ...payload }, resolve));

// ---- Tab Switching ----
function setTab(name) {
  els.tabAdaptive.classList.toggle("active", name === "adaptive");
  els.tabByos.classList.toggle("active", name === "byos");
  els.tabSettings.classList.toggle("active", name === "settings");

  els.panelAdaptive.hidden = name !== "adaptive";
  els.panelByos.hidden = name !== "byos";
  els.panelSettings.hidden = name !== "settings";

  if (name === "adaptive" || name === "byos") {
    send("adapto/set-mode", { mode: name });
    els.activeModeLabel.textContent = name === "adaptive" ? "AI Tutor Mode" : "BYOS Mode";
  }
}

els.tabAdaptive.addEventListener("click", () => { setTab("adaptive"); refreshAdaptive(); });
els.tabByos.addEventListener("click", () => { setTab("byos"); refreshByos(); });
els.tabSettings.addEventListener("click", () => { setTab("settings"); refreshSettings(); });

// ---- Adaptive AI Tutor Logic ----
async function refreshAdaptive() {
  const statusRes = await send("kt/status");
  const s = statusRes && statusRes.ok ? statusRes.data : null;

  if (s) {
    els.metricQueued.textContent = String(s.queued || 0);
    els.inpApiUrl.value = s.apiBaseUrl || "http://127.0.0.1:8081";
    els.inpUserId.value = s.userId || "";
    els.chkTracking.checked = Boolean(s.trackingEnabled);
  }

  // Try to sync/fetch latest recommendation
  els.serverText.textContent = "Connecting to Knowledge Tracer...";
  const syncRes = await send("adapto/sync-adaptive");

  if (syncRes && syncRes.ok && syncRes.data) {
    els.serverDot.className = "status-dot online";
    els.serverText.textContent = `Connected (KT_python :8081)`;
    renderRecommendation(syncRes.data);
  } else {
    // Check if we have a cached recommendation
    if (s && s.lastRecommendation) {
      els.serverDot.className = "status-dot offline";
      els.serverText.textContent = `Using cached profile (Backend offline)`;
      renderRecommendation(s.lastRecommendation);
    } else {
      els.serverDot.className = "status-dot offline";
      els.serverText.textContent = `KT_python Offline (start: python scripts/run_server.py)`;
      els.recTitle.textContent = "Backend not running";
      els.recReason.textContent = "Start KT_python on http://127.0.0.1:8081 to receive personalized problem guidance.";
      els.gapBox.hidden = true;
    }
  }
}

function renderRecommendation(data) {
  currentRecommendation = data;
  const rec = data.recommendation || {};
  const gap = data.gap || {};
  const state = data.state || {};

  els.recType.textContent = rec.type || "RECOMMENDATION";
  els.recTitle.textContent = data.resolvedTitle || rec.problemId || "Continue Practice";
  els.recReason.textContent = rec.reason || "Recommended next problem based on your cognitive profile.";

  // Difficulty badge
  const diffVal = rec.difficulty;
  const diffLabel = diffVal !== undefined && diffVal !== null
    ? (diffVal > 6 ? "HARD" : diffVal > 3.8 ? "MEDIUM" : "EASY")
    : "MEDIUM";
  els.recDiff.textContent = diffLabel;
  els.recDiff.className = "diff-badge " + diffLabel.toLowerCase();

  // Gap / focus area
  if (gap.concept || (gap.type && gap.type !== "NONE")) {
    els.gapBox.hidden = false;
    els.gapVal.textContent = gap.concept || gap.type;
  } else {
    els.gapBox.hidden = true;
  }

  // Metrics
  els.metricAbility.textContent = state.ability !== undefined ? state.ability.toFixed(2) : "5.00";
  els.metricStatus.textContent = state.calibrated ? "Calibrated" : "Calibrating";

  updateStatusMessage();
}

els.btnApplyRec.addEventListener("click", async () => {
  if (!currentRecommendation || !currentRecommendation.resolvedSlug) {
    els.statusMsg.textContent = "No recommended problem available to apply.";
    return;
  }
  const slug = currentRecommendation.resolvedSlug;
  const rec = currentRecommendation.recommendation || {};
  const state = {
    slugs: [slug],
    enabled: true,
    adaptiveSlug: slug,
    adaptiveTitle: currentRecommendation.resolvedTitle,
    adaptiveReason: rec.reason || "Cold-start calibration item near current estimate 5.00 maximizes information",
    adaptiveDifficulty: rec.difficulty,
    adaptiveType: rec.type,
    adaptiveConcept: (currentRecommendation.gap && currentRecommendation.gap.concept) || null,
  };
  await chrome.storage.local.set({ adapto: state });
  els.focusToggle.checked = true;
  updateStatusMessage();
  els.statusMsg.textContent = `Focus Mode locked to ${currentRecommendation.resolvedTitle || slug}!`;
});

els.btnOpenProblem.addEventListener("click", async () => {
  if (!currentRecommendation) return;
  const slug = currentRecommendation.resolvedSlug;
  const url = currentRecommendation.resolvedUrl ||
    (slug ? `https://leetcode.com/problems/${slug}/` : null);
  if (url) {
    if (slug) {
      const state = {
        slugs: [slug],
        enabled: true,
        adaptiveSlug: slug,
        adaptiveTitle: currentRecommendation.resolvedTitle,
      };
      await chrome.storage.local.set({ adapto: state });
    }
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab && tab.url && tab.url.includes("leetcode.com")) {
        chrome.tabs.update(tab.id, { url });
        window.close();
        return;
      }
    } catch (_) {}
    chrome.tabs.create({ url });
    window.close();
  }
});

els.btnRefreshRec.addEventListener("click", async () => {
  els.serverText.textContent = "Refreshing recommendation...";
  await refreshAdaptive();
});

// ---- BYOS Logic ----
function parseSheet(text) {
  const slugs = [];
  const seen = new Set();
  let m;
  LEETCODE_URL_RE.lastIndex = 0;
  while ((m = LEETCODE_URL_RE.exec(text)) !== null) {
    const slug = m[1].toLowerCase();
    if (!seen.has(slug)) {
      seen.add(slug);
      slugs.push(slug);
    }
  }
  return slugs;
}

function slugToTitle(slug) {
  return slug
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

async function loadState() {
  const { adapto } = await chrome.storage.local.get("adapto");
  return adapto || { slugs: [], enabled: false };
}

async function saveState(state) {
  await chrome.storage.local.set({ adapto: state });
}

async function refreshByos() {
  const state = await loadState();
  const n = state.slugs ? state.slugs.length : 0;
  els.statsCard.hidden = n === 0;
  els.listWrap.hidden = n === 0;
  els.problemCount.textContent = String(n);
  els.problemList.innerHTML = "";
  if (state.slugs) {
    state.slugs.forEach((s) => {
      const li = document.createElement("li");
      li.textContent = slugToTitle(s);
      li.title = "https://leetcode.com/problems/" + s + "/";
      els.problemList.appendChild(li);
    });
  }
}

async function handleFile(file) {
  if (!file) return;
  const name = file.name.toLowerCase();
  if (!name.endsWith(".txt") && !name.endsWith(".csv")) {
    els.statusMsg.textContent = "Please upload a .txt or .csv sheet.";
    return;
  }
  const text = await file.text();
  const slugs = parseSheet(text);
  if (slugs.length === 0) {
    els.statusMsg.textContent = "No LeetCode problem URLs found in that file.";
    return;
  }
  const state = await loadState();
  state.slugs = slugs;
  state.enabled = true;
  state.adaptiveSlug = null;
  state.adaptiveTitle = null;
  await saveState(state);
  await refreshByos();
  updateStatusMessage();
}

els.dropZone.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", () => handleFile(els.fileInput.files[0]));
els.clearBtn.addEventListener("click", async () => {
  const state = { slugs: [], enabled: false, adaptiveSlug: null, adaptiveTitle: null };
  await saveState(state);
  await refreshByos();
  updateStatusMessage();
});

// Cursor animation over dropzone
els.dropZone.addEventListener("mousemove", (e) => {
  const rect = els.dropZone.getBoundingClientRect();
  els.cursorDot.style.left = e.clientX - rect.left + "px";
  els.cursorDot.style.top = e.clientY - rect.top + "px";
});

// Drag & drop events
["dragenter", "dragover"].forEach((evt) =>
  els.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropZone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((evt) =>
  els.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropZone.classList.remove("dragover");
  })
);
els.dropZone.addEventListener("drop", (e) => handleFile(e.dataTransfer.files[0]));

// ---- Settings Logic ----
async function refreshSettings() {
  const { apiBaseUrl, apiKey, userId, trackingEnabled } = await chrome.storage.local.get([
    "apiBaseUrl", "apiKey", "userId", "trackingEnabled"
  ]);
  els.inpApiUrl.value = apiBaseUrl || "http://127.0.0.1:8081";
  els.inpUserId.value = userId || "";
  els.chkTracking.checked = trackingEnabled !== false;
}

els.btnSaveSettings.addEventListener("click", async () => {
  const apiBaseUrl = els.inpApiUrl.value.trim().replace(/\/+$/, '');
  const trackingEnabled = els.chkTracking.checked;
  await chrome.storage.local.set({ apiBaseUrl, trackingEnabled });
  els.statusMsg.textContent = "Settings saved successfully.";
});

els.btnFlushEvents.addEventListener("click", async () => {
  els.statusMsg.textContent = "Flushing events...";
  const res = await send("kt/flush");
  if (res && res.ok) {
    els.statusMsg.textContent = `Flushed: ${res.data.delivered || 0} delivered, ${res.data.retrying || 0} retrying.`;
  }
  const s = await send("kt/status");
  if (s && s.ok) els.metricQueued.textContent = String(s.data.queued || 0);
});

// ---- Global State & Toggle ----
async function updateStatusMessage() {
  const state = await loadState();
  const n = state.slugs ? state.slugs.length : 0;
  els.focusToggle.checked = state.enabled;
  els.focusState.textContent = state.enabled ? "Focus Mode ON" : "Focus Mode OFF";
  els.focusState.className = "pill " + (state.enabled ? "on" : "off");

  if (state.enabled) {
    if (state.adaptiveSlug) {
      els.statusMsg.textContent = `Focusing on recommended problem (${state.adaptiveTitle || state.adaptiveSlug}).`;
    } else if (n > 0) {
      els.statusMsg.textContent = `Filtering LeetCode to your ${n}-problem syllabus.`;
    } else {
      els.statusMsg.textContent = "Focus Mode is ON. Select or upload problems to filter.";
    }
  } else {
    els.statusMsg.textContent = "Focus Mode is idle. Toggle ON to filter LeetCode.";
  }
}

els.focusToggle.addEventListener("change", async () => {
  const state = await loadState();
  state.enabled = els.focusToggle.checked;
  await saveState(state);
  updateStatusMessage();
});

// ---- Initial Load ----
async function init() {
  const { mode } = await chrome.storage.local.get("mode");
  const initialMode = mode || "adaptive";
  setTab(initialMode);
  if (initialMode === "adaptive") {
    await refreshAdaptive();
  } else {
    await refreshByos();
  }
  await updateStatusMessage();
}

init();
