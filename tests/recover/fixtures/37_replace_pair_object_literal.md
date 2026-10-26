# Fixture 37 — replace-pair preserves const prefix

Tests that a replace pair whose NEW body is a bare object-literal
assignment (`board = {...}`, no `const` prefix) preserves the
base's `const` prefix.  Raw replace would strip it: a pair whose
old side is `const board = { a: 1 };` and whose new side is
`board = { a: 1, b: 2 };` produces `board = {...}` -- no `const` --
which either throws a TDZ error or silently rebinds the name as
an implicit global.

The pair is detected via standard `Find:` / `with:` prose, and
applied via `_try_object_literal_splice_for_pair`, which locates
the base's declaration and replaces only the object literal,
keeping the `const` / `let` / `var` / `export const` prefix intact.

## File: `board.js`

Find:

```js
const board = { a: 1 };
```

with:

```js
board = {
  a: 1,
  b: 2,
};
```
