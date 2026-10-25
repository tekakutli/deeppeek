const inpaintPromptEl = document.getElementById("inpaintPrompt");
if (inpaintPromptEl) {
  inpaintPromptEl.addEventListener("input", () => {
    console.log("hi");
  });
}
const inpaintFullCanvasEl = document.getElementById("inpaintFullCanvas");
if (inpaintFullCanvasEl) {
  inpaintFullCanvasEl.addEventListener("change", () => {
    console.log("bye");
  });
}
