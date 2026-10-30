"""recover.startup_check — startup import sanity.

UNSTABLE.  New checks are likely to be added here as the codebase
grows; the existing checks (import-failure detection and unresolvable-
name detection) will remain, but new invariants will join them.

The check is a startup gate: if any `deeppeek.recover` module fails to
import, or references a name that is nowhere bound, the pipeline
refuses to run and prints one line per failure.  This catches the
exact class of regression that motivated its existence — a module
that imports cleanly (Python is lazy about function-body names) but
raises `NameError` on first call, silently defeating a pass for
months.

`RecoverConfig.skip_import_check` (see orchestration.py) lets a run
skip this pass for latency; every test invocation should set it.

MODULE-LEVEL CACHE (2026-11-05)
===============================
`_find_unresolved_module_names` walks the module's AST — potentially
thousands of nodes — every time it is called.  In the current
pipeline it is called exactly once per module per run, so the walk
cost is bounded by `n_modules × n_nodes`.  The `@lru_cache(maxsize=
None)` decorator does not make the *first* call any cheaper, but it
makes a re-call (from a test, from a second `validate_pipeline_imports`
invocation in the same process, from a diagnostic command that wants
to re-check one module) a dict lookup.

The module's source cannot change during a process, so the cache is
safe for the process lifetime.  The public wrapper converts the
cached tuple back to a list so existing callers see the pre-fix
return type.
"""

import ast
import builtins
import importlib
import pkgutil
import sys
from functools import lru_cache
from pathlib import Path

from ._ast import parse_source


@lru_cache(maxsize=None)
def _find_unresolved_module_names_cached(
        module_name: str) -> tuple:
    """Cached worker.  Returns a tuple so callers cannot mutate the
    cached value; the public wrapper converts to list."""
    return tuple(_find_unresolved_module_names_uncached(module_name))


def _find_unresolved_module_names(
        module_name: str) -> list:
    """Public wrapper over the cached worker.

    Returns a fresh list per call — the cached value is a tuple, so
    callers who do `result.append(...)` do not corrupt the cache.
    """
    return list(_find_unresolved_module_names_cached(module_name))


def _find_unresolved_module_names_uncached(
        module_name: str) -> list:
    """Walk `module_name`'s AST and return `[(lineno, name)]` for every
    name load that is not bound ANYWHERE in the module.

    Deliberately over-conservative: a name bound in one function and
    referenced in another is treated as resolved even though that may
    be a genuine cross-scope bug.  The motivating case (the 2026-11-05
    `structural_diff` regression, where six helper names were
    referenced but never defined anywhere) is caught; the subtle
    cross-scope case is not.

    Dynamic-binding escape hatches:
      * `globals()[...] = ...` assignment anywhere in the module.
      * A module-level `__getattr__`.
      * A star import (`from x import *`).
    """
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        return []
    source_file = getattr(mod, "__file__", None)
    if not source_file:
        return []
    try:
        source = Path(source_file).read_text()
    except OSError:
        return []
    try:
        tree = parse_source(source, filename=source_file)
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "globals"):
            return []

    for top in tree.body:
        if (isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef))
                and top.name == "__getattr__"):
            return []

    declared: set = set(dir(builtins))
    declared |= {
        "__name__", "__file__", "__doc__", "__all__", "__module__",
        "__package__", "__builtins__", "__annotations__", "__dict__",
        "__class__", "__spec__", "__loader__", "__qualname__",
        "__path__", "__subclasses__", "__instancecheck__",
    }

    def _collect_targets(target, out):
        if isinstance(target, ast.Name):
            out.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _collect_targets(elt, out)
        elif isinstance(target, ast.Starred):
            _collect_targets(target.value, out)

    def _add_args(args, out):
        for a in (args.posonlyargs + args.args + args.kwonlyargs):
            out.add(a.arg)
        if args.vararg:
            out.add(args.vararg.arg)
        if args.kwarg:
            out.add(args.kwarg.arg)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            declared.add(node.name)
            if node.args:
                _add_args(node.args, declared)
        elif isinstance(node, ast.Lambda):
            if node.args:
                _add_args(node.args, declared)
        elif isinstance(node, ast.ClassDef):
            declared.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                declared.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return []
                declared.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _collect_targets(t, declared)
        elif isinstance(node, ast.AnnAssign):
            _collect_targets(node.target, declared)
        elif isinstance(node, ast.AugAssign):
            _collect_targets(node.target, declared)
        elif isinstance(node, ast.NamedExpr):
            _collect_targets(node.target, declared)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect_targets(node.target, declared)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _collect_targets(item.optional_vars, declared)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                declared.add(node.name)
        elif isinstance(node, ast.comprehension):
            _collect_targets(node.target, declared)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                declared.add(n)

    unresolved: list[tuple[int, str]] = []
    seen: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            key = (node.lineno, node.id)
            if node.id not in declared and key not in seen:
                seen.add(key)
                unresolved.append(key)

    return unresolved


def validate_pipeline_imports() -> list:
    """Import every module in `deeppeek.recover` and collect any
    ImportError, plus any module whose AST references an unresolvable
    name.

    Fails open: if `pkgutil` itself raises (defensive), the function
    returns `[]` and the pipeline proceeds under the old contract — a
    validation-check bug must not block a run.
    """
    from importlib import import_module

    failures: list = []
    try:
        import deeppeek.recover as pkg
        modules = list(pkgutil.iter_modules(
            pkg.__path__, pkg.__name__ + "."))
    except Exception as e:
        print(f"[startup] pipeline-import enumeration raised: "
              f"{type(e).__name__}: {e} (proceeding)",
              file=sys.stderr)
        return failures

    for _, name, _ in modules:
        try:
            import_module(name)
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {e}")
            continue
        unresolved = _find_unresolved_module_names(name)
        if unresolved:
            sample = ", ".join(
                f"line {ln}: {n!r}" for ln, n in unresolved[:5])
            failures.append(
                f"{name}: unresolved name(s): {sample}"
                + (f" (+{len(unresolved) - 5} more)"
                   if len(unresolved) > 5 else "")
            )
    return failures
