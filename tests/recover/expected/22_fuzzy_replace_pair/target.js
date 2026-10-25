export function handler(e) {
  const sx = e.clientX;
  const sy = e.clientY;

  // Hover cursor.
  // Skipped when the rect is hidden.
  if (r && !_contextDrag.active && !board.inpaintFullCanvas) {
    canvas.style.cursor = "pointer";
  }
}
