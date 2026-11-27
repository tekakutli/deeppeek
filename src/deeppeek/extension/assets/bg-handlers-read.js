// Read-side events: extract, watch, stop-watch, wait-for-reply,
// extract-attachments, retry-last.
//
// wait-for-reply (2026-11-15): fast reply-appearance detector for
// split_send.  Forwards one message to the page world, single ack
// on completion.  The page world polls the DOM for a new assistant
// message with content; returns as soon as one appears (~1.5s
// typical).  No content emission, no cleanup tracking.
//
// watch / error-banner detection (2026-11-24):
// The primary watch path runs inside the page world
// (injected-watch.js) and POSTs watch-delta/watch-done directly to
// /export.  This file's `handleWatch` only runs the legacy polling
// fallback when the MutationObserver path can't be started.  Both
// paths now check for DeepSeek error banners (quota, rate-limit,
// model unavailable) and report `reason: "error_banner"` with a
// structured `error` payload — so the daemon raises a BridgeError
// instead of returning stale content from a previous turn.
//
// The polling fallback uses DSX_DETECT_ERROR (content.js → page
// world __dsx_detect_error_json) once per poll.  A simple in-flight
// guard prevents overlapping polls while that extra round-trip is
// in flight.

async function handleExtract(data, port) {
  const batchId = data.batch_id;
  const includeForked = !!data.include_forked;

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_EXTRACT",
        include_forked: includeForked,
      });
      if (resp) result = { success: true, payload: resp };
      else result = {
        success: false,
        reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  await postAck(port, { __ack: "extract", batch_id: batchId, result });
}

async function handleWaitForReply(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} wait-for-reply received ` +
    `(batch=${batchId}, baseline=${opts.baseline_msg_count}, ` +
    `tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_WAIT_FOR_REPLY",
        opts,
      });
      result = resp || {
        success: false,
        reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} wait-for-reply result:`, {
    ok: result.success,
    waited_ms: result.waited_ms,
    reply_length: result.reply_length,
  });

  await postAck(port, {
    __ack: "wait-for-reply",
    batch_id: batchId,
    result,
  });
}

async function handleExtractAttachments(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} extract-attachments received (batch=${batchId}, ` +
    `mode=${opts.mode}, tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = {
      success: false, ok: false,
      reason: "target tab unavailable",
    };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_EXTRACT_ATTACHMENTS",
        opts,
      });
      result = resp || {
        success: false, ok: false,
        reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false, ok: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} extract-attachments result:`, {
    ok: result.ok,
    n_files: (result.files || []).length,
    n_errors: (result.files || []).filter(f => f.error).length,
  });

  await postAck(port, {
    __ack: "extract-attachments",
    batch_id: batchId,
    result,
  });
}

async function handleRetryLast(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} retry-last received (batch=${batchId}, ` +
    `tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_RETRY_LAST",
        opts,
      });
      result = resp || {
        success: false,
        reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} retry-last result:`, {
    ok: result.success,
    message_id: result.message_id,
    files_attached: result.files_attached,
    text_length: result.text_length,
  });

  await postAck(port, {
    __ack: "retry-last",
    batch_id: batchId,
    result,
  });
}

async function handleWatch(data, port) {
  const batchId = data.batch_id;
  const pollInterval = Math.max(100, data.poll_interval_ms || 300);
  const settleTicks = Math.max(1, data.settle_ticks || 5);
  const maxDuration = Math.max(5000, data.max_duration_ms || 180000);
  const firstContentTimeoutMs =
    Math.max(5000, data.first_content_timeout_ms || 30000);
  const pinnedTabId = (data.tab_id != null) ? data.tab_id : null;
  const strictPin = !!data.strict_pin;
  const tag = `[${port}]`;

  await postAck(port, {
    __ack: "watch",
    batch_id: batchId,
    result: { success: true },
  });

  let tab = null;
  if (pinnedTabId != null) {
    try { tab = await browser.tabs.get(pinnedTabId); } catch (_) {}
  }
  if (!tab && !strictPin) {
    const [active] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    tab = active;
  }
  if (!isSupportedTab(tab)) {
    const reason = strictPin
      ? "pinned tab closed or navigated away"
      : "no_deepseek_tab";
    await postAck(port, {
      __ack: "watch-done",
      batch_id: batchId,
      reason,
    });
    return;
  }

  let started = false;
  try {
    const resp = await browser.tabs.sendMessage(tab.id, {
      type: "DSX_WATCH_START",
      opts: {
        batch_id: batchId,
        port: port,
        settle_ms: Math.max(100, settleTicks * pollInterval),
        max_duration_ms: maxDuration,
        first_content_timeout_ms: firstContentTimeoutMs,
      },
    });
    started = !!(resp && resp.success);
  } catch (_) {}

  console.log(
    `DSX:${tag} watch via ` +
    `${started ? "mutation-observer" : "polling"} ` +
    `(batch=${batchId})`);

  if (started) return;

  // Legacy polling fallback.  Kept as a safety net when the
  // MutationObserver path can't be started; rare in practice.
  // The error-banner check (DSX_DETECT_ERROR) runs FIRST on every
  // poll — before content extraction — so a quota/rate-limit banner
  // can't cause us to emit the previous turn's assistant message.
  let lastContent = "";
  let stableTicks = 0;
  let stopped = false;
  let timer = null;
  let firstContentAt = null;
  const startedAt = Date.now();
  let lastActivityAt = startedAt;
  let initialMsgCount = 0;
  let inFlight = false;

  const pollStop = async (reason, error) => {
    if (stopped) return;
    stopped = true;
    if (timer) { clearInterval(timer); timer = null; }
    const payload = {
      __ack: "watch-done",
      batch_id: batchId,
      reason: reason || null,
    };
    if (error) payload.error = error;
    await postAck(port, payload);
  };

  const poll = async () => {
    if (stopped || inFlight) return;
    inFlight = true;
    try {
      // ---- NEW: error-banner check FIRST ----
      try {
        const errResp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_DETECT_ERROR",
        });
        if (errResp && errResp.error) {
          console.warn(
            `DSX:${tag} watch polling: error banner detected ` +
            `(pattern=${errResp.error.pattern} ` +
            `category=${errResp.error.category})`);
          await pollStop("error_banner", errResp.error);
          return;
        }
      } catch (_) {
        // Older content.js without DSX_DETECT_ERROR support — fall
        // through; error detection will be missed on this poll but
        // the primary MutationObserver path already covers the
        // common case.
      }

      if (Date.now() - startedAt > maxDuration) {
        await pollStop("max_duration"); return;
      }

      let resp;
      try {
        resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_EXTRACT", include_forked: false,
        });
      } catch (_) { stableTicks = 0; return; }
      if (!resp) { stableTicks = 0; return; }
      let content = "";
      let thinking = "";
      const msgs = resp.messages || [];
      if (initialMsgCount === 0) {
        initialMsgCount = msgs.length;
      }
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === "assistant") {
          content = msgs[i].content || "";
          thinking = msgs[i].thinking || "";
          break;
        }
      }
      if (content || thinking || (msgs.length > initialMsgCount)) {
        lastActivityAt = Date.now();
      }
      if (content) {
        if (firstContentAt === null) firstContentAt = Date.now();
      } else if (firstContentAt === null
                 && Date.now() - lastActivityAt > firstContentTimeoutMs) {
        await pollStop("no_first_content"); return;
      }
      if (content === lastContent) {
        if (lastContent) stableTicks++;
      } else if (content.startsWith(lastContent)) {
        const delta = content.slice(lastContent.length);
        lastContent = content; stableTicks = 0;
        if (delta) {
          await postAck(port, {
            __ack: "watch-delta", batch_id: batchId, delta,
          });
        }
      } else {
        lastContent = content; stableTicks = 0;
        await postAck(port, {
          __ack: "watch-rewrite", batch_id: batchId, content,
        });
      }
      if (lastContent && stableTicks >= settleTicks) {
        await pollStop("settled");
      }
    } finally {
      inFlight = false;
    }
  };
  timer = setInterval(poll, pollInterval);
  poll();
}

async function handleStopWatch(data, port) {
  const batchId = data.batch_id;
  const tag = `[${port}]`;

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_WATCH_STOP",
        opts: { batch_id: batchId },
      });
      result = resp || {
        success: false,
        reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(
    `DSX:${tag} stop-watch result (batch=${batchId}):`, result);
  await postAck(port, {
    __ack: "stop-watch", batch_id: batchId, result,
  });
}