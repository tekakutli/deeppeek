// Runs in the Firefox content-script world. Injects the page-world extractor and
// bridges messages between it and the background script. Also holds an open
// port to the background, which keeps the MV3 event page alive (Firefox docs:
// a content-script port keeps the background from being suspended).

(async function () {
  function injectPageWorldScript() {
    const url = browser.runtime.getURL("injected.js");
    const s = document.createElement("script");
    s.src = url;
    s.async = false;
    s.onload = () => s.remove();
    (document.head || document.documentElement).appendChild(s);
  }

  if (!window.__dsx_content_installed__) {
    window.__dsx_content_installed__ = true;
    injectPageWorldScript();
  }

  async function waitFor(condition, timeoutMs = 5000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (condition()) return true;
      await new Promise((r) => setTimeout(r, 50));
    }
    return false;
  }

  browser.runtime.onMessage.addListener(async (msg) => {
    if (!msg || msg.type !== "DSX_EXTRACT") {
      return null;
    }

    const ready = await waitFor(
      () =>
        window.wrappedJSObject &&
        typeof window.wrappedJSObject.__dsx_extract === "function"
    );
    if (!ready) {
      injectPageWorldScript();
      const retried = await waitFor(
        () =>
          window.wrappedJSObject &&
          typeof window.wrappedJSObject.__dsx_extract === "function",
        3000
      );
      if (!retried) {
        console.error("DSX: page-world extractor unavailable.");
        return null;
      }
    }

    try {
      const jsonString = await window.wrappedJSObject.__dsx_extract();
      return JSON.parse(jsonString);
    } catch (e) {
      console.error("DSX: extraction threw:", e);
      return null;
    }
  });

  // ---- keepalive port to background ----------------------------------
  //
  // Opening a runtime.connect() port from the content script keeps the
  // background event page alive indefinitely in Firefox MV3.  Without
  // this, the background is suspended after ~30s idle and the SSE
  // watchdog never fires — which is why `deeppeek capture --watch` was
  // slow whenever the user was already on the deepseek tab.
  //
  // The port also carries a "hello" on connect (new page load) and a
  // periodic "heartbeat" while the page is visible.  The background
  // treats either as a signal to force-reconnect SSE and re-check the
  // armed state.

  function connectKeepalive() {
    let port;
    try {
      port = browser.runtime.connect({ name: "deeppeek-keepalive" });
    } catch (e) {
      console.warn("DSX: keepalive connect failed:", e);
      return null;
    }

    // Attach a message listener so Firefox does not GC-close the port.
    port.onMessage.addListener(() => {});

    port.onDisconnect.addListener(() => {
      // Background reloaded — reconnect after a short delay.
      window.__dsx_port__ = null;
      setTimeout(connectKeepalive, 1000);
    });

    try {
      port.postMessage({ type: "hello", url: location.href });
    } catch (_) {}

    window.__dsx_port__ = port;
    return port;
  }

  const port = connectKeepalive();

  // Heartbeat while the tab is visible.  3s cadence matches the
  // background's watchdog interval; combined they give a steady
  // "user is on this page, check SSE" signal without any new polling
  // protocol — the port messages are just wake signals.
  let heartbeat = null;
  function startHeartbeat() {
    if (heartbeat || !port) return;
    heartbeat = setInterval(() => {
      try {
        port.postMessage({ type: "heartbeat" });
      } catch (_) {
        clearInterval(heartbeat);
        heartbeat = null;
      }
    }, 3000);
  }
  function stopHeartbeat() {
    if (heartbeat) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
  }
  if (document.visibilityState === "visible") startHeartbeat();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      startHeartbeat();
      // Immediate ping on becoming visible (tab switched back to)
      if (port) {
        try { port.postMessage({ type: "hello", url: location.href }); }
        catch (_) {}
      }
    } else {
      stopHeartbeat();
    }
  });
})();
