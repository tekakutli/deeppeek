"""recover.lint — Pass 8.4: post-apply lint for `@command` decorators.

An `@command(...)` decorator that declares `--foo` but whose method
body never reads `--foo` from `params` is an incomplete feature: the
arg is documented and accepted, but does nothing.  This is a distinct
class of failure from "symbol unresolved" (pass 8.5) or "content
invalid" (pass 8.5.5) — a syntactically-valid, name-resolved function
that silently ignores half its contract.

Motivating case (2026-10-28 comfyui.py):

    @command(args={
        ...
        "--output_rotation": {"type": "float", "default": 0.0, ...},
    })
    def comfy_generate(self, params):
        ...
        output_name = params.get("output_name") or None
        # NOTE: params.get("output_rotation") is never read.

The patch that added the arg declaration applied; the patch that
would have added the `params.get` call was excluded during
reconciliation.  The result compiles, the manifest reports HIGH
trust, and the feature is silently dead.

This pass is intentionally narrow: it only runs on `.py` files, only
inspects `@command`-decorated methods, and only reads the decorator
dict's arg keys against `params.get(...)`/`params[...]` usages in the
method body.  False positives would be code that destructures params
by some other mechanism (`**params`, `params.pop`, a helper).  Those
exist; the caller should follow up any hit rather than trusting it
blindly.

Fail-closed when the arg was introduced *by a patch applied this
run*: the record goes LOW so the target tree is preserved.  Warn-only
when the arg pre-existed (an old stale arg is not this run's
responsibility).

See the module docstring of `pipeline.py` for the full pass list and
where this fits in the trust pipeline.
"""

from __future__ import annotations

import re
from typing import Optional

from .core import FileRecord


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


def _find_command_violations(content: str) -> list[dict]:
    """Return one dict per `@command` whose declared args are
    unused by its method body.

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
    not the declaration — that is the fix, not the defect.  If we
    flagged both, a patch that ADDED the missing wiring would be
    treated as having introduced the unused arg, which is exactly
    backwards.

    Motivating case: file has `@command(args={"--foo": ...})` with a
    body that does not read `--foo` (pre-existing defect).  A
    conversation patch adds `params.get("foo")` (the fix).  Without
    this distinction, the patch's body mentions "foo" and the lint
    pass would demote the record — flagging the fix as the bug.
    """
    import re as _re
    pattern = _re.compile(
        r'["\'](?:-{1,2})?' + _re.escape(arg) + r'["\']\s*:'
    )
    for p in rec.patches:
        body = p.get("body") or ""
        if pattern.search(body):
            return True
    return False


def pass8_4_lint_command_args(records: dict[str, FileRecord]) -> int:
    """Scan every recovered .py file's `@command` methods.

    For each declared arg that the method body never reads:
      · If a patch introduced the arg this run → demote the record to
        LOW trust and append a warning.  Fail-closed.
      · Otherwise → append a warning only.  Pre-existing dead args are
        not this run's defect; don't fail-close a record over them.

    Returns the number of records demoted to LOW.
    """
    n_demoted = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if not path.endswith(".py"):
            continue
        if rec.trust == "LOW":
            continue
        if rec.is_deleted:
            continue

        try:
            violations = _find_command_violations(rec.content)
        except Exception as e:
            # A lint crash must not take down the pipeline.  Warn and
            # move on; the record's trust is unchanged.
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

            rec.trust = "LOW"
            rec.verification.setdefault(
                "unused_command_args", []).append({
                    "method": method,
                    "unused": introduced,
                })
            rec.warnings.append(
                f"lint: {method}() declares args never read from "
                f"params: {introduced} — a patch added the "
                f"declaration but the wiring did not apply; "
                f"fail-closing to preserve the target tree"
            )
            n_demoted += 1
            print(f"  [lint] {path}: {method}() has unused arg(s) "
                  f"{introduced}; demoted to LOW", flush=True)
            # One demotion per record is enough; subsequent violations
            # are covered by the same "record not written" outcome.
            break

    return n_demoted
