# Fixture 30 — _filter_new preserves structural closers

The block's first line is a unique anchor in base.  The block
contains its own balanced `{ ... }` block.  Base contains `}`
elsewhere (every function close), so `_filter_new`'s naive
`stripped not in base` test would drop the body's `}`, leaving
the inserted `if (...) {` with no closer.  Post-fix: the closer
is preserved, the result is balanced, and the record writes.

## File: `panel.js`

Replace `if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;` with:

```js
  if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;
  if (e.inpaintFullCanvas && document.activeElement !== e.inpaintFullCanvas) {
    e.inpaintFullCanvas.checked = !!board.inpaintFullCanvas;
  }
```
