"""Unit tests for _diagnose_patch_failure.

The diagnostic function explains why a patch failed.  Its output
flows into warnings.txt, the manifest's patch_failure_reasons, and
per-file decisions.  The function is on the cold-failure path — a
crash here previously took down the whole pass, leaving records
at trust=SKIP instead of trust=LOW.  These tests verify that:

  · every diagnostic branch produces a non-empty, sensible string;
  · the outer wrapper never raises, even on pathological input;
  · the call site in pass6 tolerates a broken diagnostic.

If any of these fail, it means a diagnostic or a heuristic is
crashing on shapes it shouldn't, and pass6's "one bad block kills
every file" regression is back.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.apply_patches import (
    _diagnose_patch_failure, _diagnose_patch_failure_inner,
)


# ---------------------------------------------------------------------------
# Empty / degenerate input
# ---------------------------------------------------------------------------

def test_empty_body():
    msg = _diagnose_patch_failure(
        {"body": "", "context_before": ""}, "", "")
    assert msg == "empty body"


def test_whitespace_body():
    msg = _diagnose_patch_failure(
        {"body": "   \n\n  ", "context_before": ""}, "", "")
    assert msg == "empty body"


def test_no_context_no_base():
    """Body has content but nothing in prose or base matches."""
    msg = _diagnose_patch_failure(
        {"body": "some_unique_line();", "context_before": ""},
        "", "")
    assert msg, "diagnostic returned empty string"


# ---------------------------------------------------------------------------
# Branch-specific diagnostics
# ---------------------------------------------------------------------------

def test_ellipsis_block_no_scope():
    body = "  doStuff();\n  ...\n"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        "existing_content\n", "existing_content\n")
    assert "ellipsis" in msg.lower(), (
        f"expected ellipsis diagnostic, got {msg!r}")


def test_diff_body_not_applied():
    body = "-old line\n+new line\n"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        "base content\n", "base content\n")
    assert "diff" in msg.lower()


def test_export_const_object_splice():
    body = "export const totallyNew = { value: 1 };"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        "base content\n", "base content\n")
    assert "export const" in msg.lower() or "splice" in msg.lower(), (
        f"expected export-const diagnostic, got {msg!r}")


def test_deletion_instruction_with_context():
    body = "export const panelState = {\n  hovered: false,\n};\n"
    ctx = "Delete the panelState block:"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ctx},
        "unrelated base content\n", "unrelated base content\n")
    assert "deletion" in msg.lower()


def test_body_appears_multiple_times():
    body = "dup();"
    content = "dup();\ndup();\n"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        content, content)
    assert "ambiguous" in msg.lower() or "2 times" in msg


def test_body_already_present_once():
    body = "already_here();"
    content = "before();\nalready_here();\nafter();\n"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        content, content)
    assert "already appears" in msg.lower()


def test_details_scope_replace():
    body = "<details><summary>Inpainting</summary>...\n"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        "no matching scope\n", "no matching scope\n")
    assert "details" in msg.lower() or "scope-replace" in msg.lower()


def test_function_scope_replace():
    body = "export function totallyNewFn() {\n  return 1;\n}\n"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ""},
        "no matching function\n", "no matching function\n")
    assert "scope-replace" in msg.lower() or "totallyNewFn" in msg


def test_prose_anchor_absent():
    body = "unrelated();"
    ctx = "Add after `e.promptEl.disabled = busy;`:"
    msg = _diagnose_patch_failure(
        {"body": body, "context_before": ctx},
        "base without anchor\n", "base without anchor\n")
    assert "anchor" in msg.lower()


# ---------------------------------------------------------------------------
# Defensive wrapper: pathological input must not raise
# ---------------------------------------------------------------------------

def test_none_body():
    """body=None should not crash the wrapper."""
    msg = _diagnose_patch_failure(
        {"body": None, "context_before": ""}, "", "")
    assert isinstance(msg, str) and msg


def test_none_patch():
    """patch=None should not crash the wrapper."""
    msg = _diagnose_patch_failure(None, "", "")
    assert isinstance(msg, str) and msg


def test_none_content():
    """content=None should not crash the wrapper."""
    msg = _diagnose_patch_failure(
        {"body": "x();", "context_before": ""}, None, None)
    assert isinstance(msg, str) and msg


def test_inner_raises_gets_caught():
    """Simulate an inner-function crash and verify the wrapper
    catches it and returns a usable message."""
    # Monkey-patch the inner function to raise.
    import deeppeek.recover.apply_patches as ap
    original = ap._diagnose_patch_failure_inner

    def _boom(*a, **k):
        raise RuntimeError("simulated inner crash")

    try:
        ap._diagnose_patch_failure_inner = _boom
        msg = _diagnose_patch_failure(
            {"body": "x();", "context_before": ""}, "", "")
        assert isinstance(msg, str) and msg
        assert "diagnostic raised" in msg.lower()
        assert "RuntimeError" in msg
    finally:
        ap._diagnose_patch_failure_inner = original


# ---------------------------------------------------------------------------
# Integration: pass6 marks failed patches as failed, not SKIP
# ---------------------------------------------------------------------------

def test_pass6_marks_failed_patch_as_low_trust():
    """Regression: the original bug (deleted _diagnose_patch_failure)
    left failed patches at trust=SKIP.  This asserts that a patch
    which fails every heuristic is recorded as `failed` and the
    record is demoted to LOW trust.
    """
    from deeppeek.recover.apply_patches import pass6_apply_patches
    from deeppeek.recover.core import Block, FileRecord

    base_body = "export function existing() {\n  return 1;\n}\n"
    base_block = Block(
        idx=0, header_path="helper.js", modifier=None,
        fence_lang="js", body=base_body,
        body_line_count=base_body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="helper.js", output_path="helper.js",
        classified_by="preexisting",
    )

    rec = FileRecord(path="helper.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [{
        "idx": 1,
        "body": ('pgkitRegisterField("x", {\n'
                 '  get: () => 1,\n'
                 '});'),
        "context_before": "Add the following field registration:",
        "fence_lang": "js",
    }]

    pass6_apply_patches({"helper.js": rec}, {0: base_block}, None)

    assert rec.trust == "LOW", (
        f"expected trust=LOW after failed patch, got {rec.trust!r}; "
        f"verification={rec.verification!r}")
    assert rec.patches[0]["applied_by"] == "failed", (
        f"expected applied_by=failed, got "
        f"{rec.patches[0].get('applied_by')!r}")
    assert rec.patches[0].get("failure_reason"), (
        "expected non-empty failure_reason on failed patch")
    assert rec.verification.get("patches_incomplete") is True


def test_pass6_survives_diagnostic_crash():
    """If the diagnostic itself crashes, the patch must still be
    marked failed and the record must still go to LOW.  Otherwise
    a diagnostic bug becomes a silent skip.
    """
    import deeppeek.recover.apply_patches as ap
    from deeppeek.recover.core import Block, FileRecord

    base_body = "export function existing() {\n  return 1;\n}\n"
    base_block = Block(
        idx=0, header_path="x.js", modifier=None, fence_lang="js",
        body=base_body, body_line_count=3,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="x.js", output_path="x.js",
        classified_by="preexisting",
    )
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [{
        "idx": 1, "body": "orphan();\n",
        "context_before": "", "fence_lang": "js",
    }]

    from deeppeek.recover.apply_patches import pass6_apply_patches

    original = ap._diagnose_patch_failure

    def _boom(*a, **k):
        raise RuntimeError("simulated")

    try:
        ap._diagnose_patch_failure = _boom
        pass6_apply_patches({"x.js": rec}, {0: base_block}, None)
    finally:
        ap._diagnose_patch_failure = original

    assert rec.trust == "LOW"
    assert rec.patches[0]["applied_by"] == "failed"
    assert "diagnostic raised" in rec.patches[0]["failure_reason"].lower()


def main():
    tests = [
        ("empty body", test_empty_body),
        ("whitespace body", test_whitespace_body),
        ("no context no base", test_no_context_no_base),
        ("ellipsis block no scope", test_ellipsis_block_no_scope),
        ("diff body not applied", test_diff_body_not_applied),
        ("export const object splice", test_export_const_object_splice),
        ("deletion instruction with context",
         test_deletion_instruction_with_context),
        ("body appears multiple times",
         test_body_appears_multiple_times),
        ("body already present once", test_body_already_present_once),
        ("details scope replace", test_details_scope_replace),
        ("function scope replace", test_function_scope_replace),
        ("prose anchor absent", test_prose_anchor_absent),
        ("none body", test_none_body),
        ("none patch", test_none_patch),
        ("none content", test_none_content),
        ("inner raises gets caught", test_inner_raises_gets_caught),
        ("pass6 marks failed patch as low trust",
         test_pass6_marks_failed_patch_as_low_trust),
        ("pass6 survives diagnostic crash",
         test_pass6_survives_diagnostic_crash),
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
        print(f"\n{failed}/{len(tests)} diagnostic test(s) failed")
        return 1
    print(f"\nAll {len(tests)} diagnostic tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
