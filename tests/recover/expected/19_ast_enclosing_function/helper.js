export function syncPanelFields() {
  const e = _get();

  if (e.imageInfo) {
    e.imageInfo.textContent = "x";
  }

  if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;
  if (e.inpaintFullCanvas && document.activeElement !== e.inpaintFullCanvas) {
    e.inpaintFullCanvas.checked = !!board.inpaintFullCanvas;
  }
}
