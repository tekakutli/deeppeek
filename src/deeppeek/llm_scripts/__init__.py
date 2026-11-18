"""llm_scripts — LLM prompt-script registry.

Each script is a standalone Python file in this directory (bundled
scripts) or in ~/.deeppeek/scripts/ (user scripts, override bundled
by name).  Files are plain Python modules; the framework imposes one
required contract and several optional hooks:

    def build_prompt(input_value, ctx) -> str           # required
    def on_response(response, ctx) -> Any | None         # optional
    def register_flags(parser) -> None                   # optional
    def ctx_from_args(args) -> dict                      # optional
    requires_llm = bool                                  # optional

`register_flags` is invoked with the `editor` subparser *before*
argparse runs, and only when this script is the target of
`--script <NAME>`.  It is how a script installs its own flags —
they exist only for this script, never in `editor --help` for any
other script, never in parsing.

`ctx_from_args` is the paired extractor: `editor` calls it once
after `parse_args` succeeds, and merges the returned dict into the
ctx passed to `build_prompt` / `on_response`.  A script that
implements `register_flags` SHOULD also implement `ctx_from_args` —
otherwise its flags parse but silently do nothing (`editor` warns
on this mismatch).

`requires_llm = False` skips `client.call` entirely: the framework
invokes `build_prompt` (whose return value is discarded) and then
`on_response` with an empty string.  Useful for scripts that solve
their problem deterministically — `complete_paths` is the reference
implementation.

Scripts may import stdlib, read files from disk, write files into
ctx["session_dir"], run subprocesses, or do anything else Python can
do.  The framework's only contract is the hooks above — everything
else lives entirely in the script.

## Composition: a script calling another script

`deeppeek editor --script <NAME>` runs exactly one script.  If that
script wants another script's work, it calls the other one directly:

    from deeppeek import llm_scripts
    result = llm_scripts.run_script("complete_paths", "foo.js", ctx)

That is the sole composition mechanism.  No `--script A --script B`
CLI chaining — composition lives in the *code* of the outer script.
A script can call `run_script` zero, one, or many times, in a loop,
conditionally, on transformed inputs — it's ordinary Python.  The
callee may itself call `run_script(...)` for *its* dependencies,
and so on.  Values flow between calls as in-process Python
objects; there is no stdout round-trip for intermediates.

`run_script` builds a sub-ctx from the caller's `ctx`:

    script         → callee name
    parent_script  → caller's ctx["script"]
    is_last        → False  (the callee's output is being
                             consumed by code, not printed to a
                             user, so the callee emits a
                             pipeline-friendly value rather than
                             a display string)
    input_kind     → "json" if input_value is non-str, else "text"

Everything else in the caller's ctx (including keys set by
`ctx_from_args`, `chat_title`, `_llm_client`, etc.) is inherited.
The callee's `build_prompt` + optional LLM + optional
`on_response` run, and `run_script` returns the callee's
`on_response` value (or the raw LLM response if no
`on_response`).  Never returns None.

The `editor` command injects the active `LLMClient` as
`ctx["_llm_client"]` so nested `run_script` calls to LLM-backed
scripts work transparently — the caller does not need to thread a
client through.

## ctx keys

The `ctx` dict carries:

    script           str        — script name (this module's NAME)
    model            str        — model being called
    session_dir      Path       — per-invocation /tmp directory
    timestamp        str        — ISO 8601 UTC
    chat_title       str|None   — from __dsx__.getChatTitle()
    chat_url         str|None
    turn_number      int|None   — 1-based
    total_turns      int|None
    message_index    int|None   — 1-based
    total_messages   int|None
    is_first         bool       — True for the top-level CLI call
    is_last          bool       — True for the top-level CLI call;
                                  False when invoked via run_script
    input_kind       str        — "text" | "json"
    _llm_client      LLMClient  — for nested run_script calls

…plus whatever `ctx_from_args` contributed, plus anything inherited
from a parent's ctx when this script was called via `run_script`.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_BUNDLED_DIR = Path(__file__).parent
_USER_DIR = Path.home() / ".deeppeek" / "scripts"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(
        f"deeppeek_script_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load script from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "build_prompt", None)):
        raise ImportError(
            f"script {path.name} has no callable build_prompt")
    if not getattr(mod, "NAME", None):
        mod.NAME = path.stem
    return mod


def _discover(directory: Path) -> dict:
    out = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            mod = _load_module(path)
            out[mod.NAME] = mod
        except Exception as e:
            print(f"[llm_scripts] skipping {path.name}: {e}",
                  file=sys.stderr)
    return out


_registry = None


def _build_registry() -> dict:
    reg = _discover(_BUNDLED_DIR)
    reg.update(_discover(_USER_DIR))
    return reg


def list_scripts() -> dict:
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def is_script(name: str) -> bool:
    return name in list_scripts()


def load_script(name: str):
    reg = list_scripts()
    if name not in reg:
        raise KeyError(
            f"unknown script: {name!r} (have: "
            f"{', '.join(sorted(reg)) or '(none)'})")
    return reg[name]


def build_ctx(*, script: str, model: str,
              session_dir: Path = None, **extra) -> dict:
    ts = datetime.now(timezone.utc)
    if session_dir is None:
        session_dir = (
            Path("/tmp")
            / f"deeppeek_answer_{ts.strftime('%Y%m%d_%H%M%S')}")
    session_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "script": script,
        "model": model,
        "session_dir": session_dir,
        "timestamp": ts.isoformat(),
    }
    ctx.update(extra)
    return ctx


def run_script(name, input_value, ctx, *,
               client=None, max_tokens=2048, temperature=0.2):
    """Invoke a registered script and return its output.

    The sole composition primitive.  Called from within a script's
    `build_prompt` or `on_response` to delegate work to another
    script in-process.  No stdout, no CLI parsing; values flow as
    plain Python objects.

    Builds a sub-ctx from `ctx`:

        script         → name (callee)
        parent_script  → ctx["script"] (caller)
        is_last        → False  (callee emits a pipeline-friendly
                                 value, not a display string)
        input_kind     → auto-derived: "json" if input_value is
                         non-str, else "text"

    All other keys (session_dir, model, chat_*, _llm_client, plus
    whatever the caller's ctx_from_args produced) are inherited.
    The caller may mutate the ctx it passes in to override keys for
    the sub-call.

    For LLM-backed callees, a client comes from the `client=` kwarg
    or `ctx["_llm_client"]` (which `editor` injects).  A callee
    with `requires_llm = False` skips the LLM entirely.

    Returns the callee's `on_response` value, or the raw LLM
    response if the callee defines no `on_response`.  Never returns
    None.
    """
    mod = load_script(name)

    sub_ctx = dict(ctx)
    sub_ctx["parent_script"] = ctx.get("script")
    sub_ctx["script"] = name
    sub_ctx["is_last"] = False
    sub_ctx["input_kind"] = (
        "text" if isinstance(input_value, (str, type(None)))
        else "json")

    prompt = mod.build_prompt(input_value, sub_ctx)

    if getattr(mod, "requires_llm", True):
        if client is None:
            client = ctx.get("_llm_client")
        if client is None:
            raise RuntimeError(
                f"run_script({name!r}) needs an LLM client — "
                f"pass client= or set ctx['_llm_client']")
        response = client.call(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            tag=f"nested:{name}",
        )
        if response is None:
            raise RuntimeError(
                f"LLM returned None for nested script {name!r}")
    else:
        response = ""

    on_response = getattr(mod, "on_response", None)
    if callable(on_response):
        output = on_response(response, sub_ctx)
        if output is None:
            output = response
    else:
        output = response
    return output
