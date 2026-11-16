// =====================================================================
// bg-bind.js — tab pinning + tab-target helper
// =====================================================================
//
// Two responsibilities:
//
//   1. Handle the `bind-tab` SSE event.  Called once at CLI startup
//      to pin the pipeline to one specific chat tab.  After a
//      successful bind, every subsequent event payload carries a
//      `tab_id`, and `_targetTab` resolves directly to that tab —
//      no more "which tab is active?" queries, no more "hover to
//      enable".
//
//   2. Expose `_targetTab(data)` — used by every composer handler
//      (attach / type / enter / clear) and the read handlers
//      (watch / extract / retry-last / extract-attachments).  The
//      helper preserves backward compatibility: if `data.tab_id` is
//      absent, it falls back to the old active-tab query, so
//      interactive `deeppeek pipe` / `type` / `enter` behave
//      exactly as before.
//
// Design rationale (paraphrased from the CLI-side docstring):
//
//   - `tabs.query({active: true, currentWindow: true})` requires the
//     target tab to be the currently-focused tab in the currently-
//     focused window.  Any user focus change mid-run breaks it.
//   - `tabs.query({url: [...]})` returns every matching tab in every
//     window, active or not.  This is the correct primitive for
//     "find the tab the user means" when the pipeline is autonomous.
//   - Ambiguity (multiple matching tabs) is reported explicitly, not
//     silently resolved by picking whichever is active.  The CLI
//     prints candidates and prompts for `--tab-id`.
//
// Composition with the composer binding (bg-bind.js's counterpart in
// the page world, `__dsx_bind_composer_json`):
//
//   bind-tab       → which tab
//   bind-composer  → which textarea inside that tab
//
// These are orthogonal.  bind-tab returns a tab_id; bind-composer
// runs INSIDE that tab and returns a selector/source.  Composer
// handlers use the pinned tab_id to route to the right content
// script, and the content script's doType/doEnter use the bound
// composer element.

async function _targetTab(data) {
  if (data && data.tab_id != null) {
    try {
      const tab = await browser.tabs.get(data.tab_id);
      if (!tab || !isSupportedTab(tab)) return null;
      return tab;
    } catch (_) {
      return null;
    }
  }
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    return isSupportedTab(tab) ? tab : null;
  } catch (_) {
    return null;
  }
}

async function _listSupportedTabs() {
  const patterns = SUPPORTED_DOMAINS.map(d => d + "*");
  try {
    return await browser.tabs.query({ url: patterns });
  } catch (_) {
    return [];
  }
}

async function handleBindTab(data, port) {
  const batchId = data.batch_id;
  const tag = `[${port}]`;
  const requestedId = (data.tab_id != null) ? data.tab_id : null;

  let result;

  if (requestedId != null) {
    // ---- explicit pin: verify it, do NOT fall back ----
    try {
      const tab = await browser.tabs.get(requestedId);
      if (!tab) {
        result = { success: false,
                   reason: `tab ${requestedId} not found` };
      } else if (!isSupportedTab(tab)) {
        result = { success: false,
                   reason: `tab ${requestedId} is not a supported chat URL`,
                   url: tab.url };
      } else {
        result = { success: true,
                   tab_id: tab.id,
                   url: tab.url,
                   title: tab.title || null,
                   pinned_by: "explicit" };
      }
    } catch (err) {
      result = { success: false,
                 reason: `tabs.get threw: ${err.message}` };
    }
  } else {
    // ---- auto-discovery: query ALL supported tabs in ALL windows ----
    const all = await _listSupportedTabs();

    if (all.length === 0) {
      result = {
        success: false,
        reason: "no supported chat tab open.  Open chat.deepseek.com " +
                "in a tab and try again.",
      };
    } else if (all.length === 1) {
      const t = all[0];
      result = {
        success: true,
        tab_id: t.id,
        url: t.url,
        title: t.title || null,
        pinned_by: "auto-single",
      };
    } else {
      // Multiple matching tabs.  The active one is a legitimate
      // tiebreaker ONLY if exactly one of the matches is active;
      // if two windows each have an active DeepSeek tab (rare but
      // possible), we refuse to guess.
      const active = all.filter(t => t.active);
      if (active.length === 1) {
        const t = active[0];
        result = {
          success: true,
          tab_id: t.id,
          url: t.url,
          title: t.title || null,
          pinned_by: "auto-active-tiebreak",
          other_tab_count: all.length - 1,
        };
      } else {
        result = {
          success: false,
          reason: `multiple supported chat tabs open (${all.length}); ` +
                  `pass --tab-id <N> or focus exactly one tab`,
          candidates: all.map(t => ({
            tab_id: t.id,
            url: t.url,
            title: t.title || null,
            active: !!t.active,
          })),
        };
      }
    }
  }

  console.log(`DSX:${tag} bind-tab result:`, result);
  await postAck(port, {
    __ack: "bind-tab", batch_id: batchId, result,
  });
}

// bind-composer runs in the CONTENT SCRIPT (this handler just
// forwards).  Same pattern as attach/type/enter: pick the target
// tab, sendMessage, ack the result.

async function handleBindComposer(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  const tab = await _targetTab(data);
  if (!tab) {
    await postAck(port, {
      __ack: "bind-composer", batch_id: batchId,
      result: { success: false, reason: "target tab unavailable" },
    });
    return;
  }

  let result;
  try {
    result = await browser.tabs.sendMessage(tab.id, {
      type: "DSX_BIND_COMPOSER", opts,
    });
    if (!result) {
      result = { success: false,
                 reason: "no response from content script" };
    }
  } catch (err) {
    result = { success: false,
               reason: `sendMessage threw: ${err.message}` };
  }

  console.log(`DSX:${tag} bind-composer result:`, result);
  await postAck(port, {
    __ack: "bind-composer", batch_id: batchId, result,
  });
}

async function handleProbeComposer(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  const tab = await _targetTab(data);
  if (!tab) {
    await postAck(port, {
      __ack: "probe-composer", batch_id: batchId,
      result: { success: false, reason: "target tab unavailable" },
    });
    return;
  }

  let result;
  try {
    result = await browser.tabs.sendMessage(tab.id, {
      type: "DSX_PROBE_COMPOSER", opts,
    });
    if (!result) {
      result = { success: false,
                 reason: "no response from content script" };
    }
  } catch (err) {
    result = { success: false,
               reason: `sendMessage threw: ${err.message}` };
  }

  await postAck(port, {
    __ack: "probe-composer", batch_id: batchId, result,
  });
}