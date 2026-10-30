"""`deeppeek prompt` — AI prompt pipelines.

Subcommands are auto-discovered.  Every module under
`deeppeek.prompts` that:
  · does not start with `_` (private helper convention)
  · is a `.py` file (not a subpackage)
  · exposes a `register(subparsers)` function

becomes a `deeppeek prompt <module-name>` subcommand.  Adding a new
prompt is a one-file operation: drop `foo.py` into `prompts/` with
`register(subparsers)` and `run(args)` defined, and
`deeppeek prompt foo` becomes available on the next CLI invocation.
No edit to this file.

If a module cannot be imported or its `register()` raises, the
whole `deeppeek prompt` subcommand tree fails loudly with the
module path and the original exception.  A silent skip would leave
the operator staring at a missing subcommand with no clue why.

Adding a helper module shared between prompts?  Prefix it with `_`
(e.g. `_common.py`) — the underscore convention excludes it from
discovery.

The order in `deeppeek prompt --help` is alphabetical (deterministic
across machines and filesystems); if a semantic order becomes
important later, the discovery loop can sort by a module-level
`ORDER = N` attribute.
"""
import argparse
import importlib
import pkgutil
import sys


def _discover_prompts():
    """Yield `(name, module)` for each registerable prompt module.

    Skips `_`-prefixed helpers and subpackages; skips modules whose
    `register` attribute is missing or not callable (a helper that
    didn't follow the underscore convention).  Raises on import
    failure so the caller can surface the module path.
    """
    from .. import prompts as prompts_pkg
    for mod_info in sorted(
            pkgutil.iter_modules(prompts_pkg.__path__),
            key=lambda m: m.name):
        if mod_info.name.startswith("_"):
            continue
        if mod_info.ispkg:
            continue
        try:
            module = importlib.import_module(
                f"{prompts_pkg.__name__}.{mod_info.name}")
        except Exception as e:
            print(
                f"error: failed to import prompt module "
                f"`{prompts_pkg.__name__}.{mod_info.name}`: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            raise
        register = getattr(module, "register", None)
        if not callable(register):
            continue
        yield mod_info.name, module


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "prompt",
        help="AI prompt pipelines (elicit, await, apply)",
    )
    psub = p.add_subparsers(dest="prompt_cmd", required=True)

    for name, module in _discover_prompts():
        try:
            module.register(psub)
        except Exception as e:
            print(
                f"error: `{module.__name__}.register()` failed: "
                f"{type(e).__name__}: {e}\n"
                f"  module: {getattr(module, '__file__', '<unknown>')}",
                file=sys.stderr,
            )
            raise

    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return args.func(args)
