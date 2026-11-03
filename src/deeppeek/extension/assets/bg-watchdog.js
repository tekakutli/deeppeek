// Watchdog: polls the active tab every 500ms while enabled and
// reconnects any dead SSE if the active tab is a DeepSeek chat.
// The setting is persisted in browser.storage.local.watchdog and
// mirrored by bg-handlers-control.js's config handler.
//
// The "fast path" (all ports already connected) is a read-only
// check that does no work — the watchdog costs nothing when
// everything is healthy.

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