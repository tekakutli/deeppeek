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
methods frequently delegate.  For each `@command` method, also
collect `params.get(...)` reads from any direct delegate
`self.X(params)`.  One level deep.

WARN-ONLY (2026-10-28 revision)
===============================
A prior version of `pass8_4_lint_command_args` demoted the record to
LOW when a patch introduced an `@command` arg that the body never
read.  That demotion — together with `verify-applied`'s — was
producing exactly the failure mode it was meant to prevent.  The
contract is: `deeppeek` applies patches; if the conversation added
an arg and did not wire it, the file ships with the dead arg and a
warning surfaces the gap.

PASS 8.4b — ADDED PARAMETERS NEVER REFERENCED IN THE BODY
=========================================================
Companion check at the function-signature level.  Warn-only.

PRE-EXISTING SHORT-CIRCUIT (2026-11-05)
=======================================
`pass8_4b_check_added_unused_params` skips pre-existing records
that pass 6 marked `preexisting_unchanged`.  Their body has not
changed — no parameter could have been "added".  Without this
guard, every untouched pre-existing `.py` file in a large
`--apply-to` tree got two `ast.parse` calls (base + content)
for a check that could never fire.
"""

from __future__ import annotations

import ast
import re
from typing import Optional

from .core import FileRecord
from ._ast import parse_source


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
    """
    n_warned = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if not path.endswith(".py"):
            continue
        if rec.is_deleted:
            continue
        # NEW (2026-11-05): skip untouched pre-existing records.
        # `pass6` marks them; the body has not changed, so no
        # `@command` decorator was added by this run — a violation
        # that only fires on *introduced* args cannot fire.  This
        # is the same guard `pass8_4b_check_added_unused_params`
        # already had; 8.4 was missed in the 2026-11-05 sweep.
        # On a 300-file untouched tree this removes ~300 file-level
        # regex passes that could only discover zero violations.
        if rec.verification.get("preexisting_unchanged"):
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
    """
    if not base or not content:
        return []
    try:
        base_tree = parse_source(base)
    except SyntaxError:
        return []
    try:
        content_tree = parse_source(content)
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
      · pre-existing records that pass 6 marked `preexisting_unchanged`
        (2026-11-05): nothing was added, nothing to check
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
        # synthetic diff.
        base: Optional[str] = None
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block is not None:
                base = base_block.body
        if base is None:
            continue

        # NEW (2026-11-05): skip untouched pre-existing records.
        # `pass6` marks them; their body hasn't changed, so no
        # parameter could have been "added".  Without this guard,
        # every untouched pre-existing `.py` file in a large
        # `--apply-to` tree paid two `ast.parse` calls
        # (`_find_added_unused_params(base, content)`) for a check
        # that could never fire.
        if rec.verification.get("preexisting_unchanged"):
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
