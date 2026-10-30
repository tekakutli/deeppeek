# tests/recover/test_scope_ast.py
"""Tests for `scope_ast.find_scope` and `find_enclosing_scope_name`."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.scope_ast import (
    find_scope, find_enclosing_scope_name,
)


# ---------------------------------------------------------------------------
# Python — find_scope
# ---------------------------------------------------------------------------

def test_find_python_function():
    base = (
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    span = find_scope(base, "foo", "x.py")
    assert span is not None
    start, end, kind = span
    assert kind == "python"
    assert base[start:end] == "def foo():\n    return 1\n"


def test_find_python_function_no_trailing_newline():
    base = "def foo():\n    return 1"
    span = find_scope(base, "foo", "x.py")
    assert span is not None
    start, end, kind = span
    assert base[start:end] == "def foo():\n    return 1"


def test_find_python_class():
    base = (
        "class Foo:\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "class Bar:\n"
        "    pass\n"
    )
    span = find_scope(base, "Foo", "x.py")
    assert span is not None
    start, end, kind = span
    assert "class Foo:" in base[start:end]
    assert "class Bar:" not in base[start:end]


def test_find_python_async_function():
    base = "async def foo():\n    return 1\n"
    span = find_scope(base, "foo", "x.py")
    assert span is not None
    assert base[span[0]:span[1]].startswith("async def foo")


def test_find_python_decorated():
    base = (
        "@decorator\n"
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    span = find_scope(base, "foo", "x.py")
    assert span is not None
    start, end, kind = span
    assert base[start:end].startswith("@decorator")
    assert "def bar" not in base[start:end]


def test_find_python_method_inside_class():
    base = (
        "class Foo:\n"
        "    def method(self):\n"
        "        return 1\n"
        "    def other(self):\n"
        "        return 2\n"
    )
    span = find_scope(base, "method", "x.py")
    assert span is not None
    start, end, kind = span
    text = base[start:end]
    assert "def method" in text
    assert "def other" not in text


def test_find_python_not_found():
    assert find_scope("def foo():\n    pass\n", "bar", "x.py") is None


def test_find_python_syntax_error():
    assert find_scope("def foo(:\n", "foo", "x.py") is None


def test_find_python_nested_scope():
    """A nested def inside another function."""
    base = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    )
    span = find_scope(base, "inner", "x.py")
    assert span is not None
    start, end, kind = span
    assert base[start:end].startswith("    def inner")


# ---------------------------------------------------------------------------
# JavaScript — find_scope
# ---------------------------------------------------------------------------

def _has_js_grammar():
    try:
        from deeppeek.recover.ast_anchor import _get_parser
        return _get_parser("javascript") is not None
    except Exception:
        return False


def test_find_js_function():
    if not _has_js_grammar():
        print("  SKIP: tree-sitter-javascript not available")
        return
    base = (
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function bar() {\n"
        "  return 2;\n"
        "}\n"
    )
    span = find_scope(base, "foo", "x.js")
    assert span is not None
    start, end, kind = span
    assert kind == "js"
    assert "function foo" in base[start:end]
    assert "function bar" not in base[start:end]


def test_find_js_export_function():
    if not _has_js_grammar():
        return
    base = (
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    span = find_scope(base, "foo", "x.js")
    assert span is not None
    start, end, kind = span
    assert base[start:end].startswith("export function foo")


def test_find_js_class():
    if not _has_js_grammar():
        return
    base = (
        "class Foo {\n"
        "  method() { return 1; }\n"
        "}\n"
        "class Bar {\n"
        "  other() { return 2; }\n"
        "}\n"
    )
    span = find_scope(base, "Foo", "x.js")
    assert span is not None
    start, end, kind = span
    assert "class Foo" in base[start:end]
    assert "class Bar" not in base[start:end]


def test_find_js_arrow_function_assigned_to_const():
    if not _has_js_grammar():
        return
    base = (
        "const foo = () => {\n"
        "  return 1;\n"
        "};\n"
        "const bar = () => {\n"
        "  return 2;\n"
        "};\n"
    )
    span = find_scope(base, "foo", "x.js")
    assert span is not None
    start, end, kind = span
    assert "const foo" in base[start:end]
    assert "const bar" not in base[start:end]


# ---------------------------------------------------------------------------
# HTML — find_scope
# ---------------------------------------------------------------------------

def _has_html_grammar():
    try:
        import tree_sitter_html  # noqa: F401
        return True
    except ImportError:
        return False


def test_find_html_details():
    if not _has_html_grammar():
        return
    base = (
        '<div class="outer">\n'
        '  <details>\n'
        '    <summary>Inpainting</summary>\n'
        '    <p>content</p>\n'
        '  </details>\n'
        '  <details>\n'
        '    <summary>Other</summary>\n'
        '    <p>other</p>\n'
        '  </details>\n'
        '</div>\n'
    )
    span = find_scope(base, "Inpainting", "x.html")
    assert span is not None
    start, end, kind = span
    assert kind == "html"
    text = base[start:end]
    assert "<summary>Inpainting</summary>" in text
    assert "<summary>Other</summary>" not in text


# ---------------------------------------------------------------------------
# find_enclosing_scope_name
# ---------------------------------------------------------------------------

def test_enclosing_scope_python():
    base = (
        "def outer():\n"
        "    x = 1\n"
        "    def inner():\n"
        "        y = 2\n"
        "        return y\n"
        "    return inner()\n"
    )
    inner_pos = base.index("y = 2")
    name = find_enclosing_scope_name(base, inner_pos, "x.py")
    assert name == "inner", f"got {name!r}"


def test_enclosing_scope_python_outer():
    base = (
        "def outer():\n"
        "    x = 1\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    )
    outer_pos = base.index("x = 1")
    name = find_enclosing_scope_name(base, outer_pos, "x.py")
    assert name == "outer", f"got {name!r}"


def test_enclosing_scope_no_scope():
    base = "x = 1\ny = 2\n"
    name = find_enclosing_scope_name(base, base.index("y = 2"), "x.py")
    assert name is None


def test_enclosing_scope_js():
    if not _has_js_grammar():
        return
    base = (
        "function outer() {\n"
        "  function inner() {\n"
        "    return 1;\n"
        "  }\n"
        "  return inner();\n"
        "}\n"
    )
    inner_pos = base.index("return 1")
    name = find_enclosing_scope_name(base, inner_pos, "x.js")
    assert name == "inner", f"got {name!r}"


def main():
    tests = [
        ("find python function", test_find_python_function),
        ("find python function no trailing newline",
         test_find_python_function_no_trailing_newline),
        ("find python class", test_find_python_class),
        ("find python async function", test_find_python_async_function),
        ("find python decorated", test_find_python_decorated),
        ("find python method inside class",
         test_find_python_method_inside_class),
        ("find python not found", test_find_python_not_found),
        ("find python syntax error", test_find_python_syntax_error),
        ("find python nested scope", test_find_python_nested_scope),
        ("find js function", test_find_js_function),
        ("find js export function", test_find_js_export_function),
        ("find js class", test_find_js_class),
        ("find js arrow function assigned to const",
         test_find_js_arrow_function_assigned_to_const),
        ("find html details", test_find_html_details),
        ("enclosing scope python", test_enclosing_scope_python),
        ("enclosing scope python outer",
         test_enclosing_scope_python_outer),
        ("enclosing scope no scope", test_enclosing_scope_no_scope),
        ("enclosing scope js", test_enclosing_scope_js),
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
        print(f"\n{failed}/{len(tests)} scope_ast test(s) failed")
        return 1
    print(f"\nAll {len(tests)} scope_ast tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
