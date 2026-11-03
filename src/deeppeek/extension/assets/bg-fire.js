// Fire-on-arm: when a port is armed and the active tab is a
// DeepSeek chat, extract once and POST the payload to that port's
// /export. `firing` guards against overlapping fires — the
// extension's arm/disarm state is per-port, but only one fire can
// be in flight at a time across all ports (that's the pre-existing
// contract).

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
    console.error(
      "DSX: content script is not loaded in this tab. " +
      "Reload the tab (F5) and try again.");
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