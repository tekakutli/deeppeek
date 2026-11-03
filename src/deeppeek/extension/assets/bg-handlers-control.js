// Control events: arm, disarm, config, request-status, open-tab.
//
// `arm`/`disarm` flip the per-port armed state and kick maybeFire.
// `config` updates include_forked (per-port) and the persisted
// watchdog setting. `request-status` replies with the persisted
// watchdog value. `open-tab` creates a tab, honouring a container
// choice if supplied — the container resolution + creation are
// shared with the onMessage `DSX_OPEN_TAB` path via
// `createTabWithContainer`.

async function handleArm(data, port) {
  console.log(`DSX:[${port}] arm received`);
  armedByPort.add(port);
  firing = false;
  maybeFire(port);
}

async function handleDisarm(data, port) {
  console.log(`DSX:[${port}] disarm received`);
  armedByPort.delete(port);
  firing = false;
}

async function handleConfig(data, port) {
  if ("include_forked" in data) {
    includeForkedByPort.set(port, !!data.include_forked);
    console.log(
      `DSX:[${port}] config include_forked=${!!data.include_forked}`);
  }
  if (!("watchdog" in data)) return;
  const enabled = data.watchdog !== false;
  await browser.storage.local.set({ watchdog: enabled });
  await postAck(port, { __ack: "watchdog", watchdog: enabled });
}

async function handleRequestStatus(data, port) {
  const { watchdog } = await browser.storage.local.get("watchdog");
  const enabled = watchdog !== false;
  await postAck(port, { __status: "watchdog", watchdog: enabled });
}

async function handleOpenTab(data, port) {
  const batchId = data.batch_id;
  const url = data.url || "https://chat.deepseek.com/";
  const containerName = data.container || null;
  const containerId = data.container_id || null;
  const tag = `[${port}]`;

  console.log(
    `DSX:${tag} open-tab received (batch=${batchId}, url=${url}, ` +
    `container=${containerName || containerId || "default"})`);

  const result = await createTabWithContainer(
    url, containerName, containerId, tag);

  console.log(`DSX:${tag} open-tab result (batch=${batchId}):`, result);
  await postAck(port, {
    __ack: "open-tab",
    batch_id: batchId,
    result,
  });
}