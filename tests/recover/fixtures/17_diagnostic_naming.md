# Fixture 17 — patch failure diagnostics name the block

Tests that a patch that cannot apply reports the block's first
line and a specific failure reason, so the user can find the
block in the conversation and fix it.

The block below is an `export const` splice that cannot be
applied because there is no matching base declaration for
`totallyNew`.

## File: `helper.js`

Add the following new export block:

```js
export const totallyNew = {
  value: 42,
};
```
