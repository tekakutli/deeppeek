"""Fixture 21 helper — JS embedded in a Python string literal."""

PANEL_FIELDS_JS = r"""
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
"""
