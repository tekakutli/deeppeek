export function syncPanelFields() {
  const e = _get();
  if (e.prompt) e.prompt.disabled = busy;
  if (e.fullCanvas && document.activeElement !== e.fullCanvas) {
    e.fullCanvas.checked = !!board.fullCanvas;
  }
}
