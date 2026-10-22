# Fixture 07 - deletion shapes

Tests the four patch shapes that make up the panel-fade
conversation: a raw code block with "Delete the ..." prose, and a
diff fence that deletes the same block.  Both must land
successfully; the second must be `already-present`, not `failed`.
The pre-existing baseline comes from `--apply-to`, so the deletion
runs against a real on-disk file.

Expected: `sample.js` no longer contains the panelState block;
trust stays HIGH; no patch is `failed`.

## File: `sample.js`

Delete the `panelState` block:

```js
export const panelState = {
  hovered: false,
  lastActivity: 0,
};
```

## File: `sample.js`

Here it is as a diff, in case you want the patch form:

```diff
 export function keepMe() {
   return 42;
 }

-export const panelState = {
-  hovered: false,
-  lastActivity: 0,
-};
-
 export function alsoKeepMe() {
   return 99;
 }
```
