// =====================================================================
// injected-idb.js — IndexedDB schema access (STABILITY TIER 1)
// =====================================================================
//
// DeepSeek stores the conversation in IndexedDB:
//
//   database:    "deepseek-chat"
//   store:       "history-message" (one record per chat_session)
//   record.data.chat_session        → { id, current_message_id, ... }
//   record.data.chat_messages[]     → [{ message_id, parent_id,
//                                         role, fragments, ... }]
//
// A message is a TREE, not a list: each message has a parent_id, and
// `current_message_id` points at the leaf the UI is rendering.  When
// the user edits an earlier prompt (or regenerates a reply), DeepSeek
// forks — the old branch stays in IndexedDB but is no longer on the
// active path and is invisible in the UI.  Sorting by message_id
// would resurrect every discarded fork.
//
// `_activePathMessages` walks backward from `current_message_id` via
// `parent_id` to reconstruct the active path in root→leaf order.
//
// Exports on `window.__dsx__` (used by sibling files):
//
//   _activePathMessages(chatSession, allMessages)  → [msg] | null
//   extractViaIndexedDB(sessionId, includeForked)  → [msg] | null
//   _collectIndexedDBMessages(includeForked)       → [msg] | null
//   extractMessageBody(msg)                        → {content, thinking}
//
// `_collectIndexedDBMessages` is the shared entry point every
// attachment/retry consumer uses; `extractViaIndexedDB` is only used
// by injected-extract.js's orchestrator.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  // ---------- active-path reconstruction --------------------------

  function _activePathMessages(chatSession, allMessages) {
    if (!Array.isArray(allMessages) || allMessages.length === 0) {
      return null;
    }
    const byId = new Map();
    const usedAsParent = new Set();
    for (const m of allMessages) {
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
    let reachedRoot = false;
    while (cur != null) {
      if (seen.has(cur)) break;          // cycle guard
      const m = byId.get(cur);
      if (!m) break;                     // parent missing from IDB
      seen.add(cur);
      path.push(m);
      if (m.parent_id == null) { reachedRoot = true; break; }
      cur = m.parent_id;
    }
    if (path.length === 0) return null;
    path.reverse();
    if (!reachedRoot) {
      // Chain broken before root — partial path still useful, but
      // callers should know it's incomplete.
      path._incomplete = true;
    }

    // NEW: a streaming assistant message can have no `parent_id` set
    // yet (the client creates the message optimistically, before
    // linking it to its parent), so the walk from leaf never reaches
    // the user prompt.  When that happens, the active path is just
    // the half-written assistant message — capture would emit only
    // `## Assistant` and drop the user prompt entirely.
    //
    // Fix: if the path has no user message but allMessages does,
    // prepend the most recent one.  This is what makes the
    // incomplete-thought case produce a usable markdown.
    if (!path.some(m => window.__dsx__.normalizeRoleFromIDB(m.role) === "user")) {
      const pathIds = new Set(path.map(m => m.message_id));
      let best = null;
      for (const m of allMessages) {
        if (!m || typeof m !== "object") continue;
        if (window.__dsx__.normalizeRoleFromIDB(m.role) !== "user") continue;
        if (pathIds.has(m.message_id)) continue;
        const ts = typeof m.inserted_at === "number" ? m.inserted_at : 0;
        if (best === null || ts > best.ts) best = { m, ts };
      }
      if (best) {
        path.unshift(best.m);
        console.log(
          `[deeppeek:idb] active path was missing user messages; ` +
          `prepended message_id=${best.m.message_id}`);
      }
    }
    return path;
  }

  // ---------- body extraction -------------------------------------
  //
  // DeepSeek has changed the shape of `content` over time: plain
  // string vs. `fragments`/`blocks` arrays with typed entries.  This
  // is the one place that knows about all shapes, so downstream code
  // stays schema-agnostic.
  //
  // MODIFIED (2026-11-05): the incomplete-thought state uses shapes
  // the completed-response state never emits — nested-object
  // `content`, thinking stored under alternative field names
  // (`thought`, `thoughts`), and fragment text carried under
  // `markdown`/`reasoning`.  Be liberal: filter only what is truly
  // empty.  Without this, a mid-generation assistant message is
  // dropped entirely and `extractViaIndexedDB` returns null.

  function extractMessageBody(m) {
    let content = "";
    let thinking = null;
    const isStr = (v) => typeof v === "string" && v.trim().length > 0;

    // Direct string fields — DeepSeek has shipped several schemas;
    // check every name we've seen.
    if (isStr(m.content)) content = m.content;
    if (isStr(m.thinking_content)) thinking = m.thinking_content;
    if (isStr(m.reasoning_content)) thinking = m.reasoning_content;
    if (isStr(m.thinking)) thinking = m.thinking;
    if (isStr(m.reasoning)) thinking = m.reasoning;
    if (isStr(m.thought)) thinking = m.thought;

    // m.content can itself be an object {text: "..."}.
    if (!content && m.content && typeof m.content === "object") {
      if (isStr(m.content.text)) content = m.content.text;
      else if (isStr(m.content.value)) content = m.content.value;
      else if (isStr(m.content.markdown)) content = m.content.markdown;
    }

    const frags = m.fragments || m.content_blocks || m.blocks || m.parts;
    if (Array.isArray(frags) && frags.length) {
      const contentParts = [];
      const thinkParts = [];
      const fileParts = [];
      for (const f of frags) {
        if (!f || typeof f !== "object") continue;
        const type = String(f.type || f.kind || "").toUpperCase();
        if (type === "FILE" && Array.isArray(f.files)) {
          for (const file of f.files) {
            if (!file || typeof file !== "object") continue;
            const name = file.file_name || file.fileName
              || file.name || "attachment";
            const size = typeof file.file_size === "number"
              ? ` (${(file.file_size / 1024).toFixed(1)} KB)`
              : "";
            fileParts.push(`[file: ${name}${size}]`);
          }
          continue;
        }
        // Text can be a string OR a nested object.  Be liberal —
        // the incomplete-thought state may use a shape the
        // completed-response state never emits.
        let text = "";
        if (isStr(f.content)) text = f.content;
        else if (isStr(f.text)) text = f.text;
        else if (isStr(f.value)) text = f.value;
        else if (isStr(f.markdown)) text = f.markdown;
        else if (isStr(f.reasoning)) text = f.reasoning;
        else if (isStr(f.thought)) text = f.thought;
        else if (f.content && typeof f.content === "object") {
          if (isStr(f.content.text)) text = f.content.text;
          else if (isStr(f.content.value)) text = f.content.value;
          else if (isStr(f.content.content)) text = f.content.content;
        } else if (f.text && typeof f.text === "object") {
          if (isStr(f.text.value)) text = f.text.value;
          else if (isStr(f.text.content)) text = f.text.content;
        }
        if (!text) continue;
        if (/THINK|REASON|THOUGHT/.test(type)) thinkParts.push(text);
        else contentParts.push(text);
      }
      const body = contentParts.join("");
      const combined = fileParts.length > 0
        ? fileParts.join("\n") + (body ? "\n\n" + body : "") : body;
      if (!content && combined) content = combined;
      if (!thinking && thinkParts.length) thinking = thinkParts.join("\n\n");
    }

    if (!content && m.message && typeof m.message === "object") {
      if (isStr(m.message.content)) content = m.message.content;
    }
    return { content, thinking };
  }

  // ---------- full extraction via IDB -----------------------------
  //
  // Returns a list of {role, content, thinking} suitable for the
  // capture pipeline, or null if the session isn't in IndexedDB
  // (e.g. the URL has no `/s/<uuid>`, or the store is empty).

  async function extractViaIndexedDB(sessionId, includeForked) {
    const LOG = (...a) => console.log("[deeppeek:idb]", ...a);
    if (!sessionId) { LOG("no session id in URL"); return null; }

    return new Promise((resolve) => {
      let req;
      try { req = indexedDB.open("deepseek-chat"); }
      catch (e) { LOG("open threw:", e); resolve(null); return; }

      req.onerror = () => { LOG("open error:", req.error); resolve(null); };
      req.onblocked = () => { LOG("open blocked"); resolve(null); };
      req.onupgradeneeded = () => {
        try { req.result.close(); } catch (_) {}
        resolve(null);
      };
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("history-message")) {
          db.close(); resolve(null); return;
        }
        const all = db.transaction("history-message", "readonly")
                      .objectStore("history-message").getAll();
        all.onerror = () => { db.close(); resolve(null); };
        all.onsuccess = () => {
          const records = all.result || [];
          db.close();
          let match = null;
          for (const rec of records) {
            const sess = rec && rec.data && rec.data.chat_session;
            if (sess && sess.id &&
                String(sess.id).toLowerCase() === sessionId.toLowerCase()) {
              match = rec; break;
            }
          }
          if (!match) { LOG("session not in IDB"); resolve(null); return; }
          const raw = (match.data && match.data.chat_messages) || [];
          const chatSession = match.data && match.data.chat_session;
          let activePath = null;
          if (includeForked) {
            LOG(`--include-forked: skipping active-path filter; `
                + `using all ${raw.length} message(s)`);
          } else {
            activePath = _activePathMessages(chatSession, raw);
          }
          let sorted;
          if (activePath) {
            sorted = activePath;
            if (activePath._incomplete) {
              LOG(`active path: ${activePath.length}/${raw.length} `
                  + `message(s); parent chain BROKEN before root`);
            } else {
              LOG(`active path: ${activePath.length}/${raw.length} `
                  + `message(s) (forked branches excluded)`);
            }
          } else {
            const allHaveMsgId = raw.every(
              (m) => m && typeof m.message_id === "number");
            sorted = allHaveMsgId
              ? [...raw].sort((a, b) => a.message_id - b.message_id)
              : raw;
            LOG(`active path unavailable; using all ${raw.length} `
                + `message(s) sorted by message_id`);
          }
          const normalized = [];
          for (const m of sorted) {
            if (!m || typeof m !== "object") continue;
            const role = window.__dsx__.normalizeRoleFromIDB(m.role);
            const { content, thinking } = extractMessageBody(m);
            if (!content && !thinking) continue;
            normalized.push({
              role,
              content: content || "",
              thinking: thinking || undefined,
            });
          }
          resolve(normalized.length > 0 ? normalized : null);
        };
      };
    });
  }

  // ---------- shared reader ---------------------------------------
  //
  // Used by every attachment/retry consumer.  Returns the raw
  // messages (active path by default; full tree with includeForked).
  // Null if the session isn't in IDB — NOT an error.

  async function _collectIndexedDBMessages(includeForked) {
    const sessionId = window.__dsx__.getSessionIdFromURL();
    if (!sessionId) return null;
    const db = await new Promise((res, rej) => {
      const req = indexedDB.open("deepseek-chat");
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
    try {
      const all = await new Promise((res, rej) => {
        const tx = db.transaction("history-message", "readonly");
        const req = tx.objectStore("history-message").getAll();
        req.onsuccess = () => res(req.result);
        req.onerror = () => rej(req.error);
      });
      let match = null;
      for (const rec of all) {
        const sess = rec && rec.data && rec.data.chat_session;
        if (sess && sess.id &&
            String(sess.id).toLowerCase() === sessionId.toLowerCase()) {
          match = rec; break;
        }
      }
      if (!match) return null;
      const raw = (match.data && match.data.chat_messages) || [];
      return includeForked
        ? raw
        : (_activePathMessages(match.data.chat_session, raw) || raw);
    } finally {
      db.close();
    }
  }

  // ---------- exports ---------------------------------------------
  window.__dsx__._activePathMessages = _activePathMessages;
  window.__dsx__.extractViaIndexedDB = extractViaIndexedDB;
  window.__dsx__._collectIndexedDBMessages = _collectIndexedDBMessages;
  window.__dsx__.extractMessageBody = extractMessageBody;
})();