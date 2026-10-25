function _get() {
  const e = {
    promptEl: document.getElementById("inpaintPrompt"),
  };
  if (e.promptEl) e.promptEl.disabled = busy;
  if (e.fullCanvasEl && document.activeElement !== e.fullCanvasEl) {
    e.fullCanvasEl.checked = !!board.inpaintFullCanvas;
  }
  return e;
}

export { _get };
