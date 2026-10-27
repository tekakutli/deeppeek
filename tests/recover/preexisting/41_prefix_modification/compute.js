export function computeW(m) {
  if (!m) return null;

  const W = m.img.width, H = m.img.height;
  let minX = W, minY = H, maxX = -1, maxY = -1;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (m.px[y * W + x] >= 128) {
        if (x < minX) minX = x;
        if (y < minY) minY = y;
        if (x > maxX) maxX = x;
        if (y > maxY) maxY = y;
      }
    }
  }
  return {x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1};
}
