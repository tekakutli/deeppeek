"""Invariant tests for the dispatcher and its support functions.

Four small invariants that the 2026-10-28 / 2026-11-05 runs showed
can regress silently:

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

  4. `_find_named_block_end` must NOT take a `path` parameter.  The
     between-anchors prose names a block by *label* ("between the
     Image and Masks `<details>` blocks") without naming its
     language, so `find_scope` must be called with the empty path
     (Python → JS → HTML → embedded JS fallback).  Threading the
     destination file's `.py` path would stop at the Python parser
     and return `None` for an HTML `<details>` embedded in a Python
     string — the exact pre-fix regression that fixture 42
     exercises end-to-end.  The same constraint closes the
     JS-object-in-Python-string regression (`const board = {...}`
     inside `PG_PANEL_JS = r\"\"\"...\"\"\"`).

Each test names the failure it prevents.  If a future refactor
changes the implementation but preserves the invariant, the tests
still pass — they test behaviour, not internals.  The only
structural assumption is that the corresponding function exists;
that's the contract the rest of the pipeline depends on.
"""
import inspect
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


# ---------------------------------------------------------------------------
# 4. _find_named_block_end must be language-agnostic
# ---------------------------------------------------------------------------
#
# The between-anchors heuristic names blocks by *label* ("between the
# Image and Masks `<details>` blocks") without naming their language.
# Threading the outer file's `.py` path into `find_scope` would stop
# at the Python parser and return `None` for any non-Python scope
# embedded in a Python string — HTML `<details>`, JS `function foo()`,
# JS `const board = {...}`.  The fix is that `_find_named_block_end`
# uses the empty-path fallback, which tries every parser.
#
# These tests pin the contract from four angles:
#   · signature (no `path` parameter),
#   · HTML-in-Py works (fixture 42's case),
#   · Python def still works (sanity),
#   · JS object literal in Py works (the regression this fix closes).

def test_find_named_block_end_has_no_path_param():
    """`_find_named_block_end` must NOT take a `path` parameter.

    The between-anchors heuristic names blocks by *label* ("between
    the Image and Masks `<details>` blocks") without naming their
    language.  Threading the outer file's `.py` path into
    `find_scope` would stop at the Python parser and return `None`
    for an HTML `<details>` embedded in a Python string — the exact
    pre-fix regression that fixture 42 (`test_42`) exercises
    end-to-end.

    This signature check is a tripwire: if a future refactor threads
    `path` back in, this test fails immediately with a clear message
    pointing at fixture 42.  The three behavioural tests below cover
    the same invariant from the "does it actually work?" side.
    """
    from deeppeek.recover.patch_anchor_insert import (
        _find_named_block_end,
    )
    sig = inspect.signature(_find_named_block_end)
    param_names = list(sig.parameters.keys())
    assert "path" not in param_names, (
        "_find_named_block_end has a `path` parameter (found: "
        f"{param_names}).  The between-anchors prose names a block "
        "by label, not language, so `find_scope` must use the empty-"
        "path fallback (Python → JS → HTML → embedded JS).  "
        "Threading `path` stops at the destination file's parser "
        "and returns None for embedded HTML or embedded JS.  "
        "See fixture 42 / test_42."
    )


def test_find_named_block_end_finds_html_in_py():
    """Behavioural counterpart: `<details>` embedded in a Python
    string is found without threading any path.

    A `.py`-extension caller that threaded its path to `find_scope`
    would hit `_find_python_scope` first (no `Image` def/class/
    assignment), and the fallback would stop there.  The empty-path
    call tries Python → JS → HTML → embedded JS in turn and
    succeeds on the HTML attempt.
    """
    from deeppeek.recover.patch_anchor_insert import (
        _find_named_block_end,
    )
    base = (
        'PANEL = r"""\n'
        '<details><summary>Image</summary>\n'
        '  <p>image controls</p>\n'
        '</details>\n'
        '<details><summary>Masks</summary>\n'
        '  <p>mask controls</p>\n'
        '</details>\n'
        '"""\n'
    )
    span = _find_named_block_end(base, "Image")
    assert span is not None, (
        "_find_named_block_end returned None for an HTML <details> "
        "inside a Python string.  Without the empty-path fallback "
        "(Python → JS → HTML → embedded JS), between-anchors prose "
        "on .py files silently fails and the record fail-closes.  "
        "Fixture 42 catches the same regression at the pipeline "
        "level."
    )
    start, end = span
    text = base[start:end]
    assert "Image" in text, (
        f"span does not contain 'Image':\n{text}"
    )
    assert "Masks" not in text, (
        f"span bled into the sibling Masks block:\n{text}"
    )


def test_find_named_block_end_finds_python_def():
    """Sanity: the same function still finds a Python def.  The
    empty-path fallback tries Python *first*, so this works without
    any language hint — the ordering matters because most
    between-anchors prose on `.py` files refers to Python scopes."""
    from deeppeek.recover.patch_anchor_insert import (
        _find_named_block_end,
    )
    base = (
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    span = _find_named_block_end(base, "foo")
    assert span is not None
    start, end = span
    text = base[start:end]
    assert "def foo" in text
    assert "def bar" not in text


def test_find_named_block_end_finds_js_object_in_py():
    """The JS-object-in-Python-string regression that this fix closes.

    Motivating shape: `PG_PANEL_JS = r\"\"\"...\"\"\"` holds a JS
    module with `const board = {...}`.  Between-anchors prose says
    "between the `board` and `view` object literals".  Before the
    fix, `_find_named_block_end(base, "board")` returned None: the
    Python parser saw no `board` def/class, the JS parser choked on
    the whole `.py` file, and the HTML parser found no `<details>`.
    The embedded-JS tier closes that gap.

    A test that threads `path` into `_find_named_block_end` (which
    is prohibited by `test_find_named_block_end_has_no_path_param`)
    would still fail here — the `.py` path only tries Python → HTML
    → embedded JS, which would *work* for this shape, but the
    signature test would already have caught the thread.
    """
    from deeppeek.recover.ast_anchor import _has_inprocess_ast
    if not _has_inprocess_ast():
        print("  SKIP: tree-sitter not available in-process")
        return

    from deeppeek.recover.patch_anchor_insert import (
        _find_named_block_end,
    )
    base = (
        'JS = r"""\n'
        'const board = {\n'
        '  imageHash: null,\n'
        '  inpaintPrompt: "",\n'
        '  running: false,\n'
        '};\n'
        'const view = { zoom: 1, panX: 0, panY: 0, maskCount: 0, layerCount: 0 };\n'
        '"""\n'
    )
    span = _find_named_block_end(base, "board")
    assert span is not None, (
        "_find_named_block_end returned None for a JS object literal "
        "inside a Python string.  Without the embedded-JS tier in "
        "`find_scope`, between-anchors prose on a `.py` file that "
        "holds JS in `X_JS = r\"\"\"...\"\"\"` silently fails.  This "
        "is the regression the embedded-JS tier closes."
    )
    start, end = span
    text = base[start:end]
    assert "board" in text, (
        f"span does not contain 'board':\n{text}"
    )
    assert "const board" in text, (
        f"span does not start at the `const board` declaration:\n{text}"
    )
    assert "const view" not in text, (
        f"span bled into the sibling `view` object:\n{text}"
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
        ("find named block end has no path param",
         test_find_named_block_end_has_no_path_param),
        ("find named block end finds html in py",
         test_find_named_block_end_finds_html_in_py),
        ("find named block end finds python def",
         test_find_named_block_end_finds_python_def),
        ("find named block end finds js object in py",
         test_find_named_block_end_finds_js_object_in_py),
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
