# Fixture 09 — replace-pair with non-canonical first-block prose

Tests that a pair is formed when the SECOND block's prose is
a `with:`-style connective, even when the FIRST block's prose
does not match any specific phrase in `_REPLACE_PROSE`.

The old, narrow rule required the first block to say
"Replace the block:" or similar.  Real conversations say
"Replace the doc comment above X:" and "And replace the
function body:" — neither of which was in the list — so the
pair was missed, the old block was dropped, and the file was
written half-applied.

Runs in --apply-to mode: the pre-existing `paint.js` under
`preexisting/09_replace_pair_docstring/` is the base.

## File: `paint.js`

Replace the doc comment above `paint`:

```js
// Old comment: v1
```

with:

```js
// New comment: v2
```

And replace the function body:

```js
export function paint(el) {
  el.style.color = "#ff0000";
}
```

with:

```js
export function paint(el) {
  el.style.color = "#88c0d0";
}
```
