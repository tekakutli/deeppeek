// Runs in the page world.  Exposes:
//   window.__dsx_extract()             — extraction (existing)
//   window.__dsx_attach(files, opts)   — attach via objects (debug only)
//   window.__dsx_attach_json(json)     — attach via JSON (bridge entry)
//   window.__dsx_type_json(json)       — type text into composer (bridge entry)
//   window.__dsx_enter_json(json)      — press Enter on composer (bridge entry)
//   window.__dsx_clear_json(json)      — clear composer text and files
//
// Extraction sources, tried in order: indexeddb, then fiber.
// There is no DOM-sweep fallback — if both miss, extraction throws.
(function () {
  "use strict";

  const config = {
    chatContainerSelector: ".ds-virtual-list-visible-items",
    titleSelector: ".afa34042.e37a04e4.e0a1edb7",
  };

  // ==================================================================
  // Part 1 — page helpers
  // ==================================================================

  function getChatTitle() {
    const el = document.querySelector(config.titleSelector);
    return el ? el.textContent.trim() : null;
  }

  // ==================================================================
  // Part 2 — IndexedDB extraction
  // ==================================================================

  function getSessionIdFromURL() {
    const m = window.location.href.match(/\/s\/([0-9a-f-]{20,})/i);
    return m ? m[1] : null;
  }

  function normalizeRoleFromIDB(raw) {
    if (typeof raw !== "string") return null;
    const s = raw.toLowerCase();
    if (s === "user" || s === "human" || s === "customer") return "user";
    if (s === "assistant" || s === "ai" || s === "bot" || s === "model") return "assistant";
    return "assistant";
  }

  // -----------------------------------------------------------------
  // Active-path reconstruction
  // -----------------------------------------------------------------
  //
  // DeepSeek stores the conversation as a message TREE: each message
  // has a `parent_id`, and `chat_session.current_message_id` points
  // at the leaf the UI is currently rendering.  When the user edits
  // an earlier prompt (or regenerates a response), DeepSeek forks:
  // the old branch stays in IndexedDB but is no longer on the active
  // path and is not visible in the UI.  Sorting ALL messages by
  // `message_id` therefore resurrects every discarded fork.
  //
  // Reconstruct the active path by walking backward from
  // current_message_id via parent_id, then reverse to get
  // root-to-leaf order.  The resulting list is ALREADY in correct
  // temporal order — the parent chain is the ground truth.
  //
  // Fallbacks:
  //   · current_message_id missing/dangling → pick the latest leaf
  //     (a message id never used as a parent) by inserted_at.
  //   · the tree has no message_id/parent_id structure at all →
  //     return null so the caller keeps its old all-messages
  //     behaviour.
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
      // Chain is broken; the partial path is still far better than
      // all-messages, but the caller may want to know.
      path._incomplete = true;
    }
    return path;
  }

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
            // The parent chain already gives root-to-leaf order.
            // Do NOT re-sort by message_id: relying on message_id
            // monotonicity along the parent chain is safe today
            // but is not part of DeepSeek's protocol.
            sorted = activePath;
            if (activePath._incomplete) {
              LOG(`active path: ${activePath.length}/${raw.length} `
                  + `message(s); parent chain BROKEN before root `
                  + `(partial path used)`);
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
            const role = normalizeRoleFromIDB(m.role);
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

  function extractMessageBody(m) {
    let content = "";
    let thinking = null;
    const isStr = (v) => typeof v === "string" && v.trim().length > 0;

    if (isStr(m.content)) content = m.content;
    if (isStr(m.thinking_content)) thinking = m.thinking_content;
    if (isStr(m.reasoning_content)) thinking = m.reasoning_content;
    if (isStr(m.thinking)) thinking = m.thinking;
    if (isStr(m.reasoning)) thinking = m.reasoning;

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
            const name = file.file_name || file.fileName || file.name || "attachment";
            const size = typeof file.file_size === "number"
              ? ` (${(file.file_size / 1024).toFixed(1)} KB)`
              : "";
            fileParts.push(`[file: ${name}${size}]`);
          }
          continue;
        }
        const text =
          isStr(f.content) ? f.content
          : isStr(f.text) ? f.text
          : isStr(f.value) ? f.value : "";
        if (!text) continue;
        if (/THINK|REASON/.test(type)) thinkParts.push(text);
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

  // ==================================================================
  // Part 3 — Fiber walk fallback
  // ==================================================================

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

  function normalizeFiberMessage(m) {
    if (!m || typeof m !== "object") return null;
    let role = null;
    for (const k of ROLE_KEYS) {
      role = normalizeRole(m[k]);
      if (role) break;
    }
    if (!role) return null;
    const content = extractContentFromMessage(m);
    if (!content) return null;
    return { role, content };
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
    const container = document.querySelector(config.chatContainerSelector);
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

  // ==================================================================
  // Part 4 — orchestration (extract)
  // ==================================================================

  async function doExtraction(args) {
    const includeForked = !!(args && args.include_forked);
    const sessionId = getSessionIdFromURL();
    let messages = null;
    let source = null;

    if (sessionId) {
      try {
        messages = await extractViaIndexedDB(sessionId, includeForked);
        if (messages && messages.length) source = "indexeddb";
      } catch (e) { console.log("[deeppeek] idb threw:", e); }
    }
    if (!messages || !messages.length) {
      try {
        messages = extractViaFiber();
        if (messages && messages.length) source = "fiber";
      } catch (e) { console.log("[deeppeek] fiber threw:", e); }
    }
    if (!messages || !messages.length) {
      throw new Error("All extraction methods failed (indexeddb, fiber).");
    }

    console.log(`[deeppeek] extract source=${source} count=${messages.length}`);
    messages = messages.map((m, i) => ({ ...m, index: i }));
    return {
      title: getChatTitle(),
      url: window.location.href,
      messages,
    };
  }

  // ==================================================================
  // Part 5 — file attachment
  //
  // Confirmed against DeepSeek's current DOM:
  //   · dragover + drop on textarea.parentElement → works (primary)
  //   · input.files = DataTransfer + change       → works (fallback)
  //   · synthetic paste                            → blocked by Xray
  // ==================================================================

  function base64ToFile(b64, name, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: mime || "application/octet-stream" });
  }

  function findTextarea() {
    const active = document.activeElement;
    if (active && active.tagName === "TEXTAREA") {
      const r = active.getBoundingClientRect();
      if (r.width > 100 && r.height > 20) return active;
    }
    return [...document.querySelectorAll("textarea")].find((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 100 && r.height > 20;
    }) || null;
  }

  function findFileInput() {
    return document.querySelector('input[type="file"]');
  }

  function blobCount() {
    return document.querySelectorAll('img[src^="blob:"]').length;
  }

  async function attachViaDrop(files) {
    const textarea = findTextarea();
    if (!textarea || !textarea.parentElement) {
      return { success: false, reason: "no textarea parent" };
    }
    const target = textarea.parentElement;

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    if (dt.files.length !== files.length) {
      return { success: false, reason: `DataTransfer only holds ${dt.files.length}/${files.length}` };
    }

    try {
      const over = new DragEvent("dragover", {
        dataTransfer: dt, bubbles: true, cancelable: true,
      });
      target.dispatchEvent(over);
      if (!over.defaultPrevented) {
        return { success: false, reason: "dragover not accepted by target" };
      }
    } catch (e) {
      return { success: false, reason: `dragover threw: ${e.message}` };
    }

    try {
      const drop = new DragEvent("drop", {
        dataTransfer: dt, bubbles: true, cancelable: true,
      });
      target.dispatchEvent(drop);
      if (!drop.defaultPrevented) {
        return { success: false, reason: "drop not accepted by target" };
      }
      return { success: true, method: "drop", dispatched: files.length };
    } catch (e) {
      return { success: false, reason: `drop threw: ${e.message}` };
    }
  }

  async function attachViaFileInput(files) {
    const input = findFileInput();
    if (!input) return { success: false, reason: "no file input on page" };

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);

    try {
      input.files = dt.files;
    } catch (e) {
      return { success: false, reason: `input.files assignment threw: ${e.message}` };
    }
    if (input.files.length !== files.length) {
      return {
        success: false,
        reason: `input.files=${input.files.length} but files=${files.length}`,
      };
    }

    try {
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    } catch (e) {
      return { success: false, reason: `dispatch threw: ${e.message}` };
    }

    return { success: true, method: "file-input", dispatched: files.length };
  }

  async function attachOnce(files, skipPreviewWait) {
    const previewsBefore = blobCount();
    let result = await attachViaDrop(files);
    if (!result.success) {
      console.log("[deeppeek:attach] drop failed, trying file-input:", result.reason);
      const fallback = await attachViaFileInput(files);
      if (fallback.success) result = fallback;
      else result = {
        success: false,
        reason: `drop: ${result.reason}; file-input: ${fallback.reason}`,
      };
    }

    if (result.success && !skipPreviewWait) {
      await new Promise((r) => setTimeout(r, 800));
      const previewsAfter = blobCount();
      result.previews_before = previewsBefore;
      result.previews_after = previewsAfter;
      result.previews_added = previewsAfter - previewsBefore;
    } else if (result.success) {
      // Fast path: trust the drop, skip the 800ms preview count.
      // Used by retry, where the user visually verifies chips.
      result.previews_before = previewsBefore;
      result.previews_after = previewsBefore;
      result.previews_added = 0;
      result.skipped_preview_wait = true;
    }

    return result;
  }

  async function doAttach(filesPayload, opts) {
    const delayMs = Math.max(0, Number(opts && opts.delay_ms) || 0);
    const skipPreviewWait = !!(opts && opts.skip_preview_wait);

    let files;
    try {
      files = filesPayload.map((f) =>
        base64ToFile(f.base64, f.name, f.mime)
      );
    } catch (e) {
      return { success: false, reason: `base64 decode failed: ${e.message}` };
    }

    if (delayMs === 0) {
      const result = await attachOnce(files, skipPreviewWait);
      return { ...result, mode: "batch", total: files.length };
    }

    let successCount = 0;
    let lastMethod = null;
    let lastReason = null;
    const previewsBefore = blobCount();
    for (let i = 0; i < files.length; i++) {
      if (i > 0) await new Promise((r) => setTimeout(r, delayMs));
      const one = await attachOnce([files[i]]);
      if (one.success) {
        successCount++;
        lastMethod = one.method;
      } else {
        lastReason = `${files[i].name}: ${one.reason}`;
      }
    }
    await new Promise((r) => setTimeout(r, 800));
    const previewsAfter = blobCount();
    return {
      success: successCount === files.length,
      mode: "serial",
      method: lastMethod,
      total: files.length,
      dispatched: successCount,
      previews_before: previewsBefore,
      previews_after: previewsAfter,
      previews_added: previewsAfter - previewsBefore,
      reason: successCount < files.length ? lastReason : undefined,
    };
  }

  // ==================================================================
  // Part 6 — exports
  // ==================================================================

  window.__dsx_extract = async function (argsJson) {
    let args = {};
    if (typeof argsJson === "string" && argsJson.length > 0) {
      try { args = JSON.parse(argsJson); } catch (_) {}
    }
    return JSON.stringify(await doExtraction(args));
  };

  // Object-taking entry point, kept for direct console debugging:
  //   await window.__dsx_attach([{name:"a.png", mime:"image/png", base64:"…"}], {delay_ms: 0})
  // Does NOT work as a content-script bridge — Firefox Xray blocks
  // property access on content-script-owned objects from page-world
  // code.  The bridge uses __dsx_attach_json below.
  window.__dsx_attach = async function (filesPayload, opts) {
    const result = await doAttach(filesPayload, opts || {});
    console.log("[deeppeek:attach] result:", result);
    return JSON.stringify(result);
  };

  // Content-script bridge.  Args arrive as a single JSON string
  // (a primitive, marshaled transparently across Xray).  Returns a
  // JSON string for the same reason — __dsx_extract already uses
  // this pattern successfully.
  window.__dsx_attach_json = async function (argsJson) {
    let args;
    try {
      args = JSON.parse(argsJson);
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    const files = Array.isArray(args.files) ? args.files : [];
    const opts = (args.opts && typeof args.opts === "object") ? args.opts : {};
    const result = await doAttach(files, opts);
    console.log("[deeppeek:attach] result:", result);
    return JSON.stringify(result);
  };

  // ==================================================================
  // Part 7 — composer typing + submit
  //
  // Confirmed against live DeepSeek DOM (console tests, 2026-10-22):
  //
  //   SET   `document.execCommand('insertText')` on a focused,
  //         selected textarea.  A native setter + InputEvent does
  //         not satisfy React's state — the app sees the value but
  //         never updates submit-readiness, and the send control
  //         never mounts.  execCommand produces a trusted input
  //         event that React treats like real typing.
  //
  //   ENTER dispatchEvent(KeyboardEvent) for keydown/keypress/keyup
  //         with key=Enter.  DeepSeek's handler does not gate on
  //         isTrusted, so this works.  keyCode/which are deprecated
  //         but still read by the handler.
  //
  // Both are exposed as JSON-string entry points, matching the
  // existing __dsx_attach_json bridge.
  // ==================================================================

  const COMPOSER_STRATEGIES = [
    'textarea[placeholder*="Message DeepSeek"]',
    'textarea[data-testid="chat-input"]',
    '#chat-input',
    'div[contenteditable][role="textbox"]',
    'textarea',
  ];

  // ---- composer discovery -----------------------------------------
  //
  // Priority order (see the chat-side rationale in commands/type.py):
  //   1. active element that matches a strategy
  //   2. active element that is the largest visible editable
  //   3. first visible strategy match
  //   4. largest visible editable
  //   5. none -> error
  //
  // The previous version picked the first DOM match, which
  // mis-targeted any page with a search box, sidebar input, or
  // hidden textarea appearing before the composer.

  function _isEditable(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "textarea") return true;
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      return ["text", "search", "email", "url", "tel", "password"]
        .includes(t);
    }
    return false;
  }

  function _isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden")
      return false;
    if (parseFloat(style.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 50 && r.height >= 15;
  }

  function _describeEl(el) {
    if (!el) return null;
    return {
      tag: el.tagName || null,
      id: el.id || null,
      type: el.type || null,
      name: el.name || null,
      placeholder: el.placeholder || null,
      cls: (typeof el.className === "string")
        ? el.className.slice(0, 80) : null,
    };
  }

  function _largestVisibleEditable() {
    const candidates = [];
    for (const el of document.querySelectorAll(
        'textarea, input, [contenteditable="true"], [contenteditable=""]')) {
      if (_isEditable(el) && _isVisible(el)) candidates.push(el);
    }
    if (!candidates.length) return null;
    candidates.sort((a, b) => {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      return (rb.width * rb.height) - (ra.width * ra.height);
    });
    return candidates[0];
  }

  function _strategyMatches() {
    const out = [];
    const seen = new Set();
    for (const sel of COMPOSER_STRATEGIES) {
      for (const el of document.querySelectorAll(sel)) {
        if (seen.has(el)) continue;
        if (_isEditable(el) && _isVisible(el)) {
          out.push(el);
          seen.add(el);
        }
      }
    }
    return out;
  }

  function findComposer() {
    const active = document.activeElement;
    const activeOk = _isEditable(active) && _isVisible(active);
    const strategy = _strategyMatches();
    const largest = _largestVisibleEditable();

    // Priority 1: active element that matches a known strategy.
    if (activeOk && strategy.includes(active)) {
      return { el: active,
               selector: "active-element (matches strategy)",
               source: "active-matches-strategy",
               active, chosen: active };
    }

    // Priority 2: active element that is the largest visible editable.
    if (activeOk && largest && active === largest) {
      return { el: active,
               selector: "active-element (largest editable)",
               source: "active-largest",
               active, chosen: active };
    }

    // Priority 3: first visible strategy match.
    if (strategy.length) {
      return { el: strategy[0],
               selector: "strategy: " +
                         (strategy[0].getAttribute("data-testid")
                          || strategy[0].placeholder
                          || strategy[0].tagName.toLowerCase()),
               source: "selector",
               active, chosen: strategy[0] };
    }

    // Priority 4: largest visible editable, any selector.
    if (largest) {
      return { el: largest,
               selector: "largest-editable",
               source: "largest-editable",
               active, chosen: largest };
    }

    return { el: null, selector: null, source: "none",
             active, chosen: null };
  }

  function setComposerValue(el, text) {
    if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
      el.focus();
      if (typeof el.select === "function") el.select();
      try {
        if (document.execCommand("insertText", false, text)) return true;
      } catch (_) { /* fall through */ }
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (!desc || !desc.set) return false;
      desc.set.call(el, text);
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true,
        inputType: "insertText", data: text,
      }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (el.isContentEditable) {
      el.focus();
      el.textContent = text;
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true,
        inputType: "insertText", data: text,
      }));
      return true;
    }
    return false;
  }

  function pressEnter(el) {
    const init = {
      key: "Enter", code: "Enter", keyCode: 13, which: 13,
      bubbles: true, cancelable: true, composed: true,
    };
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keypress", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
  }

  async function doType(text, opts) {
    const skipVerify = !!(opts && opts.skip_verify);
    const settleMs = skipVerify ? 0 : (Number(opts.settle_ms) || 300);
    const found = findComposer();
    const el = found.el;
    if (!el) {
      return { success: false,
               reason: "no composer found on page",
               composer_source: "none",
               active_element_info: _describeEl(found.active) };
    }

    const before = el.value ?? el.textContent ?? "";
    if (!setComposerValue(el, text)) {
      return { success: false,
               reason: "could not set composer value",
               composer_selector: found.selector,
               composer_source: found.source,
               composer_element_info: _describeEl(el),
               active_element_info: _describeEl(found.active) };
    }
    if (skipVerify) {
      // Trust execCommand('insertText').  Verification via a
      // 300ms settle + read is wasted when the caller (retry)
      // has the user visually inspecting the composer.  If the
      // text didn't take, the user sees it and reruns.
      return { success: true,
               composer_selector: found.selector,
               composer_source: found.source,
               composer_element_info: _describeEl(el),
               active_element_info: _describeEl(found.active),
               value_before: before,
               value_after: text,
               react_accepted: true,
               skipped_verification: true };
    }
    await new Promise((r) => setTimeout(r, settleMs));

    const after = el.value ?? el.textContent ?? "";
    const react_accepted = after === text;

    return {
      success: react_accepted,
      composer_selector: found.selector,
      composer_source: found.source,
      composer_element_info: _describeEl(el),
      active_element_info: _describeEl(found.active),
      value_before: before,
      value_after: after,
      react_accepted,
      reason: react_accepted
        ? undefined
        : "React state did not accept the value",
    };
  }

  async function doEnter(opts) {
    const verifyMs = Number(opts.verify_ms) || 700;
    const found = findComposer();
    const el = found.el;
    if (!el) {
      return { success: false,
               reason: "no composer found on page",
               composer_source: "none",
               active_element_info: _describeEl(found.active) };
    }

    const before = el.value ?? el.textContent ?? "";
    if (!before) {
      return {
        success: false,
        reason: "composer is empty — nothing to submit",
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
      };
    }

    el.focus();
    pressEnter(el);
    await new Promise((r) => setTimeout(r, verifyMs));

    const after = el.value ?? el.textContent ?? "";
    const composer_cleared = after === "";

    return {
      success: composer_cleared,
      composer_selector: found.selector,
      composer_source: found.source,
      composer_element_info: _describeEl(el),
      active_element_info: _describeEl(found.active),
      value_before: before,
      value_after: after,
      submitted: true,
      composer_cleared,
      reason: composer_cleared
        ? undefined
        : "Enter fired but composer did not clear",
    };
  }

  window.__dsx_type_json = async function (argsJson) {
    let args;
    try {
      args = JSON.parse(argsJson);
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    const text = typeof args.text === "string" ? args.text : "";
    const opts = (args.opts && typeof args.opts === "object") ? args.opts : {};
    if (!text) {
      return JSON.stringify({ success: false, reason: "empty text" });
    }
    let result;
    try {
      result = await doType(text, opts);
    } catch (e) {
      result = { success: false, reason: `doType threw: ${e.message}` };
    }
    console.log("[deeppeek:type] result:", result);
    return JSON.stringify(result);
  };

  window.__dsx_enter_json = async function (argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || "{}"); } catch (_) {}
    const opts = (args.opts && typeof args.opts === "object") ? args.opts : {};
    let result;
    try {
      result = await doEnter(opts);
    } catch (e) {
      result = { success: false, reason: `doEnter threw: ${e.message}` };
    }
    console.log("[deeppeek:enter] result:", result);
    return JSON.stringify(result);
  };

  // ==================================================================
  // Part 9 — composer clearing (deeppeek prompt delete)
  // ==================================================================

  async function doClear(opts) {
    const result = {
      success: false,
      files_removed: 0,
      files_remaining: 0,
      textarea_cleared: false,
      textarea_found: false
    };

    // 1. Clear the textarea using the native setter
    const ta = document.querySelector('textarea');
    if (ta) {
      result.textarea_found = true;
      ta.focus();
      try {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')
          .set.call(ta, '');
        ta.dispatchEvent(new InputEvent('input', {
          bubbles: true, cancelable: true, inputType: 'deleteContentBackward',
        }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
        result.textarea_cleared = (ta.value === '');
      } catch (e) {
        console.warn('[deeppeek:clear] textarea clear failed:', e);
      }
    }

    // 2. Remove file chips rapidly
    let guard = 0;
    while (guard++ < 50) {
      // Re-query the first chip on each pass (since they shift)
      const chip = document.querySelector('.ds-animated-size-item');
      if (!chip) break;

      const tabbables = chip.querySelectorAll('div[tabindex="0"]');
      if (tabbables.length === 0) break;

      // The remove button is always the last tabindex="0" div in the chip
      const removeBtn = tabbables[tabbables.length - 1];
      const propsKey = Object.keys(removeBtn).find(k => k.startsWith('__reactProps$'));

      if (propsKey && removeBtn[propsKey] && typeof removeBtn[propsKey].onClick === 'function') {
        try {
          removeBtn[propsKey].onClick({
            preventDefault: () => {},
            stopPropagation: () => {},
            target: removeBtn,
            currentTarget: removeBtn,
            nativeEvent: new MouseEvent('click')
          });
          result.files_removed++;
        } catch (e) {
          console.warn('[deeppeek:clear] onClick failed:', e);
          break;
        }
      } else {
        try {
          removeBtn.click();
          result.files_removed++;
        } catch (_) {
          break;
        }
      }

      // Yield for 0ms. This just allows React one event-loop tick to flush the DOM update.
      await new Promise(r => setTimeout(r, 0));
    }

    result.files_remaining = document.querySelectorAll('.ds-animated-size-item').length;
    result.success = result.files_remaining === 0 && (result.textarea_cleared || !result.textarea_found);
    return result;
  }

  window.__dsx_clear_json = async function(argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || '{}'); } catch (_) {}
    const opts = (args.opts && typeof args.opts === 'object') ? args.opts : {};
    let result;
    try {
      result = await doClear(opts);
    } catch (e) {
      result = { success: false, reason: `doClear threw: ${e.message}` };
    }
    console.log('[deeppeek:clear] result:', result);
    return JSON.stringify(result);
  };
  // ==================================================================
  // Part 10 — attachment extraction (deeppeek prompt attachments)
  // ==================================================================
  //
  // Two discovery paths, one fetch path:
  //
  //   composer draft / edit-mode  →  fiber walk → memoizedProps.fileList
  //                                   (camelCase: fileName, signedPath)
  //
  //   sent message attachment     →  IndexedDB history-message
  //                                   (snake_case: file_name, signed_path)
  //
  // Both normalize to one record and feed the same fetcher.
  //
  // SignedPath is a RELATIVE URL.  The real endpoint is:
  //
  //     https://files.deepseeksvc.com/api + signedPath + "&ty=r"
  //
  // The `state` token authenticates; `file_id` is decorative (POC 9:
  // corrupting it still returns the same bytes).  `ty=r` is required
  // by CORS policy.  `credentials: 'omit'` is correct — the token
  // carries auth on its own.
  //
  // Tokens do NOT expire within the conversation's lifetime (POC 7:
  // message 1's attachment still fetches correctly at the end of a
  // 353-message conversation).
  //
  // Every fetched byte count is checked against metadata.file_size.
  // Mismatch = SPA shell served instead of file (this was the
  // silent failure mode that produced 8.5KB HTML files in earlier
  // POCs).  Mismatched fetches are reported with the first 48 bytes
  // so the user can see what came back.

  const DSX_FILE_BASE = "https://files.deepseeksvc.com/api";

  function _resolveFileUrl(signedPath) {
    if (!signedPath) return null;
    let url = signedPath.startsWith("/")
      ? DSX_FILE_BASE + signedPath
      : signedPath;
    if (!url.includes("ty=")) {
      url += (url.includes("?") ? "&" : "?") + "ty=r";
    }
    return url;
  }

  function _guessMimeFromName(name) {
    const lower = (name || "").toLowerCase();
    const map = {
      ".md": "text/markdown", ".txt": "text/plain",
      ".json": "application/json", ".py": "text/x-python",
      ".js": "text/javascript", ".mjs": "text/javascript",
      ".ts": "text/typescript", ".html": "text/html",
      ".css": "text/css", ".yml": "text/yaml",
      ".yaml": "text/yaml", ".toml": "text/toml",
      ".xml": "text/xml", ".csv": "text/csv", ".sh": "text/x-sh",
      ".png": "image/png", ".jpg": "image/jpeg",
      ".jpeg": "image/jpeg", ".gif": "image/gif",
      ".webp": "image/webp", ".svg": "image/svg+xml",
      ".pdf": "application/pdf", ".zip": "application/zip",
    };
    for (const ext in map) if (lower.endsWith(ext)) return map[ext];
    return "application/octet-stream";
  }

  // Shared byte fetcher: validates signed_path, resolves URL,
  // fetches, and verifies byte length against metadata.  Every
  // downstream consumer (download, base64) reuses this so URL
  // resolution, size check, and error formatting exist exactly
  // once.
  async function _fetchBytes(signedPath, expectedSize) {
    const url = _resolveFileUrl(signedPath);
    if (!url) {
      return { buf: null, bytes_received: null,
               error: "no signedPath on file record" };
    }
    try {
      const resp = await fetch(url, { credentials: "omit" });
      if (!resp.ok) {
        return { buf: null, bytes_received: null,
                 error: `HTTP ${resp.status} ${resp.statusText}` };
      }
      const buf = await resp.arrayBuffer();
      if (expectedSize && buf.byteLength !== expectedSize) {
        const head = new Uint8Array(buf.slice(0, 48));
        const asText = new TextDecoder("utf-8", { fatal: false })
          .decode(head);
        const hex = Array.from(head.slice(0, 8))
          .map(b => b.toString(16).padStart(2, "0")).join(" ");
        return {
          buf: null,
          bytes_received: buf.byteLength,
          error: `size mismatch: expected ${expectedSize}, got ` +
                 `${buf.byteLength} (first bytes: ${hex} | ` +
                 `${JSON.stringify(asText.slice(0, 32))})`,
        };
      }
      return { buf, bytes_received: buf.byteLength, error: null };
    } catch (e) {
      return { buf: null, bytes_received: null,
               error: `fetch threw: ${e.message}` };
    }
  }

  // Consumer: trigger browser download (used by prompt attachments).
  async function _fetchAndDownload(signedPath, expectedSize,
                                    listOnly, downloadName) {
    if (listOnly) {
      return { downloaded: false, bytes_received: null, error: null };
    }
    const r = await _fetchBytes(signedPath, expectedSize);
    if (r.error) {
      return { downloaded: false,
               bytes_received: r.bytes_received,
               error: r.error };
    }
    // Route through <a download> + Blob URL so files land wherever
    // the browser's download directory points.
    const blob = new Blob([r.buf]);
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = downloadName || "attachment";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      try { a.remove(); } catch (_) {}
      try { URL.revokeObjectURL(objUrl); } catch (_) {}
    }, 1500);
    return { downloaded: true,
             bytes_received: r.bytes_received, error: null };
  }

  // Consumer: base64-encode for composer re-attachment
  // (used by prompt retry).
  async function _fetchAndEncode(signedPath, expectedSize) {
    const r = await _fetchBytes(signedPath, expectedSize);
    if (r.error) {
      return { base64: null, bytes_received: r.bytes_received,
               error: r.error };
    }
    const bytes = new Uint8Array(r.buf);
    let binary = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(i, i + CHUNK));
    }
    return { base64: btoa(binary),
             bytes_received: r.bytes_received, error: null };
  }

  function _normalizeFileRecord(rawFile, msg, msgIdx, source) {
    const name = rawFile.file_name || rawFile.fileName || "(unnamed)";
    const size = rawFile.file_size || rawFile.fileSize || 0;
    const sp = rawFile.signed_path || rawFile.signedPath || null;
    return {
      message_id: (msg && msg.message_id != null)
                    ? msg.message_id : null,
      message_index: msgIdx,
      source,
      file_name: name,
      file_size: size,
      status: rawFile.status || "UNKNOWN",
      signed_path: sp,
      mime: _guessMimeFromName(name),
      base64: null,
      bytes_received: null,
      error: null,
    };
  }

  async function _collectIndexedDBMessages(includeForked) {
    const sessionId = getSessionIdFromURL();
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

  function _fragFiles(frag) {
    if (!frag || String(frag.type).toUpperCase() !== "FILE") return [];
    return Array.isArray(frag.files) ? frag.files : [];
  }

  function _hasFiles(msg) {
    const frags = msg.fragments || msg.content_blocks || [];
    return frags.some(f => _fragFiles(f).length > 0);
  }

  async function _extractFocus() {
    const chip = document.querySelector(".ds-animated-size-item");
    if (!chip) {
      return { success: true, mode: "focus", files: [],
               reason: "no attachment chip in composer" };
    }
    let fiber = chip[Object.keys(chip)
      .find(k => k.startsWith("__reactFiber"))];
    let fileList = null;
    while (fiber && !fileList) {
      if (fiber.memoizedProps && fiber.memoizedProps.fileList) {
        fileList = fiber.memoizedProps.fileList;
        break;
      }
      fiber = fiber.return;
    }

    // Composer identity: messageId in the textarea's fiber chain.
    //   -2   → new draft (not associated with a sent message)
    //   +N   → editing sent message N
    let composerMessageId = null;
    const ta = document.querySelector("textarea");
    if (ta) {
      let tf = ta[Object.keys(ta)
        .find(k => k.startsWith("__reactFiber"))];
      let d = 0;
      while (tf && d < 30) {
        if (tf.memoizedProps &&
            tf.memoizedProps.messageId !== undefined) {
          composerMessageId = tf.memoizedProps.messageId;
          break;
        }
        tf = tf.return; d++;
      }
    }

    const files = (fileList || []).map(f =>
      _normalizeFileRecord(f, { message_id: composerMessageId },
                            -1, "composer"));
    return {
      success: true, mode: "focus",
      composer_message_id: composerMessageId,
      files,
    };
  }

  async function _extractByPromptRegex(pattern, flags, includeForked,
                                        allMatches) {
    const messages = await _collectIndexedDBMessages(includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    let re;
    try { re = new RegExp(pattern, flags); }
    catch (e) {
      return { success: false, ok: false,
               reason: `invalid regex: ${e.message}` };
    }

    // Backward scan: most recent matching prompt with attachments
    // wins.  Same semantics as capture --last-regex.
    const matches = [];
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (normalizeRoleFromIDB(m.role) !== "user") continue;
      const _promptText = extractMessageBody(m).content || "";
      if (!re.test(_promptText)) continue;
      if (!_hasFiles(m)) continue;
      matches.push({ msg: m, idx: i });
      if (!allMatches) break;
    }

    if (matches.length === 0) {
      return { success: true, mode: "prompt-regex",
               files: [], matched_message_ids: [],
               reason: "no user prompt matching regex had attachments" };
    }

    const files = [];
    for (const { msg, idx } of matches) {
      const frags = msg.fragments || msg.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(_normalizeFileRecord(
            f, msg, idx, "prompt-regex"));
        }
      }
    }
    return {
      success: true, mode: "prompt-regex",
      matched_message_ids: matches.map(x => x.msg.message_id),
      files,
    };
  }

  async function _extractByMessageId(id, includeForked) {
    const messages = await _collectIndexedDBMessages(includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const files = [];
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      if (m.message_id !== id) continue;
      const frags = m.fragments || m.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(_normalizeFileRecord(f, m, i, "message-id"));
        }
      }
    }
    return { success: true, mode: "message-id",
             matched_message_ids: [id], files };
  }

  async function _extractLastN(n, includeForked) {
    const messages = await _collectIndexedDBMessages(includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const picks = [];
    for (let i = messages.length - 1; i >= 0 && picks.length < n; i--) {
      if (_hasFiles(messages[i])) {
        picks.push({ msg: messages[i], idx: i });
      }
    }
    picks.reverse();  // oldest first in the batch
    const files = [];
    for (const { msg, idx } of picks) {
      const frags = msg.fragments || msg.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(_normalizeFileRecord(f, msg, idx, "last-N"));
        }
      }
    }
    return {
      success: true, mode: "last-N",
      matched_message_ids: picks.map(p => p.msg.message_id),
      files,
    };
  }

  async function _extractAll(includeForked) {
    const messages = await _collectIndexedDBMessages(includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const files = [];
    const ids = new Set();
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      const frags = m.fragments || m.content_blocks || [];
      let any = false;
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(_normalizeFileRecord(f, m, i, "all"));
          any = true;
        }
      }
      if (any) ids.add(m.message_id);
    }
    return {
      success: true, mode: "all",
      matched_message_ids: Array.from(ids),
      files,
    };
  }

  async function _extractByFilename(pattern, caseSensitive,
                                     includeForked) {
    const messages = await _collectIndexedDBMessages(includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const needle = caseSensitive ? pattern : pattern.toLowerCase();
    const files = [];
    const ids = new Set();
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      const frags = m.fragments || m.content_blocks || [];
      let any = false;
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          const name = f.file_name || f.fileName || "";
          const hay = caseSensitive ? name : name.toLowerCase();
          if (!hay.includes(needle)) continue;
          files.push(_normalizeFileRecord(f, m, i, "filename"));
          any = true;
        }
      }
      if (any) ids.add(m.message_id);
    }
    return {
      success: true, mode: "filename",
      matched_message_ids: Array.from(ids),
      files,
    };
  }

  async function doExtractAttachments(args) {
    const mode = args.mode || "focus";
    const listOnly = !!args.list_only;
    const includeForked = !!args.include_forked;

    let result;
    if (mode === "focus") {
      result = await _extractFocus();
    } else if (mode === "prompt-regex") {
      result = await _extractByPromptRegex(
        args.pattern, args.flags || "i", includeForked,
        !!args.all_matches);
    } else if (mode === "message-id") {
      result = await _extractByMessageId(
        Number(args.message_id), includeForked);
    } else if (mode === "last-N") {
      result = await _extractLastN(
        Number(args.last), includeForked);
    } else if (mode === "all") {
      result = await _extractAll(includeForked);
    } else if (mode === "filename") {
      result = await _extractByFilename(
        args.pattern, !!args.case_sensitive, includeForked);
    } else {
      return { success: false, ok: false,
               reason: `unknown mode: ${mode}` };
    }

    if (!result.success) return result;

    // Sequential fetch (POC 9: 14–433ms per file; parallel risks
    // throttling and would confuse per-file diagnostics).
    for (const f of result.files) {
      if (!f.signed_path) {
        f.error = "no signed_path on file record";
        continue;
      }
      const fetched = await _fetchAndDownload(
        f.signed_path, f.file_size, listOnly, f.file_name);
      f.downloaded = fetched.downloaded;
      f.bytes_received = fetched.bytes_received;
      if (fetched.error) f.error = fetched.error;
      // Give Firefox a beat between downloads so it
      // registers each one individually instead of
      // coalescing them into a "Save As" prompt storm.
      if (!listOnly && fetched.downloaded) {
        await new Promise(r => setTimeout(r, 200));
      }
    }

    result.ok = true;
    result.title = getChatTitle();
    result.url = window.location.href;
    return result;
  }

  window.__dsx_extract_attachments_json = async function (argsJson) {
    let args = {};
    try {
      if (typeof argsJson === "string" && argsJson.length > 0) {
        args = JSON.parse(argsJson);
      }
    } catch (e) {
      return JSON.stringify({
        success: false, ok: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    try {
      const result = await doExtractAttachments(args);
      console.log(
        `[deeppeek:attachments] mode=${args.mode || "focus"} ` +
        `${(result.files || []).length} file(s) ` +
        `(${(result.files || []).filter(f => !f.error).length} ok)`);
      return JSON.stringify(result);
    } catch (e) {
      console.error("[deeppeek:attachments] threw:", e);
      return JSON.stringify({
        success: false, ok: false,
        reason: `extract threw: ${e.message}`,
      });
    }
  };


  // ==================================================================
  // Part 11 — retry-last (deeppeek prompt retry)
  // ==================================================================
  //
  // Recreates the setup of the most recent user prompt that had
  // attachments: re-attaches those files to the composer, and
  // retypes the prompt's original text.
  //
  // All work happens in the page world — no base64 crosses the
  // CLI boundary.  Reuses doClear (Part 9) to reset the composer,
  // doAttach (Part 5) to place the files, doType (Part 7) to fill
  // the textarea, and _fetchAndEncode (Part 10) to reconstruct
  // File objects from the stored signed_path URLs.
  //
  // Message selection: the last user message in the conversation
  // with at least one FILE fragment.  If none, returns an error.

  async function _findLastUserPrompt(includeForked) {
    const messages = await _collectIndexedDBMessages(includeForked);
    if (!messages) return null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (normalizeRoleFromIDB(m.role) !== "user") continue;
      return { msg: m, idx: i, total: messages.length };
    }
    return null;
  }


  // Extract only the prompt text (TEXT/REQUEST fragments), skipping
  // FILE fragments (their `[file: ...]` lines are capture-time
  // display artifacts, not prompt text) and THINK/REASON fragments
  // (reasoning, not the user's question).  Used by doRetryLast so
  // retry types the actual prompt, not the file-placeholder prefix
  // that extractMessageBody produces for the capture pipeline.
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
  // completes.  If the POST fails or the message otherwise fails to
  // persist, the rendered message stays in fiber state but is never
  // written to history-message.  On reload, it is gone.
  //
  // The composer's own useState / useReducer hook (typically
  // hook[13]) holds the optimistic pending message until it either
  // persists or rolls back.  We scan every hook and every props
  // object for message-shaped objects whose message_id is missing
  // or absent from IndexedDB.
  //
  // Returns the first matching message object, or null.  The
  // pending message often has no message_id; the caller uses a
  // "(pending)" label in that case.
  async function _findPendingMessage() {
    // Collect known persisted message_ids so we can distinguish
    // "pending" (not in IDB) from "already persisted".
    const idbMessages = await _collectIndexedDBMessages(true);
    const idbIds = new Set();
    if (idbMessages) {
      for (const m of idbMessages) {
        if (m && m.message_id != null) idbIds.add(m.message_id);
      }
    }

    // Locate the React container.  __reactContainer$ is what React
    // attaches to the root DOM node; walking up from body suffices.
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
    // Prefer a candidate whose prompt text is non-empty.
    for (const c of candidates) {
      if (_extractPromptTextOnly(c).trim().length > 0) return c;
    }
    return candidates[0];
  }

  async function doRetryLast(args) {
    const includeForked = !!args.include_forked;

    // Selection order: pending fiber state (failed submissions
    // live here until reload) beats persisted IndexedDB history.
    let msg = null;
    let source = null;
    let msgLabel = null;

    let pending = null;
    try {
      pending = await _findPendingMessage();
    } catch (_e) {
      // Fiber walk failed for any reason — fall through to IDB.
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
        const _msgs = await _collectIndexedDBMessages(includeForked) || [];
        let _users = 0;
        const _roles = new Set();
        for (const _m of _msgs) {
          _roles.add(String(_m.role));
          if (normalizeRoleFromIDB(_m.role) === "user") _users++;
        }
        return { success: false,
                 reason: `no user message found in this conversation ` +
                         `(${_users} user of ${_msgs.length} msg; ` +
                         `roles seen: ` +
                         `${Array.from(_roles).join(", ")})` };
      }
      msg = found.msg;
      source = "indexeddb";
      msgLabel = msg.message_id;
    }

    // Extract only the prompt text: skips FILE fragments (their
    // "[file: ...]" lines are capture-time display artifacts,
    // not prompt text) and THINK/REASON fragments (reasoning,
    // not the user's question).
    const text = _extractPromptTextOnly(msg).trim();

    // Collect raw file records.
    const frags = msg.fragments || msg.content_blocks || [];
    const rawFiles = [];
    for (const frag of frags) {
      for (const f of _fragFiles(frag)) rawFiles.push(f);
    }

    // Fetch all files in PARALLEL.  Sequential fetches take
    // ~400ms each on cold cache; parallelizing collapses that
    // to ~400ms total regardless of N.  Order is preserved by
    // mapping over rawFiles and awaiting Promise.all.
    const fetchResults = await Promise.all(rawFiles.map(async (rf) => {
      const name = rf.file_name || rf.fileName || "attachment";
      const size = rf.file_size || rf.fileSize || 0;
      const sp = rf.signed_path || rf.signedPath;
      if (!sp) return { name, error: "no signed_path on file record" };
      const r = await _fetchAndEncode(sp, size);
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
          mime: _guessMimeFromName(fr.name),
          base64: fr.base64,
        });
      }
    }

    // Reset composer before placing new state so retry starts
    // from a clean slate (no stale chips, no stale text).
    try {
      await doClear({});
    } catch (e) {
      console.warn("[deeppeek:retry-last] clear threw:", e);
    }

    // Attach files (batch mode — single dragover/drop).
    let attachResult = null;
    if (attachPayload.length > 0) {
      try {
        attachResult = await doAttach(
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

    // Type original prompt text (only if there was any).
    let typeResult = null;
    if (text) {
      try {
        typeResult = await doType(text, { skip_verify: true });
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

  // ==================================================================
  // DSX_URL: the local CLI's HTTP server.  Page-world scripts
  // cannot see background.js's DSX_URL — the extension and the
  // page have separate top-level scopes — so this constant has
  // to be defined here, next to its only consumer (_postDownload).
  const DSX_URL = "http://127.0.0.1:8765";

  // Part 12 — download hijacker (deeppeek hijack)
  // ==================================================================
  //
  // Intercepts code-block "Download" button clicks before React's
  // handler runs. Suppresses the default browser download, extracts
  // the block's text content, and POSTs {filename, base64} to the
  // CLI's /export endpoint. The CLI writes the file to its cwd
  // instead of the browser's Downloads directory.
  //
  // Only `.md-code-block` blocks are hijacked. File-chip downloads
  // use a different UI path and are out of scope.

  const DOWNLOAD_EXT_MAP = {
    js: 'js', javascript: 'js', jsx: 'jsx', mjs: 'mjs', cjs: 'cjs',
    ts: 'ts', typescript: 'ts', tsx: 'tsx',
    py: 'py', python: 'py', pyi: 'pyi',
    sh: 'sh', bash: 'sh', zsh: 'sh', shell: 'sh',
    rb: 'rb', ruby: 'rb', go: 'go', rs: 'rs', rust: 'rs',
    java: 'java', kt: 'kt', kotlin: 'kt', scala: 'scala',
    groovy: 'groovy', c: 'c', cpp: 'cpp', 'c++': 'cpp', cc: 'cc',
    cxx: 'cxx', h: 'h', hpp: 'hpp',
    php: 'php', pl: 'pl', perl: 'pl', lua: 'lua',
    swift: 'swift', dart: 'dart', m: 'm',
    html: 'html', htm: 'html', css: 'css', scss: 'scss',
    sass: 'sass', less: 'less',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    ini: 'ini', cfg: 'cfg', xml: 'xml', svg: 'svg', csv: 'csv',
    md: 'md', markdown: 'md', rst: 'rst', tex: 'tex',
    sql: 'sql', graphql: 'graphql', gql: 'graphql',
    proto: 'proto', vue: 'vue', svelte: 'svelte',
    dockerfile: 'Dockerfile', makefile: 'Makefile',
    diff: 'diff', patch: 'patch',
    txt: 'txt', text: 'txt', plaintext: 'txt', log: 'log',
  };

  function _downloadTimestamp() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
         + `_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  function _sniffExtFromContent(text, defaultExt) {
    const first = (text.split('\n')[0] || '').trim();
    if (first.startsWith('#!')) {
      if (/bash|sh|zsh/.test(first)) return 'sh';
      if (/python/.test(first))     return 'py';
      if (/node/.test(first))       return 'js';
      if (/ruby/.test(first))       return 'rb';
      if (/perl/.test(first))       return 'pl';
    }
    return defaultExt;
  }

  // ================================================================
  // Part 12a — filename resolution
  // ================================================================
  //
  // Ports the recover pipeline's four filename strategies, chained
  // in the same priority order used by recover.py:
  //
  //   1. Header parse      — recover/extract._parse_file_header
  //        "## File: foo.py", "## Fix 1 (\`foo.py\`)", "== foo.py ==",
  //        "1. foo.py — modifier", "path: foo.py", bare "foo.py",
  //        and the single-filename natural-language fallback.
  //
  //   2. Prose single     — extract._SINGLE_FILENAME_RE fallback
  //        Exactly one source-extension filename anywhere in the
  //        preceding prose ⇒ that's the target.
  //
  //   3. Prose backtick   — attribute._files_mentioned_in
  //        Closest prose only. Handles "two paragraphs back" and
  //        "prose names several files, take the nearest" cases.
  //
  //   4. Body comment     — prose_filter.filename_in_orphan_header
  //        First 5 lines, comment only. "// foo.py" at the top is
  //        the AI's own header for headerless fragments.

  const _SOURCE_EXT_PATTERN =
    'py|js|mjs|cjs|jsx|ts|tsx|md|json|yaml|yml|toml|sh|bash|zsh|' +
    'html|htm|css|scss|sass|less|rs|go|rb|php|java|kt|kts|scala|' +
    'c|cc|cpp|cxx|h|hpp|swift|dart|m|sql|graphql|gql|proto|vue|' +
    'svelte|xml|svg|csv|ini|cfg|conf|env|txt|log';

  const _FILENAME_RE = new RegExp(
    '\\b([A-Za-z0-9_./\\-]+\\.(?:' + _SOURCE_EXT_PATTERN + '))\\b',
    'i'
  );
  const _FILENAME_RE_G = new RegExp(
    '\\b([A-Za-z0-9_./\\-]+\\.(?:' + _SOURCE_EXT_PATTERN + '))\\b',
    'gi'
  );

  function _isSourceExt(ext) {
    return new RegExp(`^(${_SOURCE_EXT_PATTERN})$`, 'i')
      .test(ext || '');
  }

  function _parseFileHeaderText(text) {
    // Direct port of recover/extract._parse_file_header.
    let s = (text || '').trim();
    if (!s) return null;

    // Strip markdown-link wrapper: [text](url) -> text
    const linkMatch = s.match(/^\[([^\]]+)\]\([^)]+\)\s*$/);
    if (linkMatch) s = linkMatch[1].trim();
    s = s.replace(/[*`]/g, '');

    // "File: foo.py" / "path: foo.py" / "source: foo.py (mod)"
    const m1 = s.match(
      /^(?:file|path|source)\s*:\s*([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]+)(?:\s*(?:\(([^)]+)\)|—(.+?)|-(.+?)))?\s*$/i
    );
    if (m1) {
      return {
        path: m1[1],
        modifier: (m1[2] || m1[3] || m1[4] || '').trim() || null,
      };
    }

    // "== foo.py =="
    const m2 = s.match(/^==+\s*(\S+)\s*==+$/);
    if (m2) return { path: m2[1], modifier: null };

    // "[foo.py]"
    const m3 = s.match(/^\[([^\]]+)\]$/);
    if (m3 && m3[1].includes('.')) return { path: m3[1], modifier: null };

    // "1. foo.py — modifier" / "foo.py (mod)" / "foo.py — mod"
    const m4 = s.match(
      /^(?:(?:\d+)\s*[.):\-]\s*)?([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]{1,6})\s*(?:(?:—|–|\s-\s|\(|\[)\s*(.+?)\s*(?:\)|\])?)?\s*$/
    );
    if (m4) {
      const ext = m4[1].split('.').pop();
      if (_isSourceExt(ext)) {
        return {
          path: m4[1],
          modifier: (m4[2] || '').trim() || null,
        };
      }
    }

    // Bare filename
    const m5 = s.match(/^([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]{1,5})$/);
    if (m5) {
      const ext = m5[1].split('.').pop();
      if (_isSourceExt(ext) || m5[1].includes('/')) {
        return { path: m5[1], modifier: null };
      }
    }

    // Natural-language fallback: exactly one source-extension
    // filename anywhere in the text.
    const matches = s.match(_FILENAME_RE_G);
    if (matches && matches.length === 1) {
      return { path: matches[0], modifier: null };
    }

    return null;
  }

  function _collectPrecedingProse(block, maxItems) {
    // Reproduces recover/extract.pass1_extract's `current_context_lines`
    // buffering, at DOM granularity: walk the block's previousElementSibling
    // chain up to (and not past) the previous code block, collecting
    // element innerText. Return in document order (earliest first).
    const parts = [];
    let node = block.previousElementSibling;
    let count = 0;
    const limit = maxItems || 4;
    while (node && count < limit) {
      if (node.classList &&
          (node.classList.contains('md-code-block') ||
           node.classList.contains('md-code-block-banner-wrap'))) {
        break;
      }
      const txt = (node.innerText || '').trim();
      if (txt) {
        parts.unshift(txt);
        count++;
      }
      node = node.previousElementSibling;
    }
    return parts;
  }

  function _extractFilenameFromBodyComment(text) {
    // Direct port of recover/prose_filter.filename_in_orphan_header,
    // with the additional "must be a comment line" guard so a
    // statement like `print("foo.py")` doesn't trigger the header.
    const lines = (text || '').split('\n').slice(0, 5);
    const scanned = lines.join('\n');
    const m = scanned.match(_FILENAME_RE);
    if (!m) return null;

    for (const line of lines) {
      if (!line.includes(m[1])) continue;
      const trimmed = line.trim();
      const isComment =
        trimmed.startsWith('//') ||
        trimmed.startsWith('#') ||
        trimmed.startsWith('/*') ||
        trimmed.startsWith('*') ||
        trimmed.startsWith('<!--') ||
        trimmed.startsWith('--');
      if (!isComment) return null;
      break;
    }
    return m[1];
  }

  function _deriveFilenameFromContext(block, bodyText) {
    const preceding = _collectPrecedingProse(block, 4);

    // 1. Header parse, closest-to-block first.
    for (let i = preceding.length - 1; i >= 0; i--) {
      const parsed = _parseFileHeaderText(preceding[i]);
      if (parsed && parsed.path) {
        return { path: parsed.path, source: 'prose-header' };
      }
    }

    // 2. Single source filename anywhere in the prose.
    if (preceding.length) {
      const allProse = preceding.join('\n\n');
      const matches = allProse.match(_FILENAME_RE_G) || [];
      const unique = Array.from(new Set(matches));
      if (unique.length === 1) {
        return { path: unique[0], source: 'prose-single' };
      }

      // 3. Backtick mentions in the CLOSEST prose only.
      const closest = preceding[preceding.length - 1] || '';
      const backtickFiles = [];
      for (const m of closest.matchAll(/`([^`\n]+)`/g)) {
        const content = (m[1] || '').trim();
        if (_FILENAME_RE.test(content)) backtickFiles.push(content);
      }
      if (backtickFiles.length) {
        return {
          path: backtickFiles[backtickFiles.length - 1],
          source: 'prose-backtick',
        };
      }
    }

    // 4. Body comment header.
    const bodyHeader = _extractFilenameFromBodyComment(bodyText);
    if (bodyHeader) return { path: bodyHeader, source: 'body-comment' };

    return null;
  }

  function _deriveDownloadFilename(block, langLabel, text) {
    const declared = _deriveFilenameFromContext(block, text);
    if (declared && declared.path) {
      return declared;
    }
    const rawLang = (langLabel || '').toLowerCase().trim();
    let ext = DOWNLOAD_EXT_MAP[rawLang] || 'txt';
    ext = _sniffExtFromContent(text || '', ext);
    const safeLang = rawLang
      ? (rawLang.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'code')
      : 'code';
    return {
      path: `${safeLang}_${_downloadTimestamp()}.${ext}`,
      source: 'lang-fallback',
    };
  }

  function _extractLangLabel(block) {
    const banner = block.querySelector('.md-code-block-banner');
    if (!banner) return '';
    const first = ((banner.innerText || '').split('\n')[0] || '').trim();
    if (!first) return '';
    if (/^(开头|结尾|Copy|Download)$/i.test(first)) return '';
    if (/^[A-Za-z0-9_+.+#-]+$/.test(first)) return first;
    return '';
  }

  function _extractCodeText(block) {
    // textContent, not innerText — innerText collapses whitespace and
    // would mangle Python/YAML/JavaScript indentation.
    const code = block.querySelector('code') || block.querySelector('pre');
    return code ? (code.textContent || '') : '';
  }

  function _bytesToBase64(bytes) {
    let binary = '';
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  // MODIFIED (2026-11-05): `lang` is now forwarded in the payload
  // so the CLI can use the correct parser when it needs to fall
  // back to structural derivation of the filename.  When the
  // extension's prose-based derivation succeeded, `source` will be
  // one of `prose-header` / `prose-single` / `prose-backtick` /
  // `body-comment` and the CLI uses the filename as-is.  When the
  // extension fell through to `lang-fallback` (timestamped name),
  // the CLI runs structural analysis on the decoded bytes with the
  // correct language hint and derives the real filename from the
  // module docstring / leading comment.
  async function _postDownload(filename, text, source, lang) {
    const bytes = new TextEncoder().encode(text);
    const base64 = _bytesToBase64(bytes);
    try {
      const resp = await fetch(`${DSX_URL}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          __ack: 'download',
          filename,
          base64,
          source: source || 'code-block',
          lang: lang || '',
          size: bytes.length,
        }),
      });
      return resp.ok;
    } catch (e) {
      console.warn('[deeppeek:hijack] POST failed:', e);
      return false;
    }
  }

  function _isDownloadButton(btn) {
    const span = btn.querySelector('.code-info-button-text');
    const text = ((span ? span.innerText : btn.innerText) || '')
      .trim().toLowerCase();
    return text === 'download' || text === '下载' || text === 'descargar';
  }

  function _hijackClickHandler(ev) {
    const path = ev.composedPath ? ev.composedPath() : [];
    let btn = null;
    for (const el of path) {
      if (el && el.classList && el.classList.contains('ds-button')) {
        btn = el;
        break;
      }
    }
    if (!btn || !_isDownloadButton(btn)) return;

    const block = btn.closest('.md-code-block');
    if (!block) return;
    if (!block.querySelector('.md-code-block-banner')) return;

    // Suppress the default browser download.
    ev.preventDefault();
    ev.stopPropagation();
    ev.stopImmediatePropagation();

    const langLabel = _extractLangLabel(block);
    const text = _extractCodeText(block);
    if (!text) {
      console.warn('[deeppeek:hijack] empty code block, nothing to send');
      return;
    }

    const derived = _deriveDownloadFilename(block, langLabel, text);
    console.log(
      `[deeppeek:hijack] ${derived.path} (${text.length} chars, `
      + `lang=${langLabel || '?'}, source=${derived.source})`);

    // MODIFIED (2026-11-05): pass langLabel through so the CLI can
    // run structural derivation on lang-fallback blocks.
    _postDownload(derived.path, text, derived.source, langLabel).then((ok) => {
      if (!ok) {
        console.warn(
          '[deeppeek:hijack] CLI not reachable — file NOT saved. '
          + 'Is `deeppeek hijack` running?');
      }
    });
  }

  function _installDownloadHijacker() {
    if (window.__dsx_hijack_installed) {
      return { success: true, already_installed: true };
    }
    window.__dsx_hijack_installed = true;
    window.__dsx_hijack_handler = _hijackClickHandler;
    document.addEventListener('click', _hijackClickHandler, true);
    console.log('[deeppeek:hijack] installed');
    return { success: true };
  }

  function _uninstallDownloadHijacker() {
    if (!window.__dsx_hijack_installed) {
      return { success: true, was_installed: false };
    }
    document.removeEventListener(
      'click', window.__dsx_hijack_handler, true);
    window.__dsx_hijack_installed = false;
    window.__dsx_hijack_handler = null;
    console.log('[deeppeek:hijack] uninstalled');
    return { success: true, was_installed: true };
  }

  window.__dsx_hijack_install_json = async function (_argsJson) {
    try {
      const r = _installDownloadHijacker();
      return JSON.stringify(r);
    } catch (e) {
      return JSON.stringify({
        success: false, reason: `install threw: ${e.message}` });
    }
  };

  window.__dsx_hijack_uninstall_json = async function (_argsJson) {
    try {
      const r = _uninstallDownloadHijacker();
      return JSON.stringify(r);
    } catch (e) {
      return JSON.stringify({
        success: false, reason: `uninstall threw: ${e.message}` });
    }
  };

  window.__dsx_hijack_status_json = async function (_argsJson) {
    return JSON.stringify({
      success: true,
      installed: !!window.__dsx_hijack_installed,
    });
  };

})();
