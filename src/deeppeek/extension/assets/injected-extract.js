// =====================================================================
// injected-extract.js — extraction orchestrator (STABILITY TIER 2)
// =====================================================================
//
// Ties the three extraction paths into the single `doExtraction`
// entry point, and exposes the `__dsx_extract` bridge that
// content.js calls through window.wrappedJSObject.
//
// Priorities (first one that returns non-empty wins):
//
//   1. API       GET /api/v0/chat/history_messages with the Bearer
//                token from localStorage.userToken.  Always current,
//                works on fresh tabs that have never been reloaded,
//                no dependency on IndexedDB write timing or React
//                internals.  This is the primary path.
//   2. IndexedDB Works offline, or when the API is unreachable.
//                Suffers from staleness on fresh tabs (DeepSeek
//                only writes to IDB on page load), so it is now
//                the SECOND choice, not the first.
//   3. Fiber     Last-resort React-tree walk; currently returns
//                null on the 2026-10 build, but kept so that a
//                future DeepSeek change that keeps React state
//                exposed (or a build where IDB and API both fail
//                for a transient reason) still has a path.
//
// If all three return empty, throws — the capture pipeline reports
// failure rather than silently emitting an empty conversation.
//
// Depends on:
//   window.__dsx__.getSessionIdFromURL
//   window.__dsx__.getChatTitle
//   window.__dsx__.extractViaIndexedDB
//   window.__dsx__.extractViaFiber
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  // -------------------------------------------------------------------
  // Server fetch — mirrors the request the page makes on every load.
  //
  // The page itself calls this endpoint (verified from the Network
  // tab on 2026-10-31) with the Bearer token stored in
  // localStorage.userToken.value.  Cookies alone are NOT sufficient
  // (the server returns code 40003 without the Bearer).  The token
  // is a plain string, not a JWT; JSON-parse the localStorage entry
  // to extract it.
  //
  // cache_version=0 / cache_reset_at=0 asks for a full snapshot
  // (server responds with `cache_control: "REPLACE"`).  Non-zero
  // cache_version causes the server to send only a delta
  // (`cache_control: "MERGE"`), which is what the page uses on
  // subsequent loads.  We always want the full state.
  // -------------------------------------------------------------------
  async function fetchSessionFromApi(sessionId) {
    const raw = localStorage.getItem("userToken");
    if (!raw) throw new Error("no userToken in localStorage");

    let token;
    try {
      const parsed = JSON.parse(raw);
      token = parsed && parsed.value;
    } catch (_) {
      token = raw;   // tolerate a raw string
    }
    if (!token || typeof token !== "string") {
      throw new Error("userToken present but no .value");
    }

    const url = "/api/v0/chat/history_messages" +
      "?chat_session_id=" + encodeURIComponent(sessionId) +
      "&cache_version=0&cache_reset_at=0";

    const r = await fetch(url, {
      credentials: "include",
      headers: {
        "accept": "*/*",
        "authorization": "Bearer " + token,
      },
    });
    if (!r.ok) throw new Error("HTTP " + r.status);

    const j = await r.json();
    if (!j || j.code !== 0) {
      throw new Error("code=" + (j && j.code) + " " + ((j && j.msg) || ""));
    }
    const bd = j.data && j.data.biz_data;
    if (!bd || !Array.isArray(bd.chat_messages)) {
      throw new Error("bad response shape (no biz_data.chat_messages)");
    }

    return {
      chatSession: bd.chat_session || {},
      chatMessages: bd.chat_messages,
    };
  }

  // -------------------------------------------------------------------
  // Active-path filter — identical logic to
  // injected-idb.js::_activePathMessages, so the API and IDB paths
  // produce byte-identical output for the same conversation.  Returns
  // null when the walk cannot be established (missing leaf, cycle,
  // etc.); the caller then falls back to "use all raw messages".
  // -------------------------------------------------------------------
  function activePathFromRaw(chatSession, raw) {
    if (!Array.isArray(raw) || raw.length === 0) return null;

    const byId = new Map();
    const usedAsParent = new Set();
    for (const m of raw) {
      if (!m || typeof m !== "object" || m.message_id == null) continue;
      byId.set(m.message_id, m);
      if (m.parent_id != null) usedAsParent.add(m.parent_id);
    }
    if (byId.size === 0) return null;

    let leafId = null;
    if (chatSession && chatSession.current_message_id != null
        && byId.has(chatSession.current_message_id)) {
      leafId = chatSession.current_message_id;
    } else {
      // Fallback: latest leaf by inserted_at.
      let best = null;
      for (const [id, m] of byId) {
        if (usedAsParent.has(id)) continue;
        const ts = typeof m.inserted_at === "number" ? m.inserted_at : 0;
        if (best === null || ts > best.ts) best = { id, ts };
      }
      if (best) leafId = best.id;
    }
    if (leafId == null) return null;

    const path = [];
    const seen = new Set();
    let cur = leafId;
    while (cur != null) {
      if (seen.has(cur)) break;            // cycle guard
      const m = byId.get(cur);
      if (!m) break;                       // parent not present
      seen.add(cur);
      path.push(m);
      if (m.parent_id == null) break;      // reached root
      cur = m.parent_id;
    }
    if (path.length === 0) return null;
    path.reverse();
    return path;
  }

  // -------------------------------------------------------------------
  // Fragment → {role, content, thinking}.
  //
  // Verified schema (from a live Network capture, 2026-10-31):
  //
  //   message.role           "USER" | "ASSISTANT"
  //   message.fragments[]    [{type, content}, ...]
  //     type="REQUEST"       the user prompt (only on USER msgs)
  //     type="RESPONSE"      the assistant reply (only on ASSISTANT msgs)
  //     type="THINK"         the reasoning trace, if thinking_enabled
  //
  // Older builds used `content` or `content_blocks` instead of
  // `fragments`; both are handled here for back-compat with any
  // server-side rollout still in flight.  Multiple REQUEST/RESPONSE
  // fragments (rare) concatenate with "\n\n".
  // -------------------------------------------------------------------
  function normalizeRole(role) {
    const s = String(role || "").toLowerCase();
    return (s === "user" || s === "human" || s === "customer")
      ? "user" : "assistant";
  }

  function parseFragments(m) {
    let content = "";
    let thinking = "";

    const frags = m.fragments || m.content_blocks || m.blocks || m.parts;
    if (Array.isArray(frags) && frags.length > 0) {
      const contentParts = [];
      const thinkParts = [];
      for (const f of frags) {
        if (!f || typeof f !== "object") continue;
        const t = String(f.type || f.kind || "").toUpperCase();
        const text = (typeof f.content === "string" && f.content)
          ? f.content
          : (typeof f.text === "string" ? f.text : "");
        if (!text) continue;
        if (/THINK|REASON|THOUGHT/.test(t)) thinkParts.push(text);
        else contentParts.push(text);
      }
      content = contentParts.join("\n\n");
      thinking = thinkParts.join("\n\n");
    } else if (typeof m.content === "string" && m.content) {
      // Fallback for schemas that never used fragments.
      content = m.content;
      if (typeof m.thinking === "string") thinking = m.thinking;
    }

    return {
      role: normalizeRole(m.role),
      content: content,
      thinking: thinking,
    };
  }

  // -------------------------------------------------------------------
  // Extraction step 1: API.
  // -------------------------------------------------------------------
  async function extractViaApi(sessionId, includeForked) {
    const {chatSession, chatMessages} = await fetchSessionFromApi(sessionId);

    let selected;
    if (includeForked) {
      // Same as IDB: --include-forked returns all messages, no
      // active-path filtering and no sorting.
      selected = chatMessages;
      console.log(
        `[deeppeek:api] --include-forked: using all ` +
        `${chatMessages.length} message(s)`);
    } else {
      const activePath = activePathFromRaw(chatSession, chatMessages);
      if (activePath) {
        selected = activePath;
        console.log(
          `[deeppeek:api] active path: ${activePath.length}/` +
          `${chatMessages.length} message(s) (forked branches excluded)`);
      } else {
        // Match IDB behavior: sort by message_id when all have
        // numeric IDs.
        const allHaveMsgId = chatMessages.every(
          (m) => m && typeof m.message_id === "number");
        selected = allHaveMsgId
          ? [...chatMessages].sort((a, b) => a.message_id - b.message_id)
          : chatMessages;
        console.log(
          `[deeppeek:api] active path unavailable; using all ` +
          `${chatMessages.length} message(s)`);
      }
    }

    const out = [];
    for (const m of selected) {
      if (!m || typeof m !== "object") continue;
      const parsed = parseFragments(m);
      if (!parsed.content && !parsed.thinking) continue;
      const entry = { role: parsed.role, content: parsed.content };
      if (parsed.thinking) entry.thinking = parsed.thinking;
      out.push(entry);
    }
    return out.length > 0 ? out : null;
  }

  // -------------------------------------------------------------------
  // Orchestrator.
  // -------------------------------------------------------------------
  async function doExtraction(args) {
    const includeForked = !!(args && args.include_forked);
    const sessionId = window.__dsx__.getSessionIdFromURL();
    let messages = null;
    let source = null;

    // 1. API — always current, no reload, works on fresh tabs.
    if (sessionId) {
      try {
        messages = await extractViaApi(sessionId, includeForked);
        if (messages && messages.length) source = "api";
      } catch (e) {
        console.log("[deeppeek:api] fetch failed:", e && e.message);
      }
    }

    // 2. IndexedDB — offline-safe fallback.
    if ((!messages || !messages.length) && sessionId) {
      try {
        messages = await window.__dsx__.extractViaIndexedDB(
          sessionId, includeForked);
        if (messages && messages.length) source = "indexeddb";
      } catch (e) {
        console.log("[deeppeek:idb] threw:", e);
      }
    }

    // 3. Fiber — last resort.
    if (!messages || !messages.length) {
      try {
        messages = window.__dsx__.extractViaFiber();
        if (messages && messages.length) source = "fiber";
      } catch (e) {
        console.log("[deeppeek:fiber] threw:", e);
      }
    }

    if (!messages || !messages.length) {
      throw new Error(
        "All extraction methods failed (api, indexeddb, fiber).");
    }

    console.log(
      `[deeppeek] extract source=${source} count=${messages.length}`);
    messages = messages.map((m, i) => ({ ...m, index: i }));
    return {
      title: window.__dsx__.getChatTitle(),
      url: window.location.href,
      messages,
    };
  }

  window.__dsx__.doExtraction = doExtraction;
  window.__dsx__.extractViaApi = extractViaApi;
  window.__dsx__.fetchSessionFromApi = fetchSessionFromApi;

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
