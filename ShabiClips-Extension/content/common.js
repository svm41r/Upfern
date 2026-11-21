(() => {
  if (window.ShabiExt) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function queryDeep(selector, root = document) {
    if (!root) return null;
    try {
      const direct = root.querySelector(selector);
      if (direct) return direct;
    } catch {
      return null;
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = root;
    while (node) {
      if (node.shadowRoot) {
        const hit = queryDeep(selector, node.shadowRoot);
        if (hit) return hit;
      }
      if (node.tagName === "IFRAME") {
        try {
          const doc = node.contentDocument;
          if (doc) {
            const hit = queryDeep(selector, doc);
            if (hit) return hit;
          }
        } catch {
          // cross-origin iframe
        }
      }
      node = walker.nextNode();
    }
    return null;
  }

  function queryAllDeep(selector, root = document, out = []) {
    if (!root) return out;
    try {
      out.push(...root.querySelectorAll(selector));
    } catch {
      // ignore invalid selector
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = root;
    while (node) {
      if (node.shadowRoot) {
        queryAllDeep(selector, node.shadowRoot, out);
      }
      if (node.tagName === "IFRAME") {
        try {
          const doc = node.contentDocument;
          if (doc) queryAllDeep(selector, doc, out);
        } catch {
          // cross-origin iframe
        }
      }
      node = walker.nextNode();
    }
    return out;
  }

  async function waitForDeep(selector, timeoutMs = 15000, predicate = null, isCancelled = null) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (isCancelled && isCancelled()) throw new Error("Cancelled");
      const matches = queryAllDeep(selector);
      for (const el of matches) {
        if (!predicate || predicate(el)) return el;
      }
      await sleep(250);
    }
    return null;
  }

  function findButtonByText(regex, selectors = 'button, [role="button"], a[role="button"]', root = document) {
    const candidates = queryAllDeep(selectors, root);
    return candidates.find((el) => {
      if (!isVisible(el)) return false;
      const txt = (el.innerText || el.textContent || el.getAttribute("aria-label") || "").trim();
      return regex.test(txt);
    }) || null;
  }

  function decodeBase64ToUint8(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
      out[i] = bin.charCodeAt(i);
    }
    return out;
  }

  async function streamClipFile(job, isCancelled, onProgress) {
    return new Promise((resolve, reject) => {
      const port = chrome.runtime.connect({ name: "shabi-video-stream" });
      const parts = [];
      let settled = false;

      port.onMessage.addListener((msg) => {
        if (isCancelled && isCancelled()) {
          settled = true;
          port.disconnect();
          return reject(new Error("Cancelled"));
        }
        if (msg.type === "chunk") {
          parts.push(decodeBase64ToUint8(msg.data));
          const pct = msg.total ? Math.round((msg.loaded / msg.total) * 70) + 10 : 40;
          if (onProgress) onProgress(pct, msg.loaded || 0, msg.total || 0);
        } else if (msg.type === "done") {
          settled = true;
          port.disconnect();
          const file = new File(parts, msg.filename || job.filename || "clip.mp4", {
            type: "video/mp4",
            lastModified: Date.now(),
          });
          resolve(file);
        } else if (msg.type === "error") {
          settled = true;
          port.disconnect();
          reject(new Error(msg.message || "Video stream error"));
        }
      });

      port.onDisconnect.addListener(() => {
        if (!settled) {
          reject(new Error("Connection to Shabi Clips interrupted during transfer."));
        }
      });

      port.postMessage({
        type: "START_STREAM",
        jobId: job.id,
        filename: job.filename,
      });
    });
  }

  function attachFileToInput(fileInput, file) {
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    fileInput.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    fileInput.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return dt;
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc?.set) {
      desc.set.call(el, value);
    } else {
      el.value = value;
    }
  }

  async function setEditableText(containerOrEl, value) {
    if (!containerOrEl) return false;
    const editable =
      (containerOrEl.matches?.('[contenteditable="true"], textarea, input') ? containerOrEl : null) ||
      queryDeep('#textbox[contenteditable="true"]', containerOrEl) ||
      queryDeep('[contenteditable="true"]', containerOrEl) ||
      queryDeep("textarea", containerOrEl) ||
      queryDeep("input", containerOrEl);
    if (!editable) return false;

    const str = String(value ?? "");
    editable.scrollIntoView({ block: "center" });
    editable.focus();
    await sleep(80);

    if (editable.tagName === "INPUT" || editable.tagName === "TEXTAREA") {
      setNativeValue(editable, str);
      editable.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: str }));
      editable.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      return true;
    }

    // ContentEditable / Draft.js / Lexical / Polymer rich editors
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(editable);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    await sleep(40);

    let inserted = false;
    try {
      const cd = new DataTransfer();
      cd.setData("text/plain", str);
      const pasteEvt = new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        composed: true,
        clipboardData: cd,
      });
      inserted = !editable.dispatchEvent(pasteEvt);
    } catch {
      inserted = false;
    }

    if (!inserted || editable.textContent.trim() === "") {
      document.execCommand("insertText", false, str);
    }

    if (editable.textContent.trim() !== str.trim()) {
      editable.textContent = str;
      editable.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          composed: true,
          inputType: "insertText",
          data: str,
        })
      );
    }
    editable.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return true;
  }

  function getEditableText(containerOrEl) {
    if (!containerOrEl) return "";
    const editable =
      (containerOrEl.matches?.('[contenteditable="true"], textarea, input') ? containerOrEl : null) ||
      queryDeep('#textbox[contenteditable="true"]', containerOrEl) ||
      queryDeep('[contenteditable="true"]', containerOrEl) ||
      queryDeep("textarea", containerOrEl) ||
      queryDeep("input", containerOrEl);
    if (!editable) return "";
    return (editable.tagName === "INPUT" || editable.tagName === "TEXTAREA"
      ? editable.value
      : editable.textContent || ""
    ).trim();
  }

  function buildSocialCaption(job) {
    if (job?.caption) return String(job.caption).trim();
    const title = String(job?.title || "").replace(/#shorts\b/gi, "").trim();
    const desc = String(job?.description || "").trim();
    if (!desc) return title;
    if (!title || desc.toLowerCase().includes(title.toLowerCase())) return desc;
    return `${title}\n\n${desc}`.slice(0, 2150);
  }

  async function reportJob(platform, jobId, payload) {
    try {
      await chrome.runtime.sendMessage({ type: "UPDATE_JOB_STATUS", platform, jobId, ...payload });
    } catch {
      // non-fatal
    }
  }

  async function checkPendingJob(platform, opts = {}) {
    return await chrome.runtime.sendMessage({
      type: "CHECK_PENDING_JOB",
      platform,
      peek: !!opts.peek,
      forceNext: !!opts.forceNext,
    });
  }

  function createHud({ platform, platformLabel, steps, onRefill, onReattach, onNext, onCancel, onClose }) {
    let hudEl = document.getElementById("shabi-clips-hud");
    if (hudEl) hudEl.remove();

    hudEl = document.createElement("aside");
    hudEl.id = "shabi-clips-hud";
    hudEl.innerHTML = `
      <div class="shabi-hud-head">
        <span class="shabi-hud-dot busy" id="shabi-hud-dot"></span>
        <span class="shabi-hud-brand">upfern</span>
        <span class="shabi-hud-plat plat-${platform}">${platformLabel}</span>
        <button type="button" class="shabi-hud-iconbtn" id="shabi-hud-min" title="Collapse">–</button>
        <button type="button" class="shabi-hud-iconbtn" id="shabi-hud-close" title="Dismiss">✕</button>
      </div>
      <div class="shabi-hud-body">
        <div class="shabi-hud-title" id="shabi-hud-title">Preparing clip…</div>
        <div class="shabi-hud-path">
          <code id="shabi-hud-path">Documents\\Upfern</code>
          <button type="button" class="shabi-hud-iconbtn" id="shabi-hud-copy" title="Copy exported path">⧉</button>
        </div>
        <div class="shabi-hud-bar"><i id="shabi-hud-progress"></i></div>
        <ul class="shabi-hud-steps" id="shabi-hud-steps"></ul>
        <div class="shabi-hud-status" id="shabi-hud-status">Starting automation…</div>
        <div class="shabi-hud-acts">
          <button type="button" class="shabi-hud-btn" id="shabi-hud-refill">Re-fill Details</button>
          <button type="button" class="shabi-hud-btn" id="shabi-hud-reattach">Re-attach Video</button>
          <button type="button" class="shabi-hud-btn primary" id="shabi-hud-next" hidden>Next Clip</button>
          <button type="button" class="shabi-hud-btn danger" id="shabi-hud-cancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(hudEl);

    let currentJob = null;
    const stepStates = {};

    function renderSteps() {
      const ul = hudEl.querySelector("#shabi-hud-steps");
      ul.innerHTML = "";
      for (const st of steps) {
        const state = stepStates[st.key] || "wait";
        const li = document.createElement("li");
        li.className = state === "done" ? "done" : state === "active" ? "active" : state === "fail" ? "fail" : "";
        const icon = state === "done" ? "✓" : state === "active" ? "●" : state === "fail" ? "✕" : "○";
        li.textContent = `${icon} ${st.label}`;
        ul.appendChild(li);
      }
      if (currentJob) {
        hudEl.querySelector("#shabi-hud-title").textContent = currentJob.title || currentJob.filename || "Clip";
        hudEl.querySelector("#shabi-hud-path").textContent = currentJob.export_path || currentJob.filename || "";
      }
    }

    const ctrl = {
      el: hudEl,
      setJob(job) {
        currentJob = job;
        for (const k of Object.keys(stepStates)) delete stepStates[k];
        stepStates.export = "done";
        hudEl.querySelector("#shabi-hud-next").hidden = true;
        renderSteps();
      },
      setStep(key, state) {
        stepStates[key] = state;
        renderSteps();
      },
      setProgress(pct) {
        const bar = hudEl.querySelector("#shabi-hud-progress");
        if (bar) bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      },
      setStatus(text, isErr = false, isDone = false) {
        const st = hudEl.querySelector("#shabi-hud-status");
        const dot = hudEl.querySelector("#shabi-hud-dot");
        st.textContent = text;
        st.className = `shabi-hud-status${isErr ? " err" : ""}`;
        dot.className = `shabi-hud-dot ${isErr ? "err" : isDone ? "ok" : "busy"}`;
      },
      showNext() {
        const b = hudEl.querySelector("#shabi-hud-next");
        if (b) b.hidden = false;
      },
      destroy() {
        hudEl.remove();
      },
    };

    hudEl.querySelector("#shabi-hud-min").addEventListener("click", () => {
      hudEl.classList.toggle("collapsed");
      hudEl.querySelector("#shabi-hud-min").textContent = hudEl.classList.contains("collapsed") ? "+" : "–";
    });
    hudEl.querySelector("#shabi-hud-close").addEventListener("click", () => {
      if (onClose) onClose();
      hudEl.remove();
    });
    hudEl.querySelector("#shabi-hud-copy").addEventListener("click", async () => {
      if (currentJob?.export_path) {
        await navigator.clipboard.writeText(currentJob.export_path).catch(() => {});
        ctrl.setStatus("Exported file path copied!");
      }
    });
    hudEl.querySelector("#shabi-hud-refill").addEventListener("click", () => onRefill && onRefill(ctrl));
    hudEl.querySelector("#shabi-hud-reattach").addEventListener("click", () => onReattach && onReattach(ctrl));
    hudEl.querySelector("#shabi-hud-next").addEventListener("click", () => onNext && onNext(ctrl));
    hudEl.querySelector("#shabi-hud-cancel").addEventListener("click", () => onCancel && onCancel(ctrl));

    renderSteps();
    return ctrl;
  }

  window.ShabiExt = {
    sleep,
    isVisible,
    queryDeep,
    queryAllDeep,
    waitForDeep,
    findButtonByText,
    streamClipFile,
    attachFileToInput,
    setEditableText,
    getEditableText,
    buildSocialCaption,
    reportJob,
    checkPendingJob,
    createHud,
  };
})();
