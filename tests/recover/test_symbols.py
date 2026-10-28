"""Unit tests for pass 8.5 (symbol resolution).

These exercise `find_unresolved_calls` directly and `pass8_5_check_symbols`
as a pass.  The motivating bug: a `pgkitRegisterField(...)` block was
applied to `pg_panel_bindings.js`, which imports `pgkitRegisterAction`
but not `pgkitRegisterField`.  The block *compiles* but throws
`ReferenceError` at module load time, blanking the UI.

The check catches this class of failure by scanning the *patch-introduced*
bare calls (statements of the form `name(` not preceded by `.`, `?.`,
`!.`) and asserting each name resolves via import, declaration, builtin,
or another record's exports.  Any failure demotes the record to LOW trust
and the target file is preserved.

Three things to test, in order of importance:

  1. The check catches an unresolved call when the block is wrong-file.
  2. The check does not flag *method* calls (`.foo()`), keywords (`if`),
     or builtins (`parseInt`).
  3. The check does not flag calls that resolve via import, declaration,
     aliased import, namespace import, or another record's exports.

Tests construct `FileRecord` objects directly.  No subprocess, no
fixtures, sub-millisecond.  Full integration coverage lives in
`test_recover.py` (fixtures 32 and 33).
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.symbols import find_unresolved_calls
from deeppeek.recover.validate import pass8_5_check_symbols


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rec(path, content, patches=None):
    rec = FileRecord(path=path, state="FULL_PLUS_PATCH")
    rec.content = content
    rec.patches = patches if patches is not None else [
        {"idx": 1, "body": "<unused>", "applied_by": "deterministic"}
    ]
    return rec


def _make_base(path, body):
    return Block(
        idx=0, header_path=path, modifier=None, fence_lang="",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_no_patches_returns_empty():
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.content = "unresolved();\n"
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


def test_empty_content_returns_empty():
    rec = _make_rec("a.js", "")
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


def test_none_content_returns_empty():
    rec = _make_rec("a.js", None)
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


# ---------------------------------------------------------------------------
# JS: positives and negatives
# ---------------------------------------------------------------------------

def test_js_all_resolved_via_import():
    content = (
        'import { foo } from "./x.js";\n'
        'foo();\n'
    )
    base = 'import { foo } from "./x.js";\n'
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, base, {"a.js": rec}) == []


def test_js_all_resolved_via_decl():
    content = (
        'function myFunc() { return 1; }\n'
        'myFunc();\n'
    )
    base = ""
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, base, {"a.js": rec}) == []


def test_js_all_resolved_via_builtin():
    content = 'parseInt("42");\n'
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


def test_js_catches_unresolved():
    """Motivating shape: `registerField` is called but neither imported
    nor declared in the destination file.  This is exactly the
    `pg_panel_bindings.py` bug."""
    content = (
        'import { registerAction } from "./kit.js";\n'
        'const action = registerAction("x", {});\n'
        'registerField("new.field", { get: () => 1 });\n'
    )
    base = (
        'import { registerAction } from "./kit.js";\n'
        'const action = registerAction("x", {});\n'
    )
    rec = _make_rec("bindings.js", content)
    result = find_unresolved_calls(rec, base, {"bindings.js": rec})
    assert result == ["registerField"], (
        f"expected ['registerField'], got {result!r}"
    )


def test_js_method_calls_excluded():
    content = (
        'document.getElementById("x");\n'
        'console.log("hi");\n'
        'window.alert("!");\n'
    )
    rec = _make_rec("a.js", content)
    # `document`, `console`, `window` are builtins; the calls
    # themselves are method calls on those builtins, excluded by
    # the negative lookbehind on `.`.
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


def test_js_optional_chaining_method_excluded():
    """`foo?.bar()` — the `bar` is a method call, not a bare call."""
    content = 'foo?.bar();\n'
    rec = _make_rec("a.js", content)
    # foo?.bar( — `bar` is preceded by `?.` -> excluded.
    # `foo` is not followed by `(` -> not matched.
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


def test_js_keywords_excluded():
    content = (
        'if (cond) { return; }\n'
        'while (x) { break; }\n'
        'typeof x;\n'
    )
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, "", {"a.js": rec}) == []


def test_js_aliased_import_resolved():
    content = (
        'import { foo as bar } from "./x.js";\n'
        'bar();\n'
    )
    base = 'import { foo as bar } from "./x.js";\n'
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, base, {"a.js": rec}) == []


def test_js_namespace_import_resolved():
    content = (
        'import * as kit from "./kit.js";\n'
        'kit();\n'
    )
    base = 'import * as kit from "./kit.js";\n'
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, base, {"a.js": rec}) == []


def test_js_no_new_calls_no_flag():
    """If the patch introduces no new bare calls, nothing to check."""
    content = (
        'import { foo } from "./x.js";\n'
        'foo();\n'
    )
    base = (
        'import { foo } from "./x.js";\n'
        'foo();\n'
    )
    rec = _make_rec("a.js", content)
    assert find_unresolved_calls(rec, base, {"a.js": rec}) == []


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def test_python_import_resolved():
    content = (
        "from helpers import foo\n"
        "foo()\n"
    )
    base = "from helpers import foo\n"
    rec = _make_rec("a.py", content)
    assert find_unresolved_calls(rec, base, {"a.py": rec}) == []


def test_python_builtin_resolved():
    content = "print('hi')\nlen([])\n"
    rec = _make_rec("a.py", content)
    assert find_unresolved_calls(rec, "", {"a.py": rec}) == []


def test_python_catches_unresolved():
    content = (
        "from helpers import foo\n"
        "foo()\n"
        "missing_helper()\n"
    )
    base = "from helpers import foo\n"
    rec = _make_rec("a.py", content)
    result = find_unresolved_calls(rec, base, {"a.py": rec})
    assert result == ["missing_helper"], (
        f"expected ['missing_helper'], got {result!r}"
    )


# ---------------------------------------------------------------------------
# Embedded JS in .py
# ---------------------------------------------------------------------------

def test_python_embedded_js_analyzed():
    """Playground pattern: JS lives inside a Python triple-quoted
    string.  The check must analyse the JS, not the outer Python."""
    Q = chr(34) * 3
    content = (
        "PANEL_JS = r" + Q + "\n"
        'import { registerAction } from "./kit.js";\n'
        'registerAction("a", {});\n'
        'registerField("b", {});\n'
        + Q + "\n"
    )
    base = (
        "PANEL_JS = r" + Q + "\n"
        'import { registerAction } from "./kit.js";\n'
        'registerAction("a", {});\n'
        + Q + "\n"
    )
    rec = _make_rec("panel.py", content)
    result = find_unresolved_calls(rec, base, {"panel.py": rec})
    assert result == ["registerField"], (
        f"expected ['registerField'], got {result!r}"
    )


# ---------------------------------------------------------------------------
# Cross-file resolution
# ---------------------------------------------------------------------------

def test_cross_file_resolution():
    """Record A calls `foo` (no local import).  Record B exports
    `foo`.  The call resolves via cross-file resolution.

    Without B, the call is flagged.  With B, it is not."""
    a_content = 'foo();\n'
    a = _make_rec("a.js", a_content)

    unresolved_alone = find_unresolved_calls(a, "", {"a.js": a})
    assert unresolved_alone == ["foo"]

    b = FileRecord(path="b.js", state="SINGLE_FULL")
    b.content = "export function foo() { return 1; }\n"
    b.patches = []

    unresolved_with_b = find_unresolved_calls(
        a, "", {"a.js": a, "b.js": b})
    assert unresolved_with_b == [], (
        f"cross-file resolution should have resolved 'foo', "
        f"got {unresolved_with_b!r}"
    )


def test_cross_file_export_brace_form():
    """`export { foo }` also counts as an export."""
    a = _make_rec("a.js", 'foo();\n')
    b = FileRecord(path="b.js", state="SINGLE_FULL")
    b.content = "function foo() {}\nexport { foo };\n"
    b.patches = []
    unresolved = find_unresolved_calls(a, "", {"a.js": a, "b.js": b})
    assert unresolved == []


# ---------------------------------------------------------------------------
# Pass 8.5 (integration on the record level)
# ---------------------------------------------------------------------------

def test_pass8_5_demotes_to_low():
    """The bug shape, end-to-end at the pass level."""
    content = (
        'import { registerAction } from "./kit.js";\n'
        'const action = registerAction("x", {});\n'
        'const newValue = "x";\n'
        'registerField("new.field", { get: () => newValue });\n'
    )
    base_body = (
        'import { registerAction } from "./kit.js";\n'
        'const action = registerAction("x", {});\n'
    )
    rec = _make_rec("bindings.js", content)
    rec.trust = "HIGH"
    rec.last_full_idx = 0
    base_block = _make_base("bindings.js", base_body)

    records = {"bindings.js": rec}
    blocks_by_idx = {0: base_block}

    pass8_5_check_symbols(records, blocks_by_idx=blocks_by_idx)

    assert rec.trust == "LOW", (
        f"expected trust=LOW, got {rec.trust!r}")
    assert "unresolved_calls" in rec.verification
    assert rec.verification["unresolved_calls"] == ["registerField"]
    assert any("unresolved symbol" in w for w in rec.warnings), (
        f"expected warning mentioning 'unresolved symbol', "
        f"got {rec.warnings!r}")


def test_pass8_5_no_demote_when_resolved():
    """Sanity: the pass does not demote when symbols resolve."""
    content = (
        'import { registerField } from "./kit.js";\n'
        'const newValue = "x";\n'
        'registerField("new.field", { get: () => newValue });\n'
    )
    base_body = 'import { registerField } from "./kit.js";\n'
    rec = _make_rec("scrubs.js", content)
    rec.trust = "HIGH"
    rec.last_full_idx = 0
    base_block = _make_base("scrubs.js", base_body)

    records = {"scrubs.js": rec}
    blocks_by_idx = {0: base_block}

    pass8_5_check_symbols(records, blocks_by_idx=blocks_by_idx)

    assert rec.trust == "HIGH", (
        f"expected trust=HIGH, got {rec.trust!r}; "
        f"unresolved={rec.verification.get('unresolved_calls')!r}")
    assert not rec.verification.get("unresolved_calls")


def test_js_line_comment_not_treated_as_call():
    """A JS line comment mentioning `hidden (full-canvas mode)` must
    not produce a false `hidden` call.  This is the exact shape that
    demoted pg_dispatch.py to LOW trust in the real run."""
    content = (
        'function f() {\n'
        '  if (r) {\n'
        '    // Suppressed when the rect is hidden (full-canvas mode); there is\n'
        '    a();\n'
        '  }\n'
        '}\n'
    )
    base = (
        'function f() {\n'
        '  if (r) {\n'
        '    a();\n'
        '  }\n'
        '}\n'
    )
    rec = _make_rec("a.js", content)
    result = find_unresolved_calls(rec, base, {"a.js": rec})
    assert result == [], (
        f"expected no unresolved calls from a comment, got {result!r}"
    )


def test_js_block_comment_not_treated_as_call():
    """Block-comment variant of the same bug: a `/* */` block
    containing `all-white (full canvas)` must not produce `white`.
    This is the exact shape that demoted pg_inpaint.py."""
    content = (
        '/* Mask building\n'
        '     fullCanvas = true   \u2192  all-white (full canvas)\n'
        '*/\n'
        'function f() { a(); }\n'
    )
    base = 'function f() { a(); }\n'
    rec = _make_rec("a.js", content)
    result = find_unresolved_calls(rec, base, {"a.js": rec})
    assert result == [], (
        f"expected no unresolved calls from a block comment, got {result!r}"
    )


def test_js_string_literal_not_treated_as_call():
    """A call-shaped sequence inside a string literal is data, not a
    call."""
    content = (
        'function f() {\n'
        '  console.log("not a call: bar ();");\n'
        '}\n'
    )
    base = 'function f() {}\n'
    rec = _make_rec("a.js", content)
    result = find_unresolved_calls(rec, base, {"a.js": rec})
    assert result == [], (
        f"expected no unresolved calls from a string, got {result!r}"
    )


def test_pass8_5_no_false_positive_from_comment():
    """End-to-end pass check for the pg_dispatch.py shape: a replace
    pair introduces a comment containing `hidden (full-canvas mode)`.
    The patched content must not be demoted to LOW."""
    base_body = (
        "  // Inpaint context rectangle:\n"
        "  if (e.button === 0) {\n"
        "    initDrag();\n"
        "  }\n"
    )
    new_body = (
        "  // Inpaint context rectangle:\n"
        "  // Suppressed when the rect is hidden (full-canvas mode); there is\n"
        "  // nothing on screen to click.\n"
        "  if (e.button === 0 && !board.inpaintFullCanvas) {\n"
        "    initDrag();\n"
        "  }\n"
    )
    rec = _make_rec("dispatch.js", new_body)
    rec.trust = "HIGH"
    rec.last_full_idx = 0
    base_block = _make_base("dispatch.js", base_body)

    records = {"dispatch.js": rec}
    blocks_by_idx = {0: base_block}
    pass8_5_check_symbols(records, blocks_by_idx=blocks_by_idx)

    assert rec.trust == "HIGH", (
        f"expected trust=HIGH (comment prose is not a call), got "
        f"{rec.trust!r}; "
        f"unresolved_calls={rec.verification.get('unresolved_calls')!r}"
    )


def test_pass8_5_no_false_positive_from_block_comment():
    """End-to-end pass check for the pg_inpaint.py shape: a scoped
    replace introduces a `/* */` comment containing
    `all-white (full canvas)`."""
    base_body = "function f() { return 1; }\n"
    new_body = (
        "/* Mask building\n"
        "     fullCanvas = true   \u2192  all-white (full canvas)\n"
        "*/\n"
        "function f() { return 1; }\n"
    )
    rec = _make_rec("mask.js", new_body)
    rec.trust = "HIGH"
    rec.last_full_idx = 0
    base_block = _make_base("mask.js", base_body)

    records = {"mask.js": rec}
    blocks_by_idx = {0: base_block}
    pass8_5_check_symbols(records, blocks_by_idx=blocks_by_idx)

    assert rec.trust == "HIGH", (
        f"expected trust=HIGH (block-comment prose is not a call), "
        f"got {rec.trust!r}; "
        f"unresolved_calls={rec.verification.get('unresolved_calls')!r}"
    )

# ---------------------------------------------------------------------------
# Regression (2026-11-05): Python-first dispatch
# ---------------------------------------------------------------------------
#
# Pre-fix, `find_unresolved_calls` skipped Python analysis whenever
# embedded JS was detected.  A `.py` file with substantial top-level
# Python structure AND a docstring that happened to contain "import " or
# "function " would trip `_extract_dominant_js_string`, get analysed as
# JS only, and miss every Python-level unresolved call in the file.
#
# The motivating failure: `comfyui_krita.py` called
# `_rotate_about_center` and `_center_crop` from `paste_image`.  The
# patches that would have defined them never applied.  The record was
# written with `trust: HIGH, ast_parse: ok` because the symbol check
# was looking at the wrong layer of the file.

def test_python_module_with_docstring_mentioning_import():
    """A .py file with 3+ top-level defs must be analysed as Python,
    regardless of what its docstring says."""
    content = (
        '"""Helpers.  Import this from .utils, not directly."""\n'
        "\n"
        "def helper_a():\n"
        "    return missing_helper_x()\n"
        "\n"
        "def helper_b():\n"
        "    return 2\n"
        "\n"
        "def helper_c():\n"
        "    return 3\n"
    )
    base = (
        '"""Helpers.  Import this from .utils, not directly."""\n'
        "\n"
        "def helper_a():\n"
        "    return 1\n"
        "\n"
        "def helper_b():\n"
        "    return 2\n"
        "\n"
        "def helper_c():\n"
        "    return 3\n"
    )
    rec = _make_rec("helpers.py", content)
    result = find_unresolved_calls(rec, base, {"helpers.py": rec})
    assert result == ["missing_helper_x"], (
        f"expected ['missing_helper_x'], got {result!r}; "
        f"a Python module was likely treated as a JS container "
        f"because its docstring mentions 'import'"
    )


def test_comfyui_krita_shape():
    """Exact shape from the 2026-11-05 run: a Python module whose
    patched body calls two helpers that were never defined.  Must
    flag both names (before the fix, returned [])."""
    content_broken = (
        '"""Utilities for the comfy handler."""\n'
        "\n"
        "def paste_image(doc, png, x, y, rotation=0.0):\n"
        "    if rotation:\n"
        "        png = _rotate_about_center(png, rotation)\n"
        "        png = _center_crop(png, 100, 100)\n"
        "    return png\n"
        "\n"
        "def other_a():\n"
        "    return 1\n"
        "\n"
        "def other_b():\n"
        "    return 2\n"
    )
    rec = _make_rec("comfyui_krita.py", content_broken)
    result = find_unresolved_calls(rec, "", {"comfyui_krita.py": rec})
    assert set(result) == {"_rotate_about_center", "_center_crop"}, (
        f"expected both helpers flagged, got {result!r}"
    )

    # With the helpers present, the check must return clean.
    content_fixed = (
        '"""Utilities for the comfy handler."""\n'
        "\n"
        "def paste_image(doc, png, x, y, rotation=0.0):\n"
        "    if rotation:\n"
        "        png = _rotate_about_center(png, rotation)\n"
        "        png = _center_crop(png, 100, 100)\n"
        "    return png\n"
        "\n"
        "def _rotate_about_center(img, angle):\n"
        "    return img\n"
        "\n"
        "def _center_crop(img, w, h):\n"
        "    return img\n"
        "\n"
        "def other_a():\n"
        "    return 1\n"
        "\n"
        "def other_b():\n"
        "    return 2\n"
    )
    rec2 = _make_rec("comfyui_krita.py", content_fixed)
    result2 = find_unresolved_calls(
        rec2, "", {"comfyui_krita.py": rec2})
    assert result2 == [], f"expected [], got {result2!r}"


def test_js_container_still_unions_python_and_js():
    """A JS-container .py file (few top-level defs, big embedded JS)
    must have BOTH Python analysis AND JS analysis run, not one or
    the other.  Pre-fix this was an either/or."""
    Q = chr(34) * 3
    base = (
        "PANEL_JS = r" + Q + "\n"
        'import { foo } from "./kit.js";\n'
        "foo();\n"
        + Q + "\n"
    )
    # Add a Python helper that calls an undefined name; the JS side
    # is unchanged and unresolved-free.  Both must be analysed.
    content = (
        base
        + "\ndef py_helper():\n"
        "    return missing_fn()\n"
    )
    rec = _make_rec("panel.py", content)
    result = find_unresolved_calls(rec, base, {"panel.py": rec})
    assert "missing_fn" in result, (
        f"Python analysis was skipped on a JS-container file: "
        f"result={result!r}; both Python and JS must be analysed "
        f"when the file is a container"
    )

def main():
    tests = [
        ("no patches returns empty", test_no_patches_returns_empty),
        ("empty content returns empty", test_empty_content_returns_empty),
        ("none content returns empty", test_none_content_returns_empty),
        ("js all resolved via import", test_js_all_resolved_via_import),
        ("js all resolved via decl", test_js_all_resolved_via_decl),
        ("js all resolved via builtin", test_js_all_resolved_via_builtin),
        ("js catches unresolved", test_js_catches_unresolved),
        ("js method calls excluded", test_js_method_calls_excluded),
        ("js optional chaining method excluded",
         test_js_optional_chaining_method_excluded),
        ("js keywords excluded", test_js_keywords_excluded),
        ("js aliased import resolved", test_js_aliased_import_resolved),
        ("js namespace import resolved",
         test_js_namespace_import_resolved),
        ("js no new calls no flag", test_js_no_new_calls_no_flag),
        ("python import resolved", test_python_import_resolved),
        ("python builtin resolved", test_python_builtin_resolved),
        ("python catches unresolved", test_python_catches_unresolved),
        ("python embedded js analyzed",
         test_python_embedded_js_analyzed),
        ("cross-file resolution", test_cross_file_resolution),
        ("cross-file export brace form",
         test_cross_file_export_brace_form),
        ("pass 8.5 demotes to low", test_pass8_5_demotes_to_low),
        ("pass 8.5 no demote when resolved",
         test_pass8_5_no_demote_when_resolved),
        ("js line comment not treated as call",
         test_js_line_comment_not_treated_as_call),
        ("js block comment not treated as call",
         test_js_block_comment_not_treated_as_call),
        ("js string literal not treated as call",
         test_js_string_literal_not_treated_as_call),
        ("pass 8.5 no false positive from comment",
         test_pass8_5_no_false_positive_from_comment),
        ("pass 8.5 no false positive from block comment",
         test_pass8_5_no_false_positive_from_block_comment),
        ("python module with docstring mentioning import",
         test_python_module_with_docstring_mentioning_import),
        ("comfyui_krita shape", test_comfyui_krita_shape),
        ("js container still unions python and js",
         test_js_container_still_unions_python_and_js),
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
        print(f"\n{failed}/{len(tests)} symbol test(s) failed")
        return 1
    print(f"\nAll {len(tests)} symbol tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
