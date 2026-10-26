# Fixture 34 — single-line field replace

Tests `_try_single_line_replace`: the body is one non-blank line;
the prose is not an add-instruction; the line matches a base line
by identifier and token-similarity.  The new value replaces the
old on the same line, preserving indentation.

Without this branch, a single-line body falls through every
insert-shaped heuristic and fails closed.  This is the shape of
"change `inpaintEnabled` to `true`".

## File: `config.js`

Change `value` from `false` to `true`:

```js
value: true,
```
