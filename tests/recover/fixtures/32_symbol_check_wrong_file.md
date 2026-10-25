# Fixture 32 — symbol resolution catches wrong-file placement

Reproduces the `pgkitRegisterField` bug in isolation.  `bindings.js`
imports `registerAction` but NOT `registerField`.  The conversation
adds a block that calls `registerField` and tells the tool to put it
in `bindings.js`.  Applying it would throw `ReferenceError` at module
load time — same failure mode as the real bug that blanked the canvas.

Pass 8.5 detects the unresolved call and demotes the record to LOW
trust.  Under the default `--min-trust MEDIUM` the target file is
preserved; the attempt lands in `deeppeek/low_trust/`.

## File: `bindings.js`

Add this field registration:

```js
const newValue = "x";
registerField("new.field", { get: () => newValue });
```
