(() => {
  if (window.__shabiFacebookInjected) return;
  window.__shabiFacebookInjected = true;

  const S = window.ShabiExt;
  const PLATFORM = "facebook";
  const STEPS = [
    { key: "export", label: "Clip saved to Documents folder" },
    { key: "upload", label: "Attach exported MP4 to Facebook" },
    { key: "caption", label: "Advance & fill Reel Description" },
    { key: "publish", label: "Ready / Publish Reel" },
  ];

  let activeRunId = 0;
  let currentJob = null;
  let hud = null;

  function ensureHud() {
    if (hud && hud.el?.isConnected) return hud;
    hud = S.createHud({
      platform: PLATFORM,
      platformLabel: "Facebook Reels",
      steps: STEPS,
      onRefill: async (h) => {
        if (!currentJob) return;
        try {
          await fillDescriptionAndAdvance(currentJob, activeRunId, false, h);
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

  function findDescBox() {
    return (
      S.queryDeep('[aria-label*="Describe your reel" i]') ||
      S.queryDeep('[aria-label*="Write a description" i]') ||
      S.queryDeep('div[role="textbox"][contenteditable="true"]') ||
      S.queryDeep('textarea[placeholder*="Describe" i], textarea')
    );
  }

  function findPublishButton() {
    return S.findButtonByText(/^(Publish|Share|Post)$/i);
  }

  async function attachVideo(job, runId, h) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("upload", "active");
    h.setProgress(15);
    await S.reportJob(PLATFORM, job.id, { status: "uploading", stage: "Streaming clip to Facebook Reels", progress: 20 });

    const fileInput = await S.waitForDeep(
      'input[type="file"][accept*="video"], input[type="file"]',
      18000,
      null,
      cancelled
    );
    if (!fileInput) {
      throw new Error("Could not find Facebook Reel upload input. Sign into Facebook first.");
    }

    const file = await S.streamClipFile(job, cancelled, (pct, loaded) => {
      h.setProgress(pct);
      h.setStatus(`Streaming MP4 to Facebook (${Math.round(loaded / 1048576)} MB)…`);
    });

    S.attachFileToInput(fileInput, file);
    h.setProgress(82);
    h.setStatus("Video attached! Waiting for Facebook Reel composer…");
    await S.sleep(1500);
    h.setStep("upload", "done");
  }

  function watchPublishButton(job, h) {
    const btn = findPublishButton();
    if (!btn) return;
    btn.classList.add("shabi-publish-glow");
    btn.addEventListener(
      "click",
      async () => {
        btn.classList.remove("shabi-publish-glow");
        h.setStep("publish", "done");
        h.setProgress(100);
        h.setStatus("Published to Facebook Reels!", false, true);
        await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Published to Facebook Reels", progress: 100 });
        h.showNext();
      },
      { once: true }
    );
  }

  async function fillDescriptionAndAdvance(job, runId, advanceSteps = true, h = ensureHud()) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("caption", "active");
    await S.reportJob(PLATFORM, job.id, { status: "filling", stage: "Advancing Facebook Reel composer", progress: 88 });

    if (advanceSteps) {
      for (let i = 0; i < 3; i++) {
        const box = findDescBox();
        if (box && S.isVisible(box)) break;
        const nextBtn = S.findButtonByText(/^Next$/i);
        if (nextBtn) {
          nextBtn.click();
          await S.sleep(1000);
        } else {
          await S.sleep(700);
        }
        if (cancelled()) throw new Error("Cancelled");
      }
    }

    const descBox = await S.waitForDeep(
      '[aria-label*="Describe your reel" i], [aria-label*="Write a description" i], div[role="textbox"][contenteditable="true"], textarea',
      15000,
      S.isVisible,
      cancelled
    );
    if (!descBox) {
      throw new Error("Facebook Reel description box not found. Advance to the last step and click Re-fill Details.");
    }

    await S.setEditableText(descBox, S.buildSocialCaption(job));
    await S.sleep(300);
    h.setStep("caption", "done");

    h.setStep("publish", "active");
    h.setProgress(96);

    if (job.auto_publish) {
      h.setStatus("Waiting for Facebook upload before auto-publishing…");
      const pubBtn = await S.waitForDeep(
        'button, [role="button"]',
        120000,
        (el) =>
          el === findPublishButton() &&
          S.isVisible(el) &&
          !el.disabled &&
          el.getAttribute("aria-disabled") !== "true",
        cancelled
      );
      if (!pubBtn) throw new Error("Facebook Publish button stayed disabled. Click Publish manually.");
      pubBtn.scrollIntoView({ block: "center" });
      pubBtn.click();
      await S.sleep(2400);
      h.setStep("publish", "done");
      h.setProgress(100);
      h.setStatus("Published to Facebook Reels!", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Published to Facebook Reels", progress: 100 });

      const nextRes = await S.checkPendingJob(PLATFORM, { peek: true }).catch(() => null);
      if (nextRes?.job) setTimeout(() => checkAndStart(true), 2000);
    } else {
      h.setProgress(100);
      h.setStatus("Description filled! Review & click Publish in Facebook Reels.", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "ready", stage: "Ready on Facebook Reels — click Publish", progress: 100 });
      watchPublishButton(job, h);
    }
  }

  async function runAutomation(job) {
    const runId = ++activeRunId;
    currentJob = job;
    const h = ensureHud();
    h.setJob(job);
    h.setProgress(8);
    h.setStatus("Starting Facebook Reels composer…");

    try {
      await attachVideo(job, runId, h);
      await fillDescriptionAndAdvance(job, runId, true, h);
    } catch (err) {
      if (err.message === "Cancelled") return;
      h.setStatus(err.message || "Facebook automation paused", true);
      await S.reportJob(PLATFORM, job.id, { status: "failed", stage: "Needs attention on Facebook", error: err.message });
    }
  }

  async function checkAndStart(forceNext = false) {
    try {
      if (!location.hostname.endsWith("facebook.com")) return;
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

  setTimeout(() => checkAndStart(false), 1400);
})();
