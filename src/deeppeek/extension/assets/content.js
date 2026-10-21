// Runs in the Firefox content-script world. Injects the page-world extractor and
// bridges messages between it and the background script.

(async function () {
  function injectPageWorldScript() {
    const url = browser.runtime.getURL("injected.js");
    const s = document.createElement("script");
    s.src = url;
    s.async = false;
    s.onload = () => s.remove();
    (document.head || document.documentElement).appendChild(s);
  }

  if (!window.__dsx_content_installed__) {
    window.__dsx_content_installed__ = true;
    injectPageWorldScript();
  }

  async function waitFor(condition, timeoutMs = 5000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (condition()) return true;
      await new Promise((r) => setTimeout(r, 50));
    }
    return false;
  }

  browser.runtime.onMessage.addListener(async (msg) => {
    if (!msg || msg.type !== "DSX_EXTRACT") {
      return null;
    }

    const ready = await waitFor(
      () =>
        window.wrappedJSObject &&
        typeof window.wrappedJSObject.__dsx_extract === "function"
    );
    if (!ready) {
      // Try one more injection in case the page was reloaded underneath us.
      injectPageWorldScript();
      const retried = await waitFor(
        () =>
          window.wrappedJSObject &&
          typeof window.wrappedJSObject.__dsx_extract === "function",
        3000
      );
      if (!retried) {
        console.error("DSX: page-world extractor unavailable.");
        return null;
      }
    }

    try {
      // The page-world function returns a JSON string so no Xray object crosses worlds.
      const jsonString = await window.wrappedJSObject.__dsx_extract();
      return JSON.parse(jsonString);
    } catch (e) {
      console.error("DSX: extraction threw:", e);
      return null;
    }
  });
})();
