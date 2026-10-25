# Fixture 24 — structural append: statement inside embedded JS

Mirrors the pg_panel_fields.py shape: a bare `if (...)` guard
with no text anchor, meant to go inside `syncPanelFields`.  Runs
**without AST** — the step-9 text-structural fallback fires,
inserting at the end of the last top-level function in the JS
string.

## File: `helper.py`

```js
if (e.inpaintFullCanvas && document.activeElement !== e.inpaintFullCanvas) {
  e.inpaintFullCanvas.checked = !!board.inpaintFullCanvas;
}
```
