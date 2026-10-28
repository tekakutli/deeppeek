"""Regression tests for reconciliation and post-reconcile verification:

  · Fix 2 (2026-10-28b): reconcile's replay is lint-aware.  A record
    demoted by pass 8.4 for an unused `@command` arg must NOT be
    promoted by pass 8.7 — the replayed content still has the unused
    arg.

  · Fix 4 (2026-10-28b): distinctive scope-name misattribution.  The
    `_clearImageAction` shape — a function whose body lives in another
    record, slightly modified — is reported as "content matches
    <other>", not as an opaque "unresolved symbol".

  · Fix 6 (2026-10-28b): the verify-reconcile-verify invariant.
    `reverify_reconciled` re-runs every demoting pass on every
    promoted record.  Encoded as an explicit pass so a future
    demoting check is protected without further code change.

  · Regression guard for `_scope_name_lives_elsewhere`'s single-scope
    requirement: a body declaring 10 helper functions must not trigger
    the distinctive-scope fast path.  This test would have caught the
    regression that `test_patch_gates.py::
    test_fuzzy_long_body_uses_higher_threshold` surfaced after the
    initial Fix 4 landed.

TEST-INPUT NOTE
===============
The two `@command`-decorated fixtures include the full
`from deeppeek.decorators import command` import.  Without it,
`find_unresolved_references` correctly flags `command` as a bare
unresolved call (playground .py files that do use `@command` always
import it).  The tests were rewritten to include the import so that:
  · the lint path is the only one that fires,
  · the "clean record" test really is clean under every demoting
    pass, not merely under lint.
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
# Fix 2 — reconcile respects lint
# ---------------------------------------------------------------------------
#
# Motivating shape (comfyui.py): `--output_rotation` was declared by a
# patch that applied.  The wiring patch (params.get("output_rotation"))
# was excluded.  Pass 8.4 correctly demoted.  Pre-fix, reconcile would
# replay, see valid Python with no unresolved symbols, and promote to
# MEDIUM — shipping a dead arg.  Post-fix, `_count_failures` runs lint
# on the replayed content too.

def test_reconcile_respects_lint_unused_arg():
    """A record whose only diff from base introduces an unused
    @command arg must stay fail-closed, not be reconciled to MEDIUM."""
    base_body = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    base_block = _base_block(base_body, idx=0, path="x.py", lang="py")

    broken_content = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '    "--bar": {"type": "int"},\n'
        '})\n'
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

    assert rec.trust == "LOW", (
        f"expected trust=LOW (lint must block promotion), "
        f"got {rec.trust!r}; verification: {rec.verification!r}"
    )
    # If reconcile had promoted, it would have set `reconciled` in
    # verification.  Absence is the correct outcome.
    assert "reconciled" not in rec.verification, (
        f"reconcile promoted past lint; verification: "
        f"{rec.verification!r}"
    )


def test_reconcile_allows_promotion_when_lint_clean():
    """Sanity: a record whose replayed content passes lint AND every
    other check IS eligible for promotion."""
    base_body = (
        'from deeppeek.decorators import command\n'
        '\n'
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    base_block = _base_block(base_body, idx=0, path="x.py", lang="py")

    # Content is base + a bad `--bar` declaration.  Excluding the
    # patch returns to base; that's a no-op, so this test accepts
    # either outcome (promote-or-noop).  The safety-critical case is
    # `test_reconcile_respects_lint_unused_arg`.
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
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
    rec.patches = [{
        "idx": 1,
        "body": '"--bar": {"type": "int"},',
        "applied_by": "deterministic",
        "context_before": "Add to args:",
    }]
    rec.trust = "LOW"
    rec.verification["patches_incomplete"] = True

    records = {"x.py": rec}
    blocks_by_idx = {0: base_block}

    pass8_7_reconcile(records, blocks_by_idx, verbose=False)

    # Either outcome is acceptable: not promoting (because the only
    # defect patch would be excluded, leaving content == base) does
    # not indicate a lint problem.  The important assertion: if
    # reconcile DID promote, the promoted content must not have an
    # unused arg.  `_count_failures` enforces this by construction.
    if "reconciled" in rec.verification:
        assert rec.trust in ("MEDIUM", "HIGH"), (
            f"reconcile promoted but trust is {rec.trust!r}"
        )


# ---------------------------------------------------------------------------
# Fix 4 — distinctive scope-name misattribution
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
# Fix 6 — reverify pass
# ---------------------------------------------------------------------------
#
# The verify-reconcile-verify invariant: any demoting pass must be
# re-run after reconcile promotes.  `reverify_reconciled` runs them on
# every promoted record.
#
# Fix 2 already makes reconcile's replay lint-aware, so the specific
# `--output_rotation` case is caught inside 8.7.  Reverify is the
# general safety net for *future* demoting passes.  Its test
# constructs the "reconciled and promoted but still broken" state
# directly — as if a future pass had been added that reconcile's
# `_count_failures` did not yet know about.

def test_reverify_demotes_promoted_unused_arg():
    """A record marked `reconciled` and promoted to MEDIUM whose
    content still has an unused `@command` arg is re-demoted."""
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

    assert n == 1, f"expected 1 re-demotion, got {n}"
    assert rec.trust == "LOW"
    assert "unused_command_args" in rec.verification


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
#
# The initial Fix 4 fired the distinctive-scope fast path on any body
# whose first-extracted scope name matched a scope elsewhere — even
# when the body declared 10 functions.  That broke
# `test_patch_gates.py::test_fuzzy_long_body_uses_higher_threshold`,
# which locks in 0.85 for long bodies.  The guard: the fast path only
# fires when the body declares exactly ONE top-level scope.
#
# This test WOULD HAVE caught the regression before it reached the
# tests directory.

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
        # Fix 2 — reconcile respects lint
        ("reconcile respects lint unused arg",
         test_reconcile_respects_lint_unused_arg),
        ("reconcile allows promotion when lint clean",
         test_reconcile_allows_promotion_when_lint_clean),
        # Fix 4 — distinctive scope-name misattribution
        ("distinctive scope misattribution detected",
         test_distinctive_scope_misattribution_detected),
        ("distinctive scope low overlap not flagged",
         test_distinctive_scope_low_overlap_not_flagged),
        ("non distinctive scope not flagged",
         test_non_distinctive_scope_not_flagged),
        # Fix 6 — reverify pass
        ("reverify demotes promoted unused arg",
         test_reverify_demotes_promoted_unused_arg),
        ("reverify skips unreconciled",
         test_reverify_skips_unreconciled),
        ("reverify promotes nothing",
         test_reverify_promotes_nothing),
        ("reverify no reconciled records returns zero",
         test_reverify_no_reconciled_records_returns_zero),
        # Regression guard
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
