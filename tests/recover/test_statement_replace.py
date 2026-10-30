"""Integration tests for `patch_statement_replace.try_statement_replace`.

Motivating case: an inpaint_server.py function has four `return {...}`
statements (image_b64 success, daemon-error, api-error, target).  A
body that is a modified version of the *target* must match that one,
not the image_b64 success return, which shares lines but means the
opposite thing (success vs failure).
"""
import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ast
from deeppeek.recover.patch_statement_replace import (
    try_statement_replace, _reindent_to,
)


_MOTIVATING_BASE = '''\
def handle_inpaint(payload):
    batch_id = "x"

    if summary is None:
        return {"success": False,
                "message": "daemon did not respond",
                "timing": round(elapsed, 3)}

    if summary.get("status") == "error":
        return {"success": False,
                "message": summary.get("message", "daemon error"),
                "timing": round(elapsed, 3)}

    if captured["image_b64"] is None:
        detail = "no image produced"
        return {"success": False,
                "message": detail,
                "timing": round(elapsed, 3)}

    return {"success": True,
            "image_b64": captured["image_b64"],
            "timing": round(elapsed, 3)}
'''

_MOTIVATING_BODY = '''\
return {"success": False,
        "message": detail,
        "errors": captured["errors"],
        "timing": round(elapsed, 3)}
'''


def test_replaces_target_return_not_success_return():
    """The body is a modified version of the `detail` failure return.
    It must replace THAT return, not the final `success: True`
    return."""
    result = try_statement_replace(
        _MOTIVATING_BASE, _MOTIVATING_BODY, "x.py")
    assert result is not None, "expected a match"
    assert '        detail = "no image produced"' in result, (
        "the target return's neighbour (`detail = ...`) was removed; "
        "the wrong return was replaced")
    assert '"errors": captured["errors"]' in result, (
        f"body was not spliced in:\n{result}")
    assert result.count('"image_b64": captured["image_b64"]') == 1, (
        f"the success return was clobbered:\n{result}")
    assert result.count('"message": "daemon did not respond"') == 1, (
        f"a daemon-error return was clobbered:\n{result}")
    assert result.count(
        '"message": summary.get("message", "daemon error")') == 1, (
        f"an api-error return was clobbered:\n{result}")
    assert result.count('"errors": captured["errors"]') == 1, (
        f"the body's errors key appears more than once:\n{result}")


def test_ambiguous_returns_fail_closed():
    """Two identical return statements produce equal scores; gap is
    0; fails closed (returns None)."""
    base = (
        "def f(cond):\n"
        "    if cond:\n"
        "        return {'success': False, 'timing': 0}\n"
        "    return {'success': False, 'timing': 0}\n"
    )
    body = "return {'success': False, 'timing': 0, 'errors': []}\n"
    assert try_statement_replace(base, body, "x.py") is None


def test_no_matching_statement_returns_none():
    base = "def f():\n    return 1\n"
    body = "return {'success': True, 'errors': [], 'timing': 0}\n"
    assert try_statement_replace(base, body, "x.py") is None


def test_multiple_statements_in_body_returns_none():
    """The heuristic is specifically for single-statement bodies."""
    base = "def f():\n    return 1\n"
    body = "x = 1\ny = 2\n"
    assert try_statement_replace(base, body, "x.py") is None


def test_def_body_is_not_a_statement_we_handle():
    """A body that is a `def` is a scope replace, not a statement
    replace.  The heuristic must decline."""
    base = "def f():\n    return 1\n"
    body = "def g():\n    return 2\n"
    assert try_statement_replace(base, body, "x.py") is None


def test_empty_body_returns_none():
    assert try_statement_replace("x = 1\n", "", "x.py") is None


def test_empty_base_returns_none():
    assert try_statement_replace("", "return {'a': 1}\n", "x.py") is None


def test_non_python_path_returns_none():
    """`.md` destination with a JS-shaped body falls through."""
    assert try_statement_replace(
        "const x = 1;", "const x = 2;", "readme.md") is None


def test_reindent_to_preserves_continuation():
    """The body's continuation lines keep their relative indent after
    reindent."""
    body_lines = [
        'return {"success": False,',
        '        "message": detail,',
        '        "timing": round(elapsed, 3)}',
    ]
    result = _reindent_to(body_lines, "        ")
    assert result[0] == '        return {"success": False,'
    assert result[1] == '                "message": detail,'
    assert result[2] == '                "timing": round(elapsed, 3)}'


def test_result_parses():
    """Any successful splice must produce parseable Python."""
    result = try_statement_replace(
        _MOTIVATING_BASE, _MOTIVATING_BODY, "x.py")
    assert result is not None
    ast.parse(result)


def main():
    tests = [
        ("replaces target return not success return",
         test_replaces_target_return_not_success_return),
        ("ambiguous returns fail closed",
         test_ambiguous_returns_fail_closed),
        ("no matching statement returns none",
         test_no_matching_statement_returns_none),
        ("multiple statements in body returns none",
         test_multiple_statements_in_body_returns_none),
        ("def body is not a statement we handle",
         test_def_body_is_not_a_statement_we_handle),
        ("empty body returns none", test_empty_body_returns_none),
        ("empty base returns none", test_empty_base_returns_none),
        ("non python path returns none", test_non_python_path_returns_none),
        ("reindent to preserves continuation",
         test_reindent_to_preserves_continuation),
        ("result parses", test_result_parses),
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
        print(f"\n{failed}/{len(tests)} statement_replace test(s) failed")
        return 1
    print(f"\nAll {len(tests)} statement_replace tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())