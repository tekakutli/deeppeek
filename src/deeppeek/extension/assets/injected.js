// =====================================================================
// injected.js — page-world bootstrap (STABILITY TIER 1)
// =====================================================================
//
// First page-world script injected by content.js.  Defines the
// shared `window.__dsx__` namespace and the schema-level helpers
// every other page-world file reads from.
//
// Sibling files loaded after this one (order set by content.js with
// `async = false` so execution order is preserved):
//
//     injected-idb.js         T1  IndexedDB schema access
//     injected-fiber.js       T2  React fiber walk (fallback)
//     injected-extract.js     T2  extraction orchestrator + bridge
//     injected-fetch.js       T1  signed-URL → bytes
//     injected-attach.js      T2  drop / file-input attach
//     injected-composer.js    T2  typing + Enter
//     injected-clear.js       T3  composer clearing (volatile)
//     injected-attachments.js T3  extraction modes (volatile)
//     injected-retry.js       T3  retry-last (volatile)
//     injected-hijack.js      T3  download hijacker (volatile)
//
// Tier legend:
//   1 = schema-level.  Changes only if DeepSeek changes storage or
//       its CDN protocol.
//   2 = DOM/React-level.  Changes when the composer or the drop
//       target moves.
//   3 = heuristic-level.  Grows, gets tuned, gets replaced.
//
// The `__dsx_*_json` bridge entry points that content.js calls
// through `window.wrappedJSObject` are defined on `window` directly
// by their owning sibling files — that Xray contract is unchanged.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  // Selectors for the chat scroll container and the title element.
  // Used by getChatTitle() and extractViaFiber().
  window.__dsx__.config = {
    chatContainerSelector: ".ds-virtual-list-visible-items",
    titleSelector: ".afa34042.e37a04e4.e0a1edb7",
  };

  window.__dsx__.getChatTitle = function () {
    const el = document.querySelector(window.__dsx__.config.titleSelector);
    return el ? el.textContent.trim() : null;
  };

  // DeepSeek URLs look like:
  //   https://chat.deepseek.com/a/chat/s/<uuid>
  // The uuid is the IndexedDB `chat_session.id` key.
  window.__dsx__.getSessionIdFromURL = function () {
    const m = window.location.href.match(/\/s\/([0-9a-f-]{20,})/i);
    return m ? m[1] : null;
  };

  // Normalise DeepSeek's role strings to the two-role model the
  // rest of the code uses.  Unknown roles default to "assistant"
  // (historical behaviour — every unknown role seen in practice
  // has been a variant of assistant).
  window.__dsx__.normalizeRoleFromIDB = function (raw) {
    if (typeof raw !== "string") return null;
    const s = raw.toLowerCase();
    if (s === "user" || s === "human" || s === "customer") return "user";
    if (s === "assistant" || s === "ai" || s === "bot" || s === "model") return "assistant";
    return "assistant";
  };
})();