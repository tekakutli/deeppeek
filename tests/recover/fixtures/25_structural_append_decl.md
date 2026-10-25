# Fixture 25 — structural append: declaration after existing pair

Mirrors the pg_panel_scrubs.py shape: a 10-line `const X = ...;
if (X) {...}` block that belongs after an existing
`const Y = document.getElementById(...); if (Y) {...}` pair.
Runs without AST — step-9 text-structural fallback matches the
block's RHS pattern against the existing declaration and inserts
after its whole block.

## File: `scrubs.py`

```js
const inpaintFullCanvasEl = document.getElementById("inpaintFullCanvas");
if (inpaintFullCanvasEl) {
  inpaintFullCanvasEl.addEventListener("change", () => {
    console.log("changed");
  });
}
```
