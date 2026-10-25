"""Fixture 25 scrubs — JS embedded in a Python string literal."""

SCRUBS_JS = r"""
const inpaintPromptEl = document.getElementById("inpaintPrompt");
if (inpaintPromptEl) {
  inpaintPromptEl.value = board.inpaintPrompt || "";
  inpaintPromptEl.addEventListener("input", () => {
    board.inpaintPrompt = inpaintPromptEl.value;
  });
}
const inpaintFullCanvasEl = document.getElementById("inpaintFullCanvas");
if (inpaintFullCanvasEl) {
  inpaintFullCanvasEl.addEventListener("change", () => {
    console.log("changed");
  });
}

export { };
"""
