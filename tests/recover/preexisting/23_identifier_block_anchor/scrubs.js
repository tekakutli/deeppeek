const inpaintPromptEl = document.getElementById("inpaintPrompt");
if (inpaintPromptEl) {
  inpaintPromptEl.value = board.inpaintPrompt || "";
  inpaintPromptEl.addEventListener("input", () => {
    board.inpaintPrompt = inpaintPromptEl.value;
  });
}

export { };
