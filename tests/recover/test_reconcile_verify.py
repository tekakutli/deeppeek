"""Regression tests for reconciliation and post-reconcile verification.

  · Reconcile's failure counter (`_count_failures`) no longer includes
    lint.  A record whose only "defect" is an unused `@command` arg
    is not blocked from promotion.  Lint is warn-only (see
    `lint.py`'s docstring); reconcile's job is to rescue records that
    are actually broken, not records with informational warnings.

  · `reverify_reconciled` re-runs every demoting pass on every
    promoted record.  Post-2026-10-28 alignment, the only demoting
    pass left is `pass8_5` (symbols).  Lint is called for warning
    parity but does not affect trust.

  · Distinctive scope-name misattribution: a function whose body
    lives in another record is reported as "content matches
    <other>", not as an opaque "unresolved symbol".

  · Regression guard for `_scope_name_lives_elsewhere`'s single-scope
    requirement: a body declaring 10 helper functions must not
    trigger the distinctive-scope fast path.

TEST-DATA NOTE
==============
`test_reconcile_ignores_lint_for_promotion` uses the multi-line
`@command(\n    args={\n        ...\n    },\n)` format.  The `args`
keyword must start its own line (after optional whitespace) for
`_dict_var_regex` / `_find_args_in_command_decorator_of` to locate
the dict — the exact format real `@command` decorators use in the
target tree.  A single-line `@command(args={...})` base is not a
shape the patch body can be replayed into, so reconcile has no
exclusion set to consider and cannot promote; that would be a
test-data artefact, not a lint-related failure.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.reconcile import (
    _fuzzy_locate_elsewhere, pass8_7_reconcile,
)
from deeppeek.recover.reverify import reverify_reconciled


def _base_block(body, idx=0, path="x.py", lang="py"):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang=lang,
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _rec(path, content=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    return rec


# ---------------------------------------------------------------------------
# Reconcile: lint is not part of the failure counter
# ---------------------------------------------------------------------------

def test_reconcile_ignores_lint_for_promotion():
    """Reconcile's failure counter no longer includes lint.  A record
    whose only "defect" is an unused @command arg is not treated as
    a failure.  Reconcile promotes it (to MEDIUM); the warning
    surfaces the incomplete feature."""
    base_body = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(\n'
        '    args={\n'
        '        "--foo": {"type": "str"},\n'
        '    },\n'
        ')\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    base_block = _base_block(base_body, idx=0, path="x.py", lang="py")

    # Same content as base + a `--bar` arg.  The patch body inserts
    # it inside the args dict.
    broken_content = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(\n'
        '    args={\n'
        '        "--foo": {"type": "str"},\n'
        '        "--bar": {"type": "int"},\n'
        '    },\n'
        ')\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.content = broken_content
    rec.patches = [{
        "idx": 1,
        "body": '"--bar": {"type": "int"},',
        "applied_by": "deterministic",
        "context_before": "Add to args:",
    }]
    rec.trust = "LOW"
    rec.verification["patches_applied"] = 1
    rec.verification["patches_incomplete"] = True

    records = {"x.py": rec}
    blocks_by_idx = {0: base_block}

    pass8_7_reconcile(records, blocks_by_idx, verbose=False)

    # Reconcile's decision is now driven by content_invalid /
    # unresolved_calls / failed patches, not by lint.  The content
    # here has none of those, so reconcile promotes.  The unused
    # arg is a warn-only concern.
    assert rec.trust in ("MEDIUM", "HIGH"), (
        f"reconcile should not demote on lint alone; got {rec.trust!r}; "
        f"verification: {rec.verification!r}"
    )


# ---------------------------------------------------------------------------
# Distinctive scope-name misattribution
# ---------------------------------------------------------------------------

def test_distinctive_scope_misattribution_detected():
    """A function whose body lives in another record is identified
    as misattributed, even when the body was modified enough to drop
    below 0.85 line overlap."""
    body = (
        'async function _clearImageAction() {\n'
        '  clearImage();\n'
        '  const rotEl = document.getElementById("viewRotation");\n'
        '  if (rotEl) rotEl.value = "0";\n'
        '  const rotVal = document.getElementById("viewRotationValue");\n'
        '  if (rotVal) rotVal.textContent = "0";\n'
        '  syncPanelFields();\n'
        '  syncMaskList();\n'
        '}\n'
    )
    other_content = (
        'async function _clearImageAction() {\n'
        '  clearImage();\n'
        '  syncPanelFields();\n'
        '  syncMaskList();\n'
        '}\n'
    )
    other_rec = _rec("pg_panel_bindings.py", other_content)
    current_rec = _rec("pg_panel_scrubs.py", "")

    records = {
        "pg_panel_scrubs.py": current_rec,
        "pg_panel_bindings.py": other_rec,
    }
    result = _fuzzy_locate_elsewhere(
        body, "pg_panel_scrubs.py", records)
    assert result == "pg_panel_bindings.py", (
        f"expected pg_panel_bindings.py, got {result!r}"
    )


def test_distinctive_scope_low_overlap_not_flagged():
    """Sanity: a distinct function with the same name but nearly no
    shared lines is NOT flagged as misattributed."""
    body = (
        'async function _clearImageAction() {\n'
        '  do_totally_different_thing_1();\n'
        '  do_totally_different_thing_2();\n'
        '  do_totally_different_thing_3();\n'
        '  do_totally_different_thing_4();\n'
        '}\n'
    )
    other_content = (
        'async function _clearImageAction() {\n'
        '  clearImage();\n'
        '  syncPanelFields();\n'
        '}\n'
    )
    other_rec = _rec("pg_panel_bindings.py", other_content)
    current_rec = _rec("pg_panel_scrubs.py", "")
    records = {
        "pg_panel_scrubs.py": current_rec,
        "pg_panel_bindings.py": other_rec,
    }
    result = _fuzzy_locate_elsewhere(
        body, "pg_panel_scrubs.py", records)
    assert result is None, (
        f"expected None (low overlap), got {result!r}"
    )


def test_non_distinctive_scope_not_flagged():
    """A short, generic function name (`draw`) does not fire the
    distinctive-scope path."""
    body = (
        'function draw() {\n'
        '  paint();\n'
        '  flush();\n'
        '  sync();\n'
        '  doMoreThings();\n'
        '}\n'
    )
    other_content = (
        'function draw() {\n'
        '  paint();\n'
        '  flush();\n'
        '  sync();\n'
        '}\n'
    )
    other_rec = _rec("other.js", other_content)
    current_rec = _rec("current.js", "")
    records = {"current.js": current_rec, "other.js": other_rec}
    result = _fuzzy_locate_elsewhere(body, "current.js", records)
    assert result is None


# ---------------------------------------------------------------------------
# Reverify: lint is warn-only; symbols still demote
# ---------------------------------------------------------------------------

def test_reverify_does_not_demote_on_lint():
    """Reverify no longer treats lint as demoting.  A promoted record
    with an unused arg stays promoted."""
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.content = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '    "--bar": {"type": "int"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    rec.trust = "MEDIUM"
    rec.verification["reconciled"] = {"excluded": []}
    rec.patches = [{
        "idx": 1,
        "body": '"--bar": {"type": "int"},',
        "applied_by": "deterministic",
    }]

    records = {"x.py": rec}
    n = reverify_reconciled(records, blocks_by_idx=None)

    assert n == 0, "reverify must not demote on lint alone"
    assert rec.trust == "MEDIUM"
    # Warnings still added (informational parity with main pipeline).
    assert "unused_command_args" in rec.verification


def test_reverify_still_demotes_on_symbols():
    """The one demoting pass left in reverify still works."""
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.content = (
        "def helper(a):\n"
        "    return a\n"
        "\n"
        "missing_fn()\n"
    )
    rec.trust = "MEDIUM"
    rec.verification["reconciled"] = {"excluded": []}
    rec.patches = [{"idx": 1, "body": "x = 1",
                    "applied_by": "deterministic"}]

    records = {"x.py": rec}
    n = reverify_reconciled(records, blocks_by_idx=None)

    assert n == 1
    assert rec.trust == "LOW"
    assert "unresolved_calls" in rec.verification


def test_reverify_skips_unreconciled():
    """A record that was never reconciled (no `reconciled` field) is
    untouched, regardless of its other state."""
    rec = _rec("x.py", "def f():\n    return 1\n")
    rec.trust = "HIGH"
    records = {"x.py": rec}
    n = reverify_reconciled(records, blocks_by_idx=None)
    assert n == 0
    assert rec.trust == "HIGH"


def test_reverify_promotes_nothing():
    """Reverify demotes or does nothing; it never raises trust."""
    rec = FileRecord(path="x.py", state="SINGLE_FULL")
    rec.content = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    rec.trust = "MEDIUM"
    rec.verification["reconciled"] = {"excluded": []}
    rec.patches = [{
        "idx": 1,
        "body": '"--foo": {"type": "str"},',
        "applied_by": "deterministic",
    }]

    records = {"x.py": rec}
    n = reverify_reconciled(records, blocks_by_idx=None)

    assert n == 0, f"clean record should not be re-demoted, got {n}"
    assert rec.trust == "MEDIUM", (
        f"reverify must not promote, got {rec.trust!r}"
    )


def test_reverify_no_reconciled_records_returns_zero():
    """No-op when nothing has the `reconciled` field."""
    records = {
        "a.py": _rec("a.py", "def f():\n    return 1\n"),
        "b.py": _rec("b.py", "def g():\n    return 2\n"),
    }
    records["a.py"].trust = "HIGH"
    records["b.py"].trust = "LOW"
    n = reverify_reconciled(records, blocks_by_idx=None)
    assert n == 0


# ---------------------------------------------------------------------------
# Regression guard for the single-scope requirement
# ---------------------------------------------------------------------------

def test_fuzzy_scope_fast_path_requires_single_decl():
    """A body declaring multiple top-level functions does not trigger
    the distinctive-scope fast path, even if one of them shares a
    name with the other file."""
    body_lines = [
        f"function helper_{i}(x) {{ return x + {i}; }}"
        for i in range(10)
    ]
    body = "\n".join(body_lines) + "\n"

    other_lines = body_lines[:8] + [
        "function other_a() {}",
        "function other_b() {}",
    ]
    other_rec = _rec("other.js", "\n".join(other_lines) + "\n")
    current_rec = _rec("current.js", "")
    records = {"current.js": current_rec, "other.js": other_rec}

    result = _fuzzy_locate_elsewhere(body, "current.js", records)
    assert result is None, (
        f"multi-decl body should not trigger distinctive-scope path; "
        f"got {result!r}.  This is the regression that broke "
        f"test_patch_gates.py::test_fuzzy_long_body_uses_higher_threshold."
    )


def test_fuzzy_scope_fast_path_still_fires_for_single_decl():
    """Sanity: the single-scope path still fires for the motivating
    shape."""
    body = (
        'async function _clearImageAction() {\n'
        '  clearImage();\n'
        '  syncPanelFields();\n'
        '  syncMaskList();\n'
        '}\n'
    )
    other = _rec(
        "pg_panel_bindings.py",
        'async function _clearImageAction() {\n'
        '  clearImage();\n'
        '  syncPanelFields();\n'
        '  syncMaskList();\n'
        '}\n',
    )
    current = _rec("pg_panel_scrubs.py", "")
    records = {
        "pg_panel_scrubs.py": current,
        "pg_panel_bindings.py": other,
    }
    result = _fuzzy_locate_elsewhere(body, "pg_panel_scrubs.py", records)
    assert result == "pg_panel_bindings.py"


def main():
    tests = [
        ("reconcile ignores lint for promotion",
         test_reconcile_ignores_lint_for_promotion),
        ("distinctive scope misattribution detected",
         test_distinctive_scope_misattribution_detected),
        ("distinctive scope low overlap not flagged",
         test_distinctive_scope_low_overlap_not_flagged),
        ("non distinctive scope not flagged",
         test_non_distinctive_scope_not_flagged),
        ("reverify does not demote on lint",
         test_reverify_does_not_demote_on_lint),
        ("reverify still demotes on symbols",
         test_reverify_still_demotes_on_symbols),
        ("reverify skips unreconciled",
         test_reverify_skips_unreconciled),
        ("reverify promotes nothing",
         test_reverify_promotes_nothing),
        ("reverify no reconciled records returns zero",
         test_reverify_no_reconciled_records_returns_zero),
        ("fuzzy scope fast path requires single decl",
         test_fuzzy_scope_fast_path_requires_single_decl),
        ("fuzzy scope fast path still fires for single decl",
         test_fuzzy_scope_fast_path_still_fires_for_single_decl),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS: {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {name}\n        {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {name}\n         {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} reconcile-verify test(s) failed")
        return 1
    print(f"\nAll {len(tests)} reconcile-verify tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
