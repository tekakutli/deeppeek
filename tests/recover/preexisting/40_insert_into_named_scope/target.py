def handle_thing(payload: dict) -> dict:
    name = payload.get("name", "")
    try:
        timeout = float(payload.get("timeout", 10))
    except (TypeError, ValueError):
        timeout = 10
    return {"name": name, "timeout": timeout}


class MyHandler:
    @command(name="existing")
    def existing_cmd(self, params):
        return {"ok": True}
