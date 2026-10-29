"""Tests for `_verify_scope_isolation`.

A scope-replace on `X` may change `X` and anything attached to `X`
(comments, docstrings, decorators, leading preamble).  It must NOT
change any *other* top-level declaration.  Additional declarations
may be added (the "replace one scope, add two helpers" shape).

These tests exercise the isolation predicate directly.  The pipeline
end-to-end cases live in `test_recover.py`; the `pg_panel_bindings.py`
2026-10-29 damage was NOT a scope-isolation failure (that path ran
correctly and would have passed these tests); it was
`_dedup_duplicate_blocks` mishandling JS-in-`.py` scope.  See
`test_dedup.py::test_python_embedded_js_different_functions_kept`.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.patch_scope import _verify_scope_isolation


def test_target_change_only_passes():
    base = (
        "function target() {\n"
        "  return 1;\n"
        "}\n"
        "function sibling() {\n"
        "  return 2;\n"
        "}\n"
    )
    result = (
        "function target() {\n"
        "  return 10;\n"
        "}\n"
        "function sibling() {\n"
        "  return 2;\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, result, "target", "x.js") is True


def test_sibling_change_rejected():
    """The `pg_panel_bindings.py` shape if it HAD come from
    scope-isolation: `_clearImageAction` changed correctly, but
    `_pickMaskAction` lost a line."""
    base = (
        "function _pickMaskAction() {\n"
        "  input.addEventListener('change', handler);\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "}\n"
    )
    result = (
        "function _pickMaskAction() {\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  resetRotation();\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, result, "_clearImageAction", "x.js") is False


def test_additional_decls_allowed():
    """Adding new helper functions alongside the target is
    legitimate (multi-scope bodies like `_inpaintAction` +
    `_rotatedMaskToCanvasB64` + `_setOriginalFromB64`)."""
    base = (
        "function target() {\n"
        "  return 1;\n"
        "}\n"
        "function sibling() {\n"
        "  return 2;\n"
        "}\n"
    )
    result = (
        "function target() {\n"
        "  return 10;\n"
        "}\n"
        "function new_helper_a() {\n"
        "  return 3;\n"
        "}\n"
        "function new_helper_b() {\n"
        "  return 4;\n"
        "}\n"
        "function sibling() {\n"
        "  return 2;\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, result, "target", "x.js") is True


def test_python_target_change_only_passes():
    base = (
        "def target():\n"
        "    return 1\n"
        "\n"
        "def sibling():\n"
        "    return 2\n"
    )
    result = (
        "def target():\n"
        "    return 10\n"
        "\n"
        "def sibling():\n"
        "    return 2\n"
    )
    assert _verify_scope_isolation(
        base, result, "target", "x.py") is True


def test_python_sibling_change_rejected():
    base = (
        "def target():\n"
        "    return 1\n"
        "\n"
        "def sibling():\n"
        "    return 2\n"
    )
    result = (
        "def target():\n"
        "    return 10\n"
        "\n"
        "def sibling():\n"
        "    return 200\n"
    )
    assert _verify_scope_isolation(
        base, result, "target", "x.py") is False


def main():
    tests = [
        ("target change only passes", test_target_change_only_passes),
        ("sibling change rejected", test_sibling_change_rejected),
        ("additional decls allowed", test_additional_decls_allowed),
        ("python target change only passes",
         test_python_target_change_only_passes),
        ("python sibling change rejected",
         test_python_sibling_change_rejected),
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
        print(f"\n{failed}/{len(tests)} scope-isolation test(s) failed")
        return 1
    print(f"\nAll {len(tests)} scope-isolation tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())