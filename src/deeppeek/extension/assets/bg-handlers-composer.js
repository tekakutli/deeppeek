// Composer-affecting events: attach, type, enter, clear, submit.
//
// Each handler forwards a single message to the target DeepSeek tab
// via browser.tabs.sendMessage and posts the resulting ack back to
// the originating port.
//
// TAB TARGETING (2026-11-15)
// ==========================
// All four handlers (attach / type / enter / clear) now use
// `_targetTab(data)` from bg-bind.js instead of hardcoding
// `tabs.query({active: true, currentWindow: true})`.  The behaviour:
//
//   · If `data.tab_id` is present: resolve directly to that tab.
//     This is the pinned path used by split_send (and any future
//     pipeline that binds once and stays pinned).  No more dependency
//     on user focus state — the CLI can Alt-Tab to the terminal and
//     the extension still finds the tab.
//
//   · If `data.tab_id` is absent: fall back to the old active-tab
//     query.  Existing commands (`deeppeek pipe`, `type`, `enter`,
//     `prompt resume`) keep working exactly as before.
//
// `submit` keeps its existing tab_id handling (which already
// predated this refactor) but shares the same tab-resolution
// semantics via `_targetTab`.

async function handleAttach(data, port) {
  const batchId = data.batch_id;
  const files = data.files || [];
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} attach received (batch=${batchId}, ` +
    `${files.length} file(s), tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_ATTACH",
        files,
        opts,
      });
      result = resp || {
        success: false, reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} attach result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "attach", batch_id: batchId, result });
}

async function handleType(data, port) {
  const batchId = data.batch_id;
  const text = data.text || "";
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} type received (batch=${batchId}, ` +
    `${text.length} chars, tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_TYPE",
        text,
        opts,
      });
      result = resp || {
        success: false, reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} type result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "type", batch_id: batchId, result });
}

async function handleEnter(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} enter received (batch=${batchId}, ` +
    `tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_ENTER",
        opts,
      });
      result = resp || {
        success: false, reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} enter result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "enter", batch_id: batchId, result });
}

async function handleClear(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} clear received (batch=${batchId}, ` +
    `tab_id=${data.tab_id ?? "active"})`);

  const tab = await _targetTab(data);
  let result;
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_CLEAR",
        opts,
      });
      result = resp || {
        success: false, reason: "no response from content script",
      };
    } catch (err) {
      result = {
        success: false,
        reason: `sendMessage threw: ${err.message}`,
      };
    }
  }

  console.log(`DSX:${tag} clear result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "clear", batch_id: batchId, result });
}

async function handleSubmit(data, port) {
  const batchId = data.batch_id;
  const text = data.text || "";
  const tag = `[${port}]`;

  let result;
  const tab = await _targetTab(data);
  if (!tab) {
    result = { success: false, reason: "target tab unavailable" };
  } else {
    try {
      const clear = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_CLEAR", opts: {},
      });
      const type = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_TYPE",
        text,
        opts: { skip_verify: true },
      });

      let enter = null;
      let enterOk = false;
      try {
        enter = await browser.tabs.sendMessage(tab.id, {
          type: "DSX_ENTER", opts: { verify_ms: 1500 },
        });
        enterOk = !!(enter && enter.submitted !== false);
      } catch (err) {
        enter = {
          success: false, submitted: false,
          reason: `sendMessage threw: ${err.message}`,
        };
        enterOk = false;
      }

      const failures = [];
      if (!(clear && clear.success)) {
        failures.push(`clear: ${(clear && clear.reason) || "failed"}`);
      }
      if (!(type && type.success)) {
        failures.push(`type: ${(type && type.reason) || "failed"}`);
      }
      if (!enterOk) {
        failures.push(`enter: ${(enter && enter.reason) || "not dispatched"}`);
      }

      result = {
        success: failures.length === 0,
        reason: failures.length ? failures.join("; ") : undefined,
        tab_id: tab.id,
        clear, type, enter,
      };
    } catch (err) {
      result = {
        success: false, reason: `submit threw: ${err.message}`,
      };
    }
  }

  await postAck(port, { __ack: "submit", batch_id: batchId, result });
}