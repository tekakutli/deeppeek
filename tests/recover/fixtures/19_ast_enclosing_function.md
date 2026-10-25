# Fixture 19 — AST anchor: enclosing function

Tests tree-sitter based structural anchoring for a bare
statement that has no unique text anchor.  The block references
local variable `e`; the tool identifies `syncPanelFields` as the
enclosing function (it declares `e`) and inserts at its end.

This is the pg_panel_fields.py shape — an `if (e.someFlag)`
check added inside the function that populates the element cache.

## File: `helper.js`

In the panel fields sync function, add:

```js
if (e.inpaintFullCanvas && document.activeElement !== e.inpaintFullCanvas) {
  e.inpaintFullCanvas.checked = !!board.inpaintFullCanvas;
}
```
