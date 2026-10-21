// Runs in the page world so it can read React fiber state AND
// access DeepSeek's IndexedDB cache.  Exposes
// window.__dsx_extract() for the content-script bridge.
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
  // Part 1 — DOM + fiber helpers (shared by the fallback extractors)
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
  // Part 2 — IndexedDB extraction (PRIMARY)
  //
  // DeepSeek stores the full conversation client-side under:
  //   database:    "deepseek-chat"
  //   store:       "history-message"
  //   record:      { data: { chat_session: {id, title, ...},
  //                          chat_messages: [{role, content, ...}, ...] } }
  //
  // The virtualizer renders a few messages at a time from this store.
  // Reading the store directly gives us the COMPLETE conversation
  // regardless of what has been scrolled into view, with zero network
  // calls, zero waiting, zero DOM interaction.  This is the data
  // source that both the DeepSeek app and the existing community
  // userscripts (Greasyfork "DeepSeek 对话导出" family) read from.
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
    // Unknown roles in chat_messages arrays are almost always the AI
    // side (system/tool messages are stored separately, if at all).
    return "assistant";
  }

  async function extractViaIndexedDB(sessionId) {
    const LOG = (...a) => console.log("[deeppeek:idb]", ...a);
    if (!sessionId) {
      LOG("no session id in URL — skipping IndexedDB lookup");
      return null;
    }
    LOG("looking up session:", sessionId);

    return new Promise((resolve) => {
      let req;
      try { req = indexedDB.open("deepseek-chat"); }
      catch (e) { LOG("indexedDB.open threw:", e); resolve(null); return; }

      req.onerror = () => { LOG("indexedDB.open error:", req.error); resolve(null); };
      req.onblocked = () => { LOG("indexedDB.open blocked"); resolve(null); };
      req.onupgradeneeded = () => {
        LOG("db missing (upgradeneeded) — aborting");
        try { req.result.close(); } catch (_) {}
        resolve(null);
      };
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("history-message")) {
          LOG("no 'history-message' store");
          db.close();
          resolve(null);
          return;
        }
        let tx;
        try { tx = db.transaction("history-message", "readonly"); }
        catch (e) { LOG("transaction threw:", e); db.close(); resolve(null); return; }

        const store = tx.objectStore("history-message");
        const all = store.getAll();
        all.onerror = () => { LOG("getAll error:", all.error); db.close(); resolve(null); };
        all.onsuccess = () => {
          const records = all.result || [];
          LOG(`got ${records.length} record(s) from history-message`);
          db.close();

          let match = null;
          for (const rec of records) {
            const sess = rec && rec.data && rec.data.chat_session;
            if (sess && sess.id &&
                String(sess.id).toLowerCase() === sessionId.toLowerCase()) {
              match = rec;
              break;
            }
          }
          if (!match) { LOG("session not in IndexedDB"); resolve(null); return; }

          const raw = (match.data && match.data.chat_messages) || [];
          LOG(`found record with ${raw.length} raw message(s)`);
          if (raw.length === 0) { resolve(null); return; }

          // -----------------------------------------------------------------
          // Sort by message_id, NOT inserted_at.
          //
          // message_id is assigned at row creation: parent first, then child,
          // monotonically.  Sorting by it yields u1, a1, u2, a2, …, uN, aN —
          // the natural conversation order.
          //
          // inserted_at is unreliable for the LAST turn: DeepSeek finalizes
          // the user's inserted_at when the turn closes, which can land a few
          // ms AFTER the assistant reply started streaming.  Sorting by it
          // therefore reverses the parent/child order for the most recent
          // pair — the assistant ends up before its own user message, and
          // --last N slices out the reply, leaving the final turn with only
          // the user prompt.  (See diagnostic: user 93 ts=…245.1369998,
          // assistant 94 ts=…245.1339998.)
          // -----------------------------------------------------------------
          const allHaveMsgId = raw.every(
            (m) => m && typeof m.message_id === "number",
          );
          const sorted = allHaveMsgId
            ? [...raw].sort((a, b) => a.message_id - b.message_id)
            : raw;

          const normalized = [];
          const extractDiag = { content: 0, fragments: 0, blocks: 0, nested: 0, none: 0 };
          const skipped = [];

          for (let i = 0; i < sorted.length; i++) {
            const m = sorted[i];
            if (!m || typeof m !== "object") continue;
            const role = normalizeRoleFromIDB(m.role);
            const { content, thinking, source } = extractMessageBody(m);
            const isLast = (i === sorted.length - 1);

            if (!content && !thinking) {
              const msgId = m.message_id;
              const status = m.status || "?";
              const fragTypes = (m.fragments || [])
                .map((f) => f && f.type).filter(Boolean).join(",") || "(none)";
              skipped.push({ msgId, role, status, fragTypes });

              // Preserve the very last message even when empty — a turn
              // whose reply is still being persisted should not vanish
              // from the output.  Placeholder text keeps the formatter
              // from silently dropping it.
              if (isLast) {
                normalized.push({
                  role,
                  content:
                    `_[empty response — message_id=${msgId}, ` +
                    `status=${status}, fragments=${fragTypes}]_`,
                });
                LOG(`included empty LAST message as placeholder ` +
                    `(id=${msgId}, status=${status})`);
              } else {
                extractDiag.none++;
              }
              continue;
            }

            extractDiag[source] = (extractDiag[source] || 0) + 1;
            normalized.push({
              role,
              content: content || "",
              thinking: thinking || undefined,
            });
          }

          LOG("extraction source breakdown:", extractDiag);
          LOG(`normalized ${normalized.length}/${raw.length} messages`);
          if (skipped.length) {
            LOG(`skipped ${skipped.length} empty message(s):`, skipped);
          }
          resolve(normalized.length > 0 ? normalized : null);
        };
      };
    });
  }

  // Extract text and reasoning from a DeepSeek message object.
  // Tries every plausible shape seen in the wild:
  //   · m.content as string
  //   · m.content as array of blocks [{text}]
  //   · m.fragments = [{type, content}] (streaming chat pattern)
  //   · m.message.content (nested)
  //   · m.thinking_content / m.reasoning_content / m.thinking / m.reasoning
  function formatFileSize(bytes) {
    if (typeof bytes !== "number" || bytes < 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function extractMessageBody(m) {
    let content = "";
    let thinking = null;
    let source = null;

    const isNonEmptyStr = (v) => typeof v === "string" && v.trim().length > 0;

    // --- 1. direct string fields ---
    if (isNonEmptyStr(m.content)) { content = m.content; source = "content"; }
    if (isNonEmptyStr(m.thinking_content)) thinking = m.thinking_content;
    if (isNonEmptyStr(m.reasoning_content)) thinking = m.reasoning_content;
    if (isNonEmptyStr(m.thinking)) thinking = m.thinking;
    if (isNonEmptyStr(m.reasoning)) thinking = m.reasoning;

    // --- 2. content as array of blocks ---
    if (!content && Array.isArray(m.content)) {
      const parts = [];
      for (const b of m.content) {
        if (typeof b === "string") { parts.push(b); continue; }
        if (b && typeof b === "object") {
          if (isNonEmptyStr(b.text)) parts.push(b.text);
          else if (isNonEmptyStr(b.content)) parts.push(b.content);
          else if (isNonEmptyStr(b.value)) parts.push(b.value);
        }
      }
      if (parts.length) { content = parts.join(""); source = source || "blocks"; }
    }

    // --- 3. fragments array (DeepSeek streaming pattern) ---
    const frags = m.fragments
      || (m.message && m.message.fragments)
      || m.content_blocks
      || m.blocks
      || m.parts;
    if (Array.isArray(frags) && frags.length) {
      const contentParts = [];
      const thinkParts = [];
      const fileParts = [];

      for (const f of frags) {
        if (!f || typeof f !== "object") continue;
        const type = String(f.type || f.kind || f.kind_name || "").toUpperCase();

        // File attachments: keep a lightweight marker so the markdown
        // records that a file was attached (content isn't in the DB —
        // DeepSeek serves attachments via signed URLs on demand).
        if (type === "FILE" && Array.isArray(f.files)) {
          for (const file of f.files) {
            if (!file || typeof file !== "object") continue;
            const name = file.file_name || file.name || "attachment";
            const sizeStr = formatFileSize(file.file_size);
            fileParts.push(`[file: ${name}${sizeStr ? ` (${sizeStr})` : ""}]`);
          }
          continue;
        }

        const text =
          isNonEmptyStr(f.content) ? f.content
          : isNonEmptyStr(f.text) ? f.text
          : isNonEmptyStr(f.value) ? f.value
          : "";
        if (!text) continue;
        if (/THINK|REASON/.test(type)) thinkParts.push(text);
        else contentParts.push(text);
      }

      // File markers first (they render above the prompt text in the UI),
      // separated by a blank line from the message body.
      const body = contentParts.join("");
      const combined = fileParts.length > 0
        ? fileParts.join("\n") + (body ? "\n\n" + body : "")
        : body;

      if (!content && combined) { content = combined; source = source || "fragments"; }
      if (!thinking && thinkParts.length) thinking = thinkParts.join("\n\n");
    }

    // --- 4. nested message.content ---
    if (!content && m.message && typeof m.message === "object") {
      if (isNonEmptyStr(m.message.content)) {
        content = m.message.content;
        source = source || "nested";
      }
    }

    return { content, thinking, source: source || "none" };
  }

  // ==================================================================
  // Part 3 — fiber walk (fallback 1)
  //
  // Walks up .return from any rendered message looking for an array of
  // role-shaped objects in props or hooks.  Rarely produces a result
  // on current DeepSeek because content lives in IndexedDB and only
  // {id, renderKey} metadata is in React state — but it's a cheap
  // safety net if DeepSeek ever starts holding the list in memory.
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
          matches++;
          break;
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
      if (Array.isArray(v)) {
        const parts = v.map((c) => {
          if (typeof c === "string") return c;
          if (c && typeof c === "object") return c.text || c.content || c.value || "";
          return "";
        }).filter(Boolean);
        if (parts.length > 0) return parts.join("");
      }
      if (v && typeof v === "object") {
        const inner = v.text || v.content || v.value || v.markdown;
        if (typeof inner === "string") return inner;
      }
    }
    return "";
  }

  function extractThinkingFromMessage(m) {
    const KEYS = ["thinking", "reasoning", "thought", "thoughts", "chain_of_thought"];
    for (const k of KEYS) {
      const v = m[k];
      if (typeof v === "string" && v.length > 0) return v;
      if (v && typeof v === "object") {
        const inner = v.content || v.text || v.markdown || v.value;
        if (typeof inner === "string") return inner;
      }
    }
    return null;
  }

  function normalizeMessage(m) {
    if (!m || typeof m !== "object") return null;
    let role = null;
    for (const k of ROLE_KEYS) {
      role = normalizeRole(m[k]);
      if (role) break;
    }
    if (!role) return null;
    const content = extractContentFromMessage(m);
    const thinking = extractThinkingFromMessage(m);
    if (!content && !thinking) return null;
    return { role, content: content || "", thinking: thinking || undefined };
  }

  function findMessageListInFiber(element) {
    const fiberKey = Object.keys(element).find((k) => k.startsWith("__reactFiber$"));
    if (!fiberKey) return null;
    let fiber = element[fiberKey];
    let depth = 0;

    while (fiber && depth < 40) {
      for (const propSet of [fiber.memoizedProps, fiber.pendingProps]) {
        if (!propSet || typeof propSet !== "object") continue;
        for (const key of MESSAGE_ARRAY_KEYS) {
          const val = propSet[key];
          if (looksLikeMessageList(val)) {
            return { messages: val, source: `props.${key}`, depth, fiber };
          }
          if (val && typeof val === "object" && !Array.isArray(val)) {
            for (const inner of NESTED_MESSAGE_KEYS) {
              if (looksLikeMessageList(val[inner])) {
                return { messages: val[inner], source: `props.${key}.${inner}`, depth, fiber };
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
            if (looksLikeMessageList(val)) {
              return { messages: val, source: `state#${idx}`, depth, fiber };
            }
            if (val && typeof val === "object") {
              if ("current" in val && looksLikeMessageList(val.current)) {
                return { messages: val.current, source: `state#${idx}.current`, depth, fiber };
              }
              for (const key of NESTED_MESSAGE_KEYS) {
                if (looksLikeMessageList(val[key])) {
                  return { messages: val[key], source: `state#${idx}.${key}`, depth, fiber };
                }
              }
            }
          }
          if (state.queue && state.queue.lastRenderedState) {
            const lrs = state.queue.lastRenderedState;
            if (looksLikeMessageList(lrs)) {
              return { messages: lrs, source: `state#${idx}.queue.lastRenderedState`, depth, fiber };
            }
            if (lrs && typeof lrs === "object") {
              for (const key of NESTED_MESSAGE_KEYS) {
                if (looksLikeMessageList(lrs[key])) {
                  return { messages: lrs[key], source: `state#${idx}.queue.lastRenderedState.${key}`, depth, fiber };
                }
              }
            }
          }
          state = state.next;
          idx++;
        }
      }

      fiber = fiber.return;
      depth++;
    }
    return null;
  }

  function extractViaFiber() {
    const LOG = (...a) => console.log("[deeppeek:fiber]", ...a);
    const chatContainer = document.querySelector(config.chatContainerSelector);
    if (!chatContainer) { LOG("no chat container"); return null; }

    const startingPoints = new Set();
    for (const sel of [".ds-message", "[data-index]", "[data-message-id]", "[data-message]"]) {
      for (const el of document.querySelectorAll(sel)) startingPoints.add(el);
    }
    for (const el of chatContainer.children) startingPoints.add(el);
    startingPoints.add(chatContainer);

    const pointsArray = Array.from(startingPoints).filter(Boolean);
    LOG(`trying ${pointsArray.length} starting points`);

    const candidates = [];
    for (const start of pointsArray) {
      const found = findMessageListInFiber(start);
      if (found && found.messages.length > 0) {
        LOG(`✓ ${found.messages.length} messages via ${found.source} (depth ${found.depth})`);
        candidates.push(found);
      }
    }

    if (candidates.length === 0) {
      LOG("no message list found in fiber tree");
      return null;
    }

    candidates.sort((a, b) => b.messages.length - a.messages.length);
    const best = candidates[0];
    LOG(`using ${best.source}: ${best.messages.length} messages`);

    const normalized = [];
    for (const m of best.messages) {
      const n = normalizeMessage(m);
      if (n) normalized.push(n);
    }
    LOG(`normalized ${normalized.length}/${best.messages.length} messages`);
    return normalized.length > 0 ? normalized : null;
  }

  // ==================================================================
  // Part 4 — DOM sweep (fallback 2)
  //
  // Streams realistic wheel events upward and reads rendered children.
  // Only used when IndexedDB and fiber both fail — should be rare in
  // practice since IndexedDB always contains the current session.
  // ==================================================================

  async function sweep(chatContainer, scrollParent) {
    const LOG = (...a) => console.log("[deeppeek:sweep]", ...a);
    const best = new Map();
    const seenOrdinals = new Set();

    const consider = (node, pos, i) => {
      const ord = tryGetMessageOrdinal(node);
      if (ord != null && seenOrdinals.has(ord)) return;
      const msg = formatRow(node);
      if (!msg) return;
      const orderKey = ord != null ? ord : pos * 10000 + i;
      const dedupeKey = ord != null
        ? `o:${ord}` : `h:${hashString(msg.role + "|" + (msg.content || ""))}`;
      const prev = best.get(dedupeKey);
      if (!prev || orderKey < prev.orderKey) {
        best.set(dedupeKey, { orderKey, msg });
        if (ord != null) seenOrdinals.add(ord);
      }
    };

    const collect = (pos) => {
      for (let i = 0; i < chatContainer.children.length; i++) {
        consider(chatContainer.children[i], pos, i);
      }
      return best.size;
    };

    if (!scrollParent) {
      collect(0);
      return Array.from(best.values()).sort((a, b) => a.orderKey - b.orderKey).map((e) => e.msg);
    }

    const saved = scrollParent.scrollTop;
    const savedDoc = document.scrollingElement ? document.scrollingElement.scrollTop : undefined;
    let observer = null;
    try {
      scrollParent.scrollTop = scrollParent.scrollHeight;
      if (document.scrollingElement) {
        document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight;
      }
      await new Promise((r) => setTimeout(r, 800));
      collect(scrollParent.scrollTop);

      let mutCount = 0;
      observer = new MutationObserver(() => { mutCount++; });
      observer.observe(chatContainer, { childList: true, subtree: true, characterData: true });
      observer.observe(scrollParent, { childList: true, subtree: true, characterData: true });

      const wheelStream = async (direction, totalPx, deltaPx = 30) => {
        const events = Math.max(1, Math.floor(totalPx / Math.abs(deltaPx)));
        for (let i = 0; i < events; i++) {
          try {
            scrollParent.dispatchEvent(new WheelEvent("wheel", {
              deltaY: direction * deltaPx, deltaMode: 0,
              bubbles: true, cancelable: true,
            }));
          } catch (_) {}
          await new Promise((r) => setTimeout(r, 16));
        }
      };

      const MAX_ITER = 200;
      const STALL_LIMIT = 4;
      let stall = 0;
      let lastSize = best.size;

      for (let iter = 0; iter < MAX_ITER; iter++) {
        const posBefore = scrollParent.scrollTop;
        const mutBefore = mutCount;

        if (posBefore <= 0) {
          await wheelStream(-1, 2000, 25);
          await new Promise((r) => setTimeout(r, 2500));
        } else {
          const toMove = Math.min(300, posBefore);
          await wheelStream(-1, toMove, 30);
          scrollParent.scrollTop = Math.max(0, posBefore - toMove);
          await new Promise((r) => setTimeout(r, 250));
        }

        const newSize = collect(scrollParent.scrollTop);
        if (newSize !== lastSize) {
          LOG(`iter ${iter}: size=${newSize}`);
          lastSize = newSize;
          stall = 0;
        } else {
          stall++;
          if (stall >= STALL_LIMIT * 2) {
            LOG(`stalled at size=${newSize}`);
            break;
          }
        }
        if (scrollParent.scrollTop <= 0 && stall >= STALL_LIMIT) break;
      }
    } finally {
      if (observer) observer.disconnect();
      scrollParent.scrollTop = saved;
      if (document.scrollingElement && savedDoc !== undefined) {
        document.scrollingElement.scrollTop = savedDoc;
      }
    }
    LOG(`done: ${best.size}`);
    return Array.from(best.values()).sort((a, b) => a.orderKey - b.orderKey).map((e) => e.msg);
  }

  // ==================================================================
  // Part 5 — orchestration
  // ==================================================================

  async function doExtraction() {
    const sessionId = getSessionIdFromURL();
    let messages = null;
    let source = null;

    // 1. IndexedDB — fast, complete, deterministic
    if (sessionId) {
      try {
        messages = await extractViaIndexedDB(sessionId);
        if (messages && messages.length) source = "indexeddb";
      } catch (e) {
        console.log("[deeppeek] indexeddb extraction threw:", e);
      }
    }

    // 2. Fiber walk — only hits if IDB failed
    if (!messages || !messages.length) {
      try {
        messages = extractViaFiber();
        if (messages && messages.length) source = "fiber";
      } catch (e) {
        console.log("[deeppeek] fiber extraction threw:", e);
      }
    }

    // 3. DOM sweep — last resort
    if (!messages || !messages.length) {
      const chatContainer = document.querySelector(config.chatContainerSelector);
      if (chatContainer) {
        const scrollParent = findScrollParent(chatContainer);
        try {
          messages = await sweep(chatContainer, scrollParent);
          if (messages && messages.length) source = "sweep";
        } catch (e) {
          console.log("[deeppeek] sweep extraction threw:", e);
        }
      }
    }

    if (!messages || !messages.length) {
      throw new Error("All extraction methods failed (indexeddb, fiber, sweep).");
    }

    console.log(`[deeppeek] source=${source} count=${messages.length}`);
    messages = messages.map((m, i) => ({ ...m, index: i }));
    return {
      title: getChatTitle(),
      url: window.location.href,
      messages,
    };
  }

  window.__dsx_extract = async function () {
    return JSON.stringify(await doExtraction());
  };

  // Diagnostic — inspect IndexedDB without running a full extraction.
  window.__deeppeek_debug_idb = async function () {
    const sessionId = getSessionIdFromURL();
    console.log("[debug] URL session id:", sessionId);
    if (!sessionId) { console.log("[debug] no session id in URL"); return null; }

    const match = await new Promise((resolve) => {
      const req = indexedDB.open("deepseek-chat");
      req.onerror = () => resolve(null);
      req.onupgradeneeded = () => { try { req.result.close(); } catch (_) {} resolve(null); };
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("history-message")) { db.close(); resolve(null); return; }
        const all = db.transaction("history-message", "readonly")
                      .objectStore("history-message").getAll();
        all.onsuccess = () => {
          const records = all.result || [];
          db.close();
          resolve(records.find((r) => r?.data?.chat_session?.id === sessionId) || null);
        };
        all.onerror = () => { db.close(); resolve(null); };
      };
    });

    if (!match) { console.log("[debug] session not in IndexedDB"); return null; }

    const msgs = match.data.chat_messages || [];
    console.log(`[debug] ${msgs.length} raw messages`);

    const firstUser = msgs.find((m) => m.role === "USER");
    const firstAI = msgs.find((m) => m.role === "ASSISTANT");
    console.log("[debug] FIRST USER (full JSON):");
    console.log(JSON.stringify(firstUser, null, 2));
    console.log("[debug] FIRST ASSISTANT (full JSON):");
    console.log(JSON.stringify(firstAI, null, 2));

    // Run the real extraction and show a small sample
    const result = await extractViaIndexedDB(sessionId);
    if (result) {
      console.log(`[debug] ✓ normalized ${result.length} messages`);
      console.log("[debug] first 2:", result.slice(0, 2));
      console.log("[debug] last 2:", result.slice(-2));
    } else {
      console.log("[debug] ✗ still 0 messages — see above dumps for the real field shape");
    }
    return result;
  };
})();
