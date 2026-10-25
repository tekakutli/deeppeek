# Fixture 31 — duplicate if-blocks are deduped

Two near-identical source blocks both insert the same guard
clause into `syncPanelFields`.  Pre-fix, the result contains
two copies of the guard; content_valid passes because the
duplicate check only catches column-0 declarations.  Post-fix,
`_dedup_duplicate_blocks` removes the second copy before
validation, so the result is clean.

## File: `panel.js`

After `e.prompt.disabled = busy;`, add:

```js
  if (e.fullCanvas && document.activeElement !== e.fullCanvas) {
    e.fullCanvas.checked = !!board.fullCanvas;
  }
```

Also after `e.prompt.disabled = busy;`, add:

```js
  if (e.fullCanvas && document.activeElement !== e.fullCanvas) {
    e.fullCanvas.checked = !!board.fullCanvas;
  }
```
