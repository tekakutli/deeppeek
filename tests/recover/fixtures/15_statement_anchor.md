# Fixture 15 — statement anchor insert

Tests that prose naming a full statement in backticks works as
an insert-after anchor.  The block body is inserted after the
line containing the anchor statement, re-indented to match.

This is the general-purpose fallback for prose like:
  "after the existing block that sets `e.promptEl.disabled = busy;`"
where the anchor is a *statement*, not an identifier.

## File: `helper.js`

After the existing code that sets `e.promptEl.disabled = busy;`,
add:

```js
if (e.fullCanvasEl && document.activeElement !== e.fullCanvasEl) {
  e.fullCanvasEl.checked = !!board.inpaintFullCanvas;
}
```
