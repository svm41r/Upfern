(() => {
  if (window.__shabiInstagramInjected) return;
  window.__shabiInstagramInjected = true;

  const S = window.ShabiExt;
  const PLATFORM = "instagram";
  const STEPS = [
    { key: "export", label: "Clip saved to Documents folder" },
    { key: "modal", label: "Open Instagram Create dialog" },
    { key: "upload", label: "Attach exported MP4 Reel" },
    { key: "caption", label: "Advance to Share & fill Caption" },
    { key: "publish", label: "Ready / Share Reel" },
  ];

  let activeRunId = 0;
  let currentJob = null;
  let hud = null;

  function ensureHud() {
    if (hud && hud.el?.isConnected) return hud;
    hud = S.createHud({
      platform: PLATFORM,
      platformLabel: "Instagram Reels",
      steps: STEPS,
      onRefill: async (h) => {
        if (!currentJob) return;
        try {
          await advanceAndFillCaption(currentJob, activeRunId, false, h);
        } catch (err) {
          h.setStatus(err.message, true);
        }
      },
      onReattach: async (h) => {
        if (!currentJob) return;
        try {
          await attachVideo(currentJob, activeRunId, h);
        } catch (err) {
          h.setStatus(err.message, true);
        }
      },
      onNext: () => checkAndStart(true),
      onCancel: async (h) => {
        activeRunId++;
        const jid = currentJob?.id;
        currentJob = null;
        if (jid) {
          await chrome.runtime.sendMessage({ type: "CANCEL_JOB", jobId: jid }).catch(() => {});
        } else {
          await chrome.runtime.sendMessage({ type: "CLEAR_SESSION_LOCK", platform: PLATFORM }).catch(() => {});
        }
        h.destroy();
        hud = null;
      },
      onClose: () => {
        activeRunId++;
        hud = null;
      },
    });
    return hud;
  }

  async function openCreateDialog(runId, h) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("modal", "active");
    h.setStatus("Opening Instagram Create dialog…");

    const existing = S.queryDeep('div[role="dialog"] input[type="file"]');
    if (existing) {
      h.setStep("modal", "done");
      return existing;
    }

    const createIcon = S.queryDeep('svg[aria-label="New post"], svg[aria-label="Create"]');
    const createBtn =
      createIcon?.closest('a, button, div[role="button"]') ||
      S.findButtonByText(/^Create$/i, 'a, button, [role="button"], span');
    if (createBtn) {
      createBtn.click();
      await S.sleep(600);
      if (cancelled()) throw new Error("Cancelled");
    }

    // Instagram sometimes shows a submenu ("Post" vs "Live video" / "AI")
    const postItem = S.queryAllDeep('[role="menuitem"], a, button, span').find((el) => {
      if (!S.isVisible(el)) return false;
      const t = (el.innerText || el.textContent || "").trim();
      return /^Post$/i.test(t) || /^Reel$/i.test(t);
    });
    if (postItem && !S.queryDeep('div[role="dialog"] input[type="file"]')) {
      postItem.click();
      await S.sleep(600);
    }

    const fileInput = await S.waitForDeep(
      'div[role="dialog"] input[type="file"], input[type="file"][accept*="video"]',
      12000,
      null,
      cancelled
    );
    if (!fileInput) {
      throw new Error("Could not open Instagram Create modal. Sign in to Instagram first, then click Re-attach Video.");
    }
    h.setStep("modal", "done");
    return fileInput;
  }

  async function attachVideo(job, runId, h) {
    const cancelled = () => runId !== activeRunId;
    const fileInput = await openCreateDialog(runId, h);

    h.setStep("upload", "active");
    h.setProgress(15);
    await S.reportJob(PLATFORM, job.id, { status: "uploading", stage: "Streaming clip to Instagram", progress: 20 });

    const file = await S.streamClipFile(job, cancelled, (pct, loaded) => {
      h.setProgress(pct);
      h.setStatus(`Streaming MP4 to Instagram (${Math.round(loaded / 1048576)} MB)…`);
    });

    S.attachFileToInput(fileInput, file);
    h.setProgress(82);
    h.setStatus("Video attached! Advancing Instagram steps…");
    await S.sleep(1200);

    // Dismiss "Video posts are now shared as reels" popup if present
    const okBtn = S.findButtonByText(/^OK$/i, 'div[role="dialog"] button, button');
    if (okBtn) {
      okBtn.click();
      await S.sleep(500);
    }

    h.setStep("upload", "done");
  }

  function findShareButton() {
    return S.findButtonByText(/^Share$/i, 'div[role="dialog"] button, div[role="dialog"] [role="button"], button');
  }

  function watchShareButton(job, h) {
    const btn = findShareButton();
    if (!btn) return;
    btn.classList.add("shabi-publish-glow");
    btn.addEventListener(
      "click",
      async () => {
        btn.classList.remove("shabi-publish-glow");
        h.setStep("publish", "done");
        h.setProgress(100);
        h.setStatus("Shared to Instagram Reels!", false, true);
        await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Shared on Instagram", progress: 100 });
        h.showNext();
      },
      { once: true }
    );
  }

  async function advanceAndFillCaption(job, runId, advanceSteps = true, h = ensureHud()) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("caption", "active");
    await S.reportJob(PLATFORM, job.id, { status: "filling", stage: "Preparing Instagram Reel caption", progress: 88 });

    if (advanceSteps) {
      // Advance Crop -> Trim/Cover -> Caption steps using "Next"
      for (let i = 0; i < 3; i++) {
        const capBox = S.queryDeep(
          'div[role="dialog"] div[aria-label*="Write a caption" i][contenteditable="true"], div[role="dialog"] textarea[aria-label*="Write a caption" i]'
        );
        if (capBox && S.isVisible(capBox)) break;

        const okBtn = S.findButtonByText(/^OK$/i, 'div[role="dialog"] button');
        if (okBtn) {
          okBtn.click();
          await S.sleep(450);
        }

        const nextBtn = S.findButtonByText(/^Next$/i, 'div[role="dialog"] button, div[role="dialog"] [role="button"]');
        if (nextBtn) {
          nextBtn.click();
          await S.sleep(1000);
        } else {
          await S.sleep(700);
        }
        if (cancelled()) throw new Error("Cancelled");
      }
    }

    const capBox = await S.waitForDeep(
      'div[role="dialog"] div[aria-label*="Write a caption" i][contenteditable="true"], div[role="dialog"] div[role="textbox"][contenteditable="true"], div[role="dialog"] textarea',
      12000,
      S.isVisible,
      cancelled
    );
    if (!capBox) {
      throw new Error("Instagram caption box not found. Click Next until the caption screen appears, then click Re-fill Details.");
    }

    await S.setEditableText(capBox, S.buildSocialCaption(job));
    await S.sleep(300);
    h.setStep("caption", "done");

    h.setStep("publish", "active");
    h.setProgress(96);

    if (job.auto_publish) {
      h.setStatus("Waiting for Instagram Share button…");
      const shareBtn = await S.waitForDeep(
        'div[role="dialog"] button, div[role="dialog"] [role="button"], button',
        90000,
        (el) =>
          el === findShareButton() &&
          S.isVisible(el) &&
          !el.disabled &&
          el.getAttribute("aria-disabled") !== "true",
        cancelled
      );
      if (!shareBtn) throw new Error("Instagram Share button not found. Click Share manually.");
      shareBtn.click();
      await S.sleep(3000);
      h.setStep("publish", "done");
      h.setProgress(100);
      h.setStatus("Shared on Instagram!", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Shared on Instagram", progress: 100 });

      const nextRes = await S.checkPendingJob(PLATFORM, { peek: true }).catch(() => null);
      if (nextRes?.job) setTimeout(() => checkAndStart(true), 2200);
    } else {
      h.setProgress(100);
      h.setStatus("Reel caption filled! Review & click Share.", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "ready", stage: "Ready on Instagram — click Share", progress: 100 });
      watchShareButton(job, h);
    }
  }

  async function runAutomation(job) {
    const runId = ++activeRunId;
    currentJob = job;
    const h = ensureHud();
    h.setJob(job);
    h.setProgress(8);
    h.setStatus("Starting Instagram Reel upload…");

    try {
      await attachVideo(job, runId, h);
      await advanceAndFillCaption(job, runId, true, h);
    } catch (err) {
      if (err.message === "Cancelled") return;
      h.setStatus(err.message || "Instagram automation paused", true);
      await S.reportJob(PLATFORM, job.id, { status: "failed", stage: "Needs attention on Instagram", error: err.message });
    }
  }

  async function checkAndStart(forceNext = false) {
    try {
      if (!location.hostname.endsWith("instagram.com")) return;
      // CRITICAL: Peek first so normal Instagram browsing never mounts a HUD or claims anything
      if (!forceNext) {
        const peeked = await S.checkPendingJob(PLATFORM, { peek: true }).catch(() => null);
        if (!peeked?.job) return;
      }
      const res = await S.checkPendingJob(PLATFORM, { forceNext });
      if (res?.job) {
        if (currentJob && currentJob.id === res.job.id && !forceNext && hud) return;
        await runAutomation(res.job);
      }
    } catch {
      // Server may not be running
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "WAKE_CHECK" && (!msg.platform || msg.platform === PLATFORM)) {
      checkAndStart(false);
      sendResponse({ ok: true });
    }
  });

  setTimeout(() => checkAndStart(false), 1500);
})();
