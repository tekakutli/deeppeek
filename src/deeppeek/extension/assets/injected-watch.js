// Streaming watcher (v17, batch mode + wait-for-reply fast path).
//
// =====================================================================
// watch: heavyweight, extracts full content via API, waits settle ticks
// =====================================================================
// Original purpose: stream a chat completion back to the daemon (or
// aider) as OpenAI-compatible deltas.  Waits for the Stop button to
// disappear, then extracts the final content via the API extractor,
// then waits settle ticks.  Total overhead per call: ~2-3s.
//
// This remains the right tool for the daemon and --use-watch mode.
//
// =====================================================================
// wait-for-reply: lightweight, DOM-only, returns when content appears
// =====================================================================
// Added 2026-11-15 for split_send's inter-part pacing.  The user does
// NOT need the reply content — it's a "just say roger" acknowledgment.
// All we need is "the model has visibly responded; the next part can
// begin".  This polls the DOM for:
//
//   1. msgCount > baseline_msg_count (a new .ds-message has appeared)
//   2. at least one message at index >= baseline + 1 has a
//      .ds-markdown child with trimmed text length >= min_reply_chars
//
// Returns as soon as both conditions hold — typically ~1.5s after
// submit for short replies.  Falls back to timeout (default 60s).
//
// Design decisions:
//
//   · Message index baseline.  `baseline_msg_count` is the count
//     BEFORE submit (captured by doEnter as `msg_count_before`).  The
//     user's new message lands at index `baseline`, the assistant
//     placeholder at `baseline + 1`.  We scan for indices >= baseline
//     + 1 so the user's own message (which can also contain markdown)
//     never false-matches.
//
//   · Content signal.  `.ds-markdown` (or `.markdown-body` or
//     `.prose`) is what DeepSeek renders answer text into.  A
//     message that has this child with non-empty trimmed innerText
//     is considered "responded".  Thinking content is excluded via
//     `_isInsideThinking` (same check the watch flow uses).
//
//   · No Stop-button wait.  We do NOT require the Stop button to be
//     gone.  The reason: for "just say roger", content appears
//     ~1.5s before Stop clears (~2s).  Since split_send's between-
//     parts ready-wait already blocks on the composer being usable,
//     returning early here just fires the next part's `attach`
//     event 0.5s sooner — the attach self-blocks on composer
//     readiness.  Net: we save the extraction round-trip, and the
//     ready-wait retains its guarantee.
//
//   · No content emission.  Unlike watch, we don't emit
//     watch-delta.  Callers either use the reply_text for logging
//     or ignore it.  split_send uses it for the log preview.
//
//   · Single ack.  No deltas, no done event.  Just one
//     `__ack: "wait-for-reply"` on completion.  Simpler than watch.
//
// Exposed as both a bridge entry point (`__dsx_wait_for_reply_json`)
// and a page-world function (`__dsx__.waitForReply`).
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

  // Delegate to the shared isGenerating in injected.js so we have a
  // single source of truth.
  function isGenerating() {
    if (typeof window.__dsx__.isGenerating === "function") {
      try { return !!window.__dsx__.isGenerating(); }
      catch (_) { /* fall through */ }
    }
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

  // =====================================================================
  // wait-for-reply — fast "content appeared" detector
  // =====================================================================

  async function _waitForReplyImpl(opts) {
    const baseline = Number(opts.baseline_msg_count) || 0;
    const minChars = Math.max(1, Number(opts.min_reply_chars) || 1);
    const maxDurationMs = Number(opts.max_duration_ms) || 60000;
    const pollMs = Math.max(50, Number(opts.poll_interval_ms) || 200);

    const startedAt = Date.now();
    let lastSig = null;
    let sawContainer = false;

    while (Date.now() - startedAt < maxDurationMs) {
      const container = findChatContainer();
      if (!container) {
        await new Promise(r => setTimeout(r, pollMs));
        continue;
      }
      if (!sawContainer) {
        sawContainer = true;
        console.log(
          "[deeppeek:wait-reply] container found, polling for reply");
      }

      const msgs = container.querySelectorAll(".ds-message");
      const count = msgs.length;

      // Scan for a new assistant message with content.  We start at
      // index baseline+1 because index baseline is the user's own
      // prompt (which can also contain markdown — we don't want to
      // false-match it).
      if (count >= baseline + 1) {
        for (let i = count - 1; i >= baseline + 1; i--) {
          const msg = msgs[i];
          const answer = _findAnswerMarkdown(msg);
          if (!answer) continue;
          const text = (answer.innerText || "").trim();
          if (text.length >= minChars) {
            console.log(
              `[deeppeek:wait-reply] reply found at index ${i} ` +
              `(${text.length} chars): ` +
              JSON.stringify(text.slice(0, 80)));
            return {
              success: true,
              found: true,
              reply_text: text,
              reply_index: i,
              reply_length: text.length,
              waited_ms: Date.now() - startedAt,
              msg_count: count,
              baseline,
              is_generating: isGenerating(),
            };
          }
        }
      }

      const gen = isGenerating();
      const sig = JSON.stringify({ count, gen });
      if (sig !== lastSig) {
        console.log("[deeppeek:wait-reply] state:", {
          elapsed_ms: Date.now() - startedAt,
          msg_count: count,
          baseline,
          needed: baseline + 1,
          is_generating: gen,
        });
        lastSig = sig;
      }

      await new Promise(r => setTimeout(r, pollMs));
    }

    const container = findChatContainer();
    const finalCount = container
      ? container.querySelectorAll(".ds-message").length : 0;
    return {
      success: false,
      found: false,
      reason: `no assistant reply with content appeared within ` +
              `${maxDurationMs}ms`,
      waited_ms: Date.now() - startedAt,
      msg_count: finalCount,
      baseline,
      is_generating: isGenerating(),
    };
  }

  window.__dsx__.waitForReply = _waitForReplyImpl;

  window.__dsx_wait_for_reply_json = async function (argsJson) {
    let args = {};
    try {
      args = JSON.parse(argsJson || "{}");
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    try {
      const result = await _waitForReplyImpl(args);
      console.log("[deeppeek:wait-reply] result:", result);
      return JSON.stringify(result);
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `wait-for-reply threw: ${e.message}`,
      });
    }
  };

  // =====================================================================
  // watch — heavyweight streaming (unchanged from previous version)
  // =====================================================================

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
    let initialMsgCount = 0;
    let initialLastMsgLen = 0;

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

      if (isGenerating()) {
        sawGenerating = true;
        return;
      }

      let content = await fetchFinalFromApi(batchId);
      let source = "api";

      if (!content) {
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

      if (!sawGenerating) {
        const curCount = container
          ? container.querySelectorAll(".ds-message").length : 0;
        const lastMsg = _lastDsMessage(container);
        const curMsgLen = lastMsg ? (lastMsg.innerText || "").length : 0;
        if (gen || curCount > initialMsgCount
            || curMsgLen > initialLastMsgLen) {
          sawGenerating = true;
          console.log(`[DSX-WATCH ${batchId}] SUBMIT_CONFIRMED`);
        } else if (Date.now() - startedAt > submitTimeoutMs) {
          console.warn(
            `[DSX-WATCH ${batchId}] SUBMIT_FAILED after ${submitTimeoutMs}ms`);
          stop("submit_failed");
        }
        return;
      }

      if (gen) {
        if (settleTimer) {
          clearTimeout(settleTimer);
          settleTimer = null;
        }
        return;
      }

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
      initialMsgCount = el.querySelectorAll(".ds-message").length;
      const last = _lastDsMessage(el);
      initialLastMsgLen = last ? (last.innerText || "").length : 0;
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