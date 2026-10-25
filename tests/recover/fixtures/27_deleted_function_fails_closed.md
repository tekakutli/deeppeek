# Fixture 27 — deleted function fails-closed

Base has two exported functions inside a Python string.  The
conversation's only block is empty (simulating a scoped-replace
that deletes its target).  `validate_content` must catch the
missing declaration and route the file to `low_trust/`.

## File: `deleted.py`

```js

```
