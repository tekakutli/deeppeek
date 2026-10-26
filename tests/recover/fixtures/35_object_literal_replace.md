# Fixture 35 — object-literal replace preserves base prefix

Tests the generalized `_OBJECT_LITERAL_ASSIGNMENT_RE` branch: the
body is a plain `board = { ... }` (no `const` prefix);
base is `const board = { a: 1 };`.

The patcher must replace the object in place, preserving the
base's `const` prefix, and must not append a duplicate
assignment (the body's `board =` must not become a second
declaration).

The subsection heading and "`Change inside` ..." prose both
force partial classification so the body becomes a patch
candidate against the preexisting base rather than a competing
`full` that dedup would drop.

## File: `board.js`

### (a) Update the board object

**Change inside the board object:**

```js
board = {
  a: 1,
  b: 2,
};
```
