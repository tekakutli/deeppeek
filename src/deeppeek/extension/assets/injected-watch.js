// Streaming watcher (v16, batch mode).
//
// Why batch mode:
//   DeepSeek's markdown renderer consumes ``` fence markers before
//   they hit the DOM.  innerText can never contain fences.  The only
//   place raw markdown with fences survives is DeepSeek's own
//   fragment storage — surfaced by the page-world extractor
//   (`__dsx_extract` in injected-extract.js), which calls the API
//   with the userToken and reads `message.fragments[].content`.
//
//   So: don't stream innerText during generation.  Wait for the
//   Stop button to disappear (model finished), then call the
//   extractor once, then emit its content as a single delta.
//   Aider sees nothing for the thinking phase, then the full
//   correct reply.  One shot, no retries.
//
// Thoughts exclusion:
//   The extractor's `parseFragments` separates THINK/REASON/THOUGHT
//   fragment text into `.thinking`, leaves RESPONSE/REQUEST in
//   `.content`.  We take the last assistant message's `.content`
//   only — no thoughts.
//
// UI chrome:
//   The extractor returns raw markdown with no `md-code-block-banner`
//   labels ("开头 / 结尾 / Copy / Download").  Those are DOM-only
//   artifacts, injected by the renderer, absent from fragment
//   content.  filterChrome is only used in the fallback path.
//
// Fallback:
//   If extraction returns nothing (API unreachable, session not
//   saved yet, both IndexedDB and API missed), fall back to the
//   DOM's answer-markdown innerText with filterChrome applied.
//   That's lossy (no fences) but at least something reaches aider
//   instead of a 0-char response.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  const watchers = new Map();
  const _SUBMIT_TIMEOUT_MS = 20000;
  const _API_EXTRACT_TIMEOUT_MS = 15000;

  const _THINK_CLASS_HINTS = ["think", "reason", "thought"];

  const _UI_CHROME_LABELS = new Set([
    "开头", "结尾",
    "开始", "结束",
    "复制", "下载",
    "Copy", "Download",
    "Start", "End",
  ]);

  function findChatContainer() {
    return document.querySelector(
      window.__dsx__.config?.chatContainerSelector
        || ".ds-virtual-list-visible-items");
  }

  function isGenerating() {
    for (const btn of document.querySelectorAll('button, [role="button"]')) {
      const r = btn.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const label = (btn.getAttribute("aria-label") || "").toLowerCase().trim();
      const text = (btn.textContent || "").toLowerCase().trim();
      const testid = (btn.getAttribute("data-testid") || "").toLowerCase().trim();
      if (label === "stop" || label === "stop generating"
          || label === "停止" || label === "停止生成"
          || text === "stop" || text === "停止"
          || testid === "stop" || testid === "stop-button"
          || testid === "stop-generating") return true;
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

  // Return the LAST .ds-markdown that is NOT inside a thinking
  // container — the answer markdown.  DeepSeek renders thoughts
  // markdown first, answer markdown last.  Iterating from the end
  // and skipping thinking-ancestored elements lands on the answer.
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
    const a = container.querySelectorAll('[data-role="assistant"]');
    if (a.length) return a[a.length - 1];
    return null;
  }

  function readLastAssistantInnerText(container) {
    const last = _lastDsMessage(container);
    if (!last) return "";
    const answer = _findAnswerMarkdown(last);
    return answer ? (answer.innerText || "") : "";
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

  // Call the page-world extractor and pull out the last assistant
  // message's raw .content (fenced markdown, no thoughts).
  //
  // `__dsx_extract` tries API first (with the userToken), then
  // IndexedDB, then fiber.  Both API and IndexedDB return
  // fragments with raw .content; fences are intact.
  //
  // Hard 15 s timeout so a hung API doesn't stall the settle path
  // past the daemon's WATCH_MAX_WAIT deadline.
  async function fetchFinalFromApi(batchId) {
    if (typeof window.__dsx_extract !== "function") {
      console.warn(
        `[DSX-WATCH ${batchId}] __dsx_extract unavailable ` +
        `(injected-extract.js not loaded)`);
      return null;
    }
    const timeoutPromise = new Promise((_, reject) => {
      setTimeout(
        () => reject(new Error("extract timeout")),
        _API_EXTRACT_TIMEOUT_MS);
    });
    try {
      const respJson = await Promise.race([
        window.__dsx_extract(JSON.stringify({ include_forked: false })),
        timeoutPromise,
      ]);
      const data = JSON.parse(respJson);
      const msgs = data.messages || [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === "assistant" && msgs[i].content) {
          return msgs[i].content;
        }
      }
      console.warn(
        `[DSX-WATCH ${batchId}] extract returned no assistant content ` +
        `(${msgs.length} msg(s))`);
      return null;
    } catch (e) {
      console.warn(
        `[DSX-WATCH ${batchId}] extract failed:`,
        e && e.message ? e.message : e);
      return null;
    }
  }

  function createWatcher(batchId, opts, onDelta, onDone) {
    const settleGraceMs = Math.max(100, opts.settle_ms || 400);
    const maxDurationMs = Math.max(5000, opts.max_duration_ms || 180000);
    const submitTimeoutMs = Math.max(
      5000, opts.submit_timeout_ms || _SUBMIT_TIMEOUT_MS);

    const startedAt = Date.now();
    let stopped = false;
    let msgObserver = null;
    let container = null;
    let containerPollTimer = null;
    let safetyPollTimer = null;
    let settleTimer = null;
    let rafScheduled = false;
    let sawGenerating = false;
    let emitted = false;

    function stop(reason) {
      if (stopped) return;
      stopped = true;
      console.log(`[DSX-WATCH ${batchId}] STOP reason=${reason} ` +
        `emitted=${emitted}`);
      if (msgObserver) msgObserver.disconnect();
      if (settleTimer) clearTimeout(settleTimer);
      if (containerPollTimer) clearInterval(containerPollTimer);
      if (safetyPollTimer) clearInterval(safetyPollTimer);
      watchers.delete(batchId);
      onDone(reason);
    }

    async function settleNow() {
      settleTimer = null;
      if (stopped) return;

      // Generation may have resumed after the grace period started.
      if (isGenerating()) {
        sawGenerating = true;
        return;
      }

      // Try the API extractor first (fenced markdown, no thoughts,
      // no chrome).
      let content = await fetchFinalFromApi(batchId);
      let source = "api";

      if (!content) {
        // Fallback: fence-less innerText with chrome stripped.
        content = filterChrome(readLastAssistantInnerText(container));
        source = "innerText-fallback";
      }

      if (content) {
        emitted = true;
        console.log(
          `[DSX-WATCH ${batchId}] EMIT_FINAL ` +
          `source=${source} len=${content.length} ` +
          `hasFences=${content.indexOf(String.fromCharCode(96,96,96)) >= 0} ` +
          `preview=${JSON.stringify(content.slice(0, 150))}`);
        onDelta(content);
      } else {
        console.warn(
          `[DSX-WATCH ${batchId}] EMIT_FINAL no content available ` +
          `(api=null, dom='')`);
      }

      stop("settled");
    }

    function extractAndEmit() {
      if (stopped) return;
      if (Date.now() - startedAt > maxDurationMs) {
        console.warn(`[DSX-WATCH ${batchId}] MAX_DURATION reached`);
        stop("max_duration");
        return;
      }

      const gen = isGenerating();

      // ---- phase 1: wait for submit confirmation ----
      if (!sawGenerating) {
        const curCount = container
          ? container.querySelectorAll(".ds-message").length : 0;
        const lastMsg = _lastDsMessage(container);
        const curMsgLen = lastMsg ? (lastMsg.innerText || "").length : 0;
        if (gen || curCount > initialMsgCount || curMsgLen > initialLastMsgLen) {
          sawGenerating = true;
          console.log(`[DSX-WATCH ${batchId}] SUBMIT_CONFIRMED`);
        } else if (Date.now() - startedAt > submitTimeoutMs) {
          console.warn(
            `[DSX-WATCH ${batchId}] SUBMIT_FAILED after ${submitTimeoutMs}ms`);
          stop("submit_failed");
        }
        return;
      }

      // ---- phase 2: generation in progress ----
      if (gen) {
        // Still generating.  If a settle timer somehow fires while
        // gen is true, settleNow will re-arm by returning early
        // (see above).  Nothing to do here.
        if (settleTimer) {
          clearTimeout(settleTimer);
          settleTimer = null;
        }
        return;
      }

      // ---- phase 3: generation stopped, arm settle ----
      if (!settleTimer) {
        settleTimer = setTimeout(settleNow, settleGraceMs);
      }
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
      } else if (Date.now() - startedAt > submitTimeoutMs) {
        if (containerPollTimer) clearInterval(containerPollTimer);
        containerPollTimer = null;
        stop("no_deepseek_tab");
      }
    };
    tryObserve();
    if (!container) containerPollTimer = setInterval(tryObserve, 50);

    safetyPollTimer = setInterval(() => {
      if (stopped) return;
      const fresh = findChatContainer();
      if (fresh && fresh !== container) {
        if (msgObserver) msgObserver.disconnect();
        container = fresh;
        msgObserver = new MutationObserver(() => scheduleExtract());
        msgObserver.observe(container, {
          childList: true, characterData: true, subtree: true,
        });
      }
      extractAndEmit();
    }, 200);

    return { stop: () => { if (!stopped) stop("client_stop"); } };
  }

  window.__dsx_startWatch = function (argsJson) {
    let args;
    try { args = JSON.parse(argsJson); } catch (e) {
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

    const port = (typeof args.port === "number") ? args.port : 8765;
    const base = `http://127.0.0.1:${port}`;
    console.log(
      `DSX:watch start (batch=${batchId}, port=${port}, mode=batch)`);

    const existing = watchers.get(batchId);
    if (existing) existing.stop();

    const onDelta = (delta) => {
      fetch(`${base}/export`, {
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
      fetch(`${base}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "watch-done",
          batch_id: batchId,
          reason,
        }),
      }).catch(() => {});
    };

    const w = createWatcher(batchId, args, onDelta, onDone);
    watchers.set(batchId, w);
    return JSON.stringify({ success: true, mode: "batch-api" });
  };

  window.__dsx_stopWatch = function (argsJson) {
    let args;
    try { args = JSON.parse(argsJson); } catch (_) { return "{}"; }
    const w = watchers.get(args.batch_id);
    if (w) { w.stop(); watchers.delete(args.batch_id); }
    return JSON.stringify({ success: true });
  };
})();
