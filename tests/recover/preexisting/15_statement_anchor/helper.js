function _get() {
  const e = {
    promptEl: document.getElementById("inpaintPrompt"),
  };
  if (e.promptEl) e.promptEl.disabled = busy;
  return e;
}

export { _get };
