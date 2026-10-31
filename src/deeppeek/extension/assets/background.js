// Background script.  SSE client + tab listeners + content-script port.
// Watchdog state persisted in browser.storage.local.watchdog.

const DSX_URL = "http://127.0.0.1:8765";

let armed = false;
let firing = false;
let includeForked = false;
let eventSource = null;
let wasConnected = false;
let watchdogTimer = null;
let watchdogEnabled = true;
let activePorts = new Set();

// ---- watchdog state: persisted in storage.local ----------------

browser.storage.local.get("watchdog").then(({ watchdog }) => {
  watchdogEnabled = watchdog !== false;
  if (watchdogEnabled) startWatchdog();
  console.log(`DSX: watchdog=${watchdogEnabled} (loaded from storage)`);
});

browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !("watchdog" in changes)) return;
  const enabled = changes.watchdog.newValue !== false;
  if (enabled === watchdogEnabled) return;
  watchdogEnabled = enabled;
  if (enabled) { console.log("DSX: watchdog ON"); startWatchdog(); }
  else { console.log("DSX: watchdog OFF"); stopWatchdog(); }
});

// ---- SSE plumbing ------------------------------------------------

function attachHandlers(es) {
  es.addEventListener("arm", () => {
    console.log("DSX: arm received");
    armed = true;
    firing = false;
    maybeFire();
  });

  es.addEventListener("disarm", () => {
    console.log("DSX: disarm received");
    armed = false;
    firing = false;
  });

  es.addEventListener("config", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}

    if ("include_forked" in data) {
      includeForked = !!data.include_forked;
      console.log(`DSX: config include_forked=${includeForked}`);
    }

    if (!("watchdog" in data)) return;
    const enabled = data.watchdog !== false;
    browser.storage.local.set({ watchdog: enabled }).then(() => {
      fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __ack: "watchdog", watchdog: enabled }),
      }).catch(() => {});
    });
  });

  es.addEventListener("request-status", () => {
    browser.storage.local.get("watchdog").then(({ watchdog }) => {
      const enabled = watchdog !== false;
      fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __status: "watchdog", watchdog: enabled }),
      }).catch(() => {});
    });
  });

  // ---- attach (deeppeek pipe) ----
  es.addEventListener("attach", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const files = data.files || [];
    const opts = data.opts || {};

    console.log(`DSX: attach received (batch=${batchId}, ${files.length} file(s))`);

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_ATTACH",
          files,
          opts,
        });
        if (resp) result = resp;
        else result = { success: false, reason: "no response from content script" };
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }

    console.log(`DSX: attach result (batch=${batchId}):`, result);

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "attach",
          batch_id: batchId,
          result,
        }),
      });
    } catch (_) {}
  });

  // ---- type (deeppeek type) ----
  es.addEventListener("type", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const text = data.text || "";
    const opts = data.opts || {};

    console.log(`DSX: type received (batch=${batchId}, ${text.length} chars)`);

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_TYPE",
          text,
          opts,
        });
        result = resp || { success: false, reason: "no response from content script" };
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }

    console.log(`DSX: type result (batch=${batchId}):`, result);

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __ack: "type", batch_id: batchId, result }),
      });
    } catch (_) {}
  });

  // ---- enter (deeppeek enter) ----
  es.addEventListener("enter", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(`DSX: enter received (batch=${batchId})`);

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_ENTER",
          opts,
        });
        result = resp || { success: false, reason: "no response from content script" };
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }

    console.log(`DSX: enter result (batch=${batchId}):`, result);

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __ack: "enter", batch_id: batchId, result }),
      });
    } catch (_) {}
  });

  // ---- open-tab (deeppeek prompt new) ----
  es.addEventListener("open-tab", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const url = data.url || "https://chat.deepseek.com/";

    console.log(`DSX: open-tab received (batch=${batchId}, url=${url})`);

    let result = { success: false, reason: "tabs.create threw" };
    try {
      const tab = await browser.tabs.create({ url, active: true });
      result = { success: true, tab_id: tab.id, url: tab.url || url };
    } catch (err) {
      result = {
        success: false,
        reason: `tabs.create threw: ${err.message}`,
      };
    }

    console.log(`DSX: open-tab result (batch=${batchId}):`, result);

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "open-tab",
          batch_id: batchId,
          result,
        }),
      });
    } catch (_) {}
  });

  // ---- hijack-install (deeppeek hijack) ----
  es.addEventListener("hijack-install", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    console.log(`DSX: hijack-install received (batch=${batchId})`);
    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_HIJACK_INSTALL",
        });
        result = resp || { success: false, reason: "no response from content script" };
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }
    console.log(`DSX: hijack-install result:`, result);
    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __ack: "hijack-install", batch_id: batchId, result }),
      });
    } catch (_) {}
  });

  // ---- hijack-uninstall ----
  es.addEventListener("hijack-uninstall", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    console.log(`DSX: hijack-uninstall received (batch=${batchId})`);
    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_HIJACK_UNINSTALL",
        });
        result = resp || { success: false, reason: "no response from content script" };
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }
    console.log(`DSX: hijack-uninstall result:`, result);
    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __ack: "hijack-uninstall", batch_id: batchId, result }),
      });
    } catch (_) {}
  });

  // ---- clear (deeppeek prompt delete) ----
  es.addEventListener("clear", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(`DSX: clear received (batch=${batchId})`);

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_CLEAR",
          opts,
        });
        result = resp || { success: false, reason: "no response from content script" };
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }

    console.log(`DSX: clear result (batch=${batchId}):`, result);

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ __ack: "clear", batch_id: batchId, result }),
      });
    } catch (_) {}
  });

  // ---- extract-attachments (deeppeek prompt attachments) ----
  es.addEventListener("extract-attachments", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(
      `DSX: extract-attachments received (batch=${batchId}, ` +
      `mode=${opts.mode})`);

    let result = { success: false, ok: false,
                   reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url &&
          tab.url.startsWith("https://chat.deepseek.com/")) {
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

    console.log(`DSX: extract-attachments result:`, {
      ok: result.ok,
      n_files: (result.files || []).length,
      n_errors: (result.files || []).filter(f => f.error).length,
    });

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "extract-attachments",
          batch_id: batchId,
          result,
        }),
      });
    } catch (_) {}
  });

  // ---- retry-last (deeppeek prompt retry) ----
  es.addEventListener("retry-last", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(`DSX: retry-last received (batch=${batchId})`);

    let result = { success: false,
                   reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url &&
          tab.url.startsWith("https://chat.deepseek.com/")) {
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

    console.log(`DSX: retry-last result:`, {
      ok: result.success,
      message_id: result.message_id,
      files_attached: result.files_attached,
      text_length: result.text_length,
    });

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "retry-last",
          batch_id: batchId,
          result,
        }),
      });
    } catch (_) {}
  });

  // ==================================================================
  // deeppeek daemon handlers — extract / submit / watch
  // ==================================================================

  // ---- extract (deeppeek daemon) ----
  //
  // Request-response extraction: unlike `arm`, this fires exactly
  // once per event and acks with the payload.  Used by the daemon's
  // fallback poller; the primary path is `watch` below.
  es.addEventListener("extract", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
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
        if (resp) {
          result = { success: true, payload: resp };
        } else {
          result = { success: false, reason: "no response from content script" };
        }
      }
    } catch (err) {
      result = { success: false, reason: `sendMessage threw: ${err.message}` };
    }

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "extract",
          batch_id: batchId,
          result,
        }),
      });
    } catch (_) {}
  });

  // ---- submit (deeppeek daemon, atomic clear+type+enter) ----
  //
  // Combines three existing events into one round-trip.  The three
  // sub-results are returned in the ack so the bridge can see which
  // step failed if the overall success is false.
  es.addEventListener("submit", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const text = data.text || "";

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const clear = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_CLEAR", opts: {},
        });
        const type = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_TYPE", text, opts: { skip_verify: true },
        });
        const enter = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_ENTER", opts: { verify_ms: 300 },
        });
        result = {
          success: !!(clear && clear.success
                      && type && type.success
                      && enter && enter.success),
          clear, type, enter,
        };
      }
    } catch (err) {
      result = { success: false, reason: `submit threw: ${err.message}` };
    }

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "submit",
          batch_id: batchId,
          result,
        }),
      });
    } catch (_) {}
  });

  // ---- watch (deeppeek daemon, streaming) ----
  //
  // Keeps a poll loop inside the extension (direct IDB access, no
  // per-poll HTTP round-trip to the daemon).  Emits one `watch-delta`
  // per content growth, a `watch-rewrite` when the message is
  // replaced wholesale (rare), and `watch-done` on stability or
  // timeout.
  //
  // The `polling` guard prevents overlapping polls when a poll takes
  // longer than `pollInterval` (extract is fast, but let's be safe).
  // `maxDuration` is a safety net: a watch that never sees content
  // never settles, so it needs an absolute ceiling.
  es.addEventListener("watch", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const includeForked = !!data.include_forked;
    const pollInterval = Math.max(100, data.poll_interval_ms || 300);
    const settleTicks = Math.max(1, data.settle_ticks || 5);
    const maxDuration = Math.max(5000, data.max_duration_ms || 180000);

    let lastContent = "";
    let stableTicks = 0;
    let stopped = false;
    let polling = false;
    let timer = null;
    const startedAt = Date.now();

    const stop = async () => {
      if (stopped) return;
      stopped = true;
      if (timer) { clearInterval(timer); timer = null; }
      try {
        await fetch(`${DSX_URL}/export`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ __ack: "watch-done", batch_id: batchId }),
        });
      } catch (_) {}
    };

    const poll = async () => {
      if (stopped || polling) return;
      polling = true;
      try {
        if (Date.now() - startedAt > maxDuration) {
          await stop();
          return;
        }
        const [tab] = await browser.tabs.query({
          active: true, currentWindow: true,
        });
        if (!tab || !tab.url || !tab.url.startsWith("https://chat.deepseek.com/")) {
          await stop();
          return;
        }
        let resp;
        try {
          resp = await browser.tabs.sendMessage(tab.id, {
            type: "DSX_EXTRACT",
            include_forked: includeForked,
          });
        } catch (_) {
          // Tab may still be loading (or no session ID yet on a
          // fresh tab).  Not fatal; keep polling until maxDuration.
          stableTicks = 0;
          return;
        }
        if (!resp) { stableTicks = 0; return; }

        let content = "";
        const msgs = resp.messages || [];
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "assistant") {
            content = msgs[i].content || "";
            break;
          }
        }

        if (content === lastContent) {
          if (lastContent) stableTicks++;
        } else if (content.startsWith(lastContent)) {
          const delta = content.slice(lastContent.length);
          lastContent = content;
          stableTicks = 0;
          if (delta) {
            await fetch(`${DSX_URL}/export`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                __ack: "watch-delta",
                batch_id: batchId,
                delta,
              }),
            });
          }
        } else {
          // Rewrite — message changed non-extending.  Emit the
          // full content as a rewrite marker; the Python side logs
          // it and passes it through.
          lastContent = content;
          stableTicks = 0;
          await fetch(`${DSX_URL}/export`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              __ack: "watch-rewrite",
              batch_id: batchId,
              content,
            }),
          });
        }

        if (lastContent && stableTicks >= settleTicks) {
          await stop();
        }
      } catch (err) {
        console.error("DSX: watch poll error:", err);
        await stop();
      } finally {
        polling = false;
      }
    };

    try {
      await fetch(`${DSX_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "watch",
          batch_id: batchId,
          result: { success: true },
        }),
      });
    } catch (_) {
      return;
    }

    timer = setInterval(poll, pollInterval);
    poll();
  });

  es.onopen = () => {
    console.log("DSX: SSE connected");
    wasConnected = true;
  };

  es.onerror = () => {
    if (wasConnected) {
      console.log("DSX: SSE disconnected (will retry)");
      wasConnected = false;
    }
  };
}

function ensureConnected() {
  if (eventSource && eventSource.readyState === EventSource.OPEN) return;
  if (eventSource) {
    try { eventSource.close(); } catch (_) {}
  }
  try {
    eventSource = new EventSource(`${DSX_URL}/events`);
    attachHandlers(eventSource);
  } catch (e) {
    console.log("DSX: EventSource construction failed:", e);
  }
}

// ---- content-script port ----------------------------------------

browser.runtime.onConnect.addListener((port) => {
  if (port.name !== "deeppeek-keepalive") return;
  activePorts.add(port);
  port.onMessage.addListener((msg) => {
    ensureConnected();
    if (msg && msg.type === "hello") maybeFire();
  });
  port.onDisconnect.addListener(() => { activePorts.delete(port); });
});

// ---- watchdog ----------------------------------------------------

async function isOnDeepSeekTab() {
  try {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    return !!(tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/"));
  } catch (_) { return false; }
}

function startWatchdog() {
  if (!watchdogEnabled) { stopWatchdog(); return; }
  if (watchdogTimer) clearInterval(watchdogTimer);
  watchdogTimer = setInterval(async () => {
    if (eventSource && eventSource.readyState === EventSource.OPEN) return;
    if (!(await isOnDeepSeekTab())) return;
    ensureConnected();
  }, 500);
}

function stopWatchdog() {
  if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
}

// ---- extract + fire ----------------------------------------------

async function postToCli(path, payload) {
  try {
    const res = await fetch(`${DSX_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return res.ok;
  } catch (e) { return false; }
}

async function getActiveDeepSeekTab() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.startsWith("https://chat.deepseek.com/")) {
    return null;
  }
  return tab;
}

async function handleExtract() {
  const tab = await getActiveDeepSeekTab();
  if (!tab) {
    console.warn("DSX: active tab is not chat.deepseek.com");
    return false;
  }
  let payload;
  try {
    payload = await browser.tabs.sendMessage(tab.id, {
      type: "DSX_EXTRACT",
      include_forked: includeForked,
    });
  } catch (e) {
    console.error("DSX: content script is not loaded in this tab. Reload the tab (F5) and try again.");
    return false;
  }
  if (!payload) {
    console.error("DSX: extraction returned no data.");
    return false;
  }
  const ok = await postToCli("/export", payload);
  if (!ok) {
    console.warn(`DSX: CLI is not listening on ${DSX_URL}.`);
    return false;
  }
  return true;
}

function clearFiringAfter(ms) {
  setTimeout(() => {
    if (firing) { console.log("DSX: firing timeout — resetting"); firing = false; }
  }, ms);
}

async function fireOnce() {
  if (firing) return;
  firing = true;
  clearFiringAfter(15000);
  try {
    const ok = await handleExtract();
    if (!ok) firing = false;
  } catch (e) {
    console.error("DSX: fireOnce threw:", e);
    firing = false;
  }
}

async function maybeFire() {
  if (!armed) return;
  const tab = await getActiveDeepSeekTab();
  if (!tab) {
    console.log("DSX: armed, but active tab isn't deepseek");
    return;
  }
  console.log("DSX: firing extract (armed)");
  await fireOnce();
}

// ---- tab listeners -----------------------------------------------

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" &&
      tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
    ensureConnected();
    maybeFire();
  }
});

browser.tabs.onActivated.addListener(async (activeInfo) => {
  try {
    const tab = await browser.tabs.get(activeInfo.tabId);
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      ensureConnected();
      maybeFire();
    }
  } catch (_) {}
});

// ---- toolbar button ----------------------------------------------

browser.action.onClicked.addListener(() => {
  console.log("DSX: toolbar click");
  fireOnce();
});

// ---- startup -----------------------------------------------------

ensureConnected();
