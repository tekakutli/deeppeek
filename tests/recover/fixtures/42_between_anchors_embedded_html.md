# Fixture 42 — between-anchors on embedded HTML

Exercises `_try_insert_between_anchors` with prose that names two
`<details>` blocks inside a `.py` file's embedded HTML string.
Threading the outer file's `.py` path into `_find_named_block_end`
would stop at the Python parser (no `Image`/`Masks` def) and return
None; the empty-path fallback tries Python → JS → HTML and succeeds
on the HTML attempt.

## File: `pg_base.py`

Between the Image and Masks `<details>` blocks, add:

```html
<div class="divider"></div>
```
