# Fixture 23 - identifier-block anchor for insert-after-anchor

Tests the const+if extension in _try_insert_after_anchor: the
prose names an identifier that lives inside a
`const x = ...; if (x) { ... }` pair. The patcher must find the
const, extend through the following if-block, and insert after
the whole pair - not between them.

This is the pg_panel_scrubs.py shape.

## File: `scrubs.js`

Add the checkbox wiring after the existing `inpaintPromptEl` block:

```js
const inpaintFullCanvasEl = document.getElementById("inpaintFullCanvas");
if (inpaintFullCanvasEl) {
  inpaintFullCanvasEl.addEventListener("change", () => {
    console.log("changed");
  });
}
```
