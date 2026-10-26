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
            const name = file.file_name || file.name || "attachment";
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

  async function attachOnce(files) {
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

    if (result.success) {
      await new Promise((r) => setTimeout(r, 800));
      const previewsAfter = blobCount();
      result.previews_before = previewsBefore;
      result.previews_after = previewsAfter;
      result.previews_added = previewsAfter - previewsBefore;
    }

    return result;
  }

  async function doAttach(filesPayload, opts) {
    const delayMs = Math.max(0, Number(opts && opts.delay_ms) || 0);

    let files;
    try {
      files = filesPayload.map((f) =>
        base64ToFile(f.base64, f.name, f.mime)
      );
    } catch (e) {
      return { success: false, reason: `base64 decode failed: ${e.message}` };
    }

    if (delayMs === 0) {
      const result = await attachOnce(files);
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
    const settleMs = Number(opts.settle_ms) || 300;
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
})();
