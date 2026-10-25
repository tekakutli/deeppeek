export function drawInpaintContext(r) {
  if (board.inpaintFullCanvas) return;
  if (!board.imageEl) return;
  ctx.fillRect(0, 0, 100, 100);
}
