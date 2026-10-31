// =====================================================================
// injected-retry.js — retry-last (STABILITY TIER 3, VOLATILE)
// =====================================================================
//
// Recreates the setup of the most recent user prompt: re-attaches
// those files to the composer and retypes the prompt text.
//
// The hard part is selection.  A failed submission lives in React
// state (fiber) but never reaches IndexedDB until reload destroys
// it, so this file walks the fiber tree looking for a message-
// shaped object that is NOT in IndexedDB.  That scan is a
// heuristic; when React changes its internal shape, this file
// needs updating.
//
// Stability: LOW.  Expect this file to need tuning when DeepSeek
// changes its React internals or its message schema.
//
// Depends on (via `window.__dsx__`):
//   _collectIndexedDBMessages, normalizeRoleFromIDB,
//   _fragFiles, _fetchAndEncode, _guessMimeFromName,
//   doClear, doAttach, doType
//
// Bridge entry point (defined on `window`):
//   window.__dsx_retry_last_json(argsJson)
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  async function _findLastUserPrompt(includeForked) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) return null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (window.__dsx__.normalizeRoleFromIDB(m.role) !== "user") continue;
      return { msg: m, idx: i, total: messages.length };
    }
    return null;
  }

  // Extract only the prompt text (TEXT/REQUEST fragments), skipping
  // FILE fragments (their `[file: ...]` lines are capture-time
  // display artifacts, not prompt text) and THINK/REASON fragments
  // (reasoning, not the user's question).
  function _extractPromptTextOnly(msg) {
    const isStr = (v) => typeof v === "string" && v.trim().length > 0;
    const frags = msg.fragments || msg.content_blocks || [];
    const parts = [];
    for (const f of frags) {
      if (!f || typeof f !== "object") continue;
      const type = String(f.type || f.kind || "").toUpperCase();
      if (type === "FILE") continue;
      if (/THINK|REASON/.test(type)) continue;
      const t = isStr(f.content) ? f.content
              : isStr(f.text) ? f.text
              : isStr(f.value) ? f.value : "";
      if (t) parts.push(t);
    }
    if (parts.length) return parts.join("");
    if (isStr(msg.content)) return msg.content;
    return "";
  }

  // Find a message that is currently held in React state but NOT in
  // IndexedDB.  This catches failed submissions: the client
  // optimistically renders the message and uploads its files
  // (status: SUCCESS on each file record) before the send POST
  // completes.  If the POST fails, the rendered message stays in
  // fiber state but never reaches history-message.  On reload, it
  // is gone.
  //
  // The composer's own useState / useReducer hook (typically
  // hook[13]) holds the optimistic pending message until it either
  // persists or rolls back.  We scan every hook and every props
  // object for message-shaped objects whose message_id is missing
  // or absent from IndexedDB.
  async function _findPendingMessage() {
    const idbMessages = await window.__dsx__._collectIndexedDBMessages(true);
    const idbIds = new Set();
    if (idbMessages) {
      for (const m of idbMessages) {
        if (m && m.message_id != null) idbIds.add(m.message_id);
      }
    }

    let rootFiber = null;
    const tryContainer = (el) => {
      for (const k of Object.keys(el)) {
        if (k.startsWith("__reactContainer$")) return el[k];
      }
      return null;
    };
    rootFiber = tryContainer(document.body);
    if (!rootFiber) {
      for (const el of document.body.querySelectorAll("*")) {
        rootFiber = tryContainer(el);
        if (rootFiber) break;
      }
    }
    if (!rootFiber) return null;

    const seen = new WeakSet();
    const seenFibers = new Set();
    const stack = [rootFiber];
    const candidates = [];
    let nodes = 0;

    function looksLikeMessage(v) {
      if (!v || typeof v !== "object" || Array.isArray(v)) return false;
      return (Array.isArray(v.fragments)
           || Array.isArray(v.content_blocks)
           || Array.isArray(v.blocks)
           || Array.isArray(v.parts));
    }

    function consider(v) {
      if (!v || typeof v !== "object" || seen.has(v)) return;
      seen.add(v);
      if (!looksLikeMessage(v)) return;
      const role = String(v.role || "").toLowerCase();
      if (!["user", "human", "customer"].includes(role)) return;
      const mid = v.message_id;
      if (mid != null && idbIds.has(mid)) return;  // already persisted
      const frags = v.fragments || v.content_blocks || [];
      if (frags.length === 0) return;
      candidates.push(v);
    }

    while (stack.length && nodes < 200000) {
      const node = stack.pop();
      nodes++;
      if (!node || seenFibers.has(node)) continue;
      seenFibers.add(node);

      // Hooks: useReducer / useState state lives on the memoizedState
      // linked list.  Each node.memoizedState is the first hook;
      // .next chains the rest.
      let state = node.memoizedState;
      let hIdx = 0;
      while (state && hIdx < 100) {
        consider(state.memoizedState);
        state = state.next;
        hIdx++;
      }

      // Props: some React shapes pass the message as a prop.
      const props = node.memoizedProps;
      if (props && typeof props === "object") {
        consider(props);
        for (const k of Object.keys(props)) {
          const v = props[k];
          if (v && typeof v === "object" && !seen.has(v)) {
            consider(v);
            if (Array.isArray(v)) {
              for (const item of v) {
                if (item && typeof item === "object") consider(item);
              }
            }
          }
        }
      }

      if (node.child) stack.push(node.child);
      if (node.sibling) stack.push(node.sibling);
    }

    if (candidates.length === 0) return null;
    for (const c of candidates) {
      if (_extractPromptTextOnly(c).trim().length > 0) return c;
    }
    return candidates[0];
  }

  async function doRetryLast(args) {
    const includeForked = !!args.include_forked;

    let msg = null;
    let source = null;
    let msgLabel = null;

    let pending = null;
    try {
      pending = await _findPendingMessage();
    } catch (_e) {
      pending = null;
    }
    if (pending) {
      msg = pending;
      source = "pending-fiber";
      msgLabel = pending.message_id != null
        ? pending.message_id
        : "(pending)";
    } else {
      const found = await _findLastUserPrompt(includeForked);
      if (!found) {
        const msgs = await window.__dsx__._collectIndexedDBMessages(
          includeForked) || [];
        let users = 0;
        const roles = new Set();
        for (const m of msgs) {
          roles.add(String(m.role));
          if (window.__dsx__.normalizeRoleFromIDB(m.role) === "user") users++;
        }
        return { success: false,
                 reason: `no user message found in this conversation ` +
                         `(${users} user of ${msgs.length} msg; ` +
                         `roles seen: ` +
                         `${Array.from(roles).join(", ")})` };
      }
      msg = found.msg;
      source = "indexeddb";
      msgLabel = msg.message_id;
    }

    const text = _extractPromptTextOnly(msg).trim();

    const frags = msg.fragments || msg.content_blocks || [];
    const rawFiles = [];
    for (const frag of frags) {
      for (const f of window.__dsx__._fragFiles(frag)) rawFiles.push(f);
    }

    const fetchResults = await Promise.all(rawFiles.map(async (rf) => {
      const name = rf.file_name || rf.fileName || "attachment";
      const size = rf.file_size || rf.fileSize || 0;
      const sp = rf.signed_path || rf.signedPath;
      if (!sp) return { name, error: "no signed_path on file record" };
      const r = await window.__dsx__._fetchAndEncode(sp, size);
      if (r.error) return { name, error: r.error };
      return { name, base64: r.base64 };
    }));

    const errors = [];
    const attachPayload = [];
    for (const fr of fetchResults) {
      if (fr.error) {
        errors.push({ name: fr.name, error: fr.error });
      } else {
        attachPayload.push({
          name: fr.name,
          mime: window.__dsx__._guessMimeFromName(fr.name),
          base64: fr.base64,
        });
      }
    }

    try {
      await window.__dsx__.doClear({});
    } catch (e) {
      console.warn("[deeppeek:retry-last] clear threw:", e);
    }

    let attachResult = null;
    if (attachPayload.length > 0) {
      try {
        attachResult = await window.__dsx__.doAttach(
          attachPayload, { delay_ms: 0, skip_preview_wait: true });
      } catch (e) {
        return {
          success: false,
          reason: `attach threw: ${e.message}`,
          message_id: msgLabel,
          files_fetched: attachPayload.length,
          file_count: rawFiles.length,
          errors,
        };
      }
    }

    let typeResult = null;
    if (text) {
      try {
        typeResult = await window.__dsx__.doType(text, { skip_verify: true });
      } catch (e) {
        return {
          success: false,
          reason: `type threw: ${e.message}`,
          message_id: msgLabel,
          attach_result: attachResult,
          errors,
        };
      }
    }

    return {
      success: true,
      source,
      message_id: msgLabel,
      file_count: rawFiles.length,
      files_fetched: attachPayload.length,
      files_attached: (attachResult && attachResult.dispatched)
                        || attachPayload.length,
      attach_result: attachResult,
      text_length: text.length,
      type_result: typeResult,
      errors: errors.length ? errors : undefined,
    };
  }

  window.__dsx__.doRetryLast = doRetryLast;

  window.__dsx_retry_last_json = async function (argsJson) {
    let args = {};
    try {
      if (typeof argsJson === "string" && argsJson.length > 0) {
        args = JSON.parse(argsJson);
      }
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    try {
      const result = await doRetryLast(args);
      console.log(
        `[deeppeek:retry-last] ${result.success ? "OK" : "FAIL"} ` +
        `(msg=${result.message_id || "?"}, ` +
        `${result.files_attached || 0}/${result.file_count || 0} ` +
        `file(s), ${result.text_length || 0} chars)`);
      return JSON.stringify(result);
    } catch (e) {
      console.error("[deeppeek:retry-last] threw:", e);
      return JSON.stringify({
        success: false,
        reason: `retry-last threw: ${e.message}`,
      });
    }
  };
})();