"""Tests for `_validate_pipeline_imports` (2026-10-29) and the
pre-existing-record invariant (2026-11-05).

Motivating failure for the import check: two modules landed in the
wrong directory, producing a `ModuleNotFoundError` on every attempt at
`_try_region_replace`.  The startup check makes this visible before
pass 1 runs, instead of after dozens of test failures.

Motivating failure for the pre-existing-record invariant: the
`pipeline.run` pre-existing-baseline pruning loop checked
`rec.preexisting_base`, a field set by pass 5's `pass5_deduplicate` —
from code that runs *before* pass 5.  On the first pass every
pre-existing record has `preexisting_base=False`, so `del
records[path]` never fired — but the pruning of the underlying
`blocks` list did.  The records survived with empty `source_blocks`,
landed as `state=MISSING` in pass 5, and produced one `[MISSING] no
full print` warning per untouched pre-existing file (hundreds in the
2026-10-29 run).

The invariant test below reproduces the failure shape with a
minimal tree and asserts the manifest is sane.
"""
import json
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.pipeline import (
    _validate_pipeline_imports, RecoverConfig, run,
)


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


def test_untouched_preexisting_not_pruned():
    """An untouched pre-existing file must remain in `decisions` with
    `state=SINGLE_FULL`, `preexisting_base=True`, and no `[MISSING]`
    warning.

    Failure mode caught: the pruning loop checked `preexisting_base`
    before pass 5 set it, so the record survived with empty
    `source_blocks`, landed as `state=MISSING`, and produced
    `[MISSING] no full print` in warnings.txt.  This invariant test
    fails immediately if that bug comes back.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.mkdir()

        # One untouched pre-existing source file.
        (target / "untouched.py").write_text(
            '"""Pre-existing module."""\n'
            "\n"
            "def existing():\n"
            "    return 1\n"
        )
        # One file that the conversation will replace.
        (target / "modified.py").write_text(
            "def old():\n    return 0\n"
        )

        # Minimal dump: only the modified file is printed.
        dump = (
            "## File: `modified.py`\n"
            "\n"
            "```python\n"
            "def new():\n"
            "    return 42\n"
            "```\n"
        )

        cfg = RecoverConfig(
            dump_text=dump,
            apply_to=target,
            min_trust="LOW",
        )
        rc = run(cfg)
        assert rc in (0, 1), f"unexpected exit code {rc}"

        manifest_path = target / "deeppeek" / "manifest.json"
        assert manifest_path.is_file(), (
            f"manifest missing at {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        decisions = manifest.get("decisions", {})

        # The untouched pre-existing record must be present with the
        # right state.
        u = decisions.get("untouched.py")
        assert u is not None, (
            f"untouched.py missing from decisions; "
            f"decisions keys: {sorted(decisions)}"
        )
        assert u.get("state") == "SINGLE_FULL", (
            f"untouched.py state should be SINGLE_FULL (via pass 5's "
            f"pre_fulls branch); got {u.get('state')!r}.  A "
            f"state=MISSING here means the pruning bug is back."
        )
        assert u.get("preexisting_base") is True, (
            f"untouched.py should have preexisting_base=True; "
            f"got {u.get('preexisting_base')!r}"
        )
        assert u.get("written") is False, (
            f"untouched.py should not be written; "
            f"got written={u.get('written')!r}"
        )
        assert u.get("sanity_rejected") is False, (
            f"untouched.py should not be sanity-rejected"
        )

        # warnings.txt must not contain one `[untouched.py] [MISSING]`
        # line per pre-existing file.
        warnings_path = target / "deeppeek" / "warnings.txt"
        assert warnings_path.is_file()
        warnings_text = warnings_path.read_text(encoding="utf-8")
        assert "[untouched.py] [MISSING]" not in warnings_text, (
            f"warnings.txt contains `[untouched.py] [MISSING]` — the "
            f"pruning bug is back:\n{warnings_text}"
        )
        assert "[untouched.py] [PREEXISTING]" not in warnings_text, (
            f"warnings.txt contains a per-file `[PREEXISTING]` line "
            f"for untouched.py; the aggregate `[preexisting]` line "
            f"should be the only representation:\n{warnings_text}"
        )

        # The aggregate line must be present (one per run, not one
        # per pre-existing file).
        assert "[preexisting]" in warnings_text, (
            f"warnings.txt should contain the aggregate `[preexisting]` "
            f"line:\n{warnings_text}"
        )

        # The modified file must have been written.
        assert (target / "modified.py").read_text().find("return 42") != -1, (
            "modified.py should have been rewritten"
        )


def test_preexisting_not_flooded_in_warnings():
    """Many untouched pre-existing files produce exactly one
    `[preexisting]` summary line, not one per file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.mkdir()

        # Ten untouched pre-existing files + one modified.
        for i in range(10):
            (target / f"file_{i}.py").write_text(
                f"def fn_{i}():\n    return {i}\n"
            )
        (target / "modified.py").write_text(
            "def old():\n    return 0\n"
        )

        dump = (
            "## File: `modified.py`\n"
            "\n"
            "```python\n"
            "def new():\n"
            "    return 42\n"
            "```\n"
        )

        cfg = RecoverConfig(
            dump_text=dump,
            apply_to=target,
            min_trust="LOW",
        )
        rc = run(cfg)
        assert rc in (0, 1)

        warnings_text = (target / "deeppeek" / "warnings.txt").read_text(
            encoding="utf-8")

        # Exactly one aggregate line — not ten.
        n_aggregate = warnings_text.count("[preexisting] 10 pre-existing")
        assert n_aggregate == 1, (
            f"expected exactly one `[preexisting] 10 pre-existing` "
            f"line; got {n_aggregate} occurrences.  Full warnings:\n"
            f"{warnings_text}"
        )

        # No per-file `[PREEXISTING]` lines.
        assert "[PREEXISTING]" not in warnings_text, (
            f"expected zero `[PREEXISTING]` lines (aggregate only); "
            f"found some in:\n{warnings_text}"
        )

        # No `[MISSING]` noise for the untouched files.
        for i in range(10):
            assert f"[file_{i}.py] [MISSING]" not in warnings_text, (
                f"file_{i}.py leaked a `[MISSING] no full print` "
                f"warning; pruning bug is back:\n{warnings_text}"
            )


def main():
    tests = [
        ("healthy pipeline imports clean",
         test_healthy_pipeline_imports_clean),
        ("broken module detected", test_broken_module_detected),
        ("broken module cleanup restores clean",
         test_broken_module_cleanup_restores_clean),
        ("untouched preexisting not pruned",
         test_untouched_preexisting_not_pruned),
        ("preexisting not flooded in warnings",
         test_preexisting_not_flooded_in_warnings),
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