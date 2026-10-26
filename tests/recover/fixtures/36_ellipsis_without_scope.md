# Fixture 36 — ellipsis without scope (fallthrough)

Tests the ellipsis-fallthrough: the body contains `...` (meaning
"rest unchanged") but has NO scope declaration (no
function/class/const-object header).  `_try_ellipsis_splice`
cannot fire -- there is nothing to splice into -- so the fallthrough
strips the `...` lines and retries `_try_deterministic_patch_inner`
with the stripped body.  The remaining snippet then inserts via
context-insert (anchor-last).

Without the fallthrough, this body fails closed: the raw `...`
line is not in base, the body has no scope, and every insert
heuristic refuses.

## File: `view.js`

Add an early-out before the existing guard:

```js
if (board.inpaintFullCanvas) return;
if (!board.imageEl) return;
...
```
