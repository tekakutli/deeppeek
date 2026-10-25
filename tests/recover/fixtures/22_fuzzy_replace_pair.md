# Fixture 22 - fuzzy replace-pair (drift-tolerant)

Tests the fuzzy replace-pair path: the AI's old_body does not
match the base byte-for-byte, but the base is a SUPERSET of the
AI's version (base has extra tokens). Exact and normalized
matching both fail; the fuzzy matcher accepts because every
differing line is base-superset-of-old_body.

This is the pg_dispatch.py shape.

## File: `target.js`

Replace:

```js
  // Hover cursor.
  if (r && !_contextDrag.active) {
```

with:

```js
  // Hover cursor.
  // Skipped when the rect is hidden.
  if (r && !_contextDrag.active && !board.inpaintFullCanvas) {
```
