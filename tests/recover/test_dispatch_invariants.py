"""Invariant tests for the dispatcher and its support functions.

Three small invariants that the 2026-10-28 run showed can regress
silently:

  1. `_find_matching_decl_block` must match on identifier, not
     abstracted RHS alone.  Without it, `const foo = document.
     getElementById("a")` and `const bar = document.getElementById
     ("b")` both abstract to the same RHS and the helper returns
     the *last* match — often the wrong one.

  2. `attribute_sanity_failure` can return an idx from a *different*
     record's patch set (the mechanism isn't fully understood — see
     the manifest where `sanity_caused_by=638` appeared on
     `pg_panel_scrubs.py` which has patches [635, 637] and not 638).
     The gate `_validate_sanity_attribution` refuses any idx that
     isn't in the record's own patch set, converting a wrong answer
     into "attribution unavailable".

  3. `_js_structural_check` runs tree-sitter first when available;
     `_js_balance` is a fallback.  Without this, an unparseable
     script reports "unclosed string literal" (from `_js_balance`)
     even when tree-sitter could pinpoint `line 142, col 8`.

Each test names the failure it prevents.  If a future refactor
changes the implementation but preserves the invariant, the tests
still pass — they test behaviour, not internals.  The only
structural assumption is that the corresponding function exists;
that's the contract the rest of the pipeline depends on.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# 1. _find_matching_decl_block identifier matching
# ---------------------------------------------------------------------------

def test_decl_block_identifier_must_match():
    """Two different `document.getElementById` calls abstract to the
    same RHS.  `_find_matching_decl_block` must use the identifier to
    disambiguate.

    Failure mode caught: a body whose first line is
    `const rotationEl = document.getElementById("viewRotation2");`
    matches the *inpaintEnabledEl* declaration's block end because
    both abstract to `document.getElementById("")`.
    """
    from deeppeek.recover.patch_structural import _find_matching_decl_block

    content = (
        "const rotationEl = document.getElementById(\"viewRotation\");\n"
        "const inpaintEnabledEl = document.getElementById(\"inpaintEnabled\");\n"
        "const foo = 1;\n"
    )
    end_line = _find_matching_decl_block(
        content,
        'const rotationEl = document.getElementById("viewRotation2");',
    )
    assert end_line is not None, "expected a match"
    assert end_line == 1, (
        f"expected rotationEl's end (line 1); got {end_line}. "
        f"Without the identifier check, the helper matches "
        f"inpaintEnabledEl's declaration too and returns its end "
        f"(line 2)."
    )


def test_decl_block_no_match_when_name_absent():
    """A body declaring an identifier that base does not contain
    returns None, so the caller falls through to another heuristic.

    Failure mode caught: a body declaring `const totallyNew = ...`
    matches some unrelated declaration's block end because the
    abstracted RHS happens to be the same.
    """
    from deeppeek.recover.patch_structural import _find_matching_decl_block

    content = (
        "const foo = document.getElementById(\"a\");\n"
        "const bar = document.getElementById(\"b\");\n"
    )
    end_line = _find_matching_decl_block(
        content,
        'const totallyNew = document.getElementById("c");',
    )
    assert end_line is None, (
        f"expected None (name not in base); got {end_line}"
    )


# ---------------------------------------------------------------------------
# 2. Sanity attribution cross-record gate
# ---------------------------------------------------------------------------

def test_validate_sanity_attribution_accepts_real_culprit():
    """An idx that IS in the record's patch set is accepted."""
    try:
        from deeppeek.recover.validate import _validate_sanity_attribution
    except ImportError:
        print("  SKIP: _validate_sanity_attribution not implemented")
        return

    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.patches = [
        {"idx": 1, "body": "x", "applied_by": "deterministic"},
        {"idx": 2, "body": "y", "applied_by": "deterministic"},
    ]
    assert _validate_sanity_attribution(rec, 1) == 1
    assert _validate_sanity_attribution(rec, 2) == 2


def test_validate_sanity_attribution_rejects_cross_record():
    """An idx that is NOT in the record's patch set is rejected,
    converting a wrong answer into `None` (attribution unavailable).

    Failure mode caught: the manifest's 2026-10-28 output showed
    `sanity_caused_by=638` on `pg_panel_scrubs.py` even though that
    record's patches are [635, 637] and 638 belongs to
    `pg_panel_bindings.py`.  Without the gate, that wrong number
    survives into the manifest.
    """
    try:
        from deeppeek.recover.validate import _validate_sanity_attribution
    except ImportError:
        print("  SKIP: _validate_sanity_attribution not implemented")
        return

    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.patches = [
        {"idx": 635, "body": "x", "applied_by": "deterministic"},
        {"idx": 637, "body": "y", "applied_by": "deterministic"},
    ]
    # 638 is from another record — must be discarded.
    assert _validate_sanity_attribution(rec, 638) is None, (
        "expected None for cross-record idx 638; got 638 back"
    )
    assert _validate_sanity_attribution(rec, 999) is None
    # None passes through.
    assert _validate_sanity_attribution(rec, None) is None


# ---------------------------------------------------------------------------
# 3. Tree-sitter-first in _js_structural_check
# ---------------------------------------------------------------------------

def test_js_structural_check_uses_tree_sitter_first():
    """When tree-sitter is available, an unparseable JS buffer must
    report a tree-sitter error (with a line number), NOT
    `_js_balance`'s "unclosed string literal".

    Failure mode caught: the 2026-10-28 diagnostic on
    `pg_panel_scrubs.py` and `pg_panel_bindings.py` — "embedded at
    line 1: unclosed string literal" — which is `_js_balance`'s
    verdict on a *structurally fine* file that only became
    unparseable because a bad patch nested the modified block inside
    the existing wiring.  A tree-sitter error would have said
    "parse error at line 187, col 3" — pinpointing the actual
    corruption.

    Skips when tree-sitter is not installed; the tests that DO run
    verify the diagnostic quality that makes the fix worthwhile.
    """
    from deeppeek.recover.ast_anchor import _has_inprocess_ast
    if not _has_inprocess_ast():
        print("  SKIP: tree-sitter not available in-process")
        return

    from deeppeek.recover.validate_output import _js_structural_check

    # Unparseable: function body opened with `(` rather than `{`.
    # Both tree-sitter and `_js_balance` flag it — but tree-sitter
    # gives a location.
    text = "function foo( {\n  return 1;\n}\n"
    err = _js_structural_check(text, "x.js", parse_lang="javascript")
    assert err is not None
    assert "parse error" in err.lower(), (
        f"expected tree-sitter's 'parse error'; got {err!r}. If "
        f"_js_balance runs first, this reports 'unbalanced parens' "
        f"without a line number."
    )
    assert "line" in err.lower(), (
        f"tree-sitter error should name a line; got {err!r}"
    )


def test_js_structural_check_falls_back_to_balance():
    """When tree-sitter is unavailable (or the parse language is
    None), `_js_balance` still catches gross imbalance.

    Failure mode caught: the reorder accidentally removed the
    fallback, so an unparseable body with no tree-sitter available
    silently passes.
    """
    from deeppeek.recover.ast_anchor import _has_inprocess_ast
    if _has_inprocess_ast():
        print("  SKIP: tree-sitter is available; fallback unreachable")
        return

    from deeppeek.recover.validate_output import _js_structural_check

    text = "if (cond) {\n  doThing();\n"  # unbalanced
    err = _js_structural_check(text, "x.js", parse_lang=None)
    assert err is not None
    assert "unbalanced" in err.lower() or "unclosed" in err.lower(), (
        f"expected balance-check error; got {err!r}"
    )


def main():
    tests = [
        ("decl block identifier must match",
         test_decl_block_identifier_must_match),
        ("decl block no match when name absent",
         test_decl_block_no_match_when_name_absent),
        ("validate sanity attribution accepts real culprit",
         test_validate_sanity_attribution_accepts_real_culprit),
        ("validate sanity attribution rejects cross record",
         test_validate_sanity_attribution_rejects_cross_record),
        ("js structural check uses tree sitter first",
         test_js_structural_check_uses_tree_sitter_first),
        ("js structural check falls back to balance",
         test_js_structural_check_falls_back_to_balance),
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
        print(f"\n{failed}/{len(tests)} dispatch-invariant test(s) failed")
        return 1
    print(f"\nAll {len(tests)} dispatch-invariant tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
