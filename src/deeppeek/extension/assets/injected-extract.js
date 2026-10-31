// =====================================================================
// injected-extract.js — extraction orchestrator (STABILITY TIER 2)
// =====================================================================
//
// Ties injected-idb.js and injected-fiber.js into the single
// `doExtraction` entry point, and exposes the `__dsx_extract`
// bridge that content.js calls through window.wrappedJSObject.
//
// Tries IndexedDB first, falls back to fiber walk, throws if both
// miss so the capture pipeline reports failure instead of silently
// returning an empty conversation.
//
// Depends on:
//   window.__dsx__.getSessionIdFromURL
//   window.__dsx__.getChatTitle
//   window.__dsx__.extractViaIndexedDB
//   window.__dsx__.extractViaFiber
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  async function doExtraction(args) {
    const includeForked = !!(args && args.include_forked);
    const sessionId = window.__dsx__.getSessionIdFromURL();
    let messages = null;
    let source = null;

    if (sessionId) {
      try {
        messages = await window.__dsx__.extractViaIndexedDB(
          sessionId, includeForked);
        if (messages && messages.length) source = "indexeddb";
      } catch (e) { console.log("[deeppeek] idb threw:", e); }
    }
    if (!messages || !messages.length) {
      try {
        messages = window.__dsx__.extractViaFiber();
        if (messages && messages.length) source = "fiber";
      } catch (e) { console.log("[deeppeek] fiber threw:", e); }
    }
    if (!messages || !messages.length) {
      throw new Error("All extraction methods failed (indexeddb, fiber).");
    }

    console.log(`[deeppeek] extract source=${source} count=${messages.length}`);
    messages = messages.map((m, i) => ({ ...m, index: i }));
    return {
      title: window.__dsx__.getChatTitle(),
      url: window.location.href,
      messages,
    };
  }

  window.__dsx__.doExtraction = doExtraction;

  // Bridge entry point.  Args arrive as a JSON string (primitive,
  // marshaled transparently across Xray).  Returns JSON string for
  // the same reason.
  window.__dsx_extract = async function (argsJson) {
    let args = {};
    if (typeof argsJson === "string" && argsJson.length > 0) {
      try { args = JSON.parse(argsJson); } catch (_) {}
    }
    return JSON.stringify(await doExtraction(args));
  };
})();