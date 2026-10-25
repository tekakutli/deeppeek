# Fixture 20 — AST anchor: top-level declaration

Tests tree-sitter based structural anchoring for a top-level
block that has no unique text anchor.  The block starts with
`const inpaintFullCanvasEl = ...` followed by `if (inpaintFullCanvasEl)
wiring; the tool finds the last top-level `if_statement` in base
(`if (inpaintPromptEl) { ... }`) and inserts after it.

This is the pg_panel_scrubs.py shape — a new element reference
plus its event listener wiring.

## File: `panel.js`

Add the following field registration:

```js
const inpaintFullCanvasEl = document.getElementById("inpaintFullCanvas");
if (inpaintFullCanvasEl) {
  inpaintFullCanvasEl.addEventListener("change", () => {
    console.log("bye");
  });
}
```
