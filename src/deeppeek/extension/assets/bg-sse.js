// SSE connection management.
//
// attachHandlers wires one EventSource up to the per-event
// dispatchers in bg-handlers-*.js. Each EventSource gets its own
// wiring, closed over `port`, so acks target the port that
// delivered the triggering event.
//
// Daemon-port backoff: if 8767 isn't listening, EventSource fails
// repeatedly and would spam the console. onerror schedules a short
// backoff that ensureConnected respects, so a steady state of
// "daemon not running" costs one probe per ~500ms worst-case
// (bounded by the watchdog cadence) rather than 2/sec.

function attachHandlers(es, port) {
  const tag = `[${port}]`;

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

// Content-script keepalive port. When the content script connects
// or sends a heartbeat, ensure all SSEs are up and let maybeFire
// re-evaluate pending arms (this is how the toolbar-click flow
// gets its extension connection back after a page reload).
browser.runtime.onConnect.addListener((port) => {
  if (port.name !== "deeppeek-keepalive") return;
  activePorts.add(port);
  port.onMessage.addListener((msg) => {
    ensureAllConnected();
    if (msg && msg.type === "hello") maybeFire();
  });
  port.onDisconnect.addListener(() => { activePorts.delete(port); });
});