// Download-hijack events: install, uninstall, all.
//
// The click-interceptor install/uninstall just forwards to the
// content script; `all` also forwards the CLI's port so the
// content script can POST downloads back to the correct server
// (relevant when the daemon runs alongside the CLI).

async function handleHijackInstall(data, port) {
  const batchId = data.batch_id;
  const tag = `[${port}]`;
  console.log(`DSX:${tag} hijack-install received (batch=${batchId})`);

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_HIJACK_INSTALL",
      });
      result = resp || {
        success: false, reason: "no response from content script",
      };
    }
  } catch (err) {
    result = { success: false, reason: `sendMessage threw: ${err.message}` };
  }

  console.log(`DSX:${tag} hijack-install result:`, result);
  await postAck(port, {
    __ack: "hijack-install", batch_id: batchId, result,
  });
}

async function handleHijackUninstall(data, port) {
  const batchId = data.batch_id;
  const tag = `[${port}]`;
  console.log(`DSX:${tag} hijack-uninstall received (batch=${batchId})`);

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_HIJACK_UNINSTALL",
      });
      result = resp || {
        success: false, reason: "no response from content script",
      };
    }
  } catch (err) {
    result = { success: false, reason: `sendMessage threw: ${err.message}` };
  }

  console.log(`DSX:${tag} hijack-uninstall result:`, result);
  await postAck(port, {
    __ack: "hijack-uninstall", batch_id: batchId, result,
  });
}

async function handleHijackAll(data, port) {
  const batchId = data.batch_id;
  const cliPort = data.port;
  const tag = `[${port}]`;
  console.log(
    `DSX:${tag} hijack-all received ` +
    `(batch=${batchId}, port=${cliPort})`);

  let result = { success: false, reason: "no active deepseek tab" };
  try {
    const [tab] = await browser.tabs.query({
      active: true, currentWindow: true,
    });
    if (tab && tab.url && tab.url.startsWith("https://chat.deepseek.com/")) {
      const resp = await browser.tabs.sendMessage(tab.id, {
        type: "DSX_HIJACK_ALL",
        opts: { port: cliPort },
      });
      result = resp || {
        success: false,
        reason: "no response from content script",
      };
    }
  } catch (err) {
    result = {
      success: false,
      reason: `sendMessage threw: ${err.message}`,
    };
  }

  console.log(`DSX:${tag} hijack-all result:`, result);
  await postAck(port, {
    __ack: "hijack-all",
    batch_id: batchId,
    result,
  });
}