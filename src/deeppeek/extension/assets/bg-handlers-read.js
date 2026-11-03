// Read-side events: extract, watch, extract-attachments, retry-last.
//
// extract / watch are the daemon's paths — extract is a one-shot
// snapshot, watch is the streaming path (event-driven via the page
// world's MutationObserver, with a legacy polling fallback if the
// observer path is unavailable). extract-attachments and retry-last
// are the interactive prompt paths.
//
// `watch` forwards `port` to the content script so the page-world
// watcher posts deltas back to the same server that sent the event.

async function handleExtract(data, port) {
  const batchId = data.batch_id;
  const includeForked = !!data.include_forked;

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_EXTRACT",
        include_forked: includeForked,
      });
      if (resp) result = { success: true, payload: resp };
      else result = { success: false, reason: "no response from content script" };
    }
  } catch (err) {
    result = { success: false, reason: `sendMessage threw: ${err.message}` };
  }

  await postAck(port, { __ack: "extract", batch_id: batchId, result });
}

async function handleExtractAttachments(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} extract-attachments received (batch=${batchId}, ` +
    `mode=${opts.mode})`);

  let result = { success: false, ok: false,
                 reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_EXTRACT_ATTACHMENTS",
        opts,
      });
      result = resp || {
        success: false, ok: false,
        reason: "no response from content script",
      };
    }
  } catch (err) {
    result = {
      success: false, ok: false,
      reason: `sendMessage threw: ${err.message}`,
    };
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

  console.log(`DSX:${tag} retry-last received (batch=${batchId})`);

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_RETRY_LAST",
        opts,
      });
      result = resp || {
        success: false,
        reason: "no response from content script",
      };
    }
  } catch (err) {
    result = {
      success: false,
      reason: `sendMessage threw: ${err.message}`,
    };
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
  const tag = `[${port}]`;

  // Ack first.
  await postAck(port, {
    __ack: "watch",
    batch_id: batchId,
    result: { success: true },
  });

  let tab = null;
  if (pinnedTabId != null) {
    try { tab = await browser.tabs.get(pinnedTabId); } catch (_) {}
  }
  if (!tab) {
    const [active] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    tab = active;
  }
  if (!tab || !tab.url
      || !tab.url.startsWith("https://chat.deepseek.com/")) {
    await postAck(port, {
      __ack: "watch-done",
      batch_id: batchId,
      reason: "no_deepseek_tab",
    });
    return;
  }

  // Event-driven path — forward `port` so injected-watch.js posts
  // deltas back to the correct server.
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

  // Legacy polling fallback (all acks via postAck).
  let lastContent = "";
  let stableTicks = 0;
  let stopped = false;
  let timer = null;
  let firstContentAt = null;
  const startedAt = Date.now();
  let lastActivityAt = startedAt;
  let initialMsgCount = 0;

  const pollStop = async (reason) => {
    if (stopped) return;
    stopped = true;
    if (timer) { clearInterval(timer); timer = null; }
    await postAck(port, {
      __ack: "watch-done",
      batch_id: batchId, reason: reason || null,
    });
  };

  const poll = async () => {
    if (stopped) return;
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
  };
  timer = setInterval(poll, pollInterval);
  poll();
}