# tests/recover/test_scope_ast.py
"""Tests for `scope_ast.find_scope` and `find_enclosing_scope_name`."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.scope_ast import (
    find_scope, find_enclosing_scope_name, find_scope_body_span,
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


def test_find_python_stacked_decorators():
    base = (
        "@dec_a\n"
        "@dec_b\n"
        "def foo():\n"
        "    pass\n"
    )
    span = find_scope(base, "foo", "x.py")
    assert span is not None
    assert base[span[0]:span[1]].startswith("@dec_a")


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


def test_find_python_assignment():
    base = "X = {'a': 1}\nY = {'b': 2}\n"
    span = find_scope(base, "X", "x.py")
    assert span is not None
    start, end, kind = span
    assert base[start:end].startswith("X = {'a': 1}")


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


def test_find_js_generator_function():
    if not _has_js_grammar():
        return
    base = "function* gen() {\n  yield 1;\n}\n"
    span = find_scope(base, "gen", "x.js")
    assert span is not None


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
        print("  SKIP: tree-sitter-html not available")
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
# Embedded JS in Python strings — find_scope
# ---------------------------------------------------------------------------
#
# Closes the `const board = { ... }` regression: before this tier
# existed, `find_scope(base, "board", "pg_base.py")` returned None
# for a `.py` file with an embedded `const board = {...}`, silently
# declining every heuristic that would have placed a patch next to
# that object.

_TD = chr(34) * 3


def _has_embedded_js():
    return _has_js_grammar()


def test_find_scope_embedded_js_py():
    """`find_scope(base, "board", "x.py")` finds a `const board =
    {...}` declaration inside a `.py` file's triple-quoted string."""
    if not _has_embedded_js():
        print("  SKIP: tree-sitter-javascript not available")
        return
    base = (
        'JS = r' + _TD + '\n'
        'function syncPanelFields() {\n'
        '  const board = {\n'
        '    imageHash: null,\n'
        '    inpaintPrompt: "",\n'
        '    running: false,\n'
        '  };\n'
        '  return board;\n'
        '}\n'
        + _TD + '\n'
    )
    span = find_scope(base, "board", "x.py")
    assert span is not None, (
        "find_scope(base, 'board', 'x.py') returned None — the "
        "embedded-JS tier is missing or not wired into the .py path"
    )
    start, end, kind = span
    assert kind == "js"
    text = base[start:end]
    assert "const board = {" in text
    assert "function syncPanelFields" not in text


def test_find_scope_embedded_js_function_py():
    """`find_scope(base, "syncPanelFields", "x.py")` finds the JS
    function inside the `.py` string."""
    if not _has_embedded_js():
        return
    base = (
        'JS = r' + _TD + '\n'
        'function syncPanelFields() {\n'
        '  const board = {\n'
        '    imageHash: null,\n'
        '    inpaintPrompt: "",\n'
        '    running: false,\n'
        '  };\n'
        '  return board;\n'
        '}\n'
        + _TD + '\n'
    )
    span = find_scope(base, "syncPanelFields", "x.py")
    assert span is not None
    start, end, kind = span
    assert kind == "js"
    assert "function syncPanelFields" in base[start:end]


def test_find_scope_embedded_js_object_in_py_via_empty_path():
    """Same lookup via the empty-path fallback (used by
    `_find_named_block_end` in `patch_anchor_insert.py`).  Ensures
    both `.py` and empty path reach embedded JS."""
    if not _has_embedded_js():
        return
    base = (
        'JS = r' + _TD + '\n'
        'const board = {\n'
        '  imageHash: null,\n'
        '  inpaintPrompt: "",\n'
        '  running: false,\n'
        '};\n'
        'const view = { zoom: 1, panX: 0, panY: 0, maskCount: 0, layerCount: 0 };\n'
        + _TD + '\n'
    )
    span = find_scope(base, "board", "")
    assert span is not None, (
        "empty-path fallback does not reach embedded JS"
    )
    start, end, kind = span
    assert kind == "js"
    assert "const board = {" in base[start:end]
    assert "const view" not in base[start:end]


# ---------------------------------------------------------------------------
# find_enclosing_scope_name
# ---------------------------------------------------------------------------

def test_enclosing_scope_python_inner():
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


def test_enclosing_scope_class_method():
    base = (
        "class Foo:\n"
        "    def method(self):\n"
        "        return 1\n"
    )
    method_pos = base.index("return 1")
    name = find_enclosing_scope_name(base, method_pos, "x.py")
    assert name == "method", f"got {name!r}"


# ---------------------------------------------------------------------------
# find_scope_body_span
# ---------------------------------------------------------------------------

def test_body_span_python():
    base = "def foo():\n    pass\n    return 1\n"
    span = find_scope_body_span(base, "foo", "x.py")
    assert span is not None
    open_char, close_char = span
    assert base[open_char:close_char] == "    pass\n    return 1"


def test_body_span_python_multiline_sig():
    base = (
        "def foo(\n"
        "    a,\n"
        "    b,\n"
        "):\n"
        "    return a + b\n"
    )
    span = find_scope_body_span(base, "foo", "x.py")
    assert span is not None
    open_char, close_char = span
    assert base[open_char:close_char] == "    return a + b"


def test_body_span_python_decorated():
    base = (
        "@decorator\n"
        "def foo():\n"
        "    return 1\n"
    )
    span = find_scope_body_span(base, "foo", "x.py")
    assert span is not None
    open_char, close_char = span
    assert base[open_char:close_char] == "    return 1"


def test_body_span_python_one_liner_returns_none():
    base = "def foo(): pass\n"
    assert find_scope_body_span(base, "foo", "x.py") is None


def test_body_span_python_not_found():
    assert find_scope_body_span(
        "def foo():\n    pass\n", "bar", "x.py") is None


def test_body_span_python_syntax_error():
    assert find_scope_body_span("def foo(:\n", "foo", "x.py") is None


def test_body_span_js():
    if not _has_js_grammar():
        return
    base = "function foo() {\n  body\n}\n"
    span = find_scope_body_span(base, "foo", "x.js")
    assert span is not None
    open_char, close_char = span
    interior = base[open_char:close_char]
    assert "body" in interior
    assert "{" not in interior
    assert "}" not in interior
    assert "function foo" not in interior


def test_body_span_js_export():
    if not _has_js_grammar():
        return
    base = (
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    span = find_scope_body_span(base, "foo", "x.js")
    assert span is not None
    open_char, close_char = span
    assert "return 1;" in base[open_char:close_char]
    assert "export function" not in base[open_char:close_char]


def test_body_span_js_object_literal():
    """`const board = { a: 1, b: 2 }` — the body span is the object
    interior.  Without `_find_js_body_node` returning the `object`
    node, this returned None and the ellipsis-splice path declined
    every `const X = {...}` edit."""
    if not _has_js_grammar():
        return
    base = "const board = { a: 1, b: 2 };\n"
    span = find_scope_body_span(base, "board", "x.js")
    assert span is not None, (
        "body span for object literal returned None — "
        "_find_js_body_node is missing the `object` case"
    )
    open_char, close_char = span
    interior = base[open_char:close_char]
    assert "a: 1" in interior
    assert "b: 2" in interior
    assert "{" not in interior
    assert "}" not in interior
    assert "const board" not in interior


def test_body_span_html():
    if not _has_html_grammar():
        return
    base = "<details><summary>X</summary><p>body</p></details>"
    span = find_scope_body_span(base, "X", "x.html")
    assert span is not None
    open_char, close_char = span
    interior = base[open_char:close_char]
    assert "<summary>" in interior
    assert "<p>body</p>" in interior
    assert "</details>" not in interior
    assert interior.startswith("<summary>")


# ---------------------------------------------------------------------------
# Embedded JS in Python strings — find_scope_body_span
# ---------------------------------------------------------------------------
#
# The `len(content) >= _MIN_EMBEDDED_JS_LEN` (100) gate in
# `_iter_embedded_js_decls` matches the threshold used by
# `patch_bounds._embedded_js_top_level_stmt_ranges`.  It is
# deliberately high: small strings are almost always docstrings or
# code samples, not whole JS modules, and parsing them as JS
# produces false positives without changing any real edit.
#
# The embedded-JS tests below therefore use bases whose
# triple-quoted content exceeds 100 chars.  The `_padded_*` helpers
# build such a base from a compact one so the intent of each test
# stays readable.

def _padded_function_base():
    """A `.py` file with a padded JS module in a raw string.  The
    triple-quoted content is ~150 chars — well over the 100-char
    gate in `_iter_embedded_js_decls`.

    The function body contains a nested `const board = {...}` object
    literal, so its *interior* (between the function's `{` and `}`)
    does contain `{` and `}` — one pair, from the nested object.
    That's the shape the motivating pg_panel_js case has, and it's
    why the "no braces in the interior" assertion used by the
    plain `test_body_span_js` case does NOT apply here.
    """
    return (
        'JS = r' + _TD + '\n'
        'function syncPanelFields() {\n'
        '  const board = {\n'
        '    imageHash: null,\n'
        '    inpaintPrompt: "",\n'
        '    running: false,\n'
        '  };\n'
        '  return board;\n'
        '}\n'
        + _TD + '\n'
    )


def _padded_object_base():
    """A `.py` file with a padded JS object literal in a raw string.
    The triple-quoted content is ~125 chars — well over the 100-char
    gate in `_iter_embedded_js_decls`."""
    return (
        'JS = r' + _TD + '\n'
        'const board = {\n'
        '  imageHash: null,\n'
        '  inpaintPrompt: "",\n'
        '  inpaintFullCanvas: false,\n'
        '  running: false,\n'
        '  lastActivity: 0,\n'
        '};\n'
        + _TD + '\n'
    )


def test_body_span_embedded_js_function_py():
    """Body span for a JS function inside a `.py` triple-quoted
    string.

    Motivating shape (fixture 41 for `.js`): a body whose prefix
    modifies a function's mid-body.  The ellipsis-splice path needs
    `find_scope_body_span` to return the function's interior in
    outer-file coordinates.  Before the embedded-JS tier existed,
    `.py` files with `X_JS = r\"\"\"function foo() { ... }\"\"\"`
    returned None and the splice fell through to the strip-and-retry
    fallback (a full scope replace that drops the mid-body).

    The base's function body holds a nested object literal, so its
    interior legitimately contains one `{...}` pair from that
    literal.  Assert the span is *exactly* the function body — the
    nested braces are present, not extra.
    """
    if not _has_embedded_js():
        return
    base = _padded_function_base()
    span = find_scope_body_span(base, "syncPanelFields", "x.py")
    assert span is not None, (
        "body span for an embedded-JS function returned None — "
        "either the embedded-JS tier is missing or the "
        "len>=100 gate in `_iter_embedded_js_decls` is rejecting "
        "the (padded) test string"
    )
    open_char, close_char = span
    interior = base[open_char:close_char]
    # Inside the function's `{...}`: the nested `const board = {...}`
    # declaration, its tail statement, and no declaration header.
    assert "const board" in interior
    assert "return board" in interior
    assert "function syncPanelFields" not in interior
    # The outer file's `JS = r\"\"\"...` wrapper is not in the
    # interior — a truncated span that returned the whole string
    # would have `JS = r` in it.
    assert "JS = r" not in interior
    # Positive completeness check: exactly one nested `{...}` pair,
    # from the `const board = {...}` object literal.  Fewer means
    # the span was truncated mid-body; more means it was over-
    # extended past the function boundary.
    assert interior.count("{") == 1
    assert interior.count("}") == 1


def test_body_span_js_object_in_py():
    """Body span for a `const board = {...}` object literal inside a
    `.py` triple-quoted string.

    Regression guard: this is the exact `_find_js_body_node`-for-
    object-literal gap the embedded-JS tier closes.  `find_scope`
    found `board` fine; without `find_scope_body_span` returning
    the object interior, the ellipsis-splice path cannot mid-body
    modify the object.
    """
    if not _has_embedded_js():
        return
    base = _padded_object_base()
    span = find_scope_body_span(base, "board", "x.py")
    assert span is not None, (
        "body span for embedded JS object returned None — either "
        "the embedded-JS tier is missing or `_find_js_body_node` "
        "is not returning the object node"
    )
    open_char, close_char = span
    interior = base[open_char:close_char]
    assert "imageHash" in interior
    assert "inpaintPrompt" in interior
    assert "{" not in interior
    assert "}" not in interior
    assert "const board" not in interior


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        # Python — find_scope
        ("find python function", test_find_python_function),
        ("find python function no trailing newline",
         test_find_python_function_no_trailing_newline),
        ("find python class", test_find_python_class),
        ("find python async function", test_find_python_async_function),
        ("find python decorated", test_find_python_decorated),
        ("find python stacked decorators",
         test_find_python_stacked_decorators),
        ("find python method inside class",
         test_find_python_method_inside_class),
        ("find python not found", test_find_python_not_found),
        ("find python syntax error", test_find_python_syntax_error),
        ("find python nested scope", test_find_python_nested_scope),
        ("find python assignment", test_find_python_assignment),
        # JS — find_scope
        ("find js function", test_find_js_function),
        ("find js export function", test_find_js_export_function),
        ("find js class", test_find_js_class),
        ("find js arrow function assigned to const",
         test_find_js_arrow_function_assigned_to_const),
        ("find js generator function", test_find_js_generator_function),
        # HTML — find_scope
        ("find html details", test_find_html_details),
        # Embedded JS — find_scope
        ("find scope embedded js py", test_find_scope_embedded_js_py),
        ("find scope embedded js function py",
         test_find_scope_embedded_js_function_py),
        ("find scope embedded js object in py via empty path",
         test_find_scope_embedded_js_object_in_py_via_empty_path),
        # find_enclosing_scope_name
        ("enclosing scope python inner",
         test_enclosing_scope_python_inner),
        ("enclosing scope python outer",
         test_enclosing_scope_python_outer),
        ("enclosing scope no scope", test_enclosing_scope_no_scope),
        ("enclosing scope js", test_enclosing_scope_js),
        ("enclosing scope class method",
         test_enclosing_scope_class_method),
        # find_scope_body_span
        ("body span python", test_body_span_python),
        ("body span python multiline sig",
         test_body_span_python_multiline_sig),
        ("body span python decorated", test_body_span_python_decorated),
        ("body span python one liner returns none",
         test_body_span_python_one_liner_returns_none),
        ("body span python not found",
         test_body_span_python_not_found),
        ("body span python syntax error",
         test_body_span_python_syntax_error),
        ("body span js", test_body_span_js),
        ("body span js export", test_body_span_js_export),
        ("body span js object literal", test_body_span_js_object_literal),
        ("body span html", test_body_span_html),
        # Embedded JS — find_scope_body_span
        ("body span embedded js function py",
         test_body_span_embedded_js_function_py),
        ("body span js object in py", test_body_span_js_object_in_py),
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
