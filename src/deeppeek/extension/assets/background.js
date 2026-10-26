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
