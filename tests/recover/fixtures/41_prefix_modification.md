# Fixture 41 — prefix modification via ellipsis

Tests prefix-modification semantics: the body replaces one line
with two new lines, then uses a comment-wrapped ellipsis marker
(`// ... rest unchanged`) to signal that the rest
of the function body is untouched.  Pure-insert would duplicate
the original line; prefix-mod replaces it in place.

Reproduces the `computeMaskBBox` shape from the
pg_pixels.py failure: `const W = m.img.width, H = m.img.height;`
is replaced by two separate `const W = ...` /
`const H = ...` declarations.

## File: `compute.js`

Replace `computeW` with:

```js
export function computeW(m) {
  if (!m) return null;

  const W = m.rotatedCanvas ? m.rotatedW : m.w;
  const H = m.rotatedCanvas ? m.rotatedH : m.h;
  // ... rest unchanged
}
```
