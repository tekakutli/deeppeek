"""Tests for the cumulative-context branch of `_diagnose_exclusion`.

A patch that only applies on top of an earlier patch in the same
record must be diagnosed against that cumulative state, not against
the bare base.  Motivating case (2026-10-29 inpaint_server.py):
patch 634 adds `"errors": captured["errors"],` to the failure return
block; `captured["errors"]` is only defined because patch 633 added
it.  Diagnosing 634 against the bare base reports a misleading
"introduces an unresolved symbol"; diagnosing against `base + 633`
identifies whether the exclusion was legitimate or was a false
positive of `_find_minimum_exclusions`.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import FileRecord
from deeppeek.recover.reconcile import _diagnose_exclusion


def _make_rec():
    rec = FileRecord(path="inpaint_server.py", state="FULL_PLUS_PATCH")
    rec.patches = [
        {"idx": 633, "body": "...", "applied_by": "deterministic"},
        {
            "idx": 634,
            "body": (
                "detail = captured.get(\"message\") or \"no image produced\"\n"
                "return {\n"
                "    \"success\": False,\n"
                "    \"message\": detail,\n"
                "    \"errors\": captured[\"errors\"],\n"
                "    \"timing\": round(elapsed, 3),\n"
                "}\n"
            ),
            "applied_by": "deterministic",
        },
    ]
    return rec


def test_diagnose_detects_valid_on_cumulative_state():
    """The exact 2026-10-29 shape: patch 634 applies cleanly on top
    of 633.  Diagnostic must say so (either "reconcile was wrong" or
    "applies structurally") rather than the misleading pre-fix
    "introduces an unresolved symbol"."""
    rec = _make_rec()
    cumulative = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    detail = 'no image'\n"
        "    elapsed = 0.0\n"
        "    if captured['image_b64'] is None:\n"
        "        detail = captured.get('message') or 'no image produced'\n"
        "        return {\n"
        "            'success': False,\n"
        "            'message': detail,\n"
        "            'timing': round(elapsed, 3),\n"
        "        }\n"
        "    return {'success': True}\n"
    )
    diag = _diagnose_exclusion(
        ("patch", 634), rec, {},
        original_status="deterministic",
        cumulative_base=cumulative,
    )
    # The pre-fix message names a non-existent unresolved symbol.
    # After the fix the message either (a) reports the specific
    # validate_content error, (b) reports a specific unresolved
    # symbol, or (c) reports that reconcile was wrong.  It must NOT
    # be the bare "introduces an unresolved symbol or structural
    # failure; content not found in any other file".
    assert "content not found in any other file" not in diag, (
        f"pre-fix diagnostic shape; got {diag!r}")


def test_diagnose_detects_fails_on_cumulative_state():
    """When the patch really does fail on cumulative state (e.g. its
    target structure isn't there yet), the diagnostic falls through
    to the pre-fix message rather than claiming reconcile was wrong."""
    rec = _make_rec()
    # Base missing the failure-return block entirely.
    cumulative = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    return {'success': True}\n"
    )
    diag = _diagnose_exclusion(
        ("patch", 634), rec, {},
        original_status="deterministic",
        cumulative_base=cumulative,
    )
    # Whatever the exact message, it must not contain the "reconcile
    # was wrong" substring — the patch genuinely doesn't apply here.
    assert "reconcile excluded it for the wrong reason" not in diag, (
        f"false-positive 'reconcile was wrong'; got {diag!r}")


def main():
    tests = [
        ("diagnose detects valid on cumulative state",
         test_diagnose_detects_valid_on_cumulative_state),
        ("diagnose detects fails on cumulative state",
         test_diagnose_detects_fails_on_cumulative_state),
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
        print(f"\n{failed}/{len(tests)} cumulative-diagnose test(s) failed")
        return 1
    print(f"\nAll {len(tests)} cumulative-diagnose tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())