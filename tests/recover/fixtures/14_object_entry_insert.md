# Fixture 14 — object-entry insert into named function

Tests the object-entry-insert heuristic: block is 2 lines of
`key: value,` entries, prose names a function like `_get()`.  The
patcher finds `_get` in the base, finds its `_els = { ... }`
object literal, inserts the entries before the closing `}`.

## File: `panel.js`

In the `_get()` element cache, add:

```js
inpaintFullCanvas: document.getElementById("inpaintFullCanvas"),
inpaintRectHint:   document.getElementById("inpaintRectHint"),
```
