"""Unit tests for `pass8_4b_check_added_unused_params`.

Companion to `test_verify_applied.py`: the same "accepted and
silently discarded" failure, but at the function-signature level
instead of the call-site level.  Motivating case (`comfyui.py`,
2026-10-28): `_paste_result` gained `output_rotation: float = 0.0`
and never references it.  The parameter is accepted, the caller
passes it, and the body ignores it.

Warn-only by design.  A function may legitimately gain a parameter
for interface-compatibility reasons (subclass overriding a base
method, callback that must accept a standard signature).  Adding a
demotion would fail-close records that are otherwise correct.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.lint import (
    _find_added_unused_params,
    pass8_4b_check_added_unused_params,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base(path, body, idx=0):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _make_rec(path, content, patches=None, trust="HIGH"):
    rec = FileRecord(path=path, state="FULL_PLUS_PATCH")
    rec.content = content
    rec.last_full_idx = 0
    rec.trust = trust
    rec.patches = patches if patches is not None else []
    return rec


# ---------------------------------------------------------------------------
# _find_added_unused_params — direct
# ---------------------------------------------------------------------------

def test_no_change_no_signal():
    base = "def foo(a):\n    return a\n"
    content = base
    assert _find_added_unused_params(base, content) == []


def test_added_param_used():
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b):\n    return a + b\n"
    assert _find_added_unused_params(base, content) == []


def test_added_param_unused():
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b):\n    return a\n"
    result = _find_added_unused_params(base, content)
    assert len(result) == 1
    assert result[0]["function"] == "foo"
    assert result[0]["param"] == "b"


def test_added_param_multiple_unused():
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b, c):\n    return a\n"
    result = _find_added_unused_params(base, content)
    params = {r["param"] for r in result}
    assert params == {"b", "c"}


def test_added_param_multiple_used_and_unused():
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b, c):\n    return a + b\n"
    result = _find_added_unused_params(base, content)
    assert len(result) == 1
    assert result[0]["param"] == "c"


def test_new_function_entirely_skipped():
    """A function that only exists in content has every parameter
    "new" — no signal, skip."""
    base = "def existing():\n    pass\n"
    content = base + "def brand_new(x, y):\n    return x + y\n"
    assert _find_added_unused_params(base, content) == []


def test_class_method_distinguished():
    """Two classes with same-named methods don't collide."""
    base = (
        "class A:\n"
        "    def m(self, x):\n"
        "        return x\n"
        "class B:\n"
        "    def m(self, x):\n"
        "        return x\n"
    )
    content = (
        "class A:\n"
        "    def m(self, x, y):\n"
        "        return x + y\n"  # y used
        "class B:\n"
        "    def m(self, x, y):\n"
        "        return x\n"  # y unused
    )
    result = _find_added_unused_params(base, content)
    assert len(result) == 1
    assert result[0]["function"] == "B.m"
    assert result[0]["param"] == "y"


def test_syntax_error_in_base_returns_empty():
    assert _find_added_unused_params(
        "def foo(:\n", "def foo(a):\n    return a\n") == []


def test_syntax_error_in_content_returns_empty():
    assert _find_added_unused_params(
        "def foo(a):\n    return a\n", "def foo(:\n") == []


# ---------------------------------------------------------------------------
# The motivating case: _paste_result gains output_rotation
# ---------------------------------------------------------------------------

def test_paste_result_motivating_case():
    """`_paste_result` gained `output_rotation: float = 0.0` and
    never references it.  This is the exact comfyui.py failure
    pass8_4b exists to catch."""
    base = (
        "def _paste_result(self, doc, result, name_override=None,\n"
        "                  blend_mode=\"normal\", output_name=None,\n"
        "                  seed_suffix=None):\n"
        "    for i, img in enumerate(result.images):\n"
        "        node = paste_image(doc, img, 0, 0, name,\n"
        "                           blend_mode=blend_mode)\n"
        "    return node\n"
    )
    # Patch adds the `output_rotation` parameter but nothing reads it.
    content = (
        "def _paste_result(self, doc, result, name_override=None,\n"
        "                  blend_mode=\"normal\", output_name=None,\n"
        "                  seed_suffix=None,\n"
        "                  output_rotation: float = 0.0):\n"
        "    for i, img in enumerate(result.images):\n"
        "        node = paste_image(doc, img, 0, 0, name,\n"
        "                           blend_mode=blend_mode)\n"
        "    return node\n"
    )
    result = _find_added_unused_params(base, content)
    assert len(result) == 1
    assert result[0]["function"] == "_paste_result"
    assert result[0]["param"] == "output_rotation"


def test_paste_result_fixed_case():
    """When the body DOES use output_rotation (e.g. passes it to
    paste_image), the check is silent."""
    base = (
        "def _paste_result(self, doc, result):\n"
        "    node = paste_image(doc, 0, 0)\n"
        "    return node\n"
    )
    content = (
        "def _paste_result(self, doc, result,\n"
        "                  output_rotation: float = 0.0):\n"
        "    node = paste_image(doc, 0, 0,\n"
        "                       rotation=output_rotation)\n"
        "    return node\n"
    )
    assert _find_added_unused_params(base, content) == []


# ---------------------------------------------------------------------------
# Pass-level
# ---------------------------------------------------------------------------

def test_pass_warns_on_added_unused():
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b):\n    return a\n"
    rec = _make_rec("x.py", content, patches=[{
        "idx": 1, "body": "def foo(a, b):",
        "applied_by": "deterministic",
    }])
    blocks_by_idx = {0: _make_base("x.py", base)}
    n = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 1
    assert rec.trust == "HIGH"  # warn-only, no demote
    assert any("added-param-unused" in w and "b" in w
               for w in rec.warnings)
    assert "added_param_warnings" in rec.verification


def test_pass_idempotent_on_rerun():
    """Re-running on the same content (via reverify) must not
    duplicate warnings."""
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b):\n    return a\n"
    rec = _make_rec("x.py", content, patches=[{
        "idx": 1, "body": "def foo(a, b):",
        "applied_by": "deterministic",
    }])
    blocks_by_idx = {0: _make_base("x.py", base)}

    n1 = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    warnings_after_first = len(rec.warnings)
    n2 = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    warnings_after_second = len(rec.warnings)

    assert n1 == 1
    assert n2 == 0, "idempotent re-run should not emit new warnings"
    assert warnings_after_first == warnings_after_second


def test_pass_skips_non_python():
    base = "function foo(a) { return a; }"
    content = "function foo(a, b) { return a; }"
    rec = _make_rec("x.js", content, patches=[{
        "idx": 1, "body": "function foo(a, b)",
        "applied_by": "deterministic",
    }])
    blocks_by_idx = {0: _make_base("x.js", base)}
    n = pass8_4b_check_added_unused_params(
        {"x.js": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0


def test_pass_skips_no_base():
    rec = _make_rec("x.py", "def foo(a, b):\n    return a\n",
                    patches=[{
                        "idx": 1, "body": "def foo(a, b):",
                        "applied_by": "deterministic",
                    }])
    rec.last_full_idx = None
    n = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx={})
    assert n == 0


def test_pass_skips_deleted():
    base = "def foo(a):\n    return a\n"
    content = "def foo(a, b):\n    return a\n"
    rec = _make_rec("x.py", content, patches=[{
        "idx": 1, "body": "def foo(a, b):",
        "applied_by": "deterministic",
    }])
    rec.is_deleted = True
    blocks_by_idx = {0: _make_base("x.py", base)}
    n = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0


def test_pass_skips_none_content():
    rec = FileRecord(path="x.py", state="MISSING")
    rec.content = None
    n = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx={})
    assert n == 0


def test_pass_skips_no_blocks_by_idx():
    """No blocks_by_idx → cannot recover base → skip."""
    rec = _make_rec("x.py", "def foo(a, b):\n    return a\n",
                    patches=[{
                        "idx": 1, "body": "def foo(a, b):",
                        "applied_by": "deterministic",
                    }])
    n = pass8_4b_check_added_unused_params(
        {"x.py": rec}, blocks_by_idx=None)
    assert n == 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        ("no change no signal", test_no_change_no_signal),
        ("added param used", test_added_param_used),
        ("added param unused", test_added_param_unused),
        ("added param multiple unused", test_added_param_multiple_unused),
        ("added param multiple used and unused",
         test_added_param_multiple_used_and_unused),
        ("new function entirely skipped",
         test_new_function_entirely_skipped),
        ("class method distinguished", test_class_method_distinguished),
        ("syntax error in base returns empty",
         test_syntax_error_in_base_returns_empty),
        ("syntax error in content returns empty",
         test_syntax_error_in_content_returns_empty),
        ("paste_result motivating case",
         test_paste_result_motivating_case),
        ("paste_result fixed case", test_paste_result_fixed_case),
        ("pass warns on added unused", test_pass_warns_on_added_unused),
        ("pass idempotent on rerun", test_pass_idempotent_on_rerun),
        ("pass skips non-python", test_pass_skips_non_python),
        ("pass skips no base", test_pass_skips_no_base),
        ("pass skips deleted", test_pass_skips_deleted),
        ("pass skips none content", test_pass_skips_none_content),
        ("pass skips no blocks_by_idx", test_pass_skips_no_blocks_by_idx),
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
        print(f"\n{failed}/{len(tests)} added-param lint test(s) failed")
        return 1
    print(f"\nAll {len(tests)} added-param lint tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
