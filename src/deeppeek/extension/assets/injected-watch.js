// =====================================================================
// injected-watch.js — streaming watcher (v14)
// =====================================================================
//
// Revert of v13's markdown reconstruction (STRONG / EM / CODE /
// PRE-fence wrapping).  Reason: the wrapping markers are
// non-monotonic during streaming.  DeepSeek's incremental markdown
// parser flips between "plain text" and "<strong>wrapped</strong>"
// as tokens arrive, so the `<strong>` closing marker MOVES from
// tick to tick.  The delta logic sees:
//
//     tick N:    ...around 17 °C–19.
//     tick N+1:  ...around **17 °C–19.8 °C**
//
// `raw.startsWith(lastContent)` is false (the `**` markers
// shifted), so it emits the whole accumulated content again as a
// rewrite.  Client concatenates -> duplicated text.  Same issue
// for `<pre>` fence reconstruction: the closing ``` moves forward
// as the body grows.
//
// v14 restores v11/v12's plain-text extraction:
//   · textContent of text nodes, verbatim
//   · citation subtrees (<sup>, <cite>, footnote classes) skipped
//   · <BR>            -> \n
//   · <TD>/<TH>       -> \t separators
//   · <TR>            -> \n row separators
//   · block elements  -> \n\n around them
//   · 3+ newlines     -> 2
//   · leading/trailing whitespace trimmed
//
// Trade-off: aider's whole-edit format needs ``` fences around
// code content.  Those are lost in v14's plain-text extraction,
// so aider's file-listing parser will fail.  Fixing that requires
// a different strategy (buffering code blocks and emitting fences
// only after they stabilize, or waiting for fiber to become
// available) — not a streaming-time reconstruction.  Deferred.
//
// All other v13 behaviour (submit-failure detection, citation
// stripping, fiber diagnostic) is unchanged.
//
// Bridge entry points: window.__dsx_startWatch / __dsx_stopWatch /
// __dsx_testFiber.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  const watchers = new Map();

  const _UI_CHROME_LABELS = new Set([
    "开头", "结尾",
    "开始", "结束",
    "复制", "下载",
    "Copy", "Download",
    "Start", "End",
  ]);

  const _THINK_CLASS_HINTS = ["think", "reason", "thought"];
  const _ASSISTANT_ROLES = [
    "assistant", "ai", "bot", "agent", "answer", "model",
  ];
  const _USER_ROLES = [
    "user", "human", "customer", "client", "question",
  ];

  const _SUBMIT_TIMEOUT_MS = 20000;

  const _MESSAGE_PROP_KEYS = [
    "message", "msg", "chatMessage", "data", "item",
    "node", "messageData", "messageObj", "chat_message",
    "activeMessage", "currentMessage", "selectedMessage",
  ];
  const _LIST_PROP_KEYS = [
    "messages", "chatMessages", "messageList", "list", "items",
    "chat_messages", "messageList", "activePath",
  ];

  // ---------- DOM helpers ------------------------------------------

  function findChatContainer() {
    return document.querySelector(
      window.__dsx__.config?.chatContainerSelector
        || ".ds-virtual-list-visible-items");
  }

  function isGenerating() {
    const buttons = document.querySelectorAll('button, [role="button"]');
    for (const btn of buttons) {
      const rect = btn.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;
      const label = (btn.getAttribute("aria-label") || "")
        .toLowerCase().trim();
      const text = (btn.textContent || "").toLowerCase().trim();
      const testid = (btn.getAttribute("data-testid") || "")
        .toLowerCase().trim();
      if (label === "stop" || label === "stop generating"
          || label === "停止" || label === "停止生成"
          || text === "stop" || text === "停止"
          || testid === "stop" || testid === "stop-button"
          || testid === "stop-generating") {
        return true;
      }
    }
    return false;
  }

  function _isInsideThinking(el) {
    let node = el;
    for (let i = 0; i < 6 && node; i++) {
      const cls = (node.className && typeof node.className === "string")
        ? node.className.toLowerCase() : "";
      for (const hint of _THINK_CLASS_HINTS) {
        if (cls.includes(hint)) return true;
      }
      node = node.parentElement;
    }
    return false;
  }

  function _findAnswerMarkdown(last) {
    const candidates = last.querySelectorAll(
      ".ds-markdown, .markdown-body, .prose");
    for (let i = candidates.length - 1; i >= 0; i--) {
      if (!_isInsideThinking(candidates[i])) return candidates[i];
    }
    return null;
  }

  function _lastDsMessage(container) {
    if (!container) return null;
    const msgs = container.querySelectorAll(".ds-message");
    if (msgs.length) return msgs[msgs.length - 1];
    const assistants = container.querySelectorAll(
      '[data-role="assistant"]');
    if (assistants.length) return assistants[assistants.length - 1];
    return null;
  }

  function _dumpParentChain(el, maxDepth) {
    const out = [];
    let node = el;
    const depth = maxDepth || 6;
    for (let i = 0; i < depth && node; i++) {
      out.push({
        tag: node.tagName || "?",
        cls: (node.className && typeof node.className === "string")
          ? node.className.slice(0, 80) : "",
      });
      node = node.parentElement;
    }
    return out;
  }

  const _CITATION_SELECTOR =
    "sup, cite, [class*='citation'], [class*='cite'], " +
    "[class*='footnote'], [class*='reference'], a[href^='#']";

  const _BLOCK_TAGS = new Set([
    "DIV", "P", "SECTION", "ARTICLE", "LI", "BLOCKQUOTE",
    "H1", "H2", "H3", "H4", "H5", "H6",
    "UL", "OL",
    "TABLE", "THEAD", "TBODY", "TFOOT",
    "FIGURE", "FIGCAPTION",
    "HEADER", "FOOTER", "MAIN", "ASIDE", "NAV",
  ]);

  function _extractCleanText(el) {
    if (!el) return "";
    const parts = [];
    function push(s) { if (s) parts.push(s); }

    function walk(node) {
      if (node.nodeType === 3) { push(node.textContent); return; }
      if (node.nodeType !== 1) return;
      if (node.matches && node.matches(_CITATION_SELECTOR)) return;
      const tag = node.tagName;

      if (tag === "BR") { push("\n"); return; }

      if (tag === "TD" || tag === "TH") {
        if (parts.length) {
          const last = parts[parts.length - 1];
          if (last && !/[\t\n]$/.test(last)) push("\t");
        }
        for (const c of node.childNodes) walk(c);
        return;
      }

      if (tag === "TR") {
        if (parts.length) {
          const last = parts[parts.length - 1];
          if (last && !last.endsWith("\n")) push("\n");
        }
        for (const c of node.childNodes) walk(c);
        if (parts.length) {
          const last = parts[parts.length - 1];
          if (last && !last.endsWith("\n")) push("\n");
        }
        return;
      }

      const isBlock = _BLOCK_TAGS.has(tag);
      if (isBlock) push("\n\n");
      for (const c of node.childNodes) walk(c);
      if (isBlock) push("\n\n");
    }

    walk(el);
    let text = parts.join("");
    text = text.replace(/\n{3,}/g, "\n\n");
    text = text.replace(/\t+/g, "\t");
    return text.replace(/^\s+|\s+$/g, "");
  }

  function lastAssistantFromDOM(container) {
    const last = _lastDsMessage(container);
    if (!last) return "";
    const body = _findAnswerMarkdown(last);
    return body ? _extractCleanText(body) : "";
  }

  function lastAssistantOuterFromDOM(container) {
    const last = _lastDsMessage(container);
    return last ? (last.innerText || "") : "";
  }

  function _mdStatesFor(last) {
    const out = [];
    if (!last) return out;
    const all = last.querySelectorAll(
      ".ds-markdown, .markdown-body, .prose");
    for (const md of all) {
      out.push({
        len: (md.innerText || "").length,
        insideThinking: _isInsideThinking(md),
        cls: (md.className && typeof md.className === "string")
          ? md.className.slice(0, 60) : "",
      });
    }
    return out;
  }

  function filterChrome(content) {
    if (!content) return "";
    const lines = content.split("\n");
    const kept = [];
    for (const line of lines) {
      const t = line.trim();
      if (t && _UI_CHROME_LABELS.has(t)) continue;
      kept.push(line);
    }
    while (kept.length && !kept[0].trim()) kept.shift();
    while (kept.length && !kept[kept.length - 1].trim()) kept.pop();
    return kept.join("\n");
  }

  // ---------- fiber helpers (unchanged; dead but opportunistic) ----

  function _looksLikeMessage(obj) {
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return false;
    if (typeof obj.role === "string") {
      const r = obj.role.toLowerCase();
      if (_ASSISTANT_ROLES.includes(r) || _USER_ROLES.includes(r)) {
        return true;
      }
    }
    if (Array.isArray(obj.fragments)
        || Array.isArray(obj.content_blocks)
        || Array.isArray(obj.blocks)) {
      return true;
    }
    if (typeof obj.content === "string" && obj.content.length > 0
        && typeof obj.isUser === "boolean") {
      return true;
    }
    return false;
  }

  function _parseFiberMessage(m) {
    let content = "";
    let thinking = "";
    let role = (typeof m.role === "string")
      ? m.role.toLowerCase() : "";
    if (!role && typeof m.isUser === "boolean") {
      role = m.isUser ? "user" : "assistant";
    }
    const isStr = (v) => typeof v === "string" && v.length > 0;

    if (isStr(m.content)) content = m.content;

    const frags = m.fragments || m.content_blocks || m.blocks;
    if (Array.isArray(frags)) {
      const contentParts = [];
      const thinkParts = [];
      for (const f of frags) {
        if (!f || typeof f !== "object") continue;
        const type = String(f.type || f.kind || "").toUpperCase();
        if (type === "FILE") continue;
        let text = "";
        if (isStr(f.content)) text = f.content;
        else if (isStr(f.text)) text = f.text;
        else if (isStr(f.value)) text = f.value;
        if (!text) continue;
        if (/THINK|REASON|THOUGHT/.test(type)) thinkParts.push(text);
        else contentParts.push(text);
      }
      if (contentParts.length) content = contentParts.join("");
      if (thinkParts.length) thinking = thinkParts.join("\n\n");
    }

    return { content, thinking, role };
  }

  function _considerObj(obj, consider) {
    if (!obj || typeof obj !== "object") return;
    consider(obj);
    if (Array.isArray(obj) && obj.length > 0) {
      consider(obj[obj.length - 1]);
    }
    if (!Array.isArray(obj)) {
      for (const key of _LIST_PROP_KEYS) {
        const list = obj[key];
        if (Array.isArray(list) && list.length > 0) {
          consider(list[list.length - 1]);
        }
      }
      for (const key of _MESSAGE_PROP_KEYS) {
        const inner = obj[key];
        if (inner && typeof inner === "object") {
          consider(inner);
          if (Array.isArray(inner) && inner.length > 0) {
            consider(inner[inner.length - 1]);
          }
        }
      }
    }
  }

  function _walkUpForMessage(fiber) {
    let best = null;
    let node = fiber;
    let depth = 0;
    const propsSeen = [];
    const hooksSeen = [];
    const consider = (obj) => {
      if (!_looksLikeMessage(obj)) return;
      const parsed = _parseFiberMessage(obj);
      if (!best || (parsed.content || "").length
                  > (best.content || "").length) {
        best = parsed;
      }
    };
    while (node && depth < 30) {
      for (const props of [node.memoizedProps, node.pendingProps]) {
        if (!props || typeof props !== "object") continue;
        if (depth < 12) {
          try {
            const entry = { depth, keys: Object.keys(props).slice(0, 20) };
            if ("value" in props) {
              entry.valueType = (props.value === null)
                ? "null"
                : Array.isArray(props.value)
                  ? `array(len=${props.value.length})`
                  : typeof props.value;
              if (props.value && typeof props.value === "object"
                  && !Array.isArray(props.value)) {
                entry.valueKeys = Object.keys(props.value).slice(0, 15);
              }
            }
            propsSeen.push(entry);
          } catch (_) {}
        }
        for (const key of _MESSAGE_PROP_KEYS) {
          _considerObj(props[key], consider);
        }
        consider(props);
        if (props.value !== undefined) {
          _considerObj(props.value, consider);
          if (props.value && typeof props.value === "object"
              && props.value.current !== undefined) {
            _considerObj(props.value.current, consider);
          }
        }
        if (props.current !== undefined) {
          _considerObj(props.current, consider);
        }
        for (const key of _LIST_PROP_KEYS) {
          const list = props[key];
          if (Array.isArray(list) && list.length > 0) {
            consider(list[list.length - 1]);
          }
        }
      }
      if (node.memoizedState) {
        let state = node.memoizedState;
        let idx = 0;
        while (state && idx < 50) {
          const val = state.memoizedState;
          if (val && typeof val !== "function") {
            _considerObj(val, consider);
            if (val && typeof val === "object"
                && val.current !== undefined) {
              _considerObj(val.current, consider);
            }
            if (val && typeof val === "object"
                && val.value !== undefined) {
              _considerObj(val.value, consider);
            }
            if (depth < 12) {
              try {
                const entry = {
                  depth, idx,
                  type: val === null ? "null" : Array.isArray(val) ? "array" : typeof val,
                };
                if (val && typeof val === "object") {
                  if (Array.isArray(val)) {
                    entry.len = val.length;
                  } else {
                    entry.keys = Object.keys(val).slice(0, 15);
                  }
                }
                hooksSeen.push(entry);
              } catch (_) {}
            }
          }
          state = state.next;
          idx++;
        }
      }
      node = node.return;
      depth++;
    }
    try {
      if (!best && propsSeen.length > 0) {
        window.__dsx_last_fiber_props = propsSeen;
      }
      if (hooksSeen.length > 0) {
        window.__dsx_last_fiber_hooks = hooksSeen;
      }
    } catch (_) {}
    return best;
  }

  function _fiberLastAssistant(container) {
    const last = _lastDsMessage(container);
    if (!last) return null;
    const fk = Object.keys(last).find(
      (k) => k.startsWith("__reactFiber$")
          || k.startsWith("__reactInternalInstance$"));
    if (!fk) return null;
    const found = _walkUpForMessage(last[fk]);
    if (!found) return null;
    if (found.role && _USER_ROLES.includes(found.role)) {
      return null;
    }
    return found;
  }

  // ---------- watcher ----------------------------------------------

  function createWatcher(batchId, opts, onDelta, onDone) {
    const settleFastMs = Math.max(50, opts.settle_ms || 250);
    const settleSlowMs = Math.max(500, opts.settle_slow_ms || 2000);
    const maxDurationMs = Math.max(5000, opts.max_duration_ms || 180000);
    const firstContentTimeoutMs = Math.max(
      5000, opts.first_content_timeout_ms || 30000);
    const submitTimeoutMs = Math.max(
      5000, opts.submit_timeout_ms || _SUBMIT_TIMEOUT_MS);
    const startedAt = Date.now();
    let lastContent = "";
    let settleTimer = null;
    let containerPollTimer = null;
    let safetyPollTimer = null;
    let stopped = false;
    let msgObserver = null;
    let container = null;
    let sawGenerating = false;
    let rafScheduled = false;
    let loggedFirstContent = false;
    let loggedSubmitConfirmed = false;
    let lastWaitLogAt = 0;
    let initialMsgCount = 0;
    let initialLastMdLen = 0;
    let initialLastMsgLen = 0;

    function jlog(tag, obj) {
      try {
        if (window.__dsx_watch_debug === false) return;
        const el = Date.now() - startedAt;
        console.log(
          `[DSX-WATCH ${batchId} t+${el}ms] ${tag} ` +
          (obj === undefined ? "" : JSON.stringify(obj)));
      } catch (_) {}
    }

    function logButtonsOnce(tag) {
      try {
        if (window.__dsx_watch_debug === false) return;
        const out = [];
        for (const btn of document.querySelectorAll(
            'button, [role="button"]')) {
          const rect = btn.getBoundingClientRect();
          const svg = btn.querySelector("svg");
          out.push({
            tag: btn.tagName,
            visible: !(rect.width === 0 || rect.height === 0),
            label: btn.getAttribute("aria-label") || "",
            text: (btn.textContent || "").trim().slice(0, 40),
            testid: btn.getAttribute("data-testid") || "",
            title: btn.getAttribute("title") || "",
            ariaPressed: btn.getAttribute("aria-pressed") || "",
            cls: (btn.className && typeof btn.className === "string")
              ? btn.className : "",
            svgCls: svg ? (svg.getAttribute("class") || "") : "",
          });
        }
        jlog(tag, { buttons: out });
      } catch (_) {}
    }

    function stop(reason) {
      if (stopped) return;
      stopped = true;
      const domNow = lastAssistantFromDOM(container);
      const outer = lastAssistantOuterFromDOM(container);
      const msgCount = container
        ? container.querySelectorAll(".ds-message").length : 0;
      jlog("STOP", {
        reason,
        lastLen: lastContent.length,
        lastPreview: lastContent.slice(0, 120).replace(/\n/g, "\\n"),
        domNowLen: domNow.length,
        domNowPreview: domNow.slice(0, 120).replace(/\n/g, "\\n"),
        outerLen: outer.length,
        msgCount,
        initialMsgCount,
      });
      if (msgObserver) msgObserver.disconnect();
      if (settleTimer) clearTimeout(settleTimer);
      if (containerPollTimer) clearInterval(containerPollTimer);
      if (safetyPollTimer) clearInterval(safetyPollTimer);
      watchers.delete(batchId);
      onDone(reason);
    }

    function armSettle() {
      if (settleTimer) clearTimeout(settleTimer);
      const dur = sawGenerating ? settleFastMs : settleSlowMs;
      jlog("SETTLE_ARM", { dur, sawGenerating });
      settleTimer = setTimeout(() => {
        if (stopped) return;
        if (!lastContent) {
          jlog("SETTLE_FIRE_NO_CONTENT", {});
          return;
        }
        const gen = isGenerating();
        jlog("SETTLE_FIRE", { isGen: gen, sawGenerating });
        if (gen) {
          sawGenerating = true;
          armSettle();
          return;
        }
        stop("settled");
      }, dur);
    }

    function extractAndEmit() {
      if (stopped) return;
      if (Date.now() - startedAt > maxDurationMs) {
        jlog("MAX_DURATION", {});
        stop("max_duration");
        return;
      }

      const gen = isGenerating();
      if (gen && !sawGenerating) {
        sawGenerating = true;
        jlog("SAW_GENERATING", {});
        logButtonsOnce("BUTTONS_GEN");
      }

      if (!loggedFirstContent) {
        const curLast = _lastDsMessage(container);
        const curCount = container
          ? container.querySelectorAll(".ds-message").length : 0;
        const curLastMd = curLast
          ? curLast.querySelector(".ds-markdown") : null;
        const curLastMdLen = curLastMd
          ? (curLastMd.innerText || "").length : 0;
        const curLastMsgLen = curLast
          ? (curLast.innerText || "").length : 0;
        const msgCountGrew = curCount > initialMsgCount;
        const contentGrew = (curLastMdLen > initialLastMdLen)
                         || (curLastMsgLen > initialLastMsgLen);
        if (!msgCountGrew && !contentGrew
            && Date.now() - startedAt > submitTimeoutMs) {
          jlog("SUBMIT_FAILED", {
            elapsed: Date.now() - startedAt,
            initialMsgCount,
            curCount,
            initialLastMdLen,
            curLastMdLen,
            initialLastMsgLen,
            curLastMsgLen,
            note: "no new .ds-message AND no content growth within " +
                  submitTimeoutMs + "ms; submit likely blocked " +
                  "by a userscript or extension",
          });
          stop("submit_failed");
          return;
        }
        if (contentGrew && !msgCountGrew && !loggedSubmitConfirmed) {
          loggedSubmitConfirmed = true;
          jlog("SUBMIT_CONFIRMED_VIA_CONTENT", {
            initialLastMdLen, curLastMdLen,
          });
        }
      }

      let fiberMsg = null;
      try { fiberMsg = _fiberLastAssistant(container); } catch (_) {}

      let fiberRaw = null;
      if (fiberMsg && fiberMsg.content) {
        fiberRaw = filterChrome(fiberMsg.content);
      }

      let raw = null;
      let source = null;

      if (fiberMsg !== null && fiberRaw !== null) {
        if (!lastContent || fiberRaw.startsWith(lastContent)) {
          raw = fiberRaw;
          source = "fiber";
        } else {
          const domRaw = filterChrome(lastAssistantFromDOM(container));
          if (domRaw && domRaw.startsWith(lastContent)) {
            raw = domRaw;
            source = "dom";
          } else {
            raw = (fiberRaw.length >= (domRaw || "").length)
              ? fiberRaw : domRaw;
            source = raw === fiberRaw ? "fiber" : "dom";
          }
        }
      } else {
        const domRaw = filterChrome(lastAssistantFromDOM(container));
        if (domRaw) {
          raw = domRaw;
          source = "dom";
        }
      }

      if (!raw) {
        const now = Date.now();
        if (now - startedAt > firstContentTimeoutMs) {
          jlog("NO_FIRST_CONTENT", {
            elapsed: now - startedAt,
            msgCount: container
              ? container.querySelectorAll(".ds-message").length : 0,
            initialMsgCount,
          });
          stop("no_first_content");
          return;
        }
        if (now - lastWaitLogAt >= 2000) {
          lastWaitLogAt = now;
          const lastMsg = _lastDsMessage(container);
          jlog("WAITING_FOR_CONTENT", {
            elapsed: now - startedAt,
            msgCount: container
              ? container.querySelectorAll(".ds-message").length : 0,
            initialMsgCount,
            lastMsgLen: lastMsg ? (lastMsg.innerText || "").length : 0,
            mdStates: _mdStatesFor(lastMsg),
            fiberFound: fiberMsg !== null,
            fiberLen: fiberMsg ? (fiberMsg.content || "").length : null,
            fiberRole: fiberMsg ? fiberMsg.role : null,
          });
        }
        return;
      }

      if (!loggedFirstContent) {
        loggedFirstContent = true;
        const domForCompare = lastAssistantFromDOM(container);
        const outer = lastAssistantOuterFromDOM(container);
        const lastMsg = _lastDsMessage(container);
        jlog("FIRST_CONTENT", {
          source,
          bodyLen: raw.length,
          bodyPreview: raw.slice(0, 150).replace(/\n/g, "\\n"),
          fiberLen: fiberRaw ? fiberRaw.length : null,
          fiberPreview: fiberRaw
            ? fiberRaw.slice(0, 100).replace(/\n/g, "\\n") : null,
          domLen: domForCompare.length,
          domPreview: domForCompare.slice(0, 100).replace(/\n/g, "\\n"),
          fiberVsDomSame: fiberRaw === domForCompare,
          msgCount: container
            ? container.querySelectorAll(".ds-message").length : 0,
          initialMsgCount,
          mdStates: _mdStatesFor(lastMsg),
        });
        logButtonsOnce("BUTTONS_AT_FIRST_CONTENT");
      }

      if (raw === lastContent) return;

      const prevLen = lastContent.length;
      const newLen = raw.length;
      let kind, delta;
      if (raw.startsWith(lastContent)) {
        kind = "append";
        delta = raw.slice(lastContent.length);
      } else {
        kind = "rewrite";
        delta = raw;
      }
      lastContent = raw;
      jlog("DELTA", {
        source,
        kind, prevLen, newLen,
        deltaLen: delta.length,
        isGen: gen,
        deltaPreview: delta.slice(0, 80).replace(/\n/g, "\\n"),
      });
      if (delta) onDelta(delta);
      armSettle();
    }

    function scheduleExtract() {
      if (rafScheduled) return;
      rafScheduled = true;
      requestAnimationFrame(() => {
        rafScheduled = false;
        extractAndEmit();
      });
    }

    function startObserving(el) {
      container = el;
      const msgs = el.querySelectorAll(".ds-message");
      initialMsgCount = msgs.length;
      const lastMsg = msgs.length ? msgs[msgs.length - 1] : null;
      const lastMd = lastMsg
        ? lastMsg.querySelector(".ds-markdown") : null;
      initialLastMdLen = lastMd ? (lastMd.innerText || "").length : 0;
      initialLastMsgLen = lastMsg
        ? (lastMsg.innerText || "").length : 0;
      jlog("OBSERVE", {
        nodeCount: el.querySelectorAll("*").length,
        initialMsgCount,
        initialLastMdLen,
        initialLastMsgLen,
      });
      msgObserver = new MutationObserver(() => scheduleExtract());
      msgObserver.observe(el, {
        childList: true, characterData: true, subtree: true,
      });
      extractAndEmit();
    }

    const tryObserve = () => {
      if (stopped) return;
      const el = findChatContainer();
      if (el) {
        if (containerPollTimer) clearInterval(containerPollTimer);
        containerPollTimer = null;
        startObserving(el);
      } else if (Date.now() - startedAt > firstContentTimeoutMs) {
        if (containerPollTimer) clearInterval(containerPollTimer);
        containerPollTimer = null;
        jlog("NO_CONTAINER", {});
        stop("no_deepseek_tab");
      }
    };
    tryObserve();
    if (!container) {
      containerPollTimer = setInterval(tryObserve, 50);
    }

    safetyPollTimer = setInterval(() => {
      if (stopped) return;
      const fresh = findChatContainer();
      if (fresh && fresh !== container) {
        jlog("CONTAINER_CHANGED", {
          oldNodeCount: container
            ? container.querySelectorAll("*").length : null,
          newNodeCount: fresh.querySelectorAll("*").length,
        });
        if (msgObserver) msgObserver.disconnect();
        container = fresh;
        msgObserver = new MutationObserver(() => scheduleExtract());
        msgObserver.observe(container, {
          childList: true, characterData: true, subtree: true,
        });
      }
      extractAndEmit();
    }, 200);

    return {
      stop: () => { if (!stopped) stop("client_stop"); },
    };
  }

  // ---------- bridge entry points ----------------------------------

  window.__dsx_startWatch = function (argsJson) {
    let args;
    try { args = JSON.parse(argsJson); }
    catch (e) {
      return JSON.stringify({
        success: false, reason: `arg parse failed: ${e.message}`,
      });
    }
    const batchId = args.batch_id;
    if (!batchId) {
      return JSON.stringify({
        success: false, reason: "missing batch_id",
      });
    }

    const existing = watchers.get(batchId);
    if (existing) existing.stop();

    const onDelta = (delta) => {
      fetch("http://127.0.0.1:8765/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "watch-delta",
          batch_id: batchId,
          delta,
        }),
      }).catch(() => {});
    };
    const onDone = (reason) => {
      fetch("http://127.0.0.1:8765/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "watch-done",
          batch_id: batchId,
          reason,
        }),
      }).catch(() => {});
    };

    const w = createWatcher(batchId, args.opts || {}, onDelta, onDone);
    watchers.set(batchId, w);
    return JSON.stringify({ success: true, mode: "mutation-observer" });
  };

  window.__dsx_stopWatch = function (argsJson) {
    let args;
    try { args = JSON.parse(argsJson); } catch (_) { return "{}"; }
    const w = watchers.get(args.batch_id);
    if (w) { w.stop(); watchers.delete(args.batch_id); }
    return JSON.stringify({ success: true });
  };

  window.__dsx_testFiber = function () {
    const c = findChatContainer();
    if (!c) return JSON.stringify({ error: "no chat container" });
    let fiber = null;
    try { fiber = _fiberLastAssistant(c); } catch (e) {
      return JSON.stringify({ error: String(e) });
    }
    const dom = lastAssistantFromDOM(c);
    const lastMsg = _lastDsMessage(c);
    return JSON.stringify({
      msgCount: c.querySelectorAll(".ds-message").length,
      lastMsgIsAssistant: lastMsg
        ? !!lastMsg.querySelector(".ds-markdown") : null,
      fiberFound: fiber !== null,
      fiberRole: fiber ? fiber.role : null,
      fiberLen: fiber ? (fiber.content || "").length : 0,
      fiberPreview: fiber
        ? (fiber.content || "").slice(0, 200) : null,
      fiberThinkingLen: fiber
        ? (fiber.thinking || "").length : 0,
      domLen: dom.length,
      domPreview: dom.slice(0, 200),
      lastFiberProps: window.__dsx_last_fiber_props || [],
      lastFiberHooks: window.__dsx_last_fiber_hooks || [],
      mdStates: _mdStatesFor(lastMsg),
    }, null, 2);
  };
})();
