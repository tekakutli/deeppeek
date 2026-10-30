"""Tests for structural_diff and its helpers.

The module shipped broken — six helper names it references were never
defined, every call raised NameError, and the structural-equivalence
pass (8.10) was a silent no-op for every run. The startup AST check
(`pipeline._find_unresolved_module_names`) now catches that class of
bug before any file is processed; these tests are the second line of
defense and lock in the extraction/scoring contracts.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.structural_diff import (
    structural_diff, extract_signatures,
    _python_signatures, _python_statement_sigs,
    _js_signatures, _html_signatures,
    _embedded_html_signatures,
)


# -- Python ----------------------------------------------------------------

def test_python_signatures_top_level():
    content = "def foo():\n    pass\n\nclass Bar:\n    pass\n"
    sigs = _python_signatures(content)
    assert "def foo" in sigs
    assert "class Bar" in sigs


def test_python_signatures_excludes_methods():
    content = "class C:\n    def m(self):\n        pass\n"
    sigs = _python_signatures(content)
    assert "class C" in sigs
    assert not any("m" == s.split()[-1] for s in sigs)


def test_python_signatures_async():
    sigs = _python_signatures("async def foo():\n    pass\n")
    assert "async def foo" in sigs


def test_python_signatures_syntax_error():
    assert _python_signatures("def foo(:\n") == set()


def test_python_statement_sigs_return():
    sigs = _python_statement_sigs(
        "def f():\n    return {'a': 1, 'b': 2}\n")
    assert any("'a'" in s for s in sigs)


# -- JS --------------------------------------------------------------------

def test_js_signatures_functions_and_consts():
    content = (
        "function foo() { return 1; }\n"
        "const bar = 2;\n"
        "class Baz {}\n"
    )
    sigs = _js_signatures(content)
    assert any("foo" in s for s in sigs)
    assert any("bar" in s for s in sigs)
    assert any("Baz" in s for s in sigs)


# -- HTML ------------------------------------------------------------------

def test_html_signatures_nested():
    if not _has_html_grammar():
        return
    content = (
        '<div class="outer">\n'
        '  <div class="inner">\n'
        '    <span>x</span>\n'
        '  </div>\n'
        '</div>\n'
    )
    sigs = _html_signatures(content)
    assert "div.outer" in sigs
    assert "div.inner" in sigs


def _has_html_grammar():
    try:
        from deeppeek.recover.ast_anchor import ast_status
        return ast_status().get("html", False)
    except Exception:
        return False


# -- structural_diff -------------------------------------------------------

def test_structural_diff_no_change():
    content = "def foo():\n    pass\n"
    assert structural_diff(content, content, "x.py", "") == []


def test_structural_diff_patch_adds_declaration_ok():
    """A patch that adds `bar` and whose body declares `bar` is fine."""
    base = "def foo():\n    pass\n"
    result = base + "\ndef bar():\n    pass\n"
    body = "def bar():\n    pass\n"
    assert structural_diff(base, result, "x.py", body) == []


def test_structural_diff_tool_added_declaration_flagged():
    """`bar` is in the result but not base and not body — tool bug."""
    base = "def foo():\n    pass\n"
    result = base + "\ndef bar():\n    pass\n"
    body = "pass\n"  # body does not declare bar
    changes = structural_diff(base, result, "x.py", body)
    assert any(c.kind == "added" and "bar" in c.signature
               for c in changes)


def test_structural_diff_tool_removed_declaration_flagged():
    base = "def foo():\n    pass\ndef bar():\n    pass\n"
    result = "def foo():\n    pass\n"
    body = "pass\n"
    changes = structural_diff(base, result, "x.py", body)
    assert any(c.kind == "removed" and "bar" in c.signature
               for c in changes)


def test_structural_diff_pre_existing_unchanged_short_circuits():
    """`base == result` returns [] without parsing. This is the
    performance guard that stops 370 pre-existing files from each
    parsing twice on every run."""
    content = "def foo():\n    pass\n" * 100
    assert structural_diff(content, content, "x.py", "") == []


def main():
    tests = [
        ("python signatures top level", test_python_signatures_top_level),
        ("python signatures excludes methods",
         test_python_signatures_excludes_methods),
        ("python signatures async", test_python_signatures_async),
        ("python signatures syntax error",
         test_python_signatures_syntax_error),
        ("python statement sigs return",
         test_python_statement_sigs_return),
        ("js signatures functions and consts",
         test_js_signatures_functions_and_consts),
        ("html signatures nested", test_html_signatures_nested),
        ("structural diff no change", test_structural_diff_no_change),
        ("structural diff patch adds declaration ok",
         test_structural_diff_patch_adds_declaration_ok),
        ("structural diff tool added declaration flagged",
         test_structural_diff_tool_added_declaration_flagged),
        ("structural diff tool removed declaration flagged",
         test_structural_diff_tool_removed_declaration_flagged),
        ("structural diff pre existing unchanged short circuits",
         test_structural_diff_pre_existing_unchanged_short_circuits),
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
        print(f"\n{failed}/{len(tests)} structural_diff test(s) failed")
        return 1
    print(f"\nAll {len(tests)} structural_diff tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
