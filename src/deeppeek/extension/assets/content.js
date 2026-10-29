// Runs in the Firefox content-script world. Injects the page-world
// extractor and bridges messages between it and the background
// script. Also holds an open port to the background, which keeps the
// MV3 event page alive.

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

  async function ensurePageWorldFn(name, timeoutMs = 5000) {
    const ready = await waitFor(
      () => window.wrappedJSObject && typeof window.wrappedJSObject[name] === "function",
      timeoutMs,
    );
    if (!ready) {
      injectPageWorldScript();
      return await waitFor(
        () => window.wrappedJSObject && typeof window.wrappedJSObject[name] === "function",
        3000,
      );
    }
    return true;
  }

  browser.runtime.onMessage.addListener(async (msg) => {
    if (!msg) return null;

    if (msg.type === "DSX_EXTRACT") {
      if (!(await ensurePageWorldFn("__dsx_extract"))) {
        console.error("DSX: page-world extractor unavailable.");
        return null;
      }
      try {
        const argsJson = JSON.stringify({
          include_forked: !!msg.include_forked,
        });
        const jsonString = await window.wrappedJSObject.__dsx_extract(argsJson);
        return JSON.parse(jsonString);
      } catch (e) {
        console.error("DSX: extraction threw:", e);
        return null;
      }
    }

    if (msg.type === "DSX_ATTACH") {
      if (!(await ensurePageWorldFn("__dsx_attach_json"))) {
        console.error("DSX: page-world attach unavailable.");
        return { success: false, reason: "page-world attach unavailable" };
      }
      try {
        // Firefox Xray: content-script-owned objects cannot be read by
        // page-world code (accessing opts.delay_ms triggers "Permission
        // denied to access property").  Strings cross transparently.
        // So stringify here, page-world JSON.parses on its side.
        // Same pattern as __dsx_extract, which already works.
        const argsJson = JSON.stringify({
          files: msg.files || [],
          opts: msg.opts || {},
        });
        const resultJson = await window.wrappedJSObject.__dsx_attach_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        console.error("DSX: attach threw:", e);
        return { success: false, reason: `attach threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_TYPE") {
      if (!(await ensurePageWorldFn("__dsx_type_json"))) {
        console.error("DSX: page-world type unavailable.");
        return { success: false, reason: "page-world type unavailable" };
      }
      try {
        const argsJson = JSON.stringify({
          text: msg.text || "",
          opts: msg.opts || {},
        });
        const resultJson = await window.wrappedJSObject.__dsx_type_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        console.error("DSX: type threw:", e);
        return { success: false, reason: `type threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_ENTER") {
      if (!(await ensurePageWorldFn("__dsx_enter_json"))) {
        console.error("DSX: page-world enter unavailable.");
        return { success: false, reason: "page-world enter unavailable" };
      }
      try {
        const argsJson = JSON.stringify({ opts: msg.opts || {} });
        const resultJson = await window.wrappedJSObject.__dsx_enter_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        console.error("DSX: enter threw:", e);
        return { success: false, reason: `enter threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_CLEAR") {
      if (!(await ensurePageWorldFn("__dsx_clear_json"))) {
        console.error("DSX: page-world clear unavailable.");
        return { success: false, reason: "page-world clear unavailable" };
      }
      try {
        const argsJson = JSON.stringify({ opts: msg.opts || {} });
        const resultJson = await window.wrappedJSObject.__dsx_clear_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        console.error("DSX: clear threw:", e);
        return { success: false, reason: `clear threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_EXTRACT_ATTACHMENTS") {
      if (!(await ensurePageWorldFn("__dsx_extract_attachments_json"))) {
        return { success: false, ok: false,
                 reason: "page-world attachment extractor unavailable" };
      }
      try {
        const argsJson = JSON.stringify(msg.opts || {});
        const resultJson = await window.wrappedJSObject
          .__dsx_extract_attachments_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        return { success: false, ok: false,
                 reason: `extract-attachments threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_RETRY_LAST") {
      if (!(await ensurePageWorldFn("__dsx_retry_last_json"))) {
        return { success: false,
                 reason: "page-world retry-last unavailable" };
      }
      try {
        const argsJson = JSON.stringify(msg.opts || {});
        const resultJson = await window.wrappedJSObject
          .__dsx_retry_last_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        return { success: false,
                 reason: `retry-last threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_HIJACK_INSTALL") {
      if (!(await ensurePageWorldFn("__dsx_hijack_install_json"))) {
        return { success: false, reason: "page-world hijack unavailable" };
      }
      try {
        const json = await window.wrappedJSObject
          .__dsx_hijack_install_json("{}");
        return JSON.parse(json);
      } catch (e) {
        return { success: false, reason: `hijack install threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_HIJACK_UNINSTALL") {
      if (!(await ensurePageWorldFn("__dsx_hijack_uninstall_json"))) {
        return { success: false, reason: "page-world hijack unavailable" };
      }
      try {
        const json = await window.wrappedJSObject
          .__dsx_hijack_uninstall_json("{}");
        return JSON.parse(json);
      } catch (e) {
        return { success: false, reason: `hijack uninstall threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_HIJACK_STATUS") {
      if (!(await ensurePageWorldFn("__dsx_hijack_status_json"))) {
        return { success: false, reason: "page-world hijack unavailable" };
      }
      try {
        const json = await window.wrappedJSObject
          .__dsx_hijack_status_json("{}");
        return JSON.parse(json);
      } catch (e) {
        return { success: false, reason: `hijack status threw: ${e.message}` };
      }
    }

    return null;
  });

  // ---- keepalive port to background ------------------------------

  function connectKeepalive() {
    let port;
    try {
      port = browser.runtime.connect({ name: "deeppeek-keepalive" });
    } catch (e) {
      console.warn("DSX: keepalive connect failed:", e);
      return null;
    }
    port.onMessage.addListener(() => {});
    port.onDisconnect.addListener(() => {
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

  let heartbeat = null;
  function startHeartbeat() {
    if (heartbeat || !port) return;
    heartbeat = setInterval(() => {
      try { port.postMessage({ type: "heartbeat" }); }
      catch (_) { clearInterval(heartbeat); heartbeat = null; }
    }, 3000);
  }
  function stopHeartbeat() {
    if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
  }
  if (document.visibilityState === "visible") startHeartbeat();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      startHeartbeat();
      if (port) {
        try { port.postMessage({ type: "hello", url: location.href }); }
        catch (_) {}
      }
    } else {
      stopHeartbeat();
    }
  });
})();
