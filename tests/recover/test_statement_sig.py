"""Unit tests for `statement_sig`.

The signature is what discriminates between a modified statement and
a token-coincidental one.  Tests cover:
  · extraction for each supported statement shape;
  · scoring — dict-bearing uses 0.4·key + 0.6·value (no kind base);
  · scoring — non-dict uses 0.30·kind base;
  · the added-kwarg fix (2026-10-29): a body with a new kwarg must
    not reject as a non-match against a base with fewer kwargs.

The motivating bug (2026-10-29): the pre-fix score() returned 0.0 for
`foo(a, new_flag=True)` vs `foo(a)` because the kwargs sets were
`{new_flag}` and `{}` respectively — non-empty intersection required
by an over-strict check.  An added kwarg IS a modification, so it
must score, just lower than a shared-kwarg modification.
"""
import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ast
from deeppeek.recover.statement_sig import (
    extract, score,
)


def test_extract_return_with_dict():
    stmt = ast.parse('return {"success": True, "timing": 0.0}').body[0]
    sig = extract(stmt, "x.py")
    assert sig is not None
    assert sig["kind"] == "return"
    assert set(sig["dict"].keys()) == {"success", "timing"}
    assert sig["dict"]["success"] == ("const", "True")


def test_extract_assign_with_dict():
    stmt = ast.parse('result = {"a": 1, "b": 2}').body[0]
    sig = extract(stmt, "x.py")
    assert sig["kind"] == "assign"
    assert sig["targets"] == ("result",)
    assert set(sig["dict"].keys()) == {"a", "b"}


def test_extract_call():
    stmt = ast.parse('foo(a, b=1, c=2)').body[0]
    sig = extract(stmt, "x.py")
    assert sig["kind"] == "call"
    assert sig["name"] == "foo"
    assert set(sig["kwargs"]) == {"b", "c"}
    assert sig["n_pos"] == 1


def test_extract_returns_none_for_unsupported_kind():
    stmt = ast.parse('if x: pass').body[0]
    assert extract(stmt, "x.py") is None


def test_score_identical_returns():
    a = {"kind": "return", "dict": {"s": ("const", "True"),
                                     "t": ("const", "0")}}
    b = {"kind": "return", "dict": {"s": ("const", "True"),
                                     "t": ("const", "0")}}
    assert score(a, b) == 1.0


def test_score_return_shared_keys_distinct_values():
    """Two returns sharing keys but with different literals score
    proportionally lower — the motivating case: the image_b64 return
    (success=True) does NOT score near the failure return."""
    body = {
        "kind": "return",
        "dict": {"success": ("const", "False"),
                 "message": ("name", "detail"),
                 "errors": ("sub", ("name", "captured")),
                 "timing": ("call", "round")},
    }
    image_b64 = {
        "kind": "return",
        "dict": {"success": ("const", "True"),
                 "image_b64": ("sub", ("name", "captured")),
                 "timing": ("call", "round")},
    }
    target = {
        "kind": "return",
        "dict": {"success": ("const", "False"),
                 "message": ("name", "detail"),
                 "timing": ("call", "round")},
    }
    image_score = score(body, image_b64)
    target_score = score(body, target)
    assert target_score > image_score, (
        f"target ({target_score}) should beat image ({image_score})")
    assert target_score - image_score >= 0.15, (
        f"gap should clear _MIN_GAP=0.10; got "
        f"{target_score - image_score:.3f}")


def test_score_disjoint_keys_rejects():
    a = {"kind": "return", "dict": {"x": ("const", "1")}}
    b = {"kind": "return", "dict": {"y": ("const", "2")}}
    assert score(a, b) == 0.0


def test_score_kind_mismatch_rejects():
    a = {"kind": "return", "dict": {"x": ("const", "1")}}
    b = {"kind": "assign", "targets": ("x",),
         "dict": {"x": ("const", "1")}}
    assert score(a, b) == 0.0


def test_score_call_added_kwarg_does_not_reject():
    """FIX 2026-10-29: adding a kwarg to a call is a real
    modification.  Pre-fix, the disjoint check fired because
    {new_flag} & {} == {} — non-match.  Post-fix, only
    both-non-empty-disjoint is a genuine non-match."""
    a = {"kind": "call", "name": "foo", "kwargs": ("a", "b"), "n_pos": 1}
    b = {"kind": "call", "name": "foo", "kwargs": ("a",), "n_pos": 1}
    s = score(a, b)
    assert s > 0.0, f"added-kwarg modification should score > 0; got {s}"
    assert s < 1.0, f"added-kwarg modification should not score 1.0; got {s}"


def test_score_call_no_kwargs_vs_added_kwarg_does_not_reject():
    """Body has kwargs; base has none.  Should not reject."""
    a = {"kind": "call", "name": "foo", "kwargs": ("flag",), "n_pos": 0}
    b = {"kind": "call", "name": "foo", "kwargs": (), "n_pos": 0}
    s = score(a, b)
    assert s > 0.0, f"should not reject; got {s}"


def test_score_call_disjoint_kwargs_rejects():
    """Both non-empty and disjoint — genuine non-match."""
    a = {"kind": "call", "name": "foo", "kwargs": ("a",), "n_pos": 1}
    b = {"kind": "call", "name": "foo", "kwargs": ("b",), "n_pos": 1}
    assert score(a, b) == 0.0


def test_score_none_returns_zero():
    assert score(None, {}) == 0.0
    assert score({}, None) == 0.0
    assert score(None, None) == 0.0


def main():
    tests = [
        ("extract return with dict", test_extract_return_with_dict),
        ("extract assign with dict", test_extract_assign_with_dict),
        ("extract call", test_extract_call),
        ("extract returns none for unsupported kind",
         test_extract_returns_none_for_unsupported_kind),
        ("score identical returns", test_score_identical_returns),
        ("score return shared keys distinct values",
         test_score_return_shared_keys_distinct_values),
        ("score disjoint keys rejects", test_score_disjoint_keys_rejects),
        ("score kind mismatch rejects", test_score_kind_mismatch_rejects),
        ("score call added kwarg does not reject",
         test_score_call_added_kwarg_does_not_reject),
        ("score call no kwargs vs added kwarg does not reject",
         test_score_call_no_kwargs_vs_added_kwarg_does_not_reject),
        ("score call disjoint kwargs rejects",
         test_score_call_disjoint_kwargs_rejects),
        ("score none returns zero", test_score_none_returns_zero),
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
        print(f"\n{failed}/{len(tests)} statement_sig test(s) failed")
        return 1
    print(f"\nAll {len(tests)} statement_sig tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())