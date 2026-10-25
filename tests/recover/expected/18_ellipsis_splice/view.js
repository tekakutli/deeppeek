export function paintCanvas(r) {
  if (board.fullCanvas) return;
  if (!board.imageEl) return;
  ctx.fillRect(0, 0, 100, 100);
}
