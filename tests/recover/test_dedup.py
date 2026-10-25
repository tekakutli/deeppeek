"""Unit tests for _dedup_duplicate_blocks.

These exercise the four safety properties directly (no recovery run):

  1. Dedup must NOT modify documentation-style files (.md, .html,
     .css, .json, ...).  A duplicate const/if pattern in those files
     is almost always an author-written example, not a pipeline
     artifact; removing it would silently corrupt the file.

  2. Dedup must strip JS comments (// and /* */) before brace
     counting.  A line like `if (x) {  // }` must count as one
     open, not one open and one close.

  3. For Python files with embedded JS, dedup only removes duplicates
     inside the SAME triple-quoted string literal — a duplicate in a
     DIFFERENT string is treated as an example and left alone.

  4. Two identical if-blocks in different enclosing scopes (.js) are
     legitimate code, not duplicates.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.validate_output import (  # noqa: E402
    _dedup_duplicate_blocks, _strip_inline_strings,
)

TD = chr(34) * 3
TS = chr(39) * 3


def test_markdown_never_deduped():
    content = (
        "# Example\n\n"
        "```js\nconst foo = 1;\nif (foo) { x(); }\n```\n\n"
        "More text\n\n"
        "```js\nconst foo = 1;\nif (foo) { x(); }\n```\n"
    )
    out, n = _dedup_duplicate_blocks(content, "readme.md")
    assert n == 0, f"expected no removals on .md, got {n}"
    assert out == content, "content modified on .md"


def test_html_never_deduped():
    content = (
        "<p>a</p>\n<script>const foo = 1;</script>\n"
        "<p>b</p>\n<script>const foo = 1;</script>\n"
    )
    out, n = _dedup_duplicate_blocks(content, "index.html")
    assert n == 0
    assert out == content


def test_css_never_deduped():
    content = (
        "@media a { if (x) { y(); } }\n"
        "@media b { if (x) { y(); } }\n"
    )
    out, n = _dedup_duplicate_blocks(content, "style.css")
    assert n == 0


def test_json_never_deduped():
    content = '{"a": "const foo = 1;", "b": "const foo = 1;"}\n'
    out, n = _dedup_duplicate_blocks(content, "data.json")
    assert n == 0


def test_js_duplicate_removed():
    content = (
        "function f() {\n"
        "  if (x) {\n    a();\n  }\n"
        "  if (x) {\n    a();\n  }\n"
        "}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 1, f"expected 1 removal, got {n}"
    assert out.count("if (x)") == 1


def test_js_different_scopes_kept():
    content = (
        "function f() {\n"
        "  if (x) { a(); }\n"
        "}\n"
        "function g() {\n"
        "  if (x) { a(); }\n"
        "}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 0, f"different scopes should not dedupe, got {n}"


def test_js_line_comment_braces_ignored():
    content = (
        "function f() {\n"
        "  if (x) {  // }\n"
        "    a();\n"
        "  }\n"
        "  if (x) {  // }\n"
        "    a();\n"
        "  }\n"
        "}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 1, (
        f"expected 1 removal despite `// }}` comments, got {n}"
    )


def test_python_embedded_js_duplicate_removed():
    content = (
        "JS = r" + TD + "\n"
        "const foo = document.getElementById('foo');\n"
        "if (foo) {\n  foo.checked = true;\n}\n"
        "const foo = document.getElementById('foo');\n"
        "if (foo) {\n  foo.checked = true;\n}\n"
        + TD + "\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.py")
    assert n == 1, f"expected 1 removal, got {n}"
    assert out.count("const foo") == 1


def test_python_different_strings_kept():
    content = (
        "BEFORE = r" + TD + "\n"
        "const foo = 1;\n"
        "if (foo) { a(); }\n"
        + TD + "\n"
        "\n"
        "AFTER = r" + TD + "\n"
        "const foo = 1;\n"
        "if (foo) { a(); }\n"
        + TD + "\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.py")
    assert n == 0, (
        f"cross-string duplicates should not dedupe, got {n}"
    )


def test_strip_inline_strings_strips_line_comment():
    assert _strip_inline_strings("a(); // }") == "a(); "


def test_strip_inline_strings_strips_block_comment():
    assert _strip_inline_strings("a(); /* } */ b();") == "a();  b();"


def test_strip_inline_strings_strips_quoted():
    assert _strip_inline_strings('x = "{";') == "x = ;"


def test_strip_inline_strings_keeps_braces():
    assert _strip_inline_strings("if (x) {") == "if (x) {"


def test_python_body_line_comment_braces_ignored():
    """`// }` inside a block BODY, in a .py file's embedded JS.

    Exercises _strip_inline_strings's `//` stripping, which is used
    by _find_matching_close_char_linebased (the .py path).  Without
    stripping, the comment's `}` would close the block early and
    dedup would remove the wrong span.
    """
    content = (
        "JS = r" + TD + "\n"
        "const foo = document.getElementById('foo');\n"
        "if (foo) {\n"
        "  // }\n"
        "  a();\n"
        "}\n"
        "const foo = document.getElementById('foo');\n"
        "if (foo) {\n"
        "  // }\n"
        "  a();\n"
        "}\n"
        + TD + "\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.py")
    assert n == 1, f"expected 1 removal, got {n}"
    assert out.count("const foo") == 1

def main():
    tests = [
        ("markdown never deduped", test_markdown_never_deduped),
        ("html never deduped", test_html_never_deduped),
        ("css never deduped", test_css_never_deduped),
        ("json never deduped", test_json_never_deduped),
        ("js duplicate removed", test_js_duplicate_removed),
        ("js different scopes kept", test_js_different_scopes_kept),
        ("js line comment braces ignored",
         test_js_line_comment_braces_ignored),
        ("python body line comment braces ignored",
         test_python_body_line_comment_braces_ignored),
        ("python embedded js duplicate removed",
         test_python_embedded_js_duplicate_removed),
        ("python different strings kept",
         test_python_different_strings_kept),
        ("strip strips line comment",
         test_strip_inline_strings_strips_line_comment),
        ("strip strips block comment",
         test_strip_inline_strings_strips_block_comment),
        ("strip strips quoted", test_strip_inline_strings_strips_quoted),
        ("strip keeps braces", test_strip_inline_strings_keeps_braces),
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
        print(f"\n{failed}/{len(tests)} dedup test(s) failed")
        return 1
    print(f"\nAll {len(tests)} dedup tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
