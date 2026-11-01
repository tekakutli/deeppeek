// =====================================================================
// injected-watch.js — MutationObserver-based streaming watcher (T2)
// =====================================================================
//
// Replaces the polling `setInterval` in background.js's `watch`
// handler with:
//
//   · MutationObserver on the message container  → instant delta
//   · MutationObserver on the composer          → instant done
//     (the "Stop generating" button vanishes when the model
//     finishes; that transition fires the observer immediately)
//   · settle_ms timeout as a fallback for both, so the watcher
//     still terminates when the button selector doesn't match
//     (DeepSeek UI change, or an intermediate "regenerating" state)
//
// Extraction during streaming uses `extractViaFiber` — the API
// and IndexedDB paths are stale until the fetch completes, but
// fiber state is updated synchronously with each token.
//
// Exports on `window.__dsx__`:
//
//   startWatch(argsJson)  → {success, mode}
//   stopWatch(argsJson)   → {success}
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  const watchers = new Map();

  // ---------- helpers ----------------------------------------------

  function findChatContainer() {
    // The messages live under `.ds-virtual-list-visible-items`.
    // On a freshly-opened tab the container may not exist yet;
    // the caller polls for it (see `createWatcher`).
    return document.querySelector(
      window.__dsx__.config?.chatContainerSelector
        || ".ds-virtual-list-visible-items");
  }

  function findComposerArea() {
    return document.querySelector("form")
        || document.querySelector('[class*="composer"]')
        || document.querySelector('[class*="chat-input"]');
  }

  function isGenerating() {
    // DeepSeek shows a stop button while generating. Detect by
    // label / text, in both English and Chinese, plus class hints.
    // Being generous here is safe: a false positive only delays
    // "done" until the settle_ms fallback fires.
    const candidates = document.querySelectorAll(
      'button, [role="button"]');
    for (const btn of candidates) {
      const label = (btn.getAttribute("aria-label") || "").toLowerCase();
      const text = (btn.textContent || "").toLowerCase().trim();
      const cls = (btn.className && typeof btn.className === "string")
        ? btn.className.toLowerCase() : "";
      if (label.includes("stop") || text === "stop"
          || label.includes("停止") || text === "停止"
          || cls.includes("stop")) {
        return true;
      }
    }
    return false;
  }

  function lastAssistantFromFiber() {
    try {
      const msgs = window.__dsx__.extractViaFiber();
      if (!msgs) return "";
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i] && msgs[i].role === "assistant") {
          return msgs[i].content || "";
        }
      }
    } catch (_) {}
    return "";
  }

  function lastAssistantFromDOM(container) {
    if (!container) return "";
    const nodes = container.querySelectorAll(
      '.ds-message, [data-role="assistant"], [class*="assistant"]');
    if (!nodes.length) return "";
    const last = nodes[nodes.length - 1];
    const textEl = last.querySelector(
      '[class*="markdown"], [class*="content"], [class*="prose"]');
    return (textEl || last).innerText || "";
  }

  // ---------- watcher ----------------------------------------------

  function createWatcher(batchId, opts, onDelta, onDone) {
    const settleMs = Math.max(100, opts.settle_ms || 800);
    const maxDurationMs = Math.max(5000, opts.max_duration_ms || 180000);
    const firstContentTimeoutMs = Math.max(
      5000, opts.first_content_timeout_ms || 30000);
    const startedAt = Date.now();
    let lastContent = "";
    let lastMutationAt = Date.now();
    let settleTimer = null;
    let containerPollTimer = null;
    let stopped = false;
    let msgObserver = null;
    let compObserver = null;
    let container = null;

    function stop(reason) {
      if (stopped) return;
      stopped = true;
      if (msgObserver) msgObserver.disconnect();
      if (compObserver) compObserver.disconnect();
      if (settleTimer) clearTimeout(settleTimer);
      if (containerPollTimer) clearInterval(containerPollTimer);
      watchers.delete(batchId);
      onDone(reason);
    }

    function armSettle() {
      if (settleTimer) clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        // Only fire "done" when both signals agree the model has
        // stopped: no recent mutations AND no stop button.
        if (!isGenerating() && lastContent) {
          stop("settled");
        }
        // Otherwise re-arm: the model paused briefly (tool call,
        // network hiccup).  The next mutation or composer change
        // will re-arm again.
        else if (lastContent) armSettle();
      }, settleMs);
    }

    function extractAndEmit() {
      if (stopped) return;
      const now = Date.now();

      if (now - startedAt > maxDurationMs) {
        stop("max_duration");
        return;
      }

      // Fiber is the freshest source while streaming.
      let content = lastAssistantFromFiber();
      if (!content) content = lastAssistantFromDOM(container);

      if (!content) {
        if (now - startedAt > firstContentTimeoutMs) {
          stop("no_first_content");
        }
        return;
      }

      if (content === lastContent) return;

      if (content.startsWith(lastContent)) {
        const delta = content.slice(lastContent.length);
        lastContent = content;
        lastMutationAt = now;
        if (delta) onDelta(delta);
      } else {
        // Rewrite — send full content as a single delta so the
        // daemon's SSE consumer sees the correction.
        lastContent = content;
        lastMutationAt = now;
        onDelta(content);
      }
      armSettle();
    }

    function startObserving(el) {
      container = el;
      msgObserver = new MutationObserver(() => {
        lastMutationAt = Date.now();
        // Coalesce: extract once per event-loop tick.
        queueMicrotask(extractAndEmit);
      });
      msgObserver.observe(el, {
        childList: true, characterData: true, subtree: true,
      });

      // Also observe the composer for stop → send transitions.
      const comp = findComposerArea();
      if (comp) {
        compObserver = new MutationObserver(() => {
          if (!isGenerating() && lastContent) {
            // Give the DOM one tick to finish the final render
            // before extraction, then decide.
            queueMicrotask(() => {
              extractAndEmit();
              if (!isGenerating()) stop("settled");
            });
          }
        });
        compObserver.observe(comp, {
          childList: true, subtree: true,
          attributes: true,
          attributeFilter: ["aria-label", "disabled", "class"],
        });
      }

      // Initial extraction — if the message is already partially
      // rendered when watch starts, capture it.
      extractAndEmit();
    }

    // The container may not exist on a fresh tab.  Poll for it.
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
        stop("no_deepseek_tab");
      }
    };
    tryObserve();
    if (!container) {
      containerPollTimer = setInterval(tryObserve, 50);
    }

    return {
      stop: () => { if (!stopped) stop("client_stop"); },
    };
  }

  // ---------- bridge entry points ----------------------------------

  window.__dsx__.startWatch = function (argsJson) {
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

    // Cancel any existing watcher for this batch_id (defensive
    // against a retried watch).
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

  window.__dsx__.stopWatch = function (argsJson) {
    let args;
    try { args = JSON.parse(argsJson); } catch (_) { return "{}"; }
    const w = watchers.get(args.batch_id);
    if (w) { w.stop(); watchers.delete(args.batch_id); }
    return JSON.stringify({ success: true });
  };
})();
