"""Unit tests for signature-aware reference checking (Fix 1)."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.symbols import (
    find_unresolved_calls,
    find_unresolved_references,
    _analyze_python_with_sigs,
    _check_signature_mismatches,
)
from deeppeek.recover.validate import pass8_5_check_symbols


def _rec(path, content, patches=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    rec.patches = patches if patches is not None else [
        {"idx": 1, "body": "<unused>", "applied_by": "deterministic"}
    ]
    return rec


def _base_block(path, body, idx=0):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


# ---------------------------------------------------------------------------
# _check_signature_mismatches (unit)
# ---------------------------------------------------------------------------

def test_signature_no_mismatch():
    content = (
        "def foo(a, b):\n"
        "    return a + b\n"
        "\n"
        "foo(1, 2)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    assert _check_signature_mismatches(calls, sigs) == []


def test_signature_arity_overflow():
    content = (
        "def foo(a, b):\n"
        "    return a + b\n"
        "\n"
        "foo(1, 2, 3)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    problems = _check_signature_mismatches(calls, sigs)
    assert len(problems) == 1
    assert problems[0][0] == "foo"
    assert "3 positional" in problems[0][1]


def test_signature_vararg_accepts_extra_positional():
    content = (
        "def foo(a, *args):\n"
        "    return a\n"
        "\n"
        "foo(1, 2, 3, 4)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    assert _check_signature_mismatches(calls, sigs) == []


def test_signature_unexpected_kwarg():
    content = (
        "def foo(a, b):\n"
        "    return a + b\n"
        "\n"
        "foo(a=1, b=2, c=3)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    problems = _check_signature_mismatches(calls, sigs)
    assert len(problems) == 1
    assert "c" in problems[0][1]


def test_signature_kwarg_matching_positional():
    """Calling `foo(a=1)` for `def foo(a)` is legal Python."""
    content = (
        "def foo(a, b):\n"
        "    return a + b\n"
        "\n"
        "foo(a=1, b=2)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    assert _check_signature_mismatches(calls, sigs) == []


def test_signature_kwargs_accepts_anything():
    content = (
        "def foo(a, **kwargs):\n"
        "    return a\n"
        "\n"
        "foo(1, x=2, y=3)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    assert _check_signature_mismatches(calls, sigs) == []


def test_signature_kwonly_required_kwarg():
    content = (
        "def foo(a, *, b):\n"
        "    return a + b\n"
        "\n"
        "foo(1, b=2)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    assert _check_signature_mismatches(calls, sigs) == []


def test_signature_class_not_checked():
    """Class constructors are not checked (unknown __init__)."""
    content = (
        "class Foo:\n"
        "    pass\n"
        "\n"
        "Foo(1, 2, 3)\n"
    )
    _, _, calls, sigs = _analyze_python_with_sigs(content)
    assert _check_signature_mismatches(calls, sigs) == []


# ---------------------------------------------------------------------------
# find_unresolved_references
# ---------------------------------------------------------------------------

def test_no_refs_returns_empty_tuple():
    rec = FileRecord(path="x.py", state="SINGLE_FULL")
    rec.content = ""
    assert find_unresolved_references(rec, "", {}) == ([], [])


def test_signature_mismatch_detected_end_to_end():
    content = (
        "def helper(a):\n"
        "    return a\n"
        "\n"
        "helper(1, 2)\n"
    )
    base = (
        "def helper(a):\n"
        "    return a\n"
    )
    rec = _rec("x.py", content)
    unresolved, mismatched = find_unresolved_references(
        rec, base, {"x.py": rec})
    assert unresolved == []
    assert len(mismatched) == 1
    assert mismatched[0][0] == "helper"


def test_signature_mismatch_only_for_patch_touched_calls():
    """Pre-existing call with wrong arity is NOT flagged (out of scope)."""
    content = (
        "def helper(a):\n"
        "    return a\n"
        "\n"
        "helper(1, 2)\n"
    )
    base = content
    rec = _rec("x.py", content)
    unresolved, mismatched = find_unresolved_references(
        rec, base, {"x.py": rec})
    assert unresolved == []
    assert mismatched == []


def test_unresolved_and_mismatched_both_populated():
    content = (
        "def helper(a):\n"
        "    return a\n"
        "\n"
        "helper(1, 2)\n"
        "totally_missing()\n"
    )
    base = (
        "def helper(a):\n"
        "    return a\n"
    )
    rec = _rec("x.py", content)
    unresolved, mismatched = find_unresolved_references(
        rec, base, {"x.py": rec})
    assert unresolved == ["totally_missing"]
    assert len(mismatched) == 1
    assert mismatched[0][0] == "helper"


def test_backward_compat_alias():
    content = "missing_call()\n"
    rec = _rec("x.py", content)
    result = find_unresolved_calls(rec, "", {"x.py": rec})
    assert result == ["missing_call"]
    assert isinstance(result, list)


def test_js_signatures_not_checked():
    """JS signature checking is not implemented; no false positives."""
    content = (
        "function helper(a) { return a; }\n"
        "helper(1, 2, 3);\n"
    )
    base = "function helper(a) { return a; }\n"
    rec = _rec("x.js", content)
    unresolved, mismatched = find_unresolved_references(
        rec, base, {"x.js": rec})
    assert unresolved == []
    assert mismatched == []


# ---------------------------------------------------------------------------
# pass8_5_check_symbols integration
# ---------------------------------------------------------------------------

def test_pass8_5_records_both_fields():
    content = (
        "def helper(a):\n"
        "    return a\n"
        "\n"
        "helper(1, 2)\n"
        "missing()\n"
    )
    base = (
        "def helper(a):\n"
        "    return a\n"
    )
    rec = _rec("x.py", content)
    rec.trust = "HIGH"
    rec.last_full_idx = 0
    blocks_by_idx = {0: _base_block("x.py", base)}
    pass8_5_check_symbols({"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert rec.trust == "LOW"
    assert "unresolved_calls" in rec.verification
    assert "signature_mismatches" in rec.verification
    assert rec.verification["unresolved_calls"] == ["missing"]
    assert len(rec.verification["signature_mismatches"]) == 1


def test_pass8_5_no_demote_when_only_resolved_calls():
    content = (
        "def helper(a):\n"
        "    return a\n"
        "\n"
        "helper(1)\n"
    )
    base = (
        "def helper(a):\n"
        "    return a\n"
    )
    rec = _rec("x.py", content)
    rec.trust = "HIGH"
    rec.last_full_idx = 0
    blocks_by_idx = {0: _base_block("x.py", base)}
    pass8_5_check_symbols({"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert rec.trust == "HIGH"
    assert "unresolved_calls" not in rec.verification
    assert "signature_mismatches" not in rec.verification


def main():
    tests = [
        ("signature no mismatch", test_signature_no_mismatch),
        ("signature arity overflow", test_signature_arity_overflow),
        ("signature vararg accepts extra positional",
         test_signature_vararg_accepts_extra_positional),
        ("signature unexpected kwarg", test_signature_unexpected_kwarg),
        ("signature kwarg matching positional",
         test_signature_kwarg_matching_positional),
        ("signature kwargs accepts anything",
         test_signature_kwargs_accepts_anything),
        ("signature kwonly required kwarg",
         test_signature_kwonly_required_kwarg),
        ("signature class not checked", test_signature_class_not_checked),
        ("no refs returns empty tuple",
         test_no_refs_returns_empty_tuple),
        ("signature mismatch detected end to end",
         test_signature_mismatch_detected_end_to_end),
        ("signature mismatch only for patch touched calls",
         test_signature_mismatch_only_for_patch_touched_calls),
        ("unresolved and mismatched both populated",
         test_unresolved_and_mismatched_both_populated),
        ("backward compat alias", test_backward_compat_alias),
        ("js signatures not checked", test_js_signatures_not_checked),
        ("pass8.5 records both fields",
         test_pass8_5_records_both_fields),
        ("pass8.5 no demote when only resolved calls",
         test_pass8_5_no_demote_when_only_resolved_calls),
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
        print(f"\n{failed}/{len(tests)} signature test(s) failed")
        return 1
    print(f"\nAll {len(tests)} signature tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
