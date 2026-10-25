# Fixture 16 — silent drop now fails closed

Tests that a `partial` block with code signal that cannot be
applied is *attempted* (not silently dropped), fails all
heuristics, and drives the record to LOW trust under default
--min-trust MEDIUM.  Target is preserved.

The block below is a helper field registration.  It has code
signal (`pgkitRegisterField(`, `=>`, `:`) but no scope-decl
at its start, no anchor line in base, and no backtick anchor
in prose.  No heuristic can place it.  Before the code-signal
policy, a block without an add-family verb in prose would have
been silently dropped.  Now it is attempted and fails closed.

## File: `helper.js`

Add the following field registration:

```js
pgkitRegisterField("helper.newField", {
  get: () => helper.newField,
  set: (v) => { helper.newField = v; },
  meta: { kind: "bool", label: "New field" },
});
```
