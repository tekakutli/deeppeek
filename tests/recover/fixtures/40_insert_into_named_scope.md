# Fixture 40 — insert into named scope

Two indented bodies, each with prose that names the container.
Exercises `_try_insert_into_named_scope`.

## File: `target.py`

In `handle_thing`, after the existing `payload.get` reads:

```python
    try:
        extra = float(payload.get("extra") or 0.0)
    except (TypeError, ValueError):
        extra = 0.0
```

Add the following method to `MyHandler`:

```python
    @command(name="new")
    def new_cmd(self, params):
        return {"ok": True}
```

