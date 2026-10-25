# Fixture 33 — symbol resolution does not over-trigger

Same block as fixture 32, but the destination file imports
`registerField`.  The call resolves and the record writes
normally.  This is the regression guard against over-triggering:
if the check demoted every record whose patch introduces a call,
half the tool's patches would fail-closed and it would be unusable.

## File: `scrubs.js`

Add this field registration:

```js
const newValue = "x";
registerField("new.field", { get: () => newValue });
```
