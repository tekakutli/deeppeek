// Content-script → background message handlers, plus the container
// helpers shared with the SSE `open-tab` handler in bg-sse.js.
//
// The content script's postMessage bridge (see content.js's
// installPostMessageBridge) forwards page-world requests here via
// browser.runtime.sendMessage.  On the way in, Firefox populates
// `sender.tab` with the full tabs.Tab the content script is
// running in — that's how DSX_CURRENT_CONTAINER detects the
// current tab's container, because content scripts see a *reduced*
// tabs.Tab and `tab.cookieStoreId` is undefined there.
//
// Resume payload (2026-11-02): stored in an in-memory module-level
// variable, NOT browser.storage.local.  Two reasons:
//   · MV3 event pages in current Zen hang on storage writes (the
//     underlying write-to-parent-process call never resolves — the
//     same hang that breaks contextualIdentities.query).
//   · An in-memory variable is instant and lives in the single
//     background instance shared across all tabs and containers.
//
// Tab reuse (2026-11-23)
// ----------------------
// `createTabWithContainer` accepts a 5th `reuse` argument:
//
//   · undefined / "never"  (default)  — always create a new tab
//   · "if_exists"          — find a DeepSeek tab in the requested
//                            container; activate and reuse it if
//                            exactly one exists, else fall through
//                            to creating a new tab.  Multiple
//                            matches are an error (ambiguous
//                            target), so the caller can disambiguate
//                            with --container-id or by closing
//                            extras.
//
// Tab-ready wait (2026-11-23)
// ---------------------------
// `browser.tabs.create({url})` returns immediately with an
// `about:blank` tab; the page loads asynchronously.  Acking
// `open-tab` before navigation completes causes the daemon to
// log `about:blank` and forces a submit retry while the content
// script boots.  Both the create and reuse paths now wait for
// `tabs.onUpdated {status: "complete"}` with the target URL on
// `chat.deepseek.com` before acking.  Bounded by
// `TAB_LOAD_TIMEOUT_MS`; on timeout the caller receives whatever
// `tabs.get` reports and the daemon's submit retry covers the
// remainder.

// ---- tab load wait -----------------------------------------------

// Bound on waiting for a freshly-created tab to reach status
// "complete" on chat.deepseek.com.  15 s is well above the typical
// 1–3 s and leaves headroom for slow networks without making a
// genuinely-broken tab hang the daemon (TAB_OPEN_TIMEOUT is 30 s).
const TAB_LOAD_TIMEOUT_MS = 15000;
const DEEPSEEK_HOST = "chat.deepseek.com";

function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (tab) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { browser.tabs.onUpdated.removeListener(listener); } catch (_) {}
      resolve(tab);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    const listener = (changedId, changeInfo, tab) => {
      if (changedId !== tabId) return;
      if (changeInfo.status !== "complete") return;
      const url = (tab && tab.url) || "";
      if (!url.includes(DEEPSEEK_HOST)) {
        // The tab completed but on the wrong page (about:blank,
        // error page, redirect chain mid-flight).  Keep waiting
        // for the real navigation.
        return;
      }
      finish(tab);
    };
    browser.tabs.onUpdated.addListener(listener);
    // If the tab was already complete before we subscribed,
    // onUpdated won't fire.  Check current state immediately.
    browser.tabs.get(tabId).then((t) => {
      if (t && t.status === "complete"
          && typeof t.url === "string"
          && t.url.includes(DEEPSEEK_HOST)) {
        finish(t);
      }
    }).catch(() => {});
  });
}

// ---- container resolution -----------------------------------------

async function resolveContainerId(containerName, containerId) {
  if (containerId) return { ok: true, cookieStoreId: containerId };
  if (!containerName) return { ok: true, cookieStoreId: null };

  // Some Zen MV3 builds expose `contextualIdentities` as a callable
  // wrapper instead of the namespace; unwrap if wrapped.
  let ci = browser.contextualIdentities;
  if (typeof ci === "function") {
    try { ci = ci(); }
    catch (err) {
      return {
        ok: false,
        reason:
          `contextualIdentities wrapper threw: ${err.message || err}`,
      };
    }
  }
  if (!ci || typeof ci.query !== "function") {
    return {
      ok: false,
      reason: "contextualIdentities.query unavailable in this browser",
    };
  }

  let all;
  try {
    const timeout = new Promise((_, rej) =>
      setTimeout(() => rej(new Error("query timed out after 3s")), 3000));
    all = await Promise.race([ci.query({}), timeout]);
  } catch (err) {
    return {
      ok: false,
      reason:
        `contextualIdentities.query threw: ${err.message || err}`,
    };
  }

  const exact = all.filter(c => c.name === containerName);
  const needle = containerName.toLowerCase();
  const ciMatches = all.filter(c => c.name.toLowerCase() === needle);

  if (exact.length === 1) {
    return {
      ok: true,
      cookieStoreId: exact[0].cookieStoreId,
      name: exact[0].name,
    };
  }
  if (ciMatches.length === 1) {
    return {
      ok: true,
      cookieStoreId: ciMatches[0].cookieStoreId,
      name: ciMatches[0].name,
    };
  }
  if (ciMatches.length > 1 || exact.length > 1) {
    const names = (exact.length ? exact : ciMatches)
      .map(c => JSON.stringify(c.name)).join(", ");
    return {
      ok: false,
      reason:
        `multiple containers match ` +
        `${JSON.stringify(containerName)} (${names}); ` +
        `use a raw cookieStoreId to disambiguate`,
    };
  }
  const available = all.map(c => JSON.stringify(c.name)).join(", ");
  return {
    ok: false,
    reason:
      `no container named ${JSON.stringify(containerName)}` +
      (available
        ? ` (available: ${available})`
        : ` (no containers defined in this profile)`),
  };
}

// ---- tab reuse ----------------------------------------------------
//
// `findTabInContainer` queries all tabs at `url`'s origin and
// filters by container in JS.  We deliberately do NOT pass
// cookieStoreId to tabs.query: the value "firefox-default" is not
// accepted as a filter by every Firefox/Zen version, and a failed
// filter is indistinguishable from "no match".  Filtering in JS
// also lets us count matches for the ambiguous case.

function urlPatternFor(url) {
  try {
    const u = new URL(url);
    return `*://${u.host}/*`;
  } catch (_) {
    return "*://chat.deepseek.com/*";
  }
}

async function findTabInContainer(url, cookieStoreId) {
  const pattern = urlPatternFor(url);
  let matches;
  try {
    matches = await browser.tabs.query({ url: pattern });
  } catch (err) {
    return {
      ok: false,
      reason: `tabs.query(${JSON.stringify(pattern)}) threw: ` +
              `${err.message || err}`,
    };
  }
  const want = cookieStoreId || "firefox-default";
  const filtered = matches.filter(t => {
    const got = t.cookieStoreId || "firefox-default";
    return got === want;
  });
  return { ok: true, tabs: filtered };
}

async function createTabWithContainer(
    url, containerName, containerId, logTag, reuse) {
  try {
    const resolved = await resolveContainerId(containerName, containerId);
    if (!resolved.ok) {
      return { success: false, reason: resolved.reason };
    }
    if (resolved.cookieStoreId && containerName && !containerId && logTag) {
      console.log(
        `DSX:${logTag} resolved container ` +
        `${JSON.stringify(containerName)} -> ${resolved.cookieStoreId}`);
    }

    // ---- reuse path ---------------------------------------------
    if (reuse === "if_exists") {
      const found = await findTabInContainer(url, resolved.cookieStoreId);
      if (!found.ok) {
        return { success: false, reason: found.reason };
      }
      if (found.tabs.length === 1) {
        const t = found.tabs[0];
        if (logTag) {
          console.log(
            `DSX:${logTag} reusing existing tab ${t.id} in container ` +
            `${resolved.cookieStoreId || "(default)"}`);
        }
        // Bring the tab to the foreground.  The daemon's submit and
        // watch already pin to `tab_id`, so focus is a UX nicety
        // rather than a correctness requirement — but users who
        // pass --reuse-tab-in want to see the tab doing work, and
        // some SPA composers require focus for keystroke handling.
        // Window focus is a separate call and may fail (e.g. the
        // tab lives in a minimised window); that's fine — the tab
        // itself still receives tabs.sendMessage.
        try {
          await browser.tabs.update(t.id, { active: true });
        } catch (err) {
          if (logTag) {
            console.log(
              `DSX:${logTag} tabs.update(active) threw ` +
              `(ignored): ${err.message || err}`);
          }
        }
        if (typeof t.windowId === "number") {
          try {
            await browser.windows.update(t.windowId, { focused: true });
          } catch (_) { /* not fatal */ }
        }
        // Already loaded in practice; still bound with a short
        // wait so the final URL we report is the real one, not
        // whatever tabs.query happened to snapshot.
        const ready = await waitForTabComplete(t.id, 3000);
        const finalTab = ready
          || await browser.tabs.get(t.id).catch(() => t);
        const resp = {
          success: true,
          reused: true,
          tab_id: finalTab.id != null ? finalTab.id : t.id,
          url: finalTab.url || t.url || url,
        };
        if (resolved.cookieStoreId) {
          resp.cookie_store_id = resolved.cookieStoreId;
        }
        return resp;
      }
      if (found.tabs.length > 1) {
        // Ambiguous: two DeepSeek tabs in the same container.  The
        // daemon cannot safely pick one for the user — surface the
        // list so they can close extras or use --container-id /
        // --tab-id to pick explicitly.
        return {
          success: false,
          reason:
            `multiple DeepSeek tabs in container ` +
            `${JSON.stringify(resolved.cookieStoreId || "default")} ` +
            `(${found.tabs.length}); close extras or use ` +
            `--container-id / --tab-id to disambiguate`,
          tabs: found.tabs.map(t => ({
            tab_id: t.id,
            url: t.url,
            title: t.title,
            cookie_store_id: t.cookieStoreId,
          })),
        };
      }
      // 0 matches → fall through to create.
    }

    // ---- create path (default) ----------------------------------
    const createProps = { url, active: true };
    if (resolved.cookieStoreId) {
      createProps.cookieStoreId = resolved.cookieStoreId;
    }
    const tab = await browser.tabs.create(createProps);

    // `tabs.create` resolves with a tab whose `url` is typically
    // "about:blank" until navigation commits.  Wait for the page
    // to actually land on chat.deepseek.com so the daemon's ack
    // carries a real URL and submit fires against a live composer.
    const ready = await waitForTabComplete(tab.id, TAB_LOAD_TIMEOUT_MS);
    if (logTag) {
      if (ready) {
        console.log(
          `DSX:${logTag} tab ${tab.id} loaded: ${ready.url || url}`);
      } else {
        console.log(
          `DSX:${logTag} tab ${tab.id} did not report "complete" within ` +
          `${TAB_LOAD_TIMEOUT_MS}ms — submit may retry`);
      }
    }
    const finalTab = ready
      || await browser.tabs.get(tab.id).catch(() => tab);

    const resp = {
      success: true,
      tab_id: finalTab.id != null ? finalTab.id : tab.id,
      url: finalTab.url || tab.url || url,
    };
    if (resolved.cookieStoreId) {
      resp.cookie_store_id = resolved.cookieStoreId;
    }
    return resp;
  } catch (err) {
    return {
      success: false,
      reason: `tabs.create threw: ${err.message || err}`,
    };
  }
}

async function handleOpenTabMsg(msg) {
  return await createTabWithContainer(
    msg.url || "https://chat.deepseek.com/",
    msg.container || null,
    msg.container_id || null,
    "[msg]",
    msg.reuse || null,
  );
}

async function handleListContainersMsg() {
  // Unwrap if wrapped (some Zen MV3 builds return a function).
  let ci = browser.contextualIdentities;
  if (typeof ci === "function") {
    try { ci = ci(); }
    catch (err) {
      return {
        success: true,
        available: false,
        containers: [],
        reason: `contextualIdentities wrapper threw: ${err.message || err}`,
      };
    }
  }
  if (!ci || typeof ci.query !== "function") {
    console.warn(
      "[DSX:bg] contextualIdentities has no .query — typeof=",
      typeof browser.contextualIdentities,
      "keys=", ci ? Object.keys(ci) : null);
    return {
      success: true,
      available: false,
      containers: [],
      reason: "contextualIdentities.query unavailable in this browser",
    };
  }
  const timeout = new Promise((_, rej) =>
    setTimeout(() => rej(new Error("query timed out after 3s")), 3000));
  try {
    const all = await Promise.race([ci.query({}), timeout]);
    return {
      success: true,
      available: true,
      containers: all.map(c => ({
        cookieStoreId: c.cookieStoreId,
        name: c.name,
        color: c.color,
        icon: c.icon,
      })),
    };
  } catch (err) {
    return {
      success: true,
      available: false,
      containers: [],
      reason: `contextualIdentities.query threw: ${err.message || err}`,
    };
  }
}

// ---- current container detection -------------------------------
//
// sender.tab is the tab the content script runs in, and (with the
// `cookies` permission granted) includes `cookieStoreId` even
// though content scripts cannot see that field on their own copy
// of the Tab.  The tabs.query fallback covers cases where
// sender.tab is somehow incomplete — e.g. a future Firefox change,
// or a message not originating from a tab.
//
// Returns:
//   { success: true, cookieStoreId, name, source }
// where:
//   cookieStoreId = null   → default container (normalized from
//                            "firefox-default")
//   cookieStoreId = string → named container
//   source = "sender.tab" | "tabs.query" | "none" (diagnostic)

async function handleCurrentContainerMsg(msg, sender) {
  let cookieStoreId = null;
  let source = "none";

  if (sender && sender.tab && typeof sender.tab.cookieStoreId === "string") {
    cookieStoreId = sender.tab.cookieStoreId;
    source = "sender.tab";
  } else {
    // Fallback: query the active tab.  currentWindow from a
    // background context means "last-focused window", which
    // matches where the user clicked.
    try {
      const tabs = await browser.tabs.query({ active: true, currentWindow: true });
      if (tabs.length > 0 && typeof tabs[0].cookieStoreId === "string") {
        cookieStoreId = tabs[0].cookieStoreId;
        source = "tabs.query";
      }
    } catch (err) {
      console.warn("[DSX:bg] DSX_CURRENT_CONTAINER query threw:", err);
    }
  }

  // "firefox-default" is the default container's ID; normalize to
  // null so callers can pass through to tabs.create without a
  // redundant container arg.
  if (cookieStoreId === "firefox-default") cookieStoreId = null;

  let name = null;
  if (cookieStoreId) {
    try {
      let ci = browser.contextualIdentities;
      if (typeof ci === "function") {
        try { ci = ci(); } catch (_) {}
      }
      if (ci && typeof ci.get === "function") {
        const ident = await ci.get(cookieStoreId);
        if (ident && typeof ident.name === "string") name = ident.name;
      }
    } catch (err) {
      console.warn("[DSX:bg] contextualIdentities.get threw:", err);
    }
  }

  console.log(
    `[DSX:bg] DSX_CURRENT_CONTAINER source=${source} ` +
    `cookieStoreId=${cookieStoreId || "(default)"} name=${name || "(none)"}`);

  return { success: true, cookieStoreId, name, source };
}

// ---- resume payload (cross-container handoff, in-memory) ----------
//
// The resume button writes its pending payload here so a tab opened
// in a *different* container can still read it.  Container tabs
// isolate localStorage per-container, so localStorage can't deliver
// the payload across the boundary — a cross-container channel is
// needed.
//
// Why in-memory, not browser.storage.local: MV3 event pages in
// current Zen hang on storage writes (the underlying write-to-
// parent-process call never resolves; the same hang that breaks
// contextualIdentities.query).  A module-level variable has no
// I/O, no cross-process round-trip, and is instant.
//
// Freshness guard: if a payload < 30s old already exists, save is
// refused.  Prevents "click 2 overwrites click 1's payload, and T1
// resumes with T2's content".  Claim removes the payload before
// returning, so only one tab ever sees it.

let _pendingResumePayload = null;

async function handleResumeSaveMsg(msg) {
  try {
    const payload = msg.payload;
    if (payload === null) {
      _pendingResumePayload = null;
      return { success: true };
    }
    if (!payload || typeof payload !== "object") {
      return { success: false, reason: "payload required" };
    }
    if (_pendingResumePayload
        && typeof _pendingResumePayload.created_at === "number"
        && Date.now() - _pendingResumePayload.created_at < 30000) {
      return {
        success: false,
        reason:
          `previous resume is still pending ` +
          `(${Math.round((Date.now() - _pendingResumePayload.created_at) / 1000)}s old). ` +
          `Wait 30s or clear.`,
      };
    }
    _pendingResumePayload = payload;
    return { success: true };
  } catch (err) {
    return {
      success: false,
      reason: String((err && err.message) || err),
    };
  }
}

async function handleResumeClaimMsg() {
  try {
    const payload = _pendingResumePayload;
    _pendingResumePayload = null;
    if (!payload) return { success: true, payload: null };
    if (typeof payload.created_at !== "number"
        || Date.now() - payload.created_at > 30000) {
      return { success: true, payload: null };
    }
    return { success: true, payload };
  } catch (err) {
    return {
      success: false,
      reason: String((err && err.message) || err),
    };
  }
}

// ---- onMessage dispatch -------------------------------------------
//
// `sender` is the second arg to onMessage, populated by Firefox for
// any message originating from a content script.  Its `tab` field
// is what DSX_CURRENT_CONTAINER uses.

browser.runtime.onMessage.addListener(async (msg, sender) => {
  if (!msg || typeof msg.type !== "string") return;
  if (msg.type === "DSX_OPEN_TAB") {
    return await handleOpenTabMsg(msg);
  }
  if (msg.type === "DSX_LIST_CONTAINERS") {
    return await handleListContainersMsg();
  }
  if (msg.type === "DSX_RESUME_SAVE") {
    return await handleResumeSaveMsg(msg);
  }
  if (msg.type === "DSX_RESUME_CLAIM") {
    return await handleResumeClaimMsg();
  }
  if (msg.type === "DSX_CURRENT_CONTAINER") {
    return await handleCurrentContainerMsg(msg, sender);
  }
  return;  // not ours
});