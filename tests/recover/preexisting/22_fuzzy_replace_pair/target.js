export function handler(e) {
  const sx = e.clientX;
  const sy = e.clientY;

  // Hover cursor.
  if (board.inpaintEnabled && r && !_contextDrag.active) {
    canvas.style.cursor = "pointer";
  }
}
