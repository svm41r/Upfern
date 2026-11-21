const DEFAULT_SERVER_URL = "http://127.0.0.1:8767";
const HEARTBEAT_ALARM = "shabi-heartbeat";
const CHUNK_BYTES = 1572864; // 1.5 MiB HTTP Range chunks

const PLATFORMS = {
  youtube: {
    label: "YouTube Shorts",
    defaultUrl: "https://studio.youtube.com/",
    settingsUrlKey: "channel_url",
    tabPatterns: ["https://studio.youtube.com/*"],
  },
  tiktok: {
    label: "TikTok",
    defaultUrl: "https://www.tiktok.com/tiktokstudio/upload",
    settingsUrlKey: "tiktok_url",
    tabPatterns: [
      "https://www.tiktok.com/tiktokstudio*",
      "https://www.tiktok.com/creator-center*",
      "https://www.tiktok.com/upload*",
    ],
  },
  instagram: {
    label: "Instagram Reels",
    defaultUrl: "https://www.instagram.com/",
    settingsUrlKey: "instagram_url",
    tabPatterns: ["https://www.instagram.com/*"],
  },
  facebook: {
    label: "Facebook Reels",
    defaultUrl: "https://www.facebook.com/reels/create",
    settingsUrlKey: "facebook_url",
    tabPatterns: [
      "https://www.facebook.com/reels/create*",
      "https://www.facebook.com/reel/create*",
      "https://business.facebook.com/latest/reels_composer*",
    ],
  },
};

const TERMINAL_STATUSES = new Set(["posted", "done", "failed", "cancelled"]);

function normPlatform(p) {
  const k = String(p || "youtube").trim().toLowerCase();
  return PLATFORMS[k] ? k : "youtube";
}

async function getServerUrl() {
  const { serverUrl } = await chrome.storage.local.get({ serverUrl: DEFAULT_SERVER_URL });
  return String(serverUrl || DEFAULT_SERVER_URL).trim().replace(/\/+$/, "");
}

async function apiFetch(path, options = {}) {
  const base = await getServerUrl();
  const headers = {
    "X-Shabi-Ext": "1",
    "X-Upfern-Ext": "1",
    ...(options.headers || {}),
  };
  if (options.body && typeof options.body === "string" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${base}${path}`, { ...options, headers });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const msg = data?.detail || data?.message || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(HEARTBEAT_ALARM);
  if (!existing) {
    chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 0.5 });
  }
}

async function updateBadgeForPending(pendingCount) {
  if (pendingCount > 0) {
    await chrome.action.setBadgeText({ text: String(pendingCount > 99 ? "99+" : pendingCount) });
    await chrome.action.setBadgeBackgroundColor({ color: "#FF5B24" });
  } else {
    const { badgeLockUntil = 0 } = await chrome.storage.session.get("badgeLockUntil");
    if (Date.now() > badgeLockUntil) {
      await chrome.action.setBadgeText({ text: "" });
    }
  }
}

async function setResultBadge(text, color) {
  await chrome.storage.session.set({ badgeLockUntil: Date.now() + 12000 });
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

async function sendHeartbeat() {
  try {
    const manifest = chrome.runtime.getManifest();
    const data = await apiFetch("/api/ext/heartbeat", {
      method: "POST",
      body: JSON.stringify({
        version: manifest.version,
        ext_id: chrome.runtime.id,
      }),
    });
    await chrome.storage.local.set({
      lastPing: { ok: true, at: Date.now(), ...data },
    });
    await updateBadgeForPending(Number(data?.pending || 0));
    return { ok: true, ...data };
  } catch (err) {
    await chrome.storage.local.set({
      lastPing: { ok: false, at: Date.now(), error: err.message },
    });
    return { ok: false, error: err.message };
  }
}

async function notifyUser(id, title, message) {
  try {
    const iconUrl = chrome.runtime.getURL("icons/icon-128.png");
    await chrome.notifications.create(id, {
      type: "basic",
      iconUrl,
      title,
      message,
    });
  } catch {
    // Notifications may be disabled by OS/browser settings
  }
}

async function getActiveJobs() {
  const { activeJobs = {} } = await chrome.storage.session.get({ activeJobs: {} });
  return activeJobs && typeof activeJobs === "object" ? activeJobs : {};
}

async function setActiveJob(platform, entry) {
  const p = normPlatform(platform);
  const all = await getActiveJobs();
  if (entry) {
    all[p] = entry;
  } else {
    delete all[p];
  }
  await chrome.storage.session.set({ activeJobs: all });
}

async function resolvePlatformUrl(platform, overrideUrl = "") {
  const p = normPlatform(platform);
  const meta = PLATFORMS[p];
  if (overrideUrl && /^https?:\/\//i.test(overrideUrl)) {
    return overrideUrl;
  }
  try {
    const st = await apiFetch("/api/ext/settings");
    const custom = st?.[meta.settingsUrlKey];
    if (custom && /^https?:\/\//i.test(custom)) {
      if (p === "youtube" && !custom.includes("studio.youtube.com")) {
        return meta.defaultUrl;
      }
      return custom;
    }
  } catch {
    // Use default URL
  }
  return meta.defaultUrl;
}

async function openOrFocusPlatform(platform, overrideUrl = "", activate = true) {
  const p = normPlatform(platform);
  const meta = PLATFORMS[p];
  const tabs = await chrome.tabs.query({ url: meta.tabPatterns });
  if (tabs.length > 0) {
    const tab = tabs[0];
    if (activate) {
      await chrome.tabs.update(tab.id, { active: true });
      if (tab.windowId != null) {
        await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
      }
    }
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "WAKE_CHECK", platform: p });
    } catch {
      // Content script may still be initializing
    }
    return { opened: false, focused: activate, tabId: tab.id, platform: p };
  }

  const targetUrl = await resolvePlatformUrl(p, overrideUrl);
  const win = await chrome.windows.getLastFocused({ populate: false }).catch(() => null);
  const created = await chrome.tabs.create({
    url: targetUrl,
    ...(win?.id != null ? { windowId: win.id } : {}),
    active: activate,
  });
  return { opened: true, focused: activate, tabId: created.id, platform: p };
}

async function triggerPlatforms(rawPlatforms) {
  await sendHeartbeat();
  const list = Array.isArray(rawPlatforms) && rawPlatforms.length
    ? [...new Set(rawPlatforms.map(normPlatform))]
    : ["youtube"];
  const results = [];
  for (let i = 0; i < list.length; i++) {
    const res = await openOrFocusPlatform(list[i], "", i === 0);
    results.push(res);
  }
  return { ok: true, results };
}

function uint8ToBase64(bytes) {
  const sliceSize = 32768;
  let binary = "";
  for (let i = 0; i < bytes.length; i += sliceSize) {
    const sub = bytes.subarray(i, Math.min(i + sliceSize, bytes.length));
    binary += String.fromCharCode.apply(null, sub);
  }
  return btoa(binary);
}

async function streamVideoOverPort(port, jobId, filename) {
  let disconnected = false;
  port.onDisconnect.addListener(() => {
    disconnected = true;
  });

  try {
    const base = await getServerUrl();
    const url = `${base}/api/ext/queue/${encodeURIComponent(jobId)}/video`;
    let offset = 0;
    let total = null;
    let chunkIndex = 0;

    while (!disconnected) {
      const rangeEnd = offset + CHUNK_BYTES - 1;
      const res = await fetch(url, {
        headers: {
          "X-Shabi-Ext": "1",
          Range: `bytes=${offset}-${rangeEnd}`,
        },
      });

      if (!res.ok && res.status !== 206) {
        throw new Error(`Video stream failed (HTTP ${res.status})`);
      }

      const contentRange = res.headers.get("Content-Range");
      if (contentRange) {
        const m = contentRange.match(/bytes\s+\d+-\d+\/(\d+|\*)/i);
        if (m && m[1] !== "*") {
          total = Number(m[1]);
        }
      } else {
        const len = res.headers.get("Content-Length");
        if (len && total == null) {
          total = Number(len);
        }
      }

      const buf = await res.arrayBuffer();
      if (disconnected) return;
      const view = new Uint8Array(buf);
      if (view.length === 0) break;

      const b64 = uint8ToBase64(view);
      offset += view.length;

      port.postMessage({
        type: "chunk",
        index: chunkIndex++,
        data: b64,
        loaded: offset,
        total: total || offset,
      });

      if (res.status === 200 || (total !== null && offset >= total)) {
        break;
      }
    }

    if (!disconnected) {
      port.postMessage({
        type: "done",
        filename: filename || "shabi-clip.mp4",
        size: total || offset,
      });
    }
  } catch (err) {
    if (!disconnected) {
      port.postMessage({ type: "error", message: err.message });
    }
  }
}

async function handleMessage(msg, sender) {
  switch (msg?.type) {
    case "PING_SERVER": {
      const hb = await sendHeartbeat();
      let queue = { items: [], by_platform: {} };
      let settings = null;
      if (hb.ok) {
        try {
          [queue, settings] = await Promise.all([
            apiFetch("/api/ext/queue"),
            apiFetch("/api/ext/settings"),
          ]);
        } catch {
          // Non-fatal
        }
      }
      return {
        ...hb,
        queue: queue.items || [],
        by_platform: queue.by_platform || hb.by_platform || {},
        settings,
      };
    }

    case "SAVE_SERVER_URL": {
      const clean = String(msg.serverUrl || DEFAULT_SERVER_URL).trim().replace(/\/+$/, "");
      await chrome.storage.local.set({ serverUrl: clean || DEFAULT_SERVER_URL });
      return await sendHeartbeat();
    }

    case "SAVE_EXT_SETTINGS": {
      const saved = await apiFetch("/api/ext/settings", {
        method: "PUT",
        body: JSON.stringify(msg.settings || {}),
      });
      return { ok: true, settings: saved };
    }

    case "OPEN_EXPORT_FOLDER": {
      return await apiFetch("/api/ext/open-folder", {
        method: "POST",
        body: JSON.stringify({ kind: msg.kind || "export" }),
      });
    }

    case "OPEN_PLATFORM": {
      return await openOrFocusPlatform(msg.platform || "youtube", msg.url || "", true);
    }

    case "OPEN_STUDIO": {
      return await openOrFocusPlatform("youtube", msg.url || "", true);
    }

    case "TRIGGER_PLATFORMS": {
      return await triggerPlatforms(msg.platforms);
    }

    case "TRIGGER_STUDIO": {
      return await triggerPlatforms(["youtube"]);
    }

    case "PROCESS_PENDING": {
      const q = await apiFetch("/api/ext/queue");
      const pendingPlats = [
        ...new Set(
          (q?.items || [])
            .filter((it) => it.status === "pending")
            .map((it) => normPlatform(it.platform))
        ),
      ];
      if (pendingPlats.length === 0) {
        return { ok: true, results: [], empty: true };
      }
      return await triggerPlatforms(pendingPlats);
    }

    case "CHECK_PENDING_JOB": {
      const platform = normPlatform(msg.platform);
      const tabId = sender?.tab?.id ?? null;
      const all = await getActiveJobs();
      const entry = all[platform];

      if (!msg.forceNext && !msg.peek && entry?.job && !TERMINAL_STATUSES.has(entry.job.status)) {
        let sameOrFree = entry.tabId == null || entry.tabId === tabId;
        if (!sameOrFree && entry.tabId != null) {
          const oldTab = await chrome.tabs.get(entry.tabId).catch(() => null);
          if (!oldTab) sameOrFree = true;
        }
        if (sameOrFree) {
          if (tabId != null && entry.tabId !== tabId) {
            await setActiveJob(platform, { job: entry.job, tabId });
          }
          return { ok: true, job: entry.job, resumed: true };
        }
      }

      const qs = new URLSearchParams({ platform });
      if (msg.peek) qs.set("peek", "1");
      const data = await apiFetch(`/api/ext/queue/next?${qs.toString()}`);
      if (data?.item && !msg.peek) {
        await setActiveJob(platform, { job: data.item, tabId });
      }
      return { ok: true, job: data?.item || null, resumed: false };
    }

    case "UPDATE_JOB_STATUS": {
      const { jobId, status, stage, progress, url, error } = msg;
      const res = await apiFetch(`/api/ext/queue/${encodeURIComponent(jobId)}/status`, {
        method: "POST",
        body: JSON.stringify({ status, stage, progress, url, error }),
      });

      const item = res?.item || null;
      const plat = normPlatform(msg.platform || item?.platform);
      const label = PLATFORMS[plat]?.label || "Social";
      const all = await getActiveJobs();
      const entry = all[plat];
      if (entry?.job?.id === jobId) {
        if (TERMINAL_STATUSES.has(status)) {
          await setActiveJob(plat, null);
        } else {
          await setActiveJob(plat, {
            job: item || { ...entry.job, status, stage, progress, url, error },
            tabId: entry.tabId,
          });
        }
      }

      if (status === "posted" || status === "done") {
        await setResultBadge("✓", "#10B981");
        await notifyUser(
          `upfern-posted-${jobId}`,
          `Upfern — ${label} Published!`,
          `${item?.title || "Clip"} is live on ${label}.`
        );
      } else if (status === "ready") {
        await setResultBadge("1", "#92E6EE");
        await notifyUser(
          `upfern-ready-${jobId}`,
          `Upfern — ${label} Ready`,
          `Details filled for "${item?.title || "Clip"}". Review & publish in ${label}.`
        );
      } else if (status === "failed") {
        await setResultBadge("!", "#EF4444");
        await notifyUser(
          `upfern-fail-${jobId}`,
          `Upfern — ${label} Needs Attention`,
          error || `Could not complete ${label} upload.`
        );
      }
      return res;
    }

    case "CLEAR_SESSION_LOCK": {
      if (msg.platform) {
        await setActiveJob(msg.platform, null);
      } else {
        await chrome.storage.session.remove("activeJobs");
      }
      return { ok: true };
    }

    case "RETRY_JOB": {
      const res = await apiFetch(`/api/ext/queue/${encodeURIComponent(msg.jobId)}/retry`, { method: "POST" });
      const plat = normPlatform(msg.platform || res?.item?.platform);
      const all = await getActiveJobs();
      if (all[plat]?.job?.id === msg.jobId) {
        await setActiveJob(plat, null);
      }
      await openOrFocusPlatform(plat, res?.item?.target_url || "", true);
      return res;
    }

    case "CANCEL_JOB": {
      const all = await getActiveJobs();
      for (const [plat, entry] of Object.entries(all)) {
        if (entry?.job?.id === msg.jobId) {
          await setActiveJob(plat, null);
        }
      }
      return await apiFetch(`/api/ext/queue/${encodeURIComponent(msg.jobId)}/cancel`, { method: "POST" });
    }

    case "DELETE_JOB": {
      const all = await getActiveJobs();
      for (const [plat, entry] of Object.entries(all)) {
        if (entry?.job?.id === msg.jobId) {
          await setActiveJob(plat, null);
        }
      }
      return await apiFetch(`/api/ext/queue/${encodeURIComponent(msg.jobId)}`, { method: "DELETE" });
    }

    default:
      return { ok: false, error: "Unknown message type" };
  }
}

// Top-level synchronous listener registrations (MV3 requirement)
chrome.runtime.onInstalled.addListener(async () => {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  if (!serverUrl || /^http:\/\/(127\.0\.0\.1|localhost):8765\/?$/.test(serverUrl)) {
    await chrome.storage.local.set({ serverUrl: DEFAULT_SERVER_URL });
  }
  await ensureAlarm();
  await sendHeartbeat();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  await sendHeartbeat();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) {
    await sendHeartbeat();
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const all = await getActiveJobs();
  for (const [plat, entry] of Object.entries(all)) {
    if (entry?.tabId !== tabId) continue;
    const job = entry.job;
    await setActiveJob(plat, null);
    if (job && (job.status === "uploading" || job.status === "filling")) {
      const label = PLATFORMS[plat]?.label || plat;
      try {
        await apiFetch(`/api/ext/queue/${encodeURIComponent(job.id)}/status`, {
          method: "POST",
          body: JSON.stringify({ status: "pending", stage: `Tab closed — ready to resume in ${label}` }),
        });
      } catch {
        // Server might be closed
      }
    }
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      const result = await handleMessage(message, sender);
      sendResponse(result);
    } catch (err) {
      sendResponse({ ok: false, error: err.message });
    }
  })();
  return true;
});

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "shabi-video-stream") return;
  port.onMessage.addListener((msg) => {
    if (msg?.type === "START_STREAM" && msg.jobId) {
      streamVideoOverPort(port, msg.jobId, msg.filename);
    }
  });
});
