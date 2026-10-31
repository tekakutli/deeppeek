// =====================================================================
// injected-fiber.js — React fiber walk fallback (STABILITY TIER 2)
// =====================================================================
//
// Used only when IndexedDB is unavailable (offline SPA, store name
// changed, session not yet persisted).  Walks the React fiber tree
// looking for a message-shaped array in props or hooks.
//
// This is a FALLBACK — it is more brittle than IndexedDB, but it
// survives DOM changes as long as React is still React.  It runs
// only when indexeddb misses, so its cost is invisible on the
// normal path.
//
// Exports: `extractViaFiber()` on `window.__dsx__`.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  const ROLE_KEYS = ["role", "type", "sender", "author", "from"];
  const USER_ROLES = ["user", "human", "customer", "client", "question"];
  const AI_ROLES = ["assistant", "ai", "bot", "agent", "answer", "model"];
  const KNOWN_ROLES = [...USER_ROLES, ...AI_ROLES, "system"];
  const MESSAGE_ARRAY_KEYS = ["messages", "conversation", "chat", "items", "history", "list"];
  const NESTED_MESSAGE_KEYS = ["messages", "items", "history", "conversation", "chat"];

  function normalizeRole(raw) {
    if (typeof raw !== "string") return null;
    const s = raw.toLowerCase();
    if (USER_ROLES.includes(s)) return "user";
    if (AI_ROLES.includes(s)) return "assistant";
    return null;
  }

  function looksLikeMessageList(val) {
    if (!Array.isArray(val) || val.length < 2) return false;
    let matches = 0;
    for (const m of val.slice(0, 20)) {
      if (!m || typeof m !== "object") continue;
      for (const k of ROLE_KEYS) {
        if (typeof m[k] === "string" && KNOWN_ROLES.includes(m[k].toLowerCase())) {
          matches++; break;
        }
      }
      if (matches >= 2) return true;
    }
    return false;
  }

  function extractContentFromMessage(m) {
    const KEYS = ["markdown", "content", "text", "message", "body", "value", "source"];
    for (const k of KEYS) {
      const v = m[k];
      if (typeof v === "string" && v.length > 0) return v;
      if (v && typeof v === "object") {
        const inner = v.text || v.content || v.value || v.markdown;
        if (typeof inner === "string") return inner;
      }
    }
    return "";
  }

  // NEW: parallel to extractContentFromMessage — pull thinking from
  // whatever shape the streaming fiber state uses.  Same key list as
  // the IDB extractor (injected-idb.js / extractMessageBody), since
  // streaming and persisted thinking are the same conceptual field.
  //
  // Without this, normalizeFiberMessage required non-empty content
  // and dropped every assistant message whose response was cut off
  // mid-thought (the incomplete-thought bug from 2026-11-05).  The
  // fiber fallback is the ONLY path left when the session isn't yet
  // persisted to IndexedDB, so dropping thinking-only messages here
  // made capture fail with "All extraction methods failed".
  function extractThinkingFromMessage(m) {
    const KEYS = [
      "thinking", "reasoning", "thinking_content", "reasoning_content",
      "thought", "thoughts",
    ];
    for (const k of KEYS) {
      const v = m[k];
      if (typeof v === "string" && v.length > 0) return v;
      if (v && typeof v === "object") {
        const inner = v.text || v.content || v.value;
        if (typeof inner === "string") return inner;
      }
    }
    return "";
  }

  function normalizeFiberMessage(m) {
    if (!m || typeof m !== "object") return null;
    let role = null;
    for (const k of ROLE_KEYS) {
      role = normalizeRole(m[k]);
      if (role) break;
    }
    if (!role) return null;
    const content = extractContentFromMessage(m);
    const thinking = extractThinkingFromMessage(m);
    // Accept thinking-only messages: an interrupted response lives in
    // the fiber state as fragments with no final content yet, but the
    // partial thinking is still worth capturing.
    if (!content && !thinking) return null;
    return { role, content, thinking };
  }

  function findMessageListInFiber(element) {
    const fk = Object.keys(element).find((k) => k.startsWith("__reactFiber$"));
    if (!fk) return null;
    let fiber = element[fk];
    let depth = 0;
    while (fiber && depth < 40) {
      for (const props of [fiber.memoizedProps, fiber.pendingProps]) {
        if (!props || typeof props !== "object") continue;
        for (const key of MESSAGE_ARRAY_KEYS) {
          const val = props[key];
          if (looksLikeMessageList(val)) return { messages: val, source: `props.${key}` };
          if (val && typeof val === "object" && !Array.isArray(val)) {
            for (const inner of NESTED_MESSAGE_KEYS) {
              if (looksLikeMessageList(val[inner])) {
                return { messages: val[inner], source: `props.${key}.${inner}` };
              }
            }
          }
        }
      }
      if (fiber.memoizedState) {
        let state = fiber.memoizedState;
        let idx = 0;
        while (state && idx < 50) {
          const val = state.memoizedState;
          if (val && typeof val !== "function") {
            if (looksLikeMessageList(val)) return { messages: val, source: `state#${idx}` };
            if (val && typeof val === "object" && "current" in val &&
                looksLikeMessageList(val.current)) {
              return { messages: val.current, source: `state#${idx}.current` };
            }
          }
          state = state.next; idx++;
        }
      }
      fiber = fiber.return; depth++;
    }
    return null;
  }

  function extractViaFiber() {
    const container = document.querySelector(
      window.__dsx__.config.chatContainerSelector);
    if (!container) return null;
    const candidates = [];
    for (const el of document.querySelectorAll(".ds-message, [data-index]")) {
      const found = findMessageListInFiber(el);
      if (found && found.messages.length > 0) candidates.push(found);
    }
    candidates.push(findMessageListInFiber(container));
    const best = candidates
      .filter(Boolean)
      .sort((a, b) => b.messages.length - a.messages.length)[0];
    if (!best) return null;
    const normalized = [];
    for (const m of best.messages) {
      const n = normalizeFiberMessage(m);
      if (n) normalized.push(n);
    }
    return normalized.length > 0 ? normalized : null;
  }

  window.__dsx__.extractViaFiber = extractViaFiber;
})();