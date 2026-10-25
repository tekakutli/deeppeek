# Fixture 28 — structural brace repair (positive)

A block that APPLIES but leaves the file with one unclosed `{`.
The truncation ends after a complete statement and, crucially,
the deepest remaining content is deeper-indented than its
innermost opener (no sibling-statement-nested-wrongly).

`try_repair` should append one `}` and produce a `.repaired`
suggestion. Target preserved.

## File: `panel.js`

After `e.inpaintPrompt.disabled = busy;`, add:

```js
if (cond) {
  doA();
  doB();
```
