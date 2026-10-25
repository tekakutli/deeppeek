export function syncPanelFields() {
  const e = _get();

  if (e.imageInfo) {
    e.imageInfo.textContent = "x";
  }

  if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;
}
