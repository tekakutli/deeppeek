"""Regression tests for deeppeek robustness fixes."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ============================================================
# Walker
# ============================================================

def test_walker_no_comment_returns_input():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "a();\n\n\ncode_here();\n"
    indent = base.index("code_here")
    assert _find_leading_comment_start(base, indent) == indent


def test_walker_no_comment_contiguous():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "a();\nb();\nc();\ncode_here();\n"
    indent = base.index("code_here")
    assert _find_leading_comment_start(base, indent) == indent


def test_walker_line_comment_blank_traversed():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "// C1\n\ncode_here();\n"
    assert _find_leading_comment_start(base, base.index("code_here")) == 0


def test_walker_block_comment_blank_traversed():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "/* block\n   comment */\n\ncode_here();\n"
    assert _find_leading_comment_start(base, base.index("code_here")) == 0


def test_walker_contiguous_comments():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "// a\n// b\n// c\ncode_here();\n"
    assert _find_leading_comment_start(base, base.index("code_here")) == 0


def test_body_starts_with_comment():
    from deeppeek.recover.apply_patches import _body_starts_with_comment
    assert _body_starts_with_comment("/* x */\ncode") is True
    assert _body_starts_with_comment("// x") is True
    assert _body_starts_with_comment("# x") is True
    assert _body_starts_with_comment("code") is False
    assert _body_starts_with_comment("\n\n// x") is True
    assert _body_starts_with_comment("") is False


def test_apply_scoped_consumes_comment_with_blank():
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "/* Old docstring\n"
        "   multi-line. */\n"
        "\n"
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    new = (
        "/* New docstring */\n"
        "\n"
        "export function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    r = _apply_scoped(base, "foo", new)
    assert r is not None
    assert "Old docstring" not in r
    assert "New docstring" in r
    assert "return 2" in r


def test_apply_scoped_keeps_comment_when_body_has_none():
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "// Pre-existing docstring\n"
        "\n"
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    new = (
        "export function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    r = _apply_scoped(base, "foo", new)
    assert r is not None
    assert "Pre-existing docstring" in r
    assert "return 2" in r


def test_apply_scoped_consumes_decorator():
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "@app.route('/old')\n"
        "def handler():\n"
        "    return 'old'\n"
    )
    new = (
        "// new\n"
        "def handler():\n"
        "    return 'new'\n"
    )
    r = _apply_scoped(base, "handler", new)
    assert r is not None
    assert "app.route('/old')" not in r
    assert "return 'new'" in r


# ============================================================
# Dedup -- patch-provenance-aware signature
# ============================================================
#
# The new signature is:
#   _dedup_repeated_statements(content, path, base=None,
#                              patch_bodies=None, window=15)
#
# All tests below pass base and patch_bodies explicitly, so they
# exercise the real guards rather than the default no-op.

_TARGET_LINE = "if (e.inpaintFullCanvas) e.inpaintFullCanvas.disabled = busy;"

def test_dedup_stmt_removes_patch_added_dup():
    """The pg_panel_fields.py case: line not in base, in patch bodies,
    exactly two occurrences, same scope, within window."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    base = (
        "function f() {\n"
        "  a = 1;\n"
        "  b = 2;\n"
        "  c = 3;\n"
        "}\n"
    )
    content = (
        "function f() {\n"
        "  a = 1;\n"
        "  b = 2;\n"
        "  " + _TARGET_LINE + "\n"
        "  c = 3;\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base=base, patch_bodies=[_TARGET_LINE])
    assert n == 1, f"expected 1 removal, got {n}"
    assert out.count(_TARGET_LINE) == 1


def test_dedup_stmt_keeps_line_in_base():
    """Legitimate duplicate: line IS in base.  The tool must not remove
    it even if it also appears in a patch body (which can happen when
    a patch re-states an existing line for context)."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    line = "resetView();"
    base = (
        "function setImage() {\n"
        "  " + line + "\n"
        "}\n"
        "function clearImage() {\n"
        "  " + line + "\n"
        "}\n"
    )
    content = base
    out, n = _dedup_repeated_statements(
        content, "test.js", base=base, patch_bodies=[line])
    assert n == 0, f"line in base should not be deduped, got {n}"


def test_dedup_stmt_keeps_no_patch_provenance():
    """Line not in base, NOT in any patch body -- user-authored
    duplication.  Must be preserved."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    line = "resetView();"
    content = (
        "function f() {\n"
        "  " + line + "\n"
        "  other();\n"
        "  " + line + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="",
        patch_bodies=["const patchLine = 1;"])
    assert n == 0, f"no-provenance dup should not be deduped, got {n}"


def test_dedup_stmt_keeps_different_scope():
    """Same patch line in two different functions: same-scope guard."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
        "function g() {\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="", patch_bodies=[_TARGET_LINE])
    assert n == 0, f"different scopes should not dedupe, got {n}"


def test_dedup_stmt_keeps_distant():
    """Same line, same scope, distance > window."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        "  " + _TARGET_LINE + "\n"
        "  b();\n"
        "  c();\n"
        "  d();\n"
        "  e();\n"
        "  g();\n"
        "  h();\n"
        "  i();\n"
        "  j();\n"
        "  k();\n"
        "  l();\n"
        "  m();\n"
        "  n();\n"
        "  o();\n"
        "  p();\n"
        "  q();\n"
        "  r();\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="", patch_bodies=[_TARGET_LINE])
    assert n == 0, f"distance > window should not dedupe, got {n}"


def test_dedup_stmt_keeps_nfold_repetition():
    """Three or more occurrences of the same patch line: intentional
    repetition (loop, generated list), not a duplicate-insertion bug."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        + ("  " + _TARGET_LINE + "\n") * 3
        + "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="", patch_bodies=[_TARGET_LINE])
    assert n == 0, f"n-fold repetition should not be deduped, got {n}"


def test_dedup_stmt_markdown_skipped():
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = "a;\nb;\nc;\na;\n"
    out, n = _dedup_repeated_statements(
        content, "readme.md", base="", patch_bodies=["a;"])
    assert n == 0
    assert out == content


def test_dedup_stmt_css_properties_preserved():
    """CSS: `flex:0 0 auto;` twice with different case -- both must
    stay.  This is the pg_base.py corruption shape."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    line = "flex:0 0 auto;"
    content = (
        "  .maskSwatch {\n"
        "    width:18px;\n"
        "    " + line + "\n"
        "    background:#0e1622;\n"
        "  }\n"
        "  .maskMeta {\n"
        "    " + line + "\n"
        "    color:#5a6774;\n"
        "  }\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.css", base="", patch_bodies=["width:18px;"])
    assert n == 0, f"CSS properties should not be deduped, got {n}"


def test_dedup_stmt_base_required_for_any_action():
    """Sanity: with no base and no patch_bodies, the function is a
    no-op.  This is the safety default."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        "  a = 1;\n"
        "  b = 2;\n"
        "  a = 1;\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(content, "test.js")
    assert n == 0
    assert out == content


# ============================================================
# Header parsing
# ============================================================

def test_parse_file_header_markdown_link():
    from deeppeek.recover.extract import _parse_file_header
    r = _parse_file_header("[file.py](http://example.com)")
    assert r is not None and r[0] == "file.py"


def main():
    tests = [
        ("walker no comment returns input", test_walker_no_comment_returns_input),
        ("walker no comment contiguous", test_walker_no_comment_contiguous),
        ("walker line comment blank traversed", test_walker_line_comment_blank_traversed),
        ("walker block comment blank traversed", test_walker_block_comment_blank_traversed),
        ("walker contiguous comments", test_walker_contiguous_comments),
        ("body_starts_with_comment", test_body_starts_with_comment),
        ("apply_scoped consumes comment with blank",
         test_apply_scoped_consumes_comment_with_blank),
        ("apply_scoped keeps comment when body has none",
         test_apply_scoped_keeps_comment_when_body_has_none),
        ("apply_scoped consumes decorator",
         test_apply_scoped_consumes_decorator),
        ("dedup_stmt removes patch-added dup",
         test_dedup_stmt_removes_patch_added_dup),
        ("dedup_stmt keeps line in base",
         test_dedup_stmt_keeps_line_in_base),
        ("dedup_stmt keeps no patch provenance",
         test_dedup_stmt_keeps_no_patch_provenance),
        ("dedup_stmt keeps different scope",
         test_dedup_stmt_keeps_different_scope),
        ("dedup_stmt keeps distant", test_dedup_stmt_keeps_distant),
        ("dedup_stmt keeps nfold repetition",
         test_dedup_stmt_keeps_nfold_repetition),
        ("dedup_stmt markdown skipped", test_dedup_stmt_markdown_skipped),
        ("dedup_stmt CSS properties preserved",
         test_dedup_stmt_css_properties_preserved),
        ("dedup_stmt base required for action",
         test_dedup_stmt_base_required_for_any_action),
        ("parse_file_header markdown link",
         test_parse_file_header_markdown_link),
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
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\nAll {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
