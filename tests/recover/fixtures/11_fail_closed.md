# Fixture 11 — fail-closed on pair failure

Tests that a replace pair whose old body does NOT match the
pre-existing baseline causes the record to be marked LOW trust
(fail-closed): the file goes to `low_trust/` and the target tree
is left untouched.

Runs in `--apply-to` mode with `--min-trust MEDIUM` (the default,
which is what fail-closed targets).  `--min-trust LOW` is the
escape hatch — this test asserts the default behaviour.

Expected: `sample.js` unchanged in the target tree; `sample.js`
present in `deeppeek/low_trust/`; manifest shows `trust=LOW` and
`pairs_incomplete=true`.

## File: `sample.js`

Replace the block:

```js
export const state = {
  value: 999,  // does NOT match the pre-existing value (1)
};
```

with:

```js
export const state = {
  value: 2,
  extra: "new",
};
```
