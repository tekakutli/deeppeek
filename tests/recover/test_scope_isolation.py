# tests/recover/test_scope_isolation.py

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_scope_isolation_rejects_sibling_change():
    from deeppeek.recover.patch_scope import _verify_scope_isolation
    base = (
        "function _pickMaskAction() {\n"
        "  input.addEventListener('change', onMask);\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "}\n"
    )
    # `_clearImageAction` replaced, but `_pickMaskAction` lost the
    # addEventListener. Bug 2.
    bad_result = (
        "function _pickMaskAction() {\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  clearRotation();\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, bad_result, "_clearImageAction", "x.js") is False

    # Only the target changed; siblings byte-identical.
    good_result = (
        "function _pickMaskAction() {\n"
        "  input.addEventListener('change', onMask);\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  clearRotation();\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, good_result, "_clearImageAction", "x.js") is True

def main():
    tests = [
        ("scope isolation rejects sibling change",
         test_scope_isolation_rejects_sibling_change),
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
