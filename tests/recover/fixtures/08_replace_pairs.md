# Fixture 08 — replace-pair edits

Tests that "Replace X with Y" prose is understood as a pair, not as
a delete followed by a drop.  Runs in --apply-to mode: the
pre-existing `colors.js` under `preexisting/08_replace_pairs/` is
the base; the conversation only carries the pair.

## File: `colors.js`

### Dark theme

Replace the block:

```js
export const colors = {
  primary: "#ff0000",
  secondary: "#00ff00",
  accent: "#0000ff",
};
```

with:

```js
export const colors = {
  primary: "#88c0d0",
  secondary: "#a3be8c",
  accent: "#b48ead",
};
```
