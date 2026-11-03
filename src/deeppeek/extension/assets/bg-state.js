// Shared state and helpers for all bg-*.js modules.
//
// Classic background scripts in Firefox MV3 share the global lexical
// environment, so top-level `let`/`const` bindings from this file are
// visible and reassignable from every sibling bg-*.js. This file MUST
// be first in manifest.json's `background.scripts` array so the state
// exists before any other file's top-level code runs.

const DSX_PORTS = [8765, 8767];

// Per-port SSE connection state: connections.get(port) = {
//   es,          // EventSource | undefined
//   wasConnected,// bool — for one-shot log
//   failures,    // int — consecutive connect failures
//   retryAfter,  // ms-since-epoch; watchdog skips until then
// }
const connections = new Map();

// Per-port state (only the CLI actually arms, but keep it keyed).
const armedByPort = new Set();
const includeForkedByPort = new Map();

// Global "only one fire in flight at a time" guard.
let firing = false;

// Watchdog state (persisted in browser.storage.local.watchdog).
let watchdogTimer = null;
let watchdogEnabled = true;

// Content-script keepalive ports.
const activePorts = new Set();

function baseUrl(port) {
  return `http://127.0.0.1:${port}`;
}

// Post an ack to a specific port. Same operation as the old
// closure-per-connection `postAck`; parameterized so event handlers
// can live in their own files without needing access to `attachHandlers`.
async function postAck(port, payload) {
  try {
    await fetch(`${baseUrl(port)}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (_) {}
}