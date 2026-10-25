# Fixture 13 — scoped-replace via add verb

Tests that prose using an add-family verb ("add X to Y")
followed by a body carrying a scope marker fires the
scoped-replace path.  This is the pg_view_inpaint.py shape.

## File: `view.js`

Add the full-canvas early-out to `drawInpaintContext`:

```js
export function drawInpaintContext(r) {
  if (board.inpaintFullCanvas) return;
  if (!board.imageEl) return;
  ctx.fillRect(0, 0, 100, 100);
}
```
