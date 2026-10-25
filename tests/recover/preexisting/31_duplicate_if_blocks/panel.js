export function syncPanelFields() {
  const e = _get();
  if (e.prompt) e.prompt.disabled = busy;
}
