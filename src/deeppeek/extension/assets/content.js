// Runs in the Firefox content-script world. Injects the page-world
// extractor and bridges messages between it and the background
// script. Also holds an open port to the background, which keeps the
// MV3 event page alive.
//
// CHANGES 2026-11-15
// ==================
// Added DSX_WAIT_FOR_REPLY message type: forwards to page-world
// `__dsx_wait_for_reply_json`.  Used by split_send for the fast
// "AI reply appeared" signal.
(async function () {
  const PAGE_WORLD_SCRIPTS = [
    "injected.js",          // bootstrap: __dsx__, config, helpers
    "injected-idb.js",      // T1: IndexedDB schema
    "injected-fiber.js",    // T2: React fiber walk (fallback)
    "injected-watch.js",    // T2: MutationObserver streaming watcher
    "injected-extract.js",  // T2: extraction orchestrator
    "injected-fetch.js",    // T1: signed-URL → bytes
    "injected-attach.js",   // T2: drop / file-input attach
    "injected-composer.js", // T2: typing + Enter
    "injected-clear.js",    // T3: composer clearing
    "injected-attachments.js", // T3: attachment extraction modes
    "injected-retry.js",    // T3: retry-last
    "injected-hijack.js",   // T3: download-button interception
    "injected-buttons.js",  // button injector FRAMEWORK (no rules)
  ];

  const BRIDGE_TYPES = new Set([
    "DSX_OPEN_TAB",
    "DSX_LIST_CONTAINERS",
    "DSX_RESUME_SAVE",
    "DSX_RESUME_CLAIM",
    "DSX_CURRENT_CONTAINER",
  ]);

  const BRIDGE_TIMEOUT_MS = 8000;

  function raceWithTimeout(promise, label) {
    const timeout = new Promise((_, rej) =>
      setTimeout(
        () => rej(new Error(`${label} timed out after ${BRIDGE_TIMEOUT_MS}ms`)),
        BRIDGE_TIMEOUT_MS));
    return Promise.race([promise, timeout]);
  }

  function injectPageWorldScript() {
    const head = document.head || document.documentElement;
    for (const name of PAGE_WORLD_SCRIPTS) {
      const url = browser.runtime.getURL(name);
      const s = document.createElement("script");
      s.src = url;
      s.async = false;
      s.onload = () => s.remove();
      head.appendChild(s);
    }
  }

  async function injectButtonRules() {
    const baseUrl = browser.runtime.getURL("button-rules/");
    const manifestUrl = baseUrl + "_manifest.json";

    let files = [];
    try {
      const resp = await fetch(manifestUrl, { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (Array.isArray(data.files)) {
        files = data.files;
      } else {
        console.warn(
          "[DSX] button-rules/_manifest.json: 'files' is not an array — " +
          "no button rules loaded");
        return;
      }
    } catch (e) {
      console.error("[DSX] button-rules manifest fetch failed:", e);
      return;
    }

    if (files.length === 0) {
      console.log("[DSX] button-rules: manifest lists 0 files");
      return;
    }

    const head = document.head || document.documentElement;
    const injected = [];
    for (const name of files) {
      if (typeof name !== "string" || !name.endsWith(".js")) {
        console.warn("[DSX] button-rules: skipping invalid entry:", name);
        continue;
      }
      const s = document.createElement("script");
      s.src = baseUrl + name;
      s.async = false;
      s.onload = () => s.remove();
      s.onerror = () => {
        console.error(
          `[DSX] button-rules: failed to load ${name} ` +
          `(check that it exists and is listed in web_accessible_resources)`);
      };
      head.appendChild(s);
      injected.push(name);
    }
    console.log(
      `[DSX] button-rules: injected ${injected.length} file(s): ` +
      injected.join(", "));
  }

  function installPostMessageBridge() {
    window.addEventListener("message", async (ev) => {
      if (ev.source !== window) return;
      const data = ev.data;
      if (!data || typeof data !== "object") return;
      if (typeof data.dsx_req !== "string") return;
      if (!data.dsx_req.startsWith("dsx_")) return;
      const msg = data.msg;
      if (!msg || typeof msg !== "object") return;
      if (!BRIDGE_TYPES.has(msg.type)) return;

      const reqId = data.dsx_req;
      let payload;
      try {
        const resp = await raceWithTimeout(
          browser.runtime.sendMessage(msg),
          `bridge ${msg.type}`);
        payload = JSON.stringify(resp || {
          success: false, reason: "no response",
        });
      } catch (e) {
        payload = JSON.stringify({
          success: false,
          reason: `sendMessage threw: ${e.message || e}`,
        });
      }
      window.postMessage({ dsx_resp: reqId, payload }, "*");
    });
  }

  if (!window.__dsx_content_installed__) {
    window.__dsx_content_installed__ = true;
    injectPageWorldScript();
    installPostMessageBridge();
    injectButtonRules();
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
      () => window.wrappedJSObject
         && typeof window.wrappedJSObject[name] === "function",
      timeoutMs,
    );
    if (!ready) {
      injectPageWorldScript();
      installPostMessageBridge();
      injectButtonRules();
      return await waitFor(
        () => window.wrappedJSObject
           && typeof window.wrappedJSObject[name] === "function",
        3000,
      );
    }
    return true;
  }

  function pageWorldFn(name) {
    try {
      const ww = window.wrappedJSObject;
      if (ww && typeof ww[name] === "function") return ww[name];
    } catch (_) {}
    return null;
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

    if (msg.type === "DSX_WAIT_FOR_REPLY") {
      if (!(await ensurePageWorldFn("__dsx_wait_for_reply_json"))) {
        return { success: false,
                 reason: "page-world wait-for-reply unavailable" };
      }
      try {
        const argsJson = JSON.stringify(msg.opts || {});
        const resultJson = await window.wrappedJSObject
          .__dsx_wait_for_reply_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        return { success: false,
                 reason: `wait-for-reply threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_WATCH_START") {
      const fn = pageWorldFn("__dsx_startWatch");
      if (!fn) {
        return { success: false, reason: "page-world watch unavailable" };
      }
      try {
        const json = await fn(JSON.stringify(msg.opts || {}));
        return JSON.parse(json);
      } catch (e) {
        return { success: false, reason: `watch threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_WATCH_STOP") {
      const fn = pageWorldFn("__dsx_stopWatch");
      if (!fn) return { success: false, reason: "page-world watch unavailable" };
      try {
        const json = await fn(JSON.stringify(msg.opts || {}));
        return JSON.parse(json);
      } catch (e) {
        return { success: false, reason: `stop threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_ATTACH") {
      if (!(await ensurePageWorldFn("__dsx_attach_json"))) {
        console.error("DSX: page-world attach unavailable.");
        return { success: false, reason: "page-world attach unavailable" };
      }
      try {
        const argsJson = JSON.stringify({
          files: msg.files || [],
          opts: msg.opts || {},
        });
        const resultJson =
          await window.wrappedJSObject.__dsx_attach_json(argsJson);
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
        const resultJson =
          await window.wrappedJSObject.__dsx_type_json(argsJson);
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
        const resultJson =
          await window.wrappedJSObject.__dsx_enter_json(argsJson);
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
        const resultJson =
          await window.wrappedJSObject.__dsx_clear_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        console.error("DSX: clear threw:", e);
        return { success: false, reason: `clear threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_BIND_COMPOSER") {
      if (!(await ensurePageWorldFn("__dsx_bind_composer_json"))) {
        console.error("DSX: page-world bind-composer unavailable.");
        return { success: false,
                 reason: "page-world bind-composer unavailable" };
      }
      try {
        const argsJson = JSON.stringify(msg.opts || {});
        const resultJson = await window.wrappedJSObject
          .__dsx_bind_composer_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        console.error("DSX: bind-composer threw:", e);
        return { success: false,
                 reason: `bind-composer threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_PROBE_COMPOSER") {
      if (!(await ensurePageWorldFn("__dsx_probe_composer_json"))) {
        return { success: false,
                 reason: "page-world probe-composer unavailable" };
      }
      try {
        const argsJson = JSON.stringify(msg.opts || {});
        const resultJson = await window.wrappedJSObject
          .__dsx_probe_composer_json(argsJson);
        return JSON.parse(resultJson);
      } catch (e) {
        return { success: false,
                 reason: `probe-composer threw: ${e.message}` };
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
        return { success: false,
                 reason: `hijack install threw: ${e.message}` };
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
        return { success: false,
                 reason: `hijack uninstall threw: ${e.message}` };
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
        return { success: false,
                 reason: `hijack status threw: ${e.message}` };
      }
    }

    if (msg.type === "DSX_HIJACK_ALL") {
      if (!(await ensurePageWorldFn("__dsx_hijack_all_json"))) {
        return { success: false, reason: "page-world hijack unavailable" };
      }
      try {
        const argsJson = JSON.stringify(msg.opts || {});
        const json = await window.wrappedJSObject
          .__dsx_hijack_all_json(argsJson);
        return JSON.parse(json);
      } catch (e) {
        return { success: false,
                 reason: `hijack all threw: ${e.message}` };
      }
    }

    return null;
  });

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