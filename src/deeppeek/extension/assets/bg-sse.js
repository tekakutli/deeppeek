// SSE connection management.
//
// Added 2026-11-15: wait-for-reply event listener (fast reply
// detection for split_send).
//
// Added 2026-11-24: get-active-tab event listener (read-only query
// backing the CLI's `deeppeek daemon --which-tab` flag).  Handler
// lives in bg-bind.js alongside the other tab helpers.
function attachHandlers(es, port) {
  const tag = `[${port}]`;

  // ---- bind events ----
  es.addEventListener("bind-tab", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleBindTab(data, port);
  });
  es.addEventListener("bind-composer", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleBindComposer(data, port);
  });
  es.addEventListener("probe-composer", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleProbeComposer(data, port);
  });

  // ---- control events ----
  es.addEventListener("arm", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleArm(data, port);
  });
  es.addEventListener("disarm", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleDisarm(data, port);
  });
  es.addEventListener("config", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleConfig(data, port);
  });
  es.addEventListener("request-status", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleRequestStatus(data, port);
  });
  es.addEventListener("open-tab", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleOpenTab(data, port);
  });
  es.addEventListener("get-active-tab", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleGetActiveTab(data, port);
  });

  // ---- composer events ----
  es.addEventListener("attach", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleAttach(data, port);
  });
  es.addEventListener("type", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleType(data, port);
  });
  es.addEventListener("enter", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleEnter(data, port);
  });
  es.addEventListener("clear", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleClear(data, port);
  });
  es.addEventListener("submit", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleSubmit(data, port);
  });

  // ---- read-side events ----
  es.addEventListener("extract", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleExtract(data, port);
  });
  es.addEventListener("wait-for-reply", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleWaitForReply(data, port);
  });
  es.addEventListener("extract-attachments", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleExtractAttachments(data, port);
  });
  es.addEventListener("retry-last", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleRetryLast(data, port);
  });
  es.addEventListener("watch", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleWatch(data, port);
  });
  es.addEventListener("stop-watch", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleStopWatch(data, port);
  });

  // ---- hijack events ----
  es.addEventListener("hijack-install", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleHijackInstall(data, port);
  });
  es.addEventListener("hijack-uninstall", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleHijackUninstall(data, port);
  });
  es.addEventListener("hijack-all", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleHijackAll(data, port);
  });

  // ---- hijack-turn events ----
  es.addEventListener("hijack-turn-install", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleHijackTurnInstall(data, port);
  });
  es.addEventListener("hijack-turn-uninstall", (e) => {
    let data = {};
    try { data = JSON.parse(e.data || "{}"); } catch (_) {}
    handleHijackTurnUninstall(data, port);
  });

  // ---- lifecycle ----
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
      s.failures = 0;
    } else {
      s.failures = (s.failures || 0) + 1;
    }
    if (s.failures >= 2) {
      s.retryAfter = Date.now() + 500;
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

browser.runtime.onConnect.addListener((port) => {
  if (port.name !== "deeppeek-keepalive") return;
  activePorts.add(port);
  port.onMessage.addListener((msg) => {
    ensureAllConnected();
    if (msg && msg.type === "hello") maybeFire();
  });
  port.onDisconnect.addListener(() => { activePorts.delete(port); });
});