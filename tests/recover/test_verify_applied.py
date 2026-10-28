"""Unit tests for `pass8_7c_verify_applied_patches`.

The pass reports — never demotes — patches whose added lines are no
longer present in the final content.  Motivating case (`comfyui.py`,
2026-10-28): patch 62's `--output_rotation` arg entry was superseded
by patch 95's larger args-dict replacement, so its exact bytes are
gone from the final file even though the feature declared in the file
is correct.  The pass should warn (with the patch's idx and the first
missing line) and never change `rec.trust`.

Shape-classification tests (`_is_insert_shaped`) are the critical
half of this file.  An earlier version demoted on every reverted
`deterministic` patch and failed `test_35`/`test_36` in
`test_recover.py` — two fixtures where the patch was correct and the
check was wrong.  The pass now distinguishes insert-shaped patches
(warn on any miss) from transform-shaped patches (warn only when
*every* added line is missing).  Both motivating shapes go to the
transform branch.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.verify_applied import (
    _patch_added_lines,
    _check_patch_landed,
    _is_insert_shaped,
    pass8_7c_verify_applied_patches,
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
# _patch_added_lines
# ---------------------------------------------------------------------------

def test_added_lines_normal():
    body = "new_line_a();\nnew_line_b();\n"
    base_lines = {"existing();"}
    added = _patch_added_lines(body, base_lines)
    assert added == ["new_line_a();", "new_line_b();"]


def test_added_lines_skip_preexisting():
    body = "existing();\nnew_line();\n"
    base_lines = {"existing();"}
    added = _patch_added_lines(body, base_lines)
    assert added == ["new_line();"]


def test_added_lines_diff_body():
    body = (
        " unchanged();\n"
        "-old_removed();\n"
        "+new_added();\n"
    )
    base_lines = {"unchanged();", "old_removed();"}
    added = _patch_added_lines(body, base_lines)
    assert added == ["new_added();"], (
        f"expected only '+' lines, got {added!r}"
    )


def test_added_lines_empty_body():
    assert _patch_added_lines("", set()) == []


# ---------------------------------------------------------------------------
# _check_patch_landed
# ---------------------------------------------------------------------------

def test_check_patch_landed_all_present():
    body = "new_line();"
    base = "old();\n"
    final = "old();\nnew_line();\n"
    assert _check_patch_landed(body, base, final) == []


def test_check_patch_landed_one_missing():
    body = "new_a();\nnew_b();"
    base = "old();\n"
    final = "old();\nnew_a();\n"
    missing = _check_patch_landed(body, base, final)
    assert missing == ["new_b();"]


def test_check_patch_landed_all_missing():
    body = "new_a();\nnew_b();"
    base = "old();\n"
    final = "old();\n"
    missing = _check_patch_landed(body, base, final)
    assert set(missing) == {"new_a();", "new_b();"}


# ---------------------------------------------------------------------------
# _is_insert_shaped — the critical shape classifier
# ---------------------------------------------------------------------------

def test_insert_shaped_bare_guard():
    """A `try:/except:` block with no scope-decl header is insert-shaped.
    This is the exact shape of the motivating comfyui.py patches."""
    body = (
        "try:\n"
        "    output_rotation = float(\n"
        "        params.get('output_rotation') or 0.0)\n"
        "except (TypeError, ValueError):\n"
        "    output_rotation = 0.0\n"
    )
    base = "def gen():\n    pass\n"
    assert _is_insert_shaped(body, base) is True


def test_insert_shaped_kwarg_addition():
    """A plain multi-line call adding a kwarg is insert-shaped."""
    body = (
        "node = paste_image(doc, img, x, y, name,\n"
        "                   blend_mode=bm,\n"
        "                   rotation=output_rotation)\n"
    )
    base = "def f():\n    pass\n"
    assert _is_insert_shaped(body, base) is True


def test_insert_shaped_new_function():
    """A scope-decl body whose scope name is NOT in base is
    insert-shaped (a new function being added to the file)."""
    body = (
        "def new_helper():\n"
        "    return 42\n"
    )
    base = (
        "def existing():\n"
        "    return 1\n"
    )
    assert _is_insert_shaped(body, base) is True


def test_insert_shaped_function_with_body_not_signature():
    """A full function definition (signature + body) whose name is
    not in base is insert-shaped, NOT a signature replacement.

    Regression guard: an earlier version checked only the first line
    for `endswith(":")`, so `def new_helper():\\n    return 42\\n`
    was misclassified as a signature-only body and skipped by
    `pass8_7c` on the transform path — the check for a missing
    inserted function never fired.
    """
    body = (
        "def new_helper():\n"
        "    return 42\n"
    )
    base = (
        "def existing():\n"
        "    return 1\n"
    )
    assert _is_insert_shaped(body, base) is True, (
        "a function with a body must not be classified as "
        "signature-only"
    )


def test_transform_shaped_multiline_signature():
    """A multi-line signature-only body (`def foo(\\n  a,\\n):` with
    no statements after) is transform-shaped — `_try_signature_replace`
    swaps the multi-line signature in place.  The last non-blank
    line is `):`, which ends with `:`, so the two-condition check
    correctly fires.
    """
    body = (
        "def existing(\n"
        "    a,\n"
        "    b,\n"
        "):\n"
    )
    base = (
        "def existing(\n"
        "    a,\n"
        "):\n"
        "    return a\n"
    )
    assert _is_insert_shaped(body, base) is False


def test_transform_shaped_object_literal():
    """`board = {...}` body with `const board = ...` base: the splice
    merges the body's prefix line into the base's declaration.
    Transform-shaped."""
    body = "board = {\n    a: 1,\n    b: 2,\n};"
    base = "const board = { a: 1 };"
    assert _is_insert_shaped(body, base) is False


def test_transform_shaped_export_const_object():
    body = "export const config = {\n    x: 1,\n    y: 2,\n};"
    base = "export const config = { x: 1 };"
    assert _is_insert_shaped(body, base) is False


def test_transform_shaped_ellipsis():
    """A body containing `...` has the marker consumed by
    `_try_ellipsis_splice`.  Transform-shaped."""
    body = (
        "if (cond) return;\n"
        "if (!guard) return;\n"
        "...\n"
    )
    base = "function f() {}\n"
    assert _is_insert_shaped(body, base) is False


def test_transform_shaped_diff():
    body = (
        " function f() {\n"
        "-  return 1;\n"
        "+  return 2;\n"
        " }\n"
    )
    base = "function f() {\n  return 1;\n}\n"
    assert _is_insert_shaped(body, base) is False


def test_transform_shaped_signature():
    """A signature-only body (`def foo(...):`) is a signature swap."""
    body = "    def bar(self, a, b=0):\n"
    base = "class Foo:\n    def bar(self, a):\n        return a\n"
    assert _is_insert_shaped(body, base) is False


def test_transform_shaped_existing_scope_function():
    """A function body whose function name IS in base: scoped-replace.
    Transform-shaped."""
    body = (
        "def existing():\n"
        "    return 99\n"
    )
    base = (
        "def existing():\n"
        "    return 1\n"
    )
    assert _is_insert_shaped(body, base) is False


# ---------------------------------------------------------------------------
# Pass-level: warning on insert-shaped miss (no demotion)
# ---------------------------------------------------------------------------

def test_warns_when_insert_shaped_patch_missing():
    """Warn-only: an insert-shaped patch whose bytes are absent is
    reported but does not affect trust.  The record still ships."""
    base = "def gen():\n    paste(doc, name)\n"
    final = base
    rec = _make_rec("x.py", final, patches=[{
        "idx": 1,
        "body": "    paste(doc, name, rotation=out)\n",
        "applied_by": "deterministic",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("x.py", base)}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n >= 1, "expected at least one warning"
    assert rec.trust == "HIGH", (
        "verify-applied is warn-only; trust unchanged")
    assert "patch_content_missing" in rec.verification
    assert len(rec.verification["patch_content_missing"]) == 1
    assert any("verify-applied" in w and "idx=1" in w
               for w in rec.warnings)


def test_no_warn_when_insert_shaped_patch_present():
    """Sanity: a correct insert-shaped patch produces no warning."""
    base = "def gen():\n    paste(doc, name)\n"
    final = (
        "def gen():\n"
        "    paste(doc, name, rotation=out)\n"
    )
    rec = _make_rec("x.py", final, patches=[{
        "idx": 1,
        "body": "    paste(doc, name, rotation=out)\n",
        "applied_by": "deterministic",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("x.py", base)}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0
    assert rec.trust == "HIGH"
    assert "patch_content_missing" not in rec.verification


# ---------------------------------------------------------------------------
# Pass-level: no warning for partial transform misses
# ---------------------------------------------------------------------------

def test_no_warn_for_transform_object_literal():
    """Reproduces test_35 (`35_object_literal_replace`).  Body's
    `board = {` line becomes part of `const board = {` in final.
    Partial miss — no warning."""
    base = "const board = { a: 1 };"
    final = (
        "const board = {\n"
        "  a: 1,\n"
        "  b: 2,\n"
        "};\n"
    )
    rec = _make_rec("board.js", final, patches=[{
        "idx": 1,
        "body": (
            "board = {\n"
            "  a: 1,\n"
            "  b: 2,\n"
            "};\n"
        ),
        "applied_by": "deterministic",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("board.js", base)}
    n = pass8_7c_verify_applied_patches(
        {"board.js": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0, (
        f"partial transform miss should not warn; "
        f"verification={rec.verification!r}"
    )
    assert rec.trust == "HIGH"
    assert "patch_content_missing" not in rec.verification


def test_no_warn_for_transform_ellipsis():
    """Reproduces test_36 (`36_ellipsis_without_scope`).  The `...`
    marker is consumed by the splice — partial miss, no warning."""
    base = (
        "export function drawInpaintContext(r) {\n"
        "  if (!board.imageEl) return;\n"
        "  ctx.fillRect(0, 0, 100, 100);\n"
        "}\n"
    )
    final = (
        "export function drawInpaintContext(r) {\n"
        "  if (board.inpaintFullCanvas) return;\n"
        "  if (!board.imageEl) return;\n"
        "  ctx.fillRect(0, 0, 100, 100);\n"
        "}\n"
    )
    rec = _make_rec("view.js", final, patches=[{
        "idx": 1,
        "body": (
            "if (board.inpaintFullCanvas) return;\n"
            "if (!board.imageEl) return;\n"
            "...\n"
        ),
        "applied_by": "deterministic",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("view.js", base)}
    n = pass8_7c_verify_applied_patches(
        {"view.js": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0
    assert rec.trust == "HIGH"
    assert "patch_content_missing" not in rec.verification


# ---------------------------------------------------------------------------
# Pass-level: advisory when transform is fully missing
# ---------------------------------------------------------------------------

def test_advisory_when_transform_fully_missing():
    """A transform-shaped patch whose every added line is missing
    emits an advisory warning.  Trust stays HIGH."""
    base = "const board = { a: 1 };"
    final = base
    rec = _make_rec("board.js", final, patches=[{
        "idx": 1,
        "body": "board = {\n  a: 1,\n  b: 2,\n};\n",
        "applied_by": "deterministic",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("board.js", base)}
    n = pass8_7c_verify_applied_patches(
        {"board.js": rec}, blocks_by_idx=blocks_by_idx)
    assert n >= 1
    assert rec.trust == "HIGH"
    assert "patch_content_missing" in rec.verification
    assert any("advisory" in w and "splice-shaped" in w
               for w in rec.warnings)


# ---------------------------------------------------------------------------
# Pass-level: supersession (the comfyui.py shape)
# ---------------------------------------------------------------------------

def test_no_demote_for_supersession():
    """The motivating supersession case: patch A adds content, patch
    B replaces it with a slightly reformatted version.  Patch A's
    bytes are gone, but B's content is in the file.  Warn-only."""
    base = "args = {\n}\n"
    final = (
        "args = {\n"
        "  '--new': {\n"
        "    'type': 'str',\n"
        "    'help': 'reformatted',\n"
        "  },\n"
        "}\n"
    )
    rec = _make_rec("x.py", final, patches=[
        {
            "idx": 1,
            "body": "'--new': {'type': 'str', 'help': 'original'},\n",
            "applied_by": "deterministic",
        },
        {
            "idx": 2,
            "body": (
                "args = {\n"
                "  '--new': {\n"
                "    'type': 'str',\n"
                "    'help': 'reformatted',\n"
                "  },\n"
                "}\n"
            ),
            "applied_by": "deterministic",
        },
    ], trust="HIGH")
    blocks_by_idx = {0: _make_base("x.py", base)}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    # Patch 1's line was reformatted by patch 2; warns but does not
    # demote.
    assert rec.trust == "HIGH", (
        f"supersession must not demote; got {rec.verification!r}")
    assert n >= 1, "supersession should still produce a warning"


# ---------------------------------------------------------------------------
# Pass-level: skip cases
# ---------------------------------------------------------------------------

def test_skip_no_base():
    """No recoverable base → skip the record entirely."""
    rec = _make_rec("x.py", "content", patches=[{
        "idx": 1, "body": "added();",
        "applied_by": "deterministic",
    }], trust="HIGH")
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx={})
    assert n == 0
    assert rec.trust == "HIGH"


def test_skip_already_present_status():
    rec = _make_rec("x.py", "content", patches=[{
        "idx": 1, "body": "added();",
        "applied_by": "already-present",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("x.py", "base")}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0


def test_skip_failed_status():
    rec = _make_rec("x.py", "content", patches=[{
        "idx": 1, "body": "added();",
        "applied_by": "failed",
    }], trust="HIGH")
    blocks_by_idx = {0: _make_base("x.py", "base")}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0


def test_skip_deleted_record():
    rec = _make_rec("x.py", "content", patches=[{
        "idx": 1, "body": "added();",
        "applied_by": "deterministic",
    }], trust="HIGH")
    rec.is_deleted = True
    blocks_by_idx = {0: _make_base("x.py", "base")}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0


def test_skip_no_patches():
    rec = _make_rec("x.py", "content", patches=[], trust="HIGH")
    blocks_by_idx = {0: _make_base("x.py", "base")}
    n = pass8_7c_verify_applied_patches(
        {"x.py": rec}, blocks_by_idx=blocks_by_idx)
    assert n == 0
    assert rec.trust == "HIGH"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        # _patch_added_lines
        ("added lines normal", test_added_lines_normal),
        ("added lines skip preexisting",
         test_added_lines_skip_preexisting),
        ("added lines diff body", test_added_lines_diff_body),
        ("added lines empty body", test_added_lines_empty_body),
        # _check_patch_landed
        ("check patch landed all present",
         test_check_patch_landed_all_present),
        ("check patch landed one missing",
         test_check_patch_landed_one_missing),
        ("check patch landed all missing",
         test_check_patch_landed_all_missing),
        # _is_insert_shaped
        ("insert shaped bare guard", test_insert_shaped_bare_guard),
        ("insert shaped kwarg addition",
         test_insert_shaped_kwarg_addition),
        ("insert shaped new function",
         test_insert_shaped_new_function),
        ("insert shaped function with body not signature",
         test_insert_shaped_function_with_body_not_signature),
        ("transform shaped multiline signature",
         test_transform_shaped_multiline_signature),
        ("transform shaped object literal",
         test_transform_shaped_object_literal),
        ("transform shaped export const object",
         test_transform_shaped_export_const_object),
        ("transform shaped ellipsis", test_transform_shaped_ellipsis),
        ("transform shaped diff", test_transform_shaped_diff),
        ("transform shaped signature",
         test_transform_shaped_signature),
        ("transform shaped existing scope function",
         test_transform_shaped_existing_scope_function),
        # Pass: warn-only
        ("warns when insert-shaped patch missing",
         test_warns_when_insert_shaped_patch_missing),
        ("no warn when insert-shaped patch present",
         test_no_warn_when_insert_shaped_patch_present),
        ("no warn for transform object literal",
         test_no_warn_for_transform_object_literal),
        ("no warn for transform ellipsis",
         test_no_warn_for_transform_ellipsis),
        ("advisory when transform fully missing",
         test_advisory_when_transform_fully_missing),
        ("no demote for supersession",
         test_no_demote_for_supersession),
        # Skip cases
        ("skip no base", test_skip_no_base),
        ("skip already present status", test_skip_already_present_status),
        ("skip failed status", test_skip_failed_status),
        ("skip deleted record", test_skip_deleted_record),
        ("skip no patches", test_skip_no_patches),
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
        print(f"\n{failed}/{len(tests)} verify-applied test(s) failed")
        return 1
    print(f"\nAll {len(tests)} verify-applied tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
