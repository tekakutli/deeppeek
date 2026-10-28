"""recover.lint — Pass 8.4: post-apply lint for `@command` decorators,
and Pass 8.4b: post-apply lint for added-but-unused parameters.

An `@command(...)` decorator that declares `--foo` but whose method
body never reads `--foo` from `params` is an incomplete feature: the
arg is documented and accepted, but does nothing.

Motivating case (2026-10-28 comfyui.py):

    @command(args={
        ...
        "--output_rotation": {"type": "float", "default": 0.0, ...},
    })
    def comfy_generate(self, params):
        ...
        # NOTE: params.get("output_rotation") is never read here.

The patch that added the arg declaration applied; the patch that
would have added the `params.get` call was excluded during
reconciliation.  The result compiles, the manifest reports HIGH
trust, and the feature is silently dead.

DELEGATION AWARENESS (2026-10-28f)
==================================
The lint above is a *shallow* check: it only looks at
`params.get(...)` reads inside the method's own body.  But `@command`
methods frequently delegate:

    @command(args={"--output_rotation": {...}})
    def comfy_generate(self, params):
        return self._comfy_generate_impl(params)

    def _comfy_generate_impl(self, params):
        output_rotation = float(params.get("output_rotation") or 0.0)
        ...

Shallow lint flags `comfy_generate` because the method's body has no
`params.get("output_rotation")`.  In the wild, this produced a false
positive on `comfy_generate` and would have shipped a correct
implementation as LOW trust.

Fix: for each `@command` method, also collect `params.get(...)` reads
from any direct delegate `self.X(params)`.  One level deep — deep
recursion would follow mutual delegations indefinitely; one level
matches the common "command method delegates to a helper" pattern.
A delegate that itself delegates is not scanned: the caller's intent
is clear from the first hop.

WARN-ONLY (2026-10-28 revision)
===============================
A prior version of `pass8_4_lint_command_args` demoted the record to
LOW when a patch introduced an `@command` arg that the body never
read.  That demotion — together with `verify-applied`'s — was
producing exactly the failure mode it was meant to prevent:
`comfyui.py` was routed into `low_trust/` for `--output_rotation`
even though every patch had landed cleanly.

The contract is: `deeppeek` applies patches.  If the conversation
added an arg and did not wire it, the file ships with the dead arg,
and a warning surfaces the gap.  Gating the write on the conversation
having been complete is a category error — it converts a reportable
fact into a pipeline failure.

`pass8_4` now emits a warning (and `verification.unused_command_args`
for auditability) and never touches `rec.trust`.

PASS 8.4b — ADDED PARAMETERS NEVER REFERENCED IN THE BODY
=========================================================
Companion check at the function-signature level.  A function that
gained a parameter (via a patch) whose body never references the
parameter is suspect — the parameter is accepted and discarded.
Warn-only, same reasoning as `pass8_4`: an interface-compatibility
signature legitimately gains unused parameters.

Idempotency: warnings keyed on `f"{function}.{param}"` are tracked in
`rec.verification["added_param_warnings"]`.  Re-running the pass on
the same content (via `reverify_reconciled` after reconcile) does not
duplicate warnings.
"""

from __future__ import annotations

import ast
import re
from typing import Optional

from .core import FileRecord


# ============================================================================
#  Pass 8.4 — @command arg lint
# ============================================================================


# `@command(...)` followed by a `def name(self, params`.  The
# `.*?` in the args span is DOTALL; the group boundary is the
# `\n<indent>def` immediately before the def, so nested strings with
# `def` inside them do not fool it.
_COMMAND_DECORATOR_RE = re.compile(
    r'@command\('
    r'(?P<decorator_args>.*?)'
    r'\)\s*\n'
    r'(?P<indent>[ \t]*)'
    r'def\s+(?P<name>\w+)\s*\(\s*self\s*,\s*params',
    re.DOTALL,
)

# An arg key in the decorator's `args={...}` dict.  Optional leading
# `--` because the codebase is inconsistent (`"--foo"` vs `"-f"`).
_ARG_KEY_RE = re.compile(r'"(-{1,2}[A-Za-z_]\w*)"\s*:')

# A `params.get("key")` / `params["key"]` / `params.get('key', ...)`
# in the method body.  Accepts single or double quotes; accepts the
# key with or without leading `--` (both are common).
_PARAM_USAGE_RE = re.compile(
    r'params\s*(?:\.\s*get\s*\(\s*|\[\s*)'
    r'(["\'])'
    r'(?:-{1,2})?([A-Za-z_]\w*)'
    r'\1'
)

# `self.X(params)` / `self.X(params, ...)` — a direct delegate call
# that receives the command method's `params` object unmodified.
# Excludes `self.X(params.get(...))` (that's an *extraction* call, not
# a delegation of the whole params dict — the callee sees the read
# result, not `params` itself).
_DELEGATE_CALL_RE = re.compile(
    r'self\.([A-Za-z_]\w*)\s*\(\s*params\s*[,)]'
)

# A subsequent method or decorated callable, to bound the method body.
_NEXT_METHOD_RE = re.compile(
    r'\n[ \t]{0,8}(?:@\w+|def\s+\w+|class\s+\w+)',
    re.MULTILINE,
)


def _extract_method_body(content: str, start: int) -> str:
    """Return the body text from `start` up to the next top-level
    method/class/decorator boundary, or end-of-file."""
    m = _NEXT_METHOD_RE.search(content, start)
    return content[start:m.start()] if m else content[start:]


def _find_method_body(content: str, method_name: str) -> Optional[str]:
    """Locate `def <method_name>(self, ...)` in `content` and return
    its body text, or None if the method isn't found.

    Matches methods at indent 0–8 (i.e. inside a class, one level).
    """
    pat = re.compile(
        r'(?m)^[ \t]{0,8}(?:async\s+)?def\s+' + re.escape(method_name) +
        r'\s*\(\s*self\b'
    )
    m = pat.search(content)
    if not m:
        return None
    return _extract_method_body(content, m.end())


def _collect_direct_delegates(content: str, method_body: str) -> list:
    """Return the bodies of every `self.X(params)` callee reachable
    from `method_body`, one level deep.

    The motivating case is `def comfy_generate(self, params):
    return self._comfy_generate_impl(params)` — the arg reads happen
    in `_comfy_generate_impl`, not in the method body.  Without this,
    the lint would falsely flag every arg on `comfy_generate` as
    unused.
    """
    bodies = []
    for m in _DELEGATE_CALL_RE.finditer(method_body):
        delegate_body = _find_method_body(content, m.group(1))
        if delegate_body is not None:
            bodies.append(delegate_body)
    return bodies


def _find_command_violations(content: str) -> list[dict]:
    """Return one dict per `@command` whose declared args are
    unused by its method body (or by one level of direct delegates).

    Each dict has:
        name           — method name
        declared       — set of arg keys the decorator declares
        unused         — set of arg keys the body never reads
    """
    violations: list[dict] = []

    for m in _COMMAND_DECORATOR_RE.finditer(content):
        name = m.group("name")
        decorator_args = m.group("decorator_args")
        method_body = _extract_method_body(content, m.end())

        declared = {
            key.lstrip("-") for key in
            _ARG_KEY_RE.findall(decorator_args)
        }
        if not declared:
            continue

        used = {
            match.group(2) for match in
            _PARAM_USAGE_RE.finditer(method_body)
        }

        # DELEGATION AWARENESS (2026-10-28f): follow direct
        # `self.X(params)` callees to find reads that happen in a
        # helper.
        for delegate_body in _collect_direct_delegates(
                content, method_body):
            used.update(
                match.group(2) for match in
                _PARAM_USAGE_RE.finditer(delegate_body)
            )

        unused = declared - used
        if unused:
            violations.append({
                "name": name,
                "declared": sorted(declared),
                "unused": sorted(unused),
            })

    return violations


def _arg_introduced_by_patch(rec: FileRecord, arg: str) -> bool:
    """True iff any patch body in `rec` DECLARES `arg` — i.e. contains
    `"--arg":` (dict-key form), not merely a mention of
    `params.get("arg")` or `"--arg"` inside a help string.

    A patch that adds `"--foo": {...}` to the decorator introduced the
    arg.  A patch that adds `params.get("foo")` provided the *wiring*,
    not the declaration — that is the fix, not the defect.
    """
    pattern = re.compile(
        r'["\'](?:-{1,2})?' + re.escape(arg) + r'["\']\s*:'
    )
    for p in rec.patches:
        body = p.get("body") or ""
        if pattern.search(body):
            return True
    return False


def pass8_4_lint_command_args(records: dict[str, FileRecord]) -> int:
    """Scan every recovered .py file's `@command` methods.  Warn —
    never demote — for each declared arg that the method body never
    reads.

    Returns the number of warnings emitted (not demotions).

    The demotion branch that used to live here (patch added a
    declaration but the wiring didn't apply → demote to LOW) was
    removed alongside `verify-applied`'s demotion.  Both were
    attempting to gate the write on the conversation having been
    complete; the record should ship with the incomplete feature and
    the warning should surface the gap for human review.

    Records already marked LOW for other reasons are also processed:
    a LOW record whose `@command` has an unused arg deserves the
    warning too, for the manifest's accuracy.
    """
    n_warned = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if not path.endswith(".py"):
            continue
        if rec.is_deleted:
            continue

        try:
            violations = _find_command_violations(rec.content)
        except Exception as e:
            rec.warnings.append(f"pass 8.4 lint raised: {e}")
            continue

        for v in violations:
            method = v["name"]
            unused = v["unused"]
            introduced = [
                arg for arg in unused
                if _arg_introduced_by_patch(rec, arg)
            ]
            if not introduced:
                rec.warnings.append(
                    f"lint: {method}() declares args never read from "
                    f"params: {unused} (pre-existing)"
                )
                continue

            rec.verification.setdefault(
                "unused_command_args", []).append({
                    "method": method,
                    "unused": introduced,
                })
            rec.warnings.append(
                f"lint: {method}() declares args never read from "
                f"params: {introduced} — a patch added the "
                f"declaration but the wiring did not apply "
                f"(informational; the file is written)"
            )
            n_warned += 1
            print(f"  [lint] {path}: {method}() has unused arg(s) "
                  f"{introduced} (informational)", flush=True)

    return n_warned


# ============================================================================
#  Pass 8.4b — added parameters never referenced in the body
# ============================================================================


def _collect_functions(tree: ast.AST) -> dict:
    """Return `{qualified_name: {params, body_refs, lineno}}` for
    every function in `tree`.

    `qualified_name` is `'Class.method'` for methods,
    `'outer.inner'` for nested functions, `'func'` for module-level
    functions.  Disambiguation by qualified name means two classes
    with same-named methods don't collide.

    `params` is the set of parameter names (positional-only,
    positional-or-keyword, keyword-only, plus `*args`/`**kwargs`
    captured by their actual names).  `body_refs` is the set of all
    `ast.Name` identifiers appearing anywhere in the function body,
    including nested lambdas and comprehensions.  Over-inclusive by
    design: false negatives (identifiers that look like parameter
    references but aren't) are preferable to false positives in a
    warn-only pass.
    """
    out: dict = {}

    def _walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = prefix + child.name
                params = set()
                for a in (child.args.posonlyargs
                          + child.args.args
                          + child.args.kwonlyargs):
                    params.add(a.arg)
                if child.args.vararg:
                    params.add(child.args.vararg.arg)
                if child.args.kwarg:
                    params.add(child.args.kwarg.arg)

                refs = set()
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Name):
                        refs.add(sub.id)

                out[qname] = {
                    "params": params,
                    "body_refs": refs,
                    "lineno": child.lineno,
                }
                _walk(child, qname + ".")
            elif isinstance(child, ast.ClassDef):
                _walk(child, prefix + child.name + ".")
            else:
                _walk(child, prefix)

    _walk(tree, "")
    return out


def _find_added_unused_params(base: str, content: str) -> list[dict]:
    """Return `[{function, param, line}]` for each parameter that
    exists in `content`'s function signature but not in `base`'s, and
    that the body never references.

    Only functions present in both trees are compared — a function
    that only exists in `content` has every parameter "new", which
    is not a signal.

    Fail-open: a syntax error in either side returns `[]`.
    """
    if not base or not content:
        return []
    try:
        base_tree = ast.parse(base)
    except SyntaxError:
        return []
    try:
        content_tree = ast.parse(content)
    except SyntaxError:
        return []

    base_fns = _collect_functions(base_tree)
    content_fns = _collect_functions(content_tree)

    out = []
    for qname, c in content_fns.items():
        b = base_fns.get(qname)
        if b is None:
            continue
        added = c["params"] - b["params"]
        if not added:
            continue
        for p in added:
            if p not in c["body_refs"]:
                out.append({
                    "function": qname,
                    "param": p,
                    "line": c["lineno"],
                })
    return out


def pass8_4b_check_added_unused_params(
        records: dict[str, FileRecord],
        blocks_by_idx: Optional[dict] = None) -> int:
    """Warn when a function gained a parameter (via a patch) that
    the function body never references.

    WARN-ONLY.  Returns the number of *new* warnings emitted this
    call.  Idempotent: repeated runs against the same record do not
    duplicate warnings, tracked via
    `verification["added_param_warnings"]`.

    Skips:
      · non-`.py` records
      · records with no content or no recoverable base
      · deleted records

    The base is `blocks_by_idx[rec.last_full_idx].body` — the
    pre-application content.  `pass6_apply_patches` and every demoting
    pass compare against the same base; consistency matters here.
    """
    n_warned = 0

    for path, rec in records.items():
        if rec.content is None:
            continue
        if not path.endswith(".py"):
            continue
        if rec.is_deleted:
            continue

        # Get base content.  Prefer last_full_idx block.  `None`
        # means we cannot compare — skip rather than warn on a
        # synthetic diff (e.g. record with no full-print baseline).
        base: Optional[str] = None
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block is not None:
                base = base_block.body
        if base is None:
            continue

        try:
            added_unused = _find_added_unused_params(base, rec.content)
        except Exception as e:
            rec.warnings.append(f"pass 8.4b raised: {e}")
            continue

        if not added_unused:
            continue

        # Idempotent warning tracking — a re-run (from reverify after
        # reconcile) must not duplicate entries.
        existing = set(rec.verification.get("added_param_warnings", []))

        for v in added_unused:
            key = f"{v['function']}.{v['param']}"
            if key in existing:
                continue
            existing.add(key)
            rec.warnings.append(
                f"added-param-unused: `{v['function']}` (line "
                f"{v['line']}) gained parameter `{v['param']}` but "
                f"the function body never references it — the "
                f"argument is accepted and silently discarded"
            )
            n_warned += 1

        if existing:
            rec.verification["added_param_warnings"] = sorted(existing)

    return n_warned
