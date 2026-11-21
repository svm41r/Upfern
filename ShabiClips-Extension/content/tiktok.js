(() => {
  if (window.__shabiTikTokInjected) return;
  window.__shabiTikTokInjected = true;

  const S = window.ShabiExt;
  const PLATFORM = "tiktok";
  const STEPS = [
    { key: "export", label: "Clip saved to Documents folder" },
    { key: "upload", label: "Attach exported MP4 to TikTok" },
    { key: "caption", label: "Fill Caption, Hashtags & Privacy" },
    { key: "publish", label: "Ready / Post on TikTok" },
  ];

  let activeRunId = 0;
  let currentJob = null;
  let hud = null;

  function ensureHud() {
    if (hud && hud.el?.isConnected) return hud;
    hud = S.createHud({
      platform: PLATFORM,
      platformLabel: "TikTok",
      steps: STEPS,
      onRefill: async (h) => {
        if (!currentJob) return;
        try {
          await fillDetails(currentJob, activeRunId, h);
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

  function findCaptionEditor() {
    return (
      S.queryDeep('.public-DraftEditor-content[contenteditable="true"]') ||
      S.queryDeep('[data-e2e*="caption" i] [contenteditable="true"]') ||
      S.queryDeep('[aria-label*="Caption" i][contenteditable="true"]') ||
      S.queryDeep('div[role="textbox"][contenteditable="true"]') ||
      S.queryDeep('textarea[placeholder*="caption" i], textarea')
    );
  }

  function findPostButton() {
    return (
      S.queryDeep('button[data-e2e="post_video_button"]') ||
      S.findButtonByText(/^(Post|Publish)$/i)
    );
  }

  async function selectTikTokPrivacy(privacy) {
    const map = { public: /Everyone|Public/i, unlisted: /Friends/i, private: /Only me|Private/i };
    const want = map[String(privacy || "public").toLowerCase()];
    if (!want) return;

    const trigger = S.queryDeep('[data-e2e="privacy_select"], [aria-label*="Who can watch" i], button[role="combobox"]');
    if (trigger && S.isVisible(trigger)) {
      trigger.click();
      await S.sleep(350);
    }
    const opt = S.queryAllDeep('[role="option"], [role="menuitem"], label').find(
      (el) => S.isVisible(el) && want.test(el.innerText || el.textContent || "")
    );
    if (opt) {
      opt.click();
      await S.sleep(200);
    }
  }

  async function attachVideo(job, runId, h) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("upload", "active");
    h.setProgress(12);
    await S.reportJob(PLATFORM, job.id, { status: "uploading", stage: "Streaming clip to TikTok", progress: 18 });

    const fileInput = await S.waitForDeep(
      'input[type="file"][accept*="video"], input[type="file"]',
      18000,
      null,
      cancelled
    );
    if (!fileInput) {
      throw new Error("Could not find TikTok upload box. Make sure you are signed into TikTok Studio.");
    }

    const file = await S.streamClipFile(job, cancelled, (pct, loaded) => {
      h.setProgress(pct);
      h.setStatus(`Streaming MP4 to TikTok (${Math.round(loaded / 1048576)} MB)…`);
    });

    S.attachFileToInput(fileInput, file);
    h.setProgress(84);
    h.setStatus("Video attached! Waiting for TikTok caption editor…");

    const editor = await S.waitForDeep(
      '.public-DraftEditor-content[contenteditable="true"], [data-e2e*="caption" i] [contenteditable="true"], div[role="textbox"][contenteditable="true"]',
      18000,
      S.isVisible,
      cancelled
    );
    if (!editor) {
      throw new Error("TikTok caption editor did not appear after attaching video.");
    }

    h.setStep("upload", "done");
    return editor;
  }

  function watchPostButton(job, h) {
    const btn = findPostButton();
    if (!btn) return;
    btn.classList.add("shabi-publish-glow");
    btn.addEventListener(
      "click",
      async () => {
        btn.classList.remove("shabi-publish-glow");
        h.setStep("publish", "done");
        h.setProgress(100);
        h.setStatus("Posted to TikTok!", false, true);
        await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Posted to TikTok", progress: 100 });
        h.showNext();
      },
      { once: true }
    );
  }

  async function fillDetails(job, runId, h = ensureHud()) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("caption", "active");
    h.setStatus("Filling TikTok caption & hashtags…");
    await S.reportJob(PLATFORM, job.id, { status: "filling", stage: "Filling TikTok caption", progress: 88 });

    await S.sleep(900);
    if (cancelled()) throw new Error("Cancelled");

    const editor = findCaptionEditor();
    if (!editor) throw new Error("TikTok caption box not found.");

    const caption = S.buildSocialCaption(job);
    await S.setEditableText(editor, caption);
    await S.sleep(400);
    await selectTikTokPrivacy(job.privacy);

    h.setStep("caption", "done");
    h.setStep("publish", "active");
    h.setProgress(96);

    if (job.auto_publish) {
      h.setStatus("Waiting for TikTok upload check before auto-posting…");
      const postBtn = await S.waitForDeep(
        'button[data-e2e="post_video_button"], button, [role="button"]',
        180000,
        (el) =>
          el === findPostButton() &&
          S.isVisible(el) &&
          !el.disabled &&
          el.getAttribute("aria-disabled") !== "true",
        cancelled
      );
      if (!postBtn) throw new Error("TikTok Post button stayed disabled. Please review and click Post manually.");
      postBtn.scrollIntoView({ block: "center" });
      postBtn.click();
      await S.sleep(1500);
      const confirmBtn = S.findButtonByText(/^(Post now|Continue to post|Post anyway|Continue)$/i, 'div[role="dialog"] button, button');
      if (confirmBtn && S.isVisible(confirmBtn)) {
        confirmBtn.click();
        await S.sleep(1800);
      }
      h.setStep("publish", "done");
      h.setProgress(100);
      h.setStatus("Posted to TikTok!", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Posted to TikTok", progress: 100 });

      const nextRes = await S.checkPendingJob(PLATFORM, { peek: true }).catch(() => null);
      if (nextRes?.job) setTimeout(() => checkAndStart(true), 1800);
    } else {
      h.setProgress(100);
      h.setStatus("Caption filled! Review & click Post in TikTok.", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "ready", stage: "Ready on TikTok — click Post", progress: 100 });
      watchPostButton(job, h);
    }
  }

  async function runAutomation(job) {
    const runId = ++activeRunId;
    currentJob = job;
    const h = ensureHud();
    h.setJob(job);
    h.setProgress(8);
    h.setStatus("Checking TikTok uploader…");

    try {
      await attachVideo(job, runId, h);
      await fillDetails(job, runId, h);
    } catch (err) {
      if (err.message === "Cancelled") return;
      h.setStatus(err.message || "TikTok automation paused", true);
      await S.reportJob(PLATFORM, job.id, { status: "failed", stage: "Needs attention on TikTok", error: err.message });
    }
  }

  async function checkAndStart(forceNext = false) {
    try {
      if (!location.hostname.endsWith("tiktok.com")) return;
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
