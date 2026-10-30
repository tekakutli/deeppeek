"""Tests for `_validate_pipeline_imports` (2026-10-29).

Motivating failure: two modules landed in the wrong directory,
producing a `ModuleNotFoundError` on every attempt at
`_try_region_replace`.  The startup check makes this visible before
pass 1 runs, instead of after dozens of test failures.
"""
import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.pipeline import _validate_pipeline_imports


def test_healthy_pipeline_imports_clean():
    """In the shipped tree, every module imports fine."""
    failures = _validate_pipeline_imports()
    assert failures == [], (
        f"healthy tree should import all modules; got {failures!r}")


def test_broken_module_detected():
    """Insert a module that fails on import; the check catches it."""
    import deeppeek.recover as pkg

    pkg_dir = Path(pkg.__path__[0])
    broken_name = "_test_broken_for_pipeline_validation"
    broken_path = pkg_dir / f"{broken_name}.py"

    try:
        broken_path.write_text(
            "import deeppeek.recover.does_not_exist_xyz  # noqa\n"
        )
        failures = _validate_pipeline_imports()
        assert any(broken_name in f for f in failures), (
            f"expected failure mentioning {broken_name!r}; "
            f"got {failures!r}")
        assert any("ModuleNotFoundError" in f for f in failures), (
            f"expected 'ModuleNotFoundError' in failure message; "
            f"got {failures!r}")
    finally:
        broken_path.unlink()
        # Purge from sys.modules so a re-run is clean.
        sys.modules.pop(
            f"deeppeek.recover.{broken_name}", None)
        if hasattr(pkg, broken_name):
            try:
                delattr(pkg, broken_name)
            except Exception:
                pass


def test_broken_module_cleanup_restores_clean():
    """Sanity: after the previous test's cleanup, the check is clean
    again."""
    failures = _validate_pipeline_imports()
    assert not any("_test_broken" in f for f in failures), (
        f"broken module not cleaned up; got {failures!r}")


def main():
    tests = [
        ("healthy pipeline imports clean",
         test_healthy_pipeline_imports_clean),
        ("broken module detected", test_broken_module_detected),
        ("broken module cleanup restores clean",
         test_broken_module_cleanup_restores_clean),
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
        print(f"\n{failed}/{len(tests)} pipeline-startup test(s) failed")
        return 1
    print(f"\nAll {len(tests)} pipeline-startup tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())