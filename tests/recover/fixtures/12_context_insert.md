# Fixture 12 — context insert (2-line snippet, anchor-last)

Tests that a 2-line body whose last line is a unique anchor and
whose first line is absent from base is context-inserted BEFORE
the anchor.  This is the pg_state.py shape: a small addition
with no scope marker, anchored by the code it sits next to.

## File: `state.js`

In the board object, add `inpaintFullCanvas: false,` right
before `inpaintPrompt: "",`:

```js
  inpaintFullCanvas: false,
  inpaintPrompt: "",
```
