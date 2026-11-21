(() => {
  if (window.__shabiYouTubeInjected) return;
  window.__shabiYouTubeInjected = true;

  const S = window.ShabiExt;
  const PLATFORM = "youtube";
  const STEPS = [
    { key: "export", label: "Clip saved to Documents folder" },
    { key: "dialog", label: "Open YouTube Studio uploader" },
    { key: "stream", label: "Attach exported MP4 video" },
    { key: "meta", label: "Set Title, Description & #Shorts" },
    { key: "audience", label: "Set Audience & Video Tags" },
    { key: "visibility", label: "Advance to Visibility & Privacy" },
    { key: "publish", label: "Ready / Publish on YouTube" },
  ];

  let activeRunId = 0;
  let currentJob = null;
  let hud = null;

  function ensureHud() {
    if (hud && hud.el?.isConnected) return hud;
    hud = S.createHud({
      platform: PLATFORM,
      platformLabel: "YouTube Shorts",
      steps: STEPS,
      onRefill: async (h) => {
        if (!currentJob) return;
        try {
          await fillMetadataAndAdvance(currentJob, activeRunId, false, h);
        } catch (err) {
          h.setStatus(err.message, true);
        }
      },
      onReattach: async (h) => {
        if (!currentJob) return;
        try {
          await attachVideoStream(currentJob, activeRunId, h);
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

  async function ensureUploadDialogOpen(runId, h) {
    h.setStep("dialog", "active");
    h.setStatus("Opening YouTube Studio upload dialog…");
    const cancelled = () => runId !== activeRunId;

    const existingInput =
      S.queryDeep('ytcp-uploads-dialog input[type="file"]') ||
      S.queryDeep('input[type="file"][name="Filedata"]');
    if (existingInput) {
      h.setStep("dialog", "done");
      return existingInput;
    }

    const uploadIcon = S.queryDeep("#upload-icon");
    if (uploadIcon && S.isVisible(uploadIcon)) {
      uploadIcon.click();
      const inp = await S.waitForDeep('ytcp-uploads-dialog input[type="file"], input[type="file"]', 4500, null, cancelled);
      if (inp) {
        h.setStep("dialog", "done");
        return inp;
      }
    }

    const createBtn =
      S.queryDeep("#create-icon") ||
      S.queryDeep('ytcp-button[id="create-icon"]') ||
      S.queryDeep('button[aria-label="Create"]');
    if (createBtn && S.isVisible(createBtn)) {
      createBtn.click();
      await S.sleep(450);
      const item =
        S.queryDeep('[test-id="upload-videos-menu-item"]') ||
        S.queryDeep("#text-item-0") ||
        S.queryAllDeep("tp-yt-paper-item, ytcp-ve").find(
          (el) => /upload video/i.test(el.textContent || "") && S.isVisible(el)
        );
      if (item) {
        item.click();
        const inp = await S.waitForDeep('ytcp-uploads-dialog input[type="file"], input[type="file"]', 5000, null, cancelled);
        if (inp) {
          h.setStep("dialog", "done");
          return inp;
        }
      }
    }

    const m = location.pathname.match(/\/channel\/(UC[\w-]+|[\w-]+)/);
    if (m && !location.search.includes("d=ud")) {
      const target = `https://studio.youtube.com/channel/${m[1]}/videos/upload?d=ud`;
      h.setStatus("Navigating to channel upload dialog…");
      location.href = target;
      await S.sleep(10000);
      throw new Error("Redirecting to upload dialog…");
    }

    const lateInp = await S.waitForDeep('ytcp-uploads-dialog input[type="file"], input[type="file"]', 6000, null, cancelled);
    if (lateInp) {
      h.setStep("dialog", "done");
      return lateInp;
    }
    throw new Error("Could not find YouTube Studio upload input. Click Create → Upload videos once.");
  }

  async function attachVideoStream(job, runId, h) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("stream", "active");
    h.setProgress(12);
    await S.reportJob(PLATFORM, job.id, { status: "uploading", stage: "Streaming clip into YouTube Studio", progress: 15 });

    const fileInput = await ensureUploadDialogOpen(runId, h);
    const file = await S.streamClipFile(job, cancelled, (pct, loaded) => {
      h.setProgress(pct);
      h.setStatus(`Streaming MP4 to YouTube Studio (${Math.round(loaded / 1048576)} MB)…`);
    });

    const dt = S.attachFileToInput(fileInput, file);
    h.setProgress(85);
    h.setStatus("Video attached! Waiting for YouTube Studio Details form…");

    let titleBox = await S.waitForDeep("#title-textarea", 4000, S.isVisible, cancelled);
    if (!titleBox) {
      const dropTarget =
        S.queryDeep("#drop-target") ||
        S.queryDeep("ytcp-uploads-file-picker") ||
        S.queryDeep("ytcp-uploads-dialog");
      if (dropTarget) {
        try {
          dropTarget.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
        } catch {
          // ignore
        }
      }
      titleBox = await S.waitForDeep("#title-textarea", 15000, S.isVisible, cancelled);
    }

    if (!titleBox) {
      throw new Error("YouTube Studio Details form did not appear after attaching video.");
    }

    h.setStep("stream", "done");
    return titleBox;
  }

  async function fillVideoTags(tags, runId) {
    if (!Array.isArray(tags) || tags.length === 0) return;
    const cancelled = () => runId !== activeRunId;

    let tagsInput =
      S.queryDeep("#tags-container input#text-input") ||
      S.queryDeep("ytcp-free-text-chip-bar input#text-input");
    if (!tagsInput || !S.isVisible(tagsInput)) {
      const showMore =
        S.queryDeep("#toggle-button") ||
        S.queryAllDeep("ytcp-button").find((b) => /show more/i.test(b.textContent || "") && S.isVisible(b));
      if (showMore) {
        showMore.click();
        await S.sleep(500);
      }
      tagsInput = await S.waitForDeep(
        "#tags-container input#text-input, ytcp-free-text-chip-bar input#text-input, input[aria-label*='Tags' i]",
        4000,
        S.isVisible,
        cancelled
      );
    }

    if (!tagsInput) return;
    tagsInput.scrollIntoView({ block: "center" });
    await S.sleep(150);
    tagsInput.focus();
    const joined = tags.join(", ");
    tagsInput.value = joined;
    tagsInput.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: joined }));
    tagsInput.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    tagsInput.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, composed: true })
    );
    tagsInput.dispatchEvent(
      new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, composed: true })
    );
    await S.sleep(250);
  }

  async function selectAudience(madeForKids, runId) {
    const cancelled = () => runId !== activeRunId;
    const name = madeForKids ? "VIDEO_MADE_FOR_KIDS_MFK" : "VIDEO_MADE_FOR_KIDS_NOT_MFK";
    const radio = await S.waitForDeep(`tp-yt-paper-radio-button[name="${name}"]`, 5000, S.isVisible, cancelled);
    if (radio) {
      radio.scrollIntoView({ block: "center" });
      radio.click();
      await S.sleep(200);
      if (radio.getAttribute("aria-checked") !== "true") {
        const offRadio = S.queryDeep("#offRadio", radio) || S.queryDeep("#radioContainer", radio);
        offRadio?.click();
      }
    }
  }

  function findPublishedUrl() {
    const link = S.queryAllDeep('a[href*="youtu.be/"], a[href*="youtube.com/shorts/"], a[href*="youtube.com/watch"]').find(
      (a) => S.isVisible(a)
    );
    if (link?.href) return link.href;
    const shareText = S.queryDeep("#share-url")?.textContent?.trim();
    if (shareText && /^https?:\/\//.test(shareText)) return shareText;
    return "";
  }

  function watchUserPublish(job, h) {
    const doneBtn = S.queryDeep("#done-button");
    if (!doneBtn) return;
    doneBtn.classList.add("shabi-publish-glow");
    doneBtn.addEventListener(
      "click",
      async () => {
        doneBtn.classList.remove("shabi-publish-glow");
        h.setStatus("Publish clicked! Waiting for YouTube link…");
        let url = "";
        for (let i = 0; i < 20; i++) {
          await S.sleep(750);
          url = findPublishedUrl();
          if (url) break;
        }
        h.setStep("publish", "done");
        h.setProgress(100);
        h.setStatus(url ? `Published! ${url}` : "Published on YouTube!", false, true);
        await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Published to YouTube", progress: 100, url });
        h.showNext();
      },
      { once: true }
    );
  }

  async function fillMetadataAndAdvance(job, runId, advanceSteps = true, h = ensureHud()) {
    const cancelled = () => runId !== activeRunId;
    h.setStep("meta", "active");
    h.setStatus("Waiting for YouTube Studio title field to stabilize…");
    await S.reportJob(PLATFORM, job.id, { status: "filling", stage: "Filling Title, Description & #Shorts", progress: 88 });

    const titleBox = await S.waitForDeep("#title-textarea", 12000, S.isVisible, cancelled);
    if (!titleBox) throw new Error("Title field not found.");

    // Wait so YouTube Studio finishes auto-populating filename into Title before overwriting
    await S.sleep(1200);
    if (cancelled()) throw new Error("Cancelled");

    await S.setEditableText(titleBox, job.title || "Short Clip");
    await S.sleep(750);
    if (S.getEditableText(titleBox) !== String(job.title || "Short Clip").trim()) {
      await S.setEditableText(titleBox, job.title || "Short Clip");
      await S.sleep(300);
    }

    const descBox = S.queryDeep("#description-textarea");
    if (descBox) {
      await S.setEditableText(descBox, job.description || "#Shorts");
      await S.sleep(250);
    }
    h.setStep("meta", "done");

    h.setStep("audience", "active");
    h.setStatus("Setting Audience & Video Tags…");
    await selectAudience(Boolean(job.made_for_kids), runId);
    await fillVideoTags(job.tags || [], runId);
    h.setStep("audience", "done");

    if (!advanceSteps) {
      h.setStatus("Details re-filled!", false, true);
      return;
    }

    h.setStep("visibility", "active");
    h.setStatus("Advancing through Video Elements & Checks to Visibility…");

    const privName = String(job.privacy || "public").toUpperCase();
    for (let stepIdx = 0; stepIdx < 4; stepIdx++) {
      const privRadio = S.queryDeep(`tp-yt-paper-radio-button[name="${privName}"]`);
      if (privRadio && S.isVisible(privRadio)) break;
      const nextBtn = S.queryDeep("#next-button");
      if (!nextBtn || !S.isVisible(nextBtn)) break;
      nextBtn.click();
      await S.sleep(850);
      if (cancelled()) throw new Error("Cancelled");
    }

    const targetPriv = await S.waitForDeep(`tp-yt-paper-radio-button[name="${privName}"]`, 6000, S.isVisible, cancelled);
    if (targetPriv) {
      targetPriv.scrollIntoView({ block: "center" });
      targetPriv.click();
      await S.sleep(300);
      if (targetPriv.getAttribute("aria-checked") !== "true") {
        S.queryDeep("#radioContainer", targetPriv)?.click();
      }
    }
    h.setStep("visibility", "done");

    h.setStep("publish", "active");
    h.setProgress(96);

    if (job.auto_publish) {
      h.setStatus("Waiting for YouTube checks before auto-publishing…");
      const doneBtn = await S.waitForDeep(
        "#done-button",
        180000,
        (el) => S.isVisible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true",
        cancelled
      );
      if (!doneBtn) throw new Error("Publish button stayed disabled too long. Please publish manually.");
      doneBtn.click();
      await S.sleep(1600);
      // Confirm secondary SD checks warning dialog if YouTube opens it
      const warnBtn = S.queryDeep("#confirm-button ytcp-button, #confirm-button") ||
        S.findButtonByText(/^Publish anyway$/i, "ytcp-button, button");
      if (warnBtn && S.isVisible(warnBtn)) {
        warnBtn.click();
        await S.sleep(1500);
      }
      let url = "";
      for (let i = 0; i < 16; i++) {
        url = findPublishedUrl();
        if (url) break;
        await S.sleep(750);
      }
      const closeBtn = S.queryDeep("ytcp-video-share-dialog #close-button, #close-button");
      if (closeBtn && S.isVisible(closeBtn)) closeBtn.click();
      h.setStep("publish", "done");
      h.setProgress(100);
      h.setStatus(url ? `Published! ${url}` : "Published to YouTube!", false, true);
      await S.reportJob(PLATFORM, job.id, { status: "posted", stage: "Published to YouTube", progress: 100, url });

      const nextRes = await S.checkPendingJob(PLATFORM, { peek: true }).catch(() => null);
      if (nextRes?.job) {
        setTimeout(() => checkAndStart(true), 1600);
      }
    } else {
      h.setProgress(100);
      h.setStatus(`All settings filled (${privName})! Review & click Publish in YouTube Studio.`, false, true);
      await S.reportJob(PLATFORM, job.id, { status: "ready", stage: "Ready in YouTube Studio — click Publish", progress: 100 });
      watchUserPublish(job, h);
    }
  }

  async function runAutomation(job) {
    const runId = ++activeRunId;
    const cancelled = () => runId !== activeRunId;
    currentJob = job;
    const h = ensureHud();
    h.setJob(job);
    h.setProgress(8);
    h.setStatus("Checking YouTube Studio session…");

    try {
      const ready = await S.waitForDeep(
        "#upload-icon, #create-icon, ytcp-uploads-dialog, ytcp-home-page",
        20000,
        null,
        cancelled
      );
      if (!ready) {
        throw new Error("Sign in to your YouTube channel in this tab, then click Re-attach Video.");
      }

      await attachVideoStream(job, runId, h);
      await fillMetadataAndAdvance(job, runId, true, h);
    } catch (err) {
      if (err.message === "Cancelled" || /Redirecting/.test(err.message)) return;
      h.setStatus(err.message || "Automation paused", true);
      await S.reportJob(PLATFORM, job.id, { status: "failed", stage: "Needs attention in YouTube Studio", error: err.message });
    }
  }

  async function checkAndStart(forceNext = false) {
    try {
      if (location.hostname !== "studio.youtube.com") return;
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

  setTimeout(() => checkAndStart(false), 1200);
})();
