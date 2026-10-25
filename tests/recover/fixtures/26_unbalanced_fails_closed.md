# Fixture 26 — unbalanced block fails-closed

A statement-shaped block with an unclosed `{` must be rejected
by step-9 (structural append) and fail-closed by `pass8_verify`.
Target preserved; block lands in `low_trust/`.

## File: `broken.js`

```js
  if (cond) {
    doThing();
  // missing close brace
```
