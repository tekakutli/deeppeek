// Background script.  SSE client for multiple ports + tab listeners
// + content-script port.  Watchdog state persisted in
// browser.storage.local.watchdog.
//
// MULTI-PORT (2026-11-01)
// =======================
// The extension dials every port in DSX_PORTS:
//
//   8765 — CLI (`type`/`enter`/`pipe`/`capture`/`hijack`/watchdog)
//   8767 — daemon (aider via OpenAI-compat facade on 8766)
//
// Each EventSource gets its own copy of attachHandlers, closed over
// its `port`.  Every ack POSTs to `${baseUrl(port)}/export` — the
// port that delivered the triggering event — never a global URL.
// `watch` events forward `port` into the content script (and thence
// to injected-watch.js) so the page-world watcher posts its deltas
// back to the correct server.
//
// `arm` and `include_forked` are tracked per-port: only the CLI arms
// (daemon never sends `arm`), but keeping them keyed by port means a
// config event from the daemon can't clobber the CLI's fork setting.
//
// Daemon-port backoff: if 8767 isn't listening, EventSource will
// fail repeatedly and spam the console.  onerror schedules an
// exponential backoff (`retryAfter`) that ensureConnected respects,
// so a steady state of "daemon not running" costs one probe per ~16s
// rather than 2/sec.

const DSX_PORTS = [8765, 8767];

// Per-port connection state.
//   connections.get(port) = {
//     es,               // EventSource | undefined
//     wasConnected,     // bool — for one-shot log
//     failures,         // int — consecutive connect failures
//     retryAfter,       // ms-since-epoch; watchdog skips until then
//   }
const connections = new Map();

// Per-port state (only the CLI actually arms, but keep it keyed).
const armedByPort = new Set();
const includeForkedByPort = new Map();

// Global "only one fire in flight at a time" guard.
let firing = false;

// Watchdog.
let watchdogTimer = null;
let watchdogEnabled = true;

// Content-script keepalive ports.
const activePorts = new Set();

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

// ---- SSE plumbing: per-connection handlers ------------------------

function baseUrl(port) {
  return `http://127.0.0.1:${port}`;
}

function attachHandlers(es, port) {
  const base = baseUrl(port);
  const tag = `[${port}]`;

  // Post an ack to THIS connection's port.  Swallows errors so a
  // dead server doesn't unhandled-reject.
  const postAck = async (payload) => {
    try {
      await fetch(`${base}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (_) {}
  };

  es.addEventListener("arm", () => {
    console.log(`DSX:${tag} arm received`);
    armedByPort.add(port);
    firing = false;
    maybeFire(port);
  });

  es.addEventListener("disarm", () => {
    console.log(`DSX:${tag} disarm received`);
    armedByPort.delete(port);
    firing = false;
  });

  es.addEventListener("config", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}

    if ("include_forked" in data) {
      includeForkedByPort.set(port, !!data.include_forked);
      console.log(
        `DSX:${tag} config include_forked=${!!data.include_forked}`);
    }

    if (!("watchdog" in data)) return;
    const enabled = data.watchdog !== false;
    browser.storage.local.set({ watchdog: enabled }).then(() => {
      postAck({ __ack: "watchdog", watchdog: enabled });
    });
  });

  es.addEventListener("request-status", () => {
    browser.storage.local.get("watchdog").then(({ watchdog }) => {
      const enabled = watchdog !== false;
      postAck({ __status: "watchdog", watchdog: enabled });
    });
  });

  // ---- attach (deeppeek pipe) ----
  es.addEventListener("attach", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const files = data.files || [];
    const opts = data.opts || {};

    console.log(
      `DSX:${tag} attach received (batch=${batchId}, ` +
      `${files.length} file(s))`);

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

    console.log(`DSX:${tag} attach result (batch=${batchId}):`, result);
    await postAck({ __ack: "attach", batch_id: batchId, result });
  });

  // ---- type (deeppeek type) ----
  es.addEventListener("type", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const text = data.text || "";
    const opts = data.opts || {};

    console.log(
      `DSX:${tag} type received (batch=${batchId}, ` +
      `${text.length} chars)`);

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

    console.log(`DSX:${tag} type result (batch=${batchId}):`, result);
    await postAck({ __ack: "type", batch_id: batchId, result });
  });

  // ---- enter (deeppeek enter) ----
  es.addEventListener("enter", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(`DSX:${tag} enter received (batch=${batchId})`);

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

    console.log(`DSX:${tag} enter result (batch=${batchId}):`, result);
    await postAck({ __ack: "enter", batch_id: batchId, result });
  });

  // ---- open-tab (deeppeek prompt new / daemon) ----
  es.addEventListener("open-tab", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const url = data.url || "https://chat.deepseek.com/";
    const containerName = data.container || null;
    const containerId = data.container_id || null;

    console.log(
      `DSX:${tag} open-tab received (batch=${batchId}, url=${url}, ` +
      `container=${containerName || containerId || "default"})`);

    let result;
    try {
      let cookieStoreId = null;
      if (containerId) {
        cookieStoreId = containerId;
      } else if (containerName) {
        if (!browser.contextualIdentities) {
          result = {
            success: false,
            reason:
              "container tabs unavailable in this browser " +
              "(no contextualIdentities API)",
          };
        } else {
          const all = await browser.contextualIdentities.query({});
          const exact = all.filter(c => c.name === containerName);
          const needle = containerName.toLowerCase();
          const ci = all.filter(
            c => c.name.toLowerCase() === needle);

          let matched = null;
          if (exact.length === 1) {
            matched = exact[0];
          } else if (ci.length === 1) {
            matched = ci[0];
          } else if (ci.length > 1 || exact.length > 1) {
            const names = (exact.length ? exact : ci)
              .map(c => JSON.stringify(c.name)).join(", ");
            result = {
              success: false,
              reason:
                `multiple containers match ` +
                `${JSON.stringify(containerName)} (${names}); ` +
                `use --container-id to disambiguate`,
            };
          } else {
            const available = all
              .map(c => JSON.stringify(c.name)).join(", ");
            result = {
              success: false,
              reason:
                `no container named ` +
                `${JSON.stringify(containerName)}` +
                (available
                  ? ` (available: ${available})`
                  : ` (no containers defined in this profile)`),
            };
          }
          if (matched) {
            cookieStoreId = matched.cookieStoreId;
            console.log(
              `DSX:${tag} resolved container ` +
              `${JSON.stringify(containerName)} -> ${cookieStoreId}`);
          }
        }
      }

      if (!result) {
        const createProps = { url, active: true };
        if (cookieStoreId) createProps.cookieStoreId = cookieStoreId;
        const tab = await browser.tabs.create(createProps);
        result = {
          success: true,
          tab_id: tab.id,
          url: tab.url || url,
        };
        if (cookieStoreId) result.cookie_store_id = cookieStoreId;
      }
    } catch (err) {
      if (!result) {
        result = {
          success: false,
          reason: `open-tab threw: ${err.message || err}`,
        };
      }
    }

    console.log(`DSX:${tag} open-tab result (batch=${batchId}):`, result);
    await postAck({ __ack: "open-tab", batch_id: batchId, result });
  });

  // ---- hijack-install / hijack-uninstall ----
  es.addEventListener("hijack-install", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    console.log(`DSX:${tag} hijack-install received (batch=${batchId})`);
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
    console.log(`DSX:${tag} hijack-install result:`, result);
    await postAck({ __ack: "hijack-install", batch_id: batchId, result });
  });

  es.addEventListener("hijack-uninstall", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    console.log(`DSX:${tag} hijack-uninstall received (batch=${batchId})`);
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
    console.log(`DSX:${tag} hijack-uninstall result:`, result);
    await postAck({ __ack: "hijack-uninstall", batch_id: batchId, result });
  });

  // ---- hijack-all (deeppeek hijack --all) ----
  es.addEventListener("hijack-all", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const cliPort = data.port;
    console.log(
      `DSX:${tag} hijack-all received ` +
      `(batch=${batchId}, port=${cliPort})`);

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      const [tab] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_HIJACK_ALL",
          opts: { port: cliPort },
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

    console.log(`DSX:${tag} hijack-all result:`, result);
    await postAck({
      __ack: "hijack-all",
      batch_id: batchId,
      result,
    });
  });

  // ---- clear (deeppeek prompt delete) ----
  es.addEventListener("clear", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(`DSX:${tag} clear received (batch=${batchId})`);

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

    console.log(`DSX:${tag} clear result (batch=${batchId}):`, result);
    await postAck({ __ack: "clear", batch_id: batchId, result });
  });

  // ---- extract-attachments (deeppeek prompt attachments) ----
  es.addEventListener("extract-attachments", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(
      `DSX:${tag} extract-attachments received (batch=${batchId}, ` +
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

    console.log(`DSX:${tag} extract-attachments result:`, {
      ok: result.ok,
      n_files: (result.files || []).length,
      n_errors: (result.files || []).filter(f => f.error).length,
    });

    await postAck({
      __ack: "extract-attachments",
      batch_id: batchId,
      result,
    });
  });

  // ---- retry-last (deeppeek prompt retry) ----
  es.addEventListener("retry-last", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const opts = data.opts || {};

    console.log(`DSX:${tag} retry-last received (batch=${batchId})`);

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

    console.log(`DSX:${tag} retry-last result:`, {
      ok: result.success,
      message_id: result.message_id,
      files_attached: result.files_attached,
      text_length: result.text_length,
    });

    await postAck({
      __ack: "retry-last",
      batch_id: batchId,
      result,
    });
  });

  // ==================================================================
  // deeppeek daemon handlers — extract / submit / watch
  // ==================================================================

  // ---- extract (request-response, single-shot) ----
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

    await postAck({ __ack: "extract", batch_id: batchId, result });
  });

  // ---- submit (atomic clear+type+enter) ----
  es.addEventListener("submit", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const text = data.text || "";

    let result = { success: false, reason: "no active deepseek tab" };
    try {
      let tab = null;
      if (data.tab_id != null) {
        try { tab = await browser.tabs.get(data.tab_id); } catch (_) { tab = null; }
      }
      if (!tab) {
        const [active] = await browser.tabs.query({
          active: true, currentWindow: true,
        });
        tab = active;
      }

      if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
        const clear = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_CLEAR", opts: {},
        });
        const type = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_TYPE", text, opts: { skip_verify: true },
        });

        // Enter is fire-and-forget.  The watch is the source of
        // truth for "did content appear".
        let enter = null;
        let enterOk = false;
        try {
          enter = await browser.tabs.sendMessage(tab.id, {
            type: "DSX_ENTER", opts: { verify_ms: 1500 },
          });
          enterOk = !!(enter && enter.submitted !== false);
        } catch (err) {
          enter = {
            success: false, submitted: false,
            reason: `sendMessage threw: ${err.message}`,
          };
          enterOk = false;
        }

        const failures = [];
        if (!(clear && clear.success)) {
          failures.push(`clear: ${(clear && clear.reason) || "failed"}`);
        }
        if (!(type && type.success)) {
          failures.push(`type: ${(type && type.reason) || "failed"}`);
        }
        if (!enterOk) {
          failures.push(`enter: ${(enter && enter.reason) || "not dispatched"}`);
        }

        result = {
          success: failures.length === 0,
          reason: failures.length ? failures.join("; ") : undefined,
          tab_id: tab.id,
          clear, type, enter,
        };
      }
    } catch (err) {
      result = { success: false, reason: `submit threw: ${err.message}` };
    }

    await postAck({ __ack: "submit", batch_id: batchId, result });
  });

  // ---- watch (streaming) ----
  //
  // Primary path: hand off to the page-world MutationObserver,
  // passing `port` so watch-deltas POST to the same server that
  // sent the `watch` event.  Fallback path: legacy polling in this
  // file, also using postAck (bound to this connection's port).
  es.addEventListener("watch", async (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    const batchId = data.batch_id;
    const pollInterval = Math.max(100, data.poll_interval_ms || 300);
    const settleTicks = Math.max(1, data.settle_ticks || 5);
    const maxDuration = Math.max(5000, data.max_duration_ms || 180000);
    const firstContentTimeoutMs =
      Math.max(5000, data.first_content_timeout_ms || 30000);
    const pinnedTabId = (data.tab_id != null) ? data.tab_id : null;

    // Ack first.
    await postAck({
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
      await postAck({
        __ack: "watch-done",
        batch_id: batchId,
        reason: "no_deepseek_tab",
      });
      return;
    }

    // Try event-driven path, passing `port` down so the page-world
    // watcher acks back to the correct server.
    let started = false;
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_WATCH_START",
        opts: {
          batch_id: batchId,
          port: port,   // <-- forwarded to injected-watch.js
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

    // ---- legacy polling fallback (all acks via postAck) ----
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
      await postAck({
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
          await postAck({
            __ack: "watch-delta", batch_id: batchId, delta,
          });
        }
      } else {
        lastContent = content; stableTicks = 0;
        await postAck({
          __ack: "watch-rewrite", batch_id: batchId, content,
        });
      }
      if (lastContent && stableTicks >= settleTicks) {
        await pollStop("settled");
      }
    };
    timer = setInterval(poll, pollInterval);
    poll();
  });

  es.onopen = () => {
    console.log(`DSX:${tag} SSE connected`);
    const s = connections.get(port);
    if (s) {
      s.wasConnected = true;
      s.failures = 0;
      s.retryAfter = 0;
    }
  };

  es.onerror = () => {
    const s = connections.get(port);
    if (!s) return;
    if (s.wasConnected) {
      console.log(`DSX:${tag} SSE disconnected (will retry)`);
      s.wasConnected = false;
      s.failures = 0;      // reset: prior success means transient
    } else {
      s.failures = (s.failures || 0) + 1;
    }
    // After 2 consecutive failures with no intervening success,
    // schedule exponential backoff so a not-listening port doesn't
    // spam the console.  Cap at 16s.
    if (s.failures >= 2) {
      const backoff = Math.min(
        16000, 500 * Math.pow(2, s.failures - 2));
      s.retryAfter = Date.now() + backoff;
    }
  };
}

function ensureConnected(port) {
  const state = connections.get(port);
  if (state && state.es && state.es.readyState === EventSource.OPEN) return;
  if (state && state.retryAfter && Date.now() < state.retryAfter) return;
  if (state && state.es) {
    try { state.es.close(); } catch (_) {}
  }
  try {
    const es = new EventSource(`${baseUrl(port)}/events`);
    connections.set(port, {
      es,
      wasConnected: state ? state.wasConnected : false,
      failures: state ? (state.failures || 0) : 0,
      retryAfter: 0,
    });
    attachHandlers(es, port);
  } catch (e) {
    console.log(`DSX:${port} EventSource construction failed:`, e);
  }
}

function ensureAllConnected() {
  for (const port of DSX_PORTS) ensureConnected(port);
}

// ---- content-script port ----------------------------------------

browser.runtime.onConnect.addListener((port) => {
  if (port.name !== "deeppeek-keepalive") return;
  activePorts.add(port);
  port.onMessage.addListener((msg) => {
    ensureAllConnected();
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
    // Fast path: everything is up.
    const disconnected = DSX_PORTS.filter((p) => {
      const s = connections.get(p);
      return !s || !s.es || s.es.readyState !== EventSource.OPEN;
    });
    if (disconnected.length === 0) return;

    if (!(await isOnDeepSeekTab())) return;

    for (const port of disconnected) ensureConnected(port);
  }, 500);
}

function stopWatchdog() {
  if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
}

// ---- extract + fire ----------------------------------------------

async function getActiveDeepSeekTab() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.startsWith("https://chat.deepseek.com/")) {
    return null;
  }
  return tab;
}

async function handleExtract(port) {
  const tab = await getActiveDeepSeekTab();
  if (!tab) {
    console.warn("DSX: active tab is not chat.deepseek.com");
    return false;
  }
  const includeForked = includeForkedByPort.get(port) || false;
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
  try {
    const res = await fetch(`${baseUrl(port)}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      console.warn(`DSX:${port} CLI returned non-OK from /export.`);
      return false;
    }
    return true;
  } catch (e) {
    console.warn(`DSX:${port} CLI is not listening on ${baseUrl(port)}.`);
    return false;
  }
}

function clearFiringAfter(ms) {
  setTimeout(() => {
    if (firing) { console.log("DSX: firing timeout — resetting"); firing = false; }
  }, ms);
}

async function fireOnce(port) {
  if (firing) return;
  firing = true;
  clearFiringAfter(15000);
  try {
    const ok = await handleExtract(port);
    if (!ok) firing = false;
  } catch (e) {
    console.error("DSX: fireOnce threw:", e);
    firing = false;
  }
}

async function maybeFire(port = null) {
  const portsToTry = port !== null ? [port] : Array.from(armedByPort);
  for (const p of portsToTry) {
    if (!armedByPort.has(p)) continue;
    const tab = await getActiveDeepSeekTab();
    if (!tab) {
      console.log(`DSX: armed on ${p}, but active tab isn't deepseek`);
      continue;
    }
    console.log(`DSX: firing extract (armed on ${p})`);
    await fireOnce(p);
  }
}

// ---- tab listeners -----------------------------------------------

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" &&
      tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
    ensureAllConnected();
    maybeFire();
  }
});

browser.tabs.onActivated.addListener(async (activeInfo) => {
  try {
    const tab = await browser.tabs.get(activeInfo.tabId);
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      ensureAllConnected();
      maybeFire();
    }
  } catch (_) {}
});

// ---- toolbar button ----------------------------------------------

browser.action.onClicked.addListener(async () => {
  console.log("DSX: toolbar click");
  // Prefer 8765 (CLI) — that's what the toolbar button
  // traditionally targets.  Fall through to 8767 if the CLI isn't
  // listening.
  for (const port of [8765, 8767]) {
    const s = connections.get(port);
    if (s && s.es && s.es.readyState === EventSource.OPEN) {
      await fireOnce(port);
      return;
    }
  }
  console.log("DSX: toolbar click but no CLI/daemon connected");
});

// ---- startup -----------------------------------------------------

ensureAllConnected();
