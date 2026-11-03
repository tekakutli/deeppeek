// Composer-affecting events: attach, type, enter, clear, submit.
//
// Each handler forwards a single message to the active
// (or caller-pinned) DeepSeek tab via browser.tabs.sendMessage and
// posts the resulting ack back to the originating port. Bodies are
// unchanged from the pre-split background.js.

async function handleAttach(data, port) {
  const batchId = data.batch_id;
  const files = data.files || [];
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} attach received (batch=${batchId}, ` +
    `${files.length} file(s))`);

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
    `${text.length} chars)`);

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

  console.log(`DSX:${tag} type result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "type", batch_id: batchId, result });
}

async function handleEnter(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(`DSX:${tag} enter received (batch=${batchId})`);

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

  console.log(`DSX:${tag} enter result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "enter", batch_id: batchId, result });
}

async function handleClear(data, port) {
  const batchId = data.batch_id;
  const opts = data.opts || {};
  const tag = `[${port}]`;

  console.log(`DSX:${tag} clear received (batch=${batchId})`);

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_CLEAR",
        opts,
      });
      result = resp || { success: false, reason: "no response from content script" };
    }
  } catch (err) {
    result = { success: false, reason: `sendMessage threw: ${err.message}` };
  }

  console.log(`DSX:${tag} clear result (batch=${batchId}):`, result);
  await postAck(port, { __ack: "clear", batch_id: batchId, result });
}

async function handleSubmit(data, port) {
  const batchId = data.batch_id;
  const text = data.text || "";
  const tag = `[${port}]`;

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    let tab = null;
    if (data.tab_id != null) {
      try { tab = await browser.tabs.get(data.tab_id); }
      catch (_) { tab = null; }
    }
    if (!tab) {
      const [active] = await browser.tabs.query({
        active: true, currentWindow: true,
      });
      tab = active;
    }

    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const clear = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_CLEAR", opts: {},
      });
      const type = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_TYPE", text, opts: { skip_verify: true },
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
    }
  } catch (err) {
    result = { success: false, reason: `submit threw: ${err.message}` };
  }

  await postAck(port, { __ack: "submit", batch_id: batchId, result });
}