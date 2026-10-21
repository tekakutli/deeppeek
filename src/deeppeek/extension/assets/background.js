const DSX_URL = "http://127.0.0.1:8765";

async function postToCli(path, payload) {
  try {
    const res = await fetch(`${DSX_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return res.ok;
  } catch (e) {
    return false;
  }
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
    return;
  }

  let payload;
  try {
    payload = await browser.tabs.sendMessage(tab.id, { type: "DSX_EXTRACT" });
  } catch (e) {
    console.error(
      "DSX: content script is not loaded in this tab. Reload the tab (F5) and try again."
    );
    return;
  }

  if (!payload) {
    console.error("DSX: extraction returned no data.");
    return;
  }

  const ok = await postToCli("/export", payload);
  if (!ok) {
    console.warn(`DSX: CLI is not listening on ${DSX_URL}. Start \`dsx export\` first.`);
  }
}

browser.action.onClicked.addListener(() => {
  handleExtract();
});

// Low-frequency poll so `dsx export --watch` can drive the export without a click.
setInterval(async () => {
  try {
    const res = await fetch(`${DSX_URL}/poll`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.action === "extract") {
      await handleExtract();
    }
  } catch (_) {
    // CLI not running — expected most of the time.
  }
}, 3000);
