(() => {
  if (window.__shabiBridgeLoaded) return;
  window.__shabiBridgeLoaded = true;

  const manifestVersion = chrome.runtime.getManifest?.().version || "2.0.0";

  window.addEventListener("message", async (event) => {
    if (event.source !== window || !event.data) return;
    const cmd = event.data.shabiExt;
    if (!cmd) return;

    if (cmd === "PING") {
      try {
        const res = await chrome.runtime.sendMessage({ type: "PING_SERVER" });
        window.postMessage(
          { shabiExt: "PONG", ok: !!res?.ok, version: manifestVersion, by_platform: res?.by_platform || {} },
          location.origin
        );
      } catch {
        window.postMessage({ shabiExt: "PONG", ok: false, version: manifestVersion }, location.origin);
      }
    } else if (cmd === "TRIGGER_PLATFORMS") {
      try {
        await chrome.runtime.sendMessage({
          type: "TRIGGER_PLATFORMS",
          platforms: Array.isArray(event.data.platforms) ? event.data.platforms : ["youtube"],
        });
      } catch {
        // Ignore if extension context invalidated
      }
    } else if (cmd === "TRIGGER_STUDIO") {
      try {
        await chrome.runtime.sendMessage({ type: "TRIGGER_PLATFORMS", platforms: ["youtube"] });
      } catch {
        // Ignore if extension context invalidated
      }
    }
  });

  chrome.runtime
    .sendMessage({ type: "PING_SERVER" })
    .then((res) => {
      window.postMessage(
        { shabiExt: "READY", ok: !!res?.ok, version: manifestVersion, by_platform: res?.by_platform || {} },
        location.origin
      );
    })
    .catch(() => {
      window.postMessage({ shabiExt: "READY", ok: true, version: manifestVersion }, location.origin);
    });
})();
