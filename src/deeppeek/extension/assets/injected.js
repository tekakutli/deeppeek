// Runs in the page world.  Exposes:
//   window.__dsx_extract()             — extraction (existing)
//   window.__dsx_attach(files, opts)   — attach via objects (debug only)
//   window.__dsx_attach_json(json)     — attach via JSON (bridge entry)
(function () {
  "use strict";

  const config = {
    chatContainerSelector: ".ds-virtual-list-visible-items",
    userMessageSelector: "._9663006",
    aiClassPrefix: "_4f9bf79",
    searchHintSelector: "._5255ff8._4d41763",
    thinkingChainSelector: ".ds-think-content",
    finalAnswerSelector: ".ds-message .ds-markdown:last-child",
    titleSelector: ".afa34042.e37a04e4.e0a1edb7",
    answerMarkdownPath: "$0.return.return.return",
    thinkingContentPath: "$0.return.return.return.return",
  };

  // ==================================================================
  // Part 1 — DOM + fiber helpers
  // ==================================================================

  function navigateFiberPathFromElement(element, pathString) {
    if (!element || !pathString) return null;
    const fiberKey = Object.keys(element).find((k) => k.startsWith("__reactFiber$"));
    if (!fiberKey) return null;
    let fiber = element[fiberKey];
    const cleaned = pathString.replace(/^\$0\.*/, "");
    if (!cleaned) return fiber;
    for (const step of cleaned.split(".")) {
      if (!step) continue;
      fiber = fiber ? fiber[step] : null;
      if (!fiber) return null;
    }
    return fiber;
  }

  function closeUnclosedFencedCodeBlocks(markdown) {
    if (typeof markdown !== "string" || !markdown.length) return markdown;
    let inFence = false;
    for (const line of markdown.split(/\r?\n/)) {
      const m = line.match(/^ {0,3}(```+)/);
      if (!m) continue;
      if (m[1].length === 3) inFence = !inFence;
    }
    return inFence ? markdown + "\n```" : markdown;
  }

  function findScrollParent(el) {
    let p = el;
    for (let i = 0; i < 40 && p && p !== document.body && p !== document.documentElement; i++) {
      const st = window.getComputedStyle(p);
      const oy = st.overflowY;
      if ((oy === "auto" || oy === "scroll" || oy === "overlay") &&
          p.scrollHeight > p.clientHeight + 4) return p;
      p = p.parentElement;
    }
    p = el;
    for (let i = 0; i < 40 && p && p !== document.body; i++) {
      if (p.scrollHeight > p.clientHeight + 4) return p;
      p = p.parentElement;
    }
    const doc = document.scrollingElement;
    if (doc && doc.scrollHeight > doc.clientHeight + 4) return doc;
    return null;
  }

  function tryGetMessageOrdinal(node) {
    if (!node || node.nodeType !== 1) return null;
    const direct = node.getAttribute("data-index");
    if (direct != null && /^\d+$/.test(direct)) return parseInt(direct, 10);
    const nested = node.querySelector("[data-index]");
    if (nested) {
      const v = nested.getAttribute("data-index");
      if (v != null && /^\d+$/.test(v)) return parseInt(v, 10);
    }
    const fiberKey = Object.keys(node).find((k) => k.startsWith("__reactFiber$"));
    if (!fiberKey) return null;
    let fiber = node[fiberKey];
    for (let depth = 0; depth < 80 && fiber; depth++) {
      const mp = fiber.memoizedProps;
      if (mp && typeof mp === "object") {
        for (const k of ["index", "messageIndex", "msgIndex", "order"]) {
          const v = mp[k];
          if (typeof v === "number" && Number.isFinite(v)) return v;
        }
      }
      fiber = fiber.return;
    }
    return null;
  }

  function hashString(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    return String(h);
  }

  function getUserMessage(node) {
    const m = node.matches(config.userMessageSelector)
      ? node : node.querySelector(config.userMessageSelector);
    return m ? m.textContent.trim() : null;
  }

  function isAIMessage(node) {
    return node.classList.contains(config.aiClassPrefix);
  }

  function extractSearchOrThinking(node) {
    const hint = node.querySelector(config.searchHintSelector);
    return hint ? hint.textContent.trim() : null;
  }

  function extractThinkingChain(node) {
    const markdownEl = node.querySelector("div.ds-markdown");
    const baseEl = markdownEl || node;
    const nav = navigateFiberPathFromElement(baseEl, config.thinkingContentPath);
    if (!nav || !nav.memoizedProps || !nav.memoizedProps.content) return null;
    return closeUnclosedFencedCodeBlocks(nav.memoizedProps.content);
  }

  function extractFinalAnswer(node) {
    let answerNode = null;
    for (const el of node.querySelectorAll("div.ds-markdown")) {
      if (!el.closest(config.thinkingChainSelector)) { answerNode = el; break; }
    }
    if (!answerNode) answerNode = node.querySelector(config.finalAnswerSelector);
    if (!answerNode) return null;
    const nav = navigateFiberPathFromElement(answerNode, config.answerMarkdownPath);
    if (!nav || !nav.memoizedProps || !nav.memoizedProps.markdown) return null;
    return closeUnclosedFencedCodeBlocks(nav.memoizedProps.markdown);
  }

  function getChatTitle() {
    const el = document.querySelector(config.titleSelector);
    return el ? el.textContent.trim() : null;
  }

  function formatRow(node) {
    const user = getUserMessage(node);
    if (user) return { role: "user", content: user };
    if (!isAIMessage(node)) return null;
    const hint = extractSearchOrThinking(node);
    const thinkingNode = node.querySelector(config.thinkingChainSelector);
    const thinking = thinkingNode ? extractThinkingChain(thinkingNode) : null;
    const content = extractFinalAnswer(node);
    if (!content && !thinking && !hint) return null;
    return {
      role: "assistant",
      content: content || "",
      thinking: thinking || undefined,
      hint: hint || undefined,
    };
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

  async function extractViaIndexedDB(sessionId) {
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
          const allHaveMsgId = raw.every((m) => m && typeof m.message_id === "number");
          const sorted = allHaveMsgId
            ? [...raw].sort((a, b) => a.message_id - b.message_id)
            : raw;
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
  // Part 4 — DOM sweep fallback
  // ==================================================================

  async function sweep(chatContainer, scrollParent) {
    const best = new Map();
    const seen = new Set();
    const consider = (node, pos, i) => {
      const ord = tryGetMessageOrdinal(node);
      if (ord != null && seen.has(ord)) return;
      const msg = formatRow(node);
      if (!msg) return;
      const orderKey = ord != null ? ord : pos * 10000 + i;
      const key = ord != null
        ? `o:${ord}` : `h:${hashString(msg.role + "|" + (msg.content || ""))}`;
      const prev = best.get(key);
      if (!prev || orderKey < prev.orderKey) {
        best.set(key, { orderKey, msg });
        if (ord != null) seen.add(ord);
      }
    };
    const collect = (pos) => {
      for (let i = 0; i < chatContainer.children.length; i++) {
        consider(chatContainer.children[i], pos, i);
      }
    };
    if (!scrollParent) {
      collect(0);
      return Array.from(best.values()).sort((a, b) => a.orderKey - b.orderKey).map((e) => e.msg);
    }
    const saved = scrollParent.scrollTop;
    let observer = null;
    try {
      scrollParent.scrollTop = scrollParent.scrollHeight;
      await new Promise((r) => setTimeout(r, 800));
      collect(scrollParent.scrollTop);
      let mutCount = 0;
      observer = new MutationObserver(() => { mutCount++; });
      observer.observe(chatContainer, { childList: true, subtree: true, characterData: true });
      for (let i = 0; i < 400; i++) {
        const pos = scrollParent.scrollTop;
        if (pos <= 0) break;
        const mut = mutCount;
        scrollParent.scrollTop = Math.max(0, pos - 300);
        try {
          scrollParent.dispatchEvent(new WheelEvent("wheel", {
            deltaY: -300, bubbles: true, cancelable: true,
          }));
        } catch (_) {}
        const t0 = Date.now();
        while (mutCount === mut && Date.now() - t0 < 1000) {
          await new Promise((r) => setTimeout(r, 30));
        }
        await new Promise((r) => setTimeout(r, 300));
        const prevSize = best.size;
        collect(scrollParent.scrollTop);
        if (best.size === prevSize) {
          if (i > 5) break;
        }
      }
    } finally {
      if (observer) observer.disconnect();
      scrollParent.scrollTop = saved;
    }
    return Array.from(best.values()).sort((a, b) => a.orderKey - b.orderKey).map((e) => e.msg);
  }

  // ==================================================================
  // Part 5 — orchestration (extract)
  // ==================================================================

  async function doExtraction() {
    const sessionId = getSessionIdFromURL();
    let messages = null;
    let source = null;

    if (sessionId) {
      try {
        messages = await extractViaIndexedDB(sessionId);
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
      const container = document.querySelector(config.chatContainerSelector);
      if (container) {
        try {
          messages = await sweep(container, findScrollParent(container));
          if (messages && messages.length) source = "sweep";
        } catch (e) { console.log("[deeppeek] sweep threw:", e); }
      }
    }
    if (!messages || !messages.length) {
      throw new Error("All extraction methods failed (indexeddb, fiber, sweep).");
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
  // Part 6 — file attachment
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
  // Part 7 — exports
  // ==================================================================

  window.__dsx_extract = async function () {
    return JSON.stringify(await doExtraction());
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
})();
