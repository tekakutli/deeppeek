"""Tests for `patch_comments.get_comment_spans` and the comment-aware
anchor heuristics it enables.

The motivating failure (2026-10-28d): block idx=72 targeted
`pg_panel_scrubs.py` with a `_clearImageAction` fragment.  The
statement-anchor heuristic found the anchor text `pgkitNotifyAll()`
inside the `/* ... */` docstring-block comment for
`masks.selectedIndex` and inserted the fragment *into the comment*.
The file compiled, the AST parsed, and pass 8.7 reconciled the
record to MEDIUM — dead code shipped silently.

Two invariants these tests lock in:

  1. `get_comment_spans` covers `//`, `/* */`, `#`, and docstrings,
     and recurses into JS inside Python triple-quoted strings.

  2. The anchor heuristics skip any candidate position inside a
     comment span, and count uniqueness *after* filtering — so an
     anchor that appears once in code and once in a comment is
     treated as unique-in-code.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.patch_comments import (
    get_comment_spans, is_inside_comment,
)
from deeppeek.recover.patch_anchor_insert import (
    _try_insert_after_anchor, _try_insert_before_anchor,
)


TD = chr(34) * 3
TS = chr(39) * 3


# ---------------------------------------------------------------------------
# get_comment_spans — direct regex/regex tests
# ---------------------------------------------------------------------------

def test_js_line_comment_detected():
    text = "const x = 1; // this is a comment\nconst y = 2;\n"
    spans = get_comment_spans(text, "f.js")
    assert len(spans) == 1
    assert is_inside_comment(text.index("//"), spans)
    assert is_inside_comment(text.index("this is a comment"), spans)
    assert not is_inside_comment(text.index("const y"), spans)


def test_js_block_comment_detected():
    text = "/* block\n   comment */\ncode();\n"
    spans = get_comment_spans(text, "f.js")
    assert len(spans) == 1
    assert is_inside_comment(text.index("block"), spans)
    assert is_inside_comment(text.index("comment */"), spans)
    assert not is_inside_comment(text.index("code();"), spans)


def test_js_string_with_slashes_not_comment():
    text = 'const url = "http://example.com";\nconst x = 1;\n'
    spans = get_comment_spans(text, "f.js")
    # `//` inside the string must not be a comment.
    assert spans == []


def test_python_hash_comment():
    text = "x = 1  # a comment\n"
    spans = get_comment_spans(text, "f.py")
    assert len(spans) == 1
    assert is_inside_comment(text.index("#"), spans)


def test_python_hash_in_string_not_comment():
    text = 'x = "not a # comment"\n'
    spans = get_comment_spans(text, "f.py")
    assert spans == []


def test_python_docstring_detected_as_string_not_comment():
    """A Python docstring is a string, not a comment.  Content inside
    is code for the purposes of JS analysis; only comments INSIDE the
    string are comment spans."""
    text = (
        'def foo():\n'
        '    """This is a docstring with # inside."""\n'
        '    return 1\n'
    )
    spans = get_comment_spans(text, "f.py")
    # The `#` is inside a docstring, not a `#` comment.
    assert not any(
        text[start:end].strip() == "# inside." for start, end in spans
    )


def test_python_js_block_comment_inside_triple_string():
    """The playground pattern: JS in a `r\"\"\"...\"\"\"` with a `/* */`
    block inside.  The block comment must be detected."""
    text = (
        'PANEL_JS = r' + TD + '\n'
        '/* ============\n'
        '   Doc comment.\n'
        '   ============ */\n'
        'pgkitRegisterField(...);\n'
        + TD + '\n'
    )
    spans = get_comment_spans(text, "f.py")
    assert len(spans) >= 1
    assert is_inside_comment(text.index("Doc comment"), spans)
    assert not is_inside_comment(
        text.index("pgkitRegisterField"), spans)


def test_python_js_line_comment_inside_triple_string():
    """JS `//` comments inside a Python triple-quoted string are
    detected as comments."""
    text = (
        'PANEL_JS = r' + TD + '\n'
        'const x = 1;  // a JS line comment\n'
        'const y = 2;\n'
        + TD + '\n'
    )
    spans = get_comment_spans(text, "f.py")
    assert any(
        "a JS line comment" in text[start:end]
        for start, end in spans
    )


def test_multiple_block_comments():
    text = (
        "/* one */\ncode();\n/* two */\nmore();\n"
    )
    spans = get_comment_spans(text, "f.js")
    assert len(spans) == 2


def test_unclosed_block_comment_runs_to_eof():
    text = "code();\n/* unclosed comment"
    spans = get_comment_spans(text, "f.js")
    assert len(spans) == 1
    assert spans[0][1] == len(text)


# ---------------------------------------------------------------------------
# Anchor heuristic skips comment positions
# ---------------------------------------------------------------------------

def test_anchor_after_not_placed_in_js_block_comment():
    """The motivating shape: anchor text lives only in a `/* */`
    comment.  Placement must NOT happen there."""
    base = (
        "code();\n"
        "/* docstring with helper() mentioned inside */\n"
        "more_code();\n"
    )
    body = "new_line_1();\nnew_line_2()\n"
    result = _try_insert_after_anchor(
        base, body,
        "After `helper()`:",
        path="f.js",
    )
    # `helper()` appears only in the comment, so the statement branch
    # finds zero non-comment positions and no other heuristic matches.
    assert result is None or "/* docstring" not in result[
        result.index("new_line_1()") - 50:result.index("new_line_1()")
    ], f"anchor placed inside comment:\n{result}"


def test_anchor_after_prefers_code_over_comment():
    """When the anchor appears once in a comment and once in code,
    the code occurrence wins."""
    base = (
        "/* mention of helper() in comment */\n"
        "do_work();\n"
        "helper();\n"
        "after();\n"
    )
    body = "inserted_1();\ninserted_2()\n"
    result = _try_insert_after_anchor(
        base, body,
        "After `helper()`:",
        path="f.js",
    )
    assert result is not None
    # Insert must land AFTER `helper();` in code, not after the
    # mention in the comment.
    insert_pos = result.index("inserted_1()")
    helper_code_pos = result.index("helper();")
    assert insert_pos > helper_code_pos, (
        f"expected insertion after code helper, got:\n{result}"
    )
    # Must NOT be before the code helper.
    assert insert_pos > result.index("/* mention")


def test_anchor_after_comment_only_via_scope_fails_closed():
    """Anchor in a comment AND in a different scope: with scope-aware
    selection and comment filtering, the comment occurrence is
    skipped, the scope occurrence is selected."""
    base = (
        "function a() {\n"
        "  /* helper() mentioned */\n"
        "  work();\n"
        "}\n"
        "function b() {\n"
        "  helper();\n"
        "}\n"
    )
    body = "new_1();\nnew_2()\n"
    result = _try_insert_after_anchor(
        base, body,
        "In `b`, after `helper()`:",
        path="f.js",
    )
    assert result is not None, (
        "expected insertion into b(); scope-aware + comment-aware "
        "selection failed"
    )
    # Insert must be in b()'s body.
    a_end = result.index("function b")
    insert_pos = result.index("new_1()")
    assert insert_pos > a_end, (
        f"expected insertion in b(), got:\n{result}"
    )


def test_anchor_before_skips_comment():
    """`_try_insert_before_anchor` must also skip comment positions."""
    base = (
        "/* target_fn() mentioned in docstring */\n"
        "do_work();\n"
        "target_fn();\n"
    )
    body = "before_1();\nbefore_2()\n"
    result = _try_insert_before_anchor(
        base, body,
        "Before `target_fn()`:",
        path="f.js",
    )
    assert result is not None
    # Insert must land BEFORE the code target_fn, not before the
    # comment line.
    insert_pos = result.index("before_1()")
    target_code_pos = result.index("target_fn();")
    assert insert_pos < target_code_pos
    # Must be after the opening comment.
    assert insert_pos > result.index("/* target_fn")


def test_anchor_before_comment_only_fails_closed():
    """`_try_insert_before_anchor` returns None when the anchor
    appears only inside a comment."""
    base = (
        "/* target() only mentioned in comment */\n"
        "code();\n"
    )
    body = "before_1();\nbefore_2()\n"
    result = _try_insert_before_anchor(
        base, body,
        "Before `target()`:",
        path="f.js",
    )
    assert result is None, (
        f"expected None (comment-only anchor), got:\n{result}"
    )


def test_anchor_python_hash_comment_skipped():
    """Anchor inside a `#` comment in a `.py` file is skipped."""
    base = (
        "def foo():\n"
        "    # helper() is defined elsewhere\n"
        "    do_work()\n"
    )
    body = "inserted_1()\ninserted_2()\n"
    result = _try_insert_after_anchor(
        base, body,
        "After `helper()`:",
        path="f.py",
    )
    assert result is None, (
        f"expected None (only in a Python # comment), got:\n{result}"
    )


def test_anchor_python_js_docstring_skipped():
    """The exact pg_panel_scrubs shape: JS `/* */` inside a `.py`
    triple-quoted string.  Anchor inside that block comment is
    skipped."""
    base = (
        'PANEL_JS = r' + TD + '\n'
        '/*\n'
        '   helper() is mentioned here.\n'
        '*/\n'
        'pgkitRegisterField("x", {});\n'
        + TD + '\n'
    )
    body = (
        'function _helper() {\n'
        '  call_something();\n'
        '  helper();\n'
        '}\n'
    )
    result = _try_insert_after_anchor(
        base, body,
        "After `helper()`:",
        path="f.py",
    )
    assert result is None, (
        f"expected None (only in Python-string JS block comment), "
        f"got:\n{result}"
    )


def test_anchor_prior_insertion_skips_comment():
    """The prior-insertion fallback must also skip comment positions."""
    base = (
        "/* marker_line() mentioned */\n"
        "code();\n"
    )
    body = "new_1()\nnew_2()\n"
    result = _try_insert_after_anchor(
        base, body,
        "Add:",
        prior_insertions={"marker_line() mentioned"},
        path="f.js",
    )
    # The prior-insertion anchor appears in base exactly once (in the
    # comment), so `count == 1` — but the comment filter rejects it.
    assert result is None, (
        f"expected None (prior anchor only in comment), got:\n{result}"
    )


def main():
    tests = [
        ("js line comment detected", test_js_line_comment_detected),
        ("js block comment detected", test_js_block_comment_detected),
        ("js string with slashes not comment",
         test_js_string_with_slashes_not_comment),
        ("python hash comment", test_python_hash_comment),
        ("python hash in string not comment",
         test_python_hash_in_string_not_comment),
        ("python docstring detected as string not comment",
         test_python_docstring_detected_as_string_not_comment),
        ("python js block comment inside triple string",
         test_python_js_block_comment_inside_triple_string),
        ("python js line comment inside triple string",
         test_python_js_line_comment_inside_triple_string),
        ("multiple block comments", test_multiple_block_comments),
        ("unclosed block comment runs to eof",
         test_unclosed_block_comment_runs_to_eof),
        ("anchor after not placed in js block comment",
         test_anchor_after_not_placed_in_js_block_comment),
        ("anchor after prefers code over comment",
         test_anchor_after_prefers_code_over_comment),
        ("anchor after comment only via scope fails closed",
         test_anchor_after_comment_only_via_scope_fails_closed),
        ("anchor before skips comment",
         test_anchor_before_skips_comment),
        ("anchor before comment only fails closed",
         test_anchor_before_comment_only_fails_closed),
        ("anchor python hash comment skipped",
         test_anchor_python_hash_comment_skipped),
        ("anchor python js docstring skipped",
         test_anchor_python_js_docstring_skipped),
        ("anchor prior insertion skips comment",
         test_anchor_prior_insertion_skips_comment),
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
        print(f"\n{failed}/{len(tests)} comment-span test(s) failed")
        return 1
    print(f"\nAll {len(tests)} comment-span tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
