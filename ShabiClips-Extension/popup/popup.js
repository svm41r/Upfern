const $ = (id) => document.getElementById(id);

const PLATFORM_NAMES = {
  youtube: "YouTube",
  tiktok: "TikTok",
  instagram: "Instagram",
  facebook: "Facebook",
};

function statusClass(st) {
  if (st === "posted") return "ok";
  if (st === "failed" || st === "cancelled") return "bad";
  return "warn";
}

function updateCounts(byPlat = {}) {
  let total = 0;
  for (const p of Object.keys(PLATFORM_NAMES)) {
    const el = $(`cnt-${p}`);
    if (!el) continue;
    const n = Number(byPlat[p] || 0);
    total += n;
    el.hidden = n <= 0;
    el.textContent = n > 9 ? "9+" : String(n);
  }
  const sub = $("heroSub");
  if (sub) {
    sub.textContent = total > 0
      ? `${total} clip upload${total === 1 ? "" : "s"} queued & ready`
      : "YouTube · TikTok · Instagram · Facebook";
  }
}

function renderQueue(items) {
  const list = $("queueList");
  list.innerHTML = "";
  if (!items || items.length === 0) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "No queued clips yet. Click ⚡ Post All 4 in Upfern!";
    list.appendChild(p);
    return;
  }

  for (const item of items.slice(0, 14)) {
    const plat = PLATFORM_NAMES[item.platform] ? item.platform : "youtube";
    const row = document.createElement("div");
    row.className = "qitem";

    const top = document.createElement("div");
    top.className = "qitem-top";

    const pBadge = document.createElement("span");
    pBadge.className = `plat-pill plat-${plat}`;
    pBadge.textContent = PLATFORM_NAMES[plat];

    const title = document.createElement("span");
    title.className = "qitem-title";
    title.textContent = item.title || item.filename || "Clip";

    const pill = document.createElement("span");
    pill.className = `pill ${statusClass(item.status)}`;
    pill.textContent = item.status;

    top.append(pBadge, title, pill);

    const sub = document.createElement("div");
    sub.className = "qitem-sub";
    sub.textContent = item.error || item.stage || item.export_path || "";

    const acts = document.createElement("div");
    acts.className = "qitem-acts";

    if (item.url) {
      const link = document.createElement("a");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Open Post ↗";
      acts.appendChild(link);
    }

    if (item.status === "failed" || item.status === "cancelled") {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn sm";
      retry.textContent = "Retry";
      retry.addEventListener("click", async () => {
        await chrome.runtime.sendMessage({ type: "RETRY_JOB", jobId: item.id, platform: plat });
        await refreshState();
      });
      acts.appendChild(retry);
    }

    if (item.status === "pending" || item.status === "uploading" || item.status === "filling" || item.status === "ready") {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn sm";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", async () => {
        await chrome.runtime.sendMessage({ type: "CANCEL_JOB", jobId: item.id });
        await refreshState();
      });
      acts.appendChild(cancel);
    }

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn sm";
    del.textContent = "✕";
    del.title = "Remove from queue";
    del.addEventListener("click", async () => {
      await chrome.runtime.sendMessage({ type: "DELETE_JOB", jobId: item.id });
      await refreshState();
    });
    acts.appendChild(del);

    row.append(top, sub);
    if (acts.childElementCount > 0) row.appendChild(acts);
    list.appendChild(row);
  }
}

async function refreshState() {
  const { serverUrl = "http://127.0.0.1:8767" } = await chrome.storage.local.get("serverUrl");
  $("serverUrl").value = serverUrl;

  const pill = $("connPill");
  try {
    const res = await chrome.runtime.sendMessage({ type: "PING_SERVER" });
    if (res?.ok) {
      pill.className = "pill ok";
      pill.textContent = "Connected ✓";
      $("exportPath").textContent = res.export_dir || res.settings?.export_dir || "Documents\\Upfern";
      updateCounts(res.by_platform || {});
      if (res.settings) {
        $("privacySel").value = res.settings.privacy || "public";
        $("channelUrl").value = res.settings.channel_url || "";
        $("addShortsChk").checked = res.settings.add_shorts_tag !== false;
        $("notKidsChk").checked = !res.settings.made_for_kids;
        $("autoPubChk").checked = res.settings.auto_publish !== false;
      }
      renderQueue(res.queue || []);
    } else {
      pill.className = "pill bad";
      pill.textContent = "App Offline";
      updateCounts({});
      $("queueList").innerHTML = `<p class="empty">Start Upfern first, then click Connect.</p>`;
    }
  } catch (err) {
    pill.className = "pill bad";
    pill.textContent = "Offline";
    updateCounts({});
    const list = $("queueList");
    list.innerHTML = "";
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = err.message;
    list.appendChild(p);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("saveUrlBtn").addEventListener("click", async () => {
    await chrome.runtime.sendMessage({ type: "SAVE_SERVER_URL", serverUrl: $("serverUrl").value });
    await refreshState();
  });

  $("openFolderBtn").addEventListener("click", async () => {
    await chrome.runtime.sendMessage({ type: "OPEN_EXPORT_FOLDER", kind: "export" });
  });

  document.querySelectorAll(".plat-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await chrome.runtime.sendMessage({ type: "OPEN_PLATFORM", platform: btn.dataset.plat });
      window.close();
    });
  });

  $("processBtn").addEventListener("click", async () => {
    const res = await chrome.runtime.sendMessage({ type: "PROCESS_PENDING" });
    if (res?.empty) {
      $("saveMsg").textContent = "No pending items";
      setTimeout(() => { $("saveMsg").textContent = ""; }, 2000);
    } else {
      window.close();
    }
  });

  $("saveSettingsBtn").addEventListener("click", async () => {
    const res = await chrome.runtime.sendMessage({
      type: "SAVE_EXT_SETTINGS",
      settings: {
        privacy: $("privacySel").value,
        channel_url: $("channelUrl").value.trim(),
        add_shorts_tag: $("addShortsChk").checked,
        made_for_kids: !$("notKidsChk").checked,
        auto_publish: $("autoPubChk").checked,
      },
    });
    $("saveMsg").textContent = res?.ok ? "Saved ✓" : (res?.error || "Error");
    setTimeout(() => { $("saveMsg").textContent = ""; }, 2000);
    await refreshState();
  });

  $("refreshBtn").addEventListener("click", refreshState);
  refreshState();
});
