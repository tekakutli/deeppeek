"""Tests for the tool-bug surfacing infrastructure (2026-10-29).

Motivating failure: two new modules (`statement_sig`,
`patch_statement_replace`) landed in `tests/recover/` instead of
`src/deeppeek/recover/`.  Every `_try_region_replace` call raised
`ModuleNotFoundError`, which the wrapper silently caught, cascading
37 test failures.  Post-fix, a raise of `_TOOL_BUG_EXCEPTIONS` is
recorded on the patch dict as `tool_bug`, forwarded to `rec.warnings`
as a "deeppeek bug" line, and tagged `[TOOL_BUG]` in warnings.txt.
Trust policy is unchanged (LOW on non-application); the point is a
*distinguishable* status.
"""
import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import json
import tempfile
from pathlib import Path

import deeppeek.recover.patch_dispatch as pd
from deeppeek.recover.core import Block, FileRecord


def _base_block(body, idx=0, path="x.py"):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="py",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _patch(body, idx=1):
    return {
        "idx": idx, "body": body, "context_before": "",
        "fence_lang": "py",
    }


def test_tool_bug_exceptions_list():
    """The tuple marks code invariants, not data conditions."""
    assert ImportError in pd._TOOL_BUG_EXCEPTIONS
    assert ModuleNotFoundError in pd._TOOL_BUG_EXCEPTIONS
    assert NameError in pd._TOOL_BUG_EXCEPTIONS
    # Data-condition exceptions must NOT be in the list.
    assert ValueError not in pd._TOOL_BUG_EXCEPTIONS
    assert AttributeError not in pd._TOOL_BUG_EXCEPTIONS
    assert SyntaxError not in pd._TOOL_BUG_EXCEPTIONS


def test_module_not_found_records_tool_bug():
    """Simulate the 2026-10-29 failure: `_try_region_replace` raises
    ModuleNotFoundError.  The wrapper must record it on the patch."""
    original = pd._try_region_replace

    def _raise(*a, **k):
        raise ModuleNotFoundError("simulated missing module")

    try:
        pd._try_region_replace = _raise
        patch = _patch("return {'success': True, 'timing': 0}\n")
        base = "def f():\n    return 1\n"
        result = pd._try_deterministic_patch(base, patch, "x.py")
        assert result is None
        assert "tool_bug" in patch, (
            f"tool_bug not recorded; patch={patch!r}")
        assert "ModuleNotFoundError" in patch["tool_bug"]
        assert "simulated missing module" in patch["tool_bug"]
    finally:
        pd._try_region_replace = original


def test_value_error_is_not_a_tool_bug():
    """A data-condition raise (ValueError) must NOT be recorded as a
    tool bug."""
    original = pd._try_region_replace

    def _raise(*a, **k):
        raise ValueError("body shape isn't right")

    try:
        pd._try_region_replace = _raise
        patch = _patch("return {'success': True, 'timing': 0}\n")
        base = "def f():\n    return 1\n"
        result = pd._try_deterministic_patch(base, patch, "x.py")
        assert result is None
        assert "tool_bug" not in patch, (
            f"ValueError should not be a tool bug; patch={patch!r}")
    finally:
        pd._try_region_replace = original


def test_pass6_records_tool_bug_in_verification():
    """`pass6_apply_patches` propagates the recorded tool_bug into
    `rec.verification["tool_bug"]` and adds a warning."""
    from deeppeek.recover.apply_patches import pass6_apply_patches

    base_body = "def f():\n    return 1\n"
    body = "return {'success': True, 'timing': 0}\n"

    base_block = _base_block(base_body)
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [_patch(body)]

    original = pd._try_region_replace

    def _raise(*a, **k):
        raise ModuleNotFoundError("simulated")

    try:
        pd._try_region_replace = _raise
        pass6_apply_patches({"x.py": rec}, {0: base_block}, None)
    finally:
        pd._try_region_replace = original

    assert rec.patches[0].get("tool_bug"), (
        f"tool_bug missing; patch={rec.patches[0]!r}")
    assert "tool_bug" in rec.verification
    assert rec.verification["tool_bug"][0]["idx"] == 1
    assert "ModuleNotFoundError" in rec.verification["tool_bug"][0]["error"]
    assert any("deeppeek bug" in w for w in rec.warnings), (
        f"expected 'deeppeek bug' warning; got {rec.warnings!r}")
    # Trust policy unchanged: fail-closed on non-application.
    assert rec.trust == "LOW"


def test_pass9_emits_tool_bug_line():
    """The warnings writer emits `[TOOL_BUG]` (not `[PATCH_FAILED]`)
    for a patch carrying `tool_bug`."""
    from deeppeek.recover.write import pass9_write

    base_body = "def f():\n    return 1\n"
    base_block = _base_block(base_body)
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.content = base_body
    rec.trust = "LOW"
    rec.patches = [{
        "idx": 1,
        "body": "return {'success': True, 'timing': 0}\n",
        "context_before": "",
        "fence_lang": "py",
        "applied_by": "failed",
        "failure_reason": "deeppeek bug: ModuleNotFoundError: simulated",
        "tool_bug": "ModuleNotFoundError: simulated",
    }]

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        pass9_write(
            records={"x.py": rec},
            blocks=[base_block],
            deleted_files=[],
            out_dir=out_dir,
            layout="flat",
            min_trust="MEDIUM",
            dump_display="<test>",
            dump_size=0,
            dump_sha="0",
            dump_text="",
            llm=None,
            llm_mode="off",
            cross_validate={"exports": {}, "imports": {}, "problems": []},
            apply_to=None,
            resolutions=[],
            cross_file_duplicates=[],
            verbose=False,
            run_errors=[],
        )
        warnings = (out_dir / "warnings.txt").read_text()
        assert "[TOOL_BUG]" in warnings, (
            f"expected [TOOL_BUG] in warnings.txt; got:\n{warnings}")
        assert "[PATCH_FAILED]" not in warnings, (
            f"[PATCH_FAILED] should be suppressed for tool bugs; "
            f"got:\n{warnings}")
        manifest = json.loads((out_dir / "manifest.json").read_text())
        reasons = (manifest.get("decisions", {})
                           .get("x.py", {})
                           .get("patch_failure_reasons", []))
        assert any(r.get("tool_bug") for r in reasons), (
            f"expected tool_bug in patch_failure_reasons; got {reasons!r}")


def main():
    tests = [
        ("tool bug exceptions list", test_tool_bug_exceptions_list),
        ("module not found records tool bug",
         test_module_not_found_records_tool_bug),
        ("value error is not a tool bug",
         test_value_error_is_not_a_tool_bug),
        ("pass6 records tool bug in verification",
         test_pass6_records_tool_bug_in_verification),
        ("pass9 emits tool bug line", test_pass9_emits_tool_bug_line),
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
        print(f"\n{failed}/{len(tests)} tool-bug test(s) failed")
        return 1
    print(f"\nAll {len(tests)} tool-bug tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())