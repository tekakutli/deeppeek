# Fixture 29 — semantic truncation refuses repair

A block that APPLIES but leaves the file with one unclosed `{`.
Inside the block, a statement sits at the innermost opener's
own indent — the pg_panel_fields.py shape. Brace insertion
would silently nest it, changing semantics. `try_repair` must
refuse and produce no `.repaired` suggestion.

## File: `panel.js`

After `e.inpaintPrompt.disabled = busy;`, add:

```js
if (A) {
  doA();
if (B) doB();
```
