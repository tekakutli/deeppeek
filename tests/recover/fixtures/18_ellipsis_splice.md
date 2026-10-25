# Fixture 18 — ellipsis-splice (scope prefix insert)

Tests the ellipsis-splice path: an AI writes the scope's new
prefix, then `...` meaning 'rest unchanged', then the scope's
closing.  The correct handling is a partial splice — insert only
the genuinely-new lines at their position within the base scope,
keep the base's middle content verbatim.

This is the pg_view_inpaint.py shape: `drawInpaintContext` gets
a new early-out guard at the top; the rest of the function is
preserved as-is.

## File: `view.js`

Replace the entire `paintCanvas` function with:

```js
export function paintCanvas(r) {
  if (board.fullCanvas) return;
  if (!board.imageEl) return;
  ...
}
```
