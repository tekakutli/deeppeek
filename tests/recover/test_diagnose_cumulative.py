# tests/recover/test_diagnose_cumulative.py

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_diagnose_uses_cumulative_context():
    """Patch 634's shape: adding `errors` to a return block that is
    only valid because an *earlier* patch (633) added the `errors`
    list. Diagnosing against the bare base reports a misleading
    'unresolved symbol'; diagnosing against cumulative state (633
    applied) says the patch is valid."""
    from deeppeek.recover.reconcile import _diagnose_exclusion
    from deeppeek.recover.core import FileRecord

    rec = FileRecord(path="inpaint_server.py", state="FULL_PLUS_PATCH")
    rec.patches = [
        {"idx": 633, "body": "...", "applied_by": "deterministic"},
        {
            "idx": 634,
            "body": (
                "return {\n"
                '    "success": False,\n'
                '    "message": detail,\n'
                '    "errors": captured["errors"],\n'
                '    "timing": round(elapsed, 3),\n'
                "}\n"
            ),
            "applied_by": "deterministic",
        },
    ]
    # cumulative_base for patch 634: base + patch 633 applied.
    cumulative = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    detail = 'no image'\n"
        "    elapsed = 0.0\n"
        "    return {\n"
        '        "success": False,\n'
        '        "message": detail,\n'
        '        "timing": round(elapsed, 3),\n'
        "    }\n"
    )
    diag = _diagnose_exclusion(
        ("patch", 634), rec, {},
        original_status="deterministic",
        cumulative_base=cumulative,
    )
    assert "for the wrong reason" in diag, (
        f"diagnostic should identify the false-positive exclusion as "
        f"a reconcile bug; got {diag!r}")


def main():
    tests = [
        ("diagnose uses cumulative context",
         test_diagnose_uses_cumulative_context),
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
