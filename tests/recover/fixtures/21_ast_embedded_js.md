# Fixture 21 — AST anchor: JS embedded in a Python string

Tests the embedded-container path: base is a `.py` file with a
`r"""..."""` string holding JS.  The block is a bare JS
`if` statement meant to live inside `syncPanelFields` (which is
itself inside the JS string).

This is the pg_panel_fields.py shape — JS constants in Python
playground sources.

## File: `helper.py`

In the panel fields sync function, add:

```js
if (e.inpaintFullCanvas && document.activeElement !== e.inpaintFullCanvas) {
  e.inpaintFullCanvas.checked = !!board.inpaintFullCanvas;
}
```
