// Tab listeners and toolbar button.
//
// - onUpdated: re-connect the extension's SSEs and re-fire if the
//   active tab navigated to a DeepSeek chat (covers reload, SPA
//   navigation, and "click a bookmark" flows).
// - onActivated: same, but the tab change may have landed on a
//   DeepSeek chat while another arm is pending.
// - action.onClicked: the toolbar button. Prefers 8765 (CLI), falls
//   through to 8767 (daemon) if only the daemon is listening.

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

browser.action.onClicked.addListener(async () => {
  console.log("DSX: toolbar click");
  for (const port of [8765, 8767]) {
    const s = connections.get(port);
    if (s && s.es && s.es.readyState === EventSource.OPEN) {
      await fireOnce(port);
      return;
    }
  }
  console.log("DSX: toolbar click but no CLI/daemon connected");
});