"""Unit tests for `structural.check_balance`.

The motivating case (comfyui.py, 2026-10-28): a Python body with a
`#` comment containing an apostrophe (`# ComfyUI's ...`) was
misclassified by `_js_balance` as having an unclosed string.  Every
Python body that reaches the dispatcher must be checked with
`ast.parse`, not `_js_balance`.

Four guarantees the tests lock in:

  1. A Python body with `#` comment apostrophes passes.
  2. A Python body with `#` comment apostrophes passes even when
     `path` is empty (the body looks Python, so `ast.parse` is
     attempted).
  3. Genuinely unbalanced JS still fails.
  4. Genuinely unbalanced Python still fails (via `ast.parse`).
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.structural import check_balance


def test_python_body_with_apostrophe_comment_passes():
    """The motivating case: `# ComfyUI's ...` in a Python comment."""
    body = (
        "def f(x):\n"
        "    # It at encode time so ComfyUI's 1 - alpha works\n"
        "    return x\n"
    )
    assert check_balance(body, "x.py") is None


def test_python_body_with_apostrophe_comment_passes_empty_path():
    """Same body, but with no path — `_looks_like_python` fires on
    the leading `def `, so `ast.parse` is still attempted."""
    body = (
        "def f(x):\n"
        "    # ComfyUI's mask\n"
        "    return x\n"
    )
    assert check_balance(body, "") is None


def test_python_body_with_apostrophe_comment_passes_indented():
    """4-space-indented body (fence indent preserved) must dedent
    cleanly before `ast.parse`."""
    body = (
        "    def comfy_set_layer_pixels(self, params):\n"
        "        # ComfyUI's 1 - alpha/255\n"
        "        return {'success': True}\n"
    )
    assert check_balance(body, "kritomatic_daemon/handlers/comfyui.py") is None


def test_multistatement_python_body_passes():
    """The `inpaint_server.py` shape: assignment + def.  Both at
    col 0 after dedent."""
    body = (
        "    captured = {'errors': []}\n"
        "\n"
        "    def on_progress(i, r):\n"
        "        captured['errors'].append(r)\n"
    )
    assert check_balance(
        body, "playgrounds/inpaint/inpaint_server.py") is None


def test_unbalanced_js_fails():
    body = "if (cond) {\n  doThing();\n"
    reason = check_balance(body, "x.js")
    assert reason is not None
    assert "braces" in reason


def test_unbalanced_python_fails():
    body = "def f(x):\n    return (\n"
    reason = check_balance(body, "x.py")
    assert reason is not None


def test_empty_body_passes():
    assert check_balance("", "x.py") is None
    assert check_balance("   \n  ", "x.py") is None


def test_js_body_still_uses_js_balance():
    """A balanced JS body must pass — `_looks_like_python` returns
    False for `if (`, so it falls to `_js_balance`."""
    body = "if (cond) {\n  doThing();\n}\n"
    assert check_balance(body, "x.js") is None


def test_html_body_falls_through_to_js_balance():
    body = '<details><summary>X</summary></details>\n'
    assert check_balance(body, "x.html") is None


def main():
    tests = [
        ("python body with apostrophe comment passes",
         test_python_body_with_apostrophe_comment_passes),
        ("python body with apostrophe comment passes empty path",
         test_python_body_with_apostrophe_comment_passes_empty_path),
        ("python body with apostrophe comment passes indented",
         test_python_body_with_apostrophe_comment_passes_indented),
        ("multistatement python body passes",
         test_multistatement_python_body_passes),
        ("unbalanced js fails", test_unbalanced_js_fails),
        ("unbalanced python fails", test_unbalanced_python_fails),
        ("empty body passes", test_empty_body_passes),
        ("js body still uses js balance",
         test_js_body_still_uses_js_balance),
        ("html body falls through to js balance",
         test_html_body_falls_through_to_js_balance),
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
        print(f"\n{failed}/{len(tests)} structural test(s) failed")
        return 1
    print(f"\nAll {len(tests)} structural tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
