"""Unit tests for the four robustness fixes:

  1. Pair mis-detection — a "new"-prose block cannot become the old
     side of a pair.
  2. Drift detection — replace-pair with new-present-but-old-absent
     is marked already-applied, not failed.
  3. Partial splice — a scoped replace whose new_body is a prefix
     splices into the base instead of truncating it.
  4. Fuzzy line match — token-equivalent lines with different
     whitespace match at 0.95.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block
from deeppeek.recover.prose import (
    is_new_block_prose, is_old_block_prose, is_partial_replacement,
    is_scoped_replace_by_body,
)
from deeppeek.recover.pairing import (
    detect_all, build_pairs, _bodies_look_like_versions,
)
from deeppeek.recover.apply_patches import (
    _pair_already_applied, _try_replace_pair,
    _new_body_is_complete_for_scope, _try_scoped_partial_splice,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_block(idx, header, body, prose="", kind="partial"):
    return Block(
        idx=idx, header_path=header, modifier=None, fence_lang="js",
        body=body, body_line_count=body.count("\n") + 1,
        context_before=prose, line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind=kind,
        canonical_path=header, output_path=header,
    )


# ---------------------------------------------------------------------------
# 1. prose classification
# ---------------------------------------------------------------------------

def test_old_prose_find():
    assert is_old_block_prose("**CSS.** Find:")
    assert is_old_block_prose("Here is the current:")
    assert is_old_block_prose("Old code:")
    assert is_old_block_prose("Replace the block:")


def test_new_prose_with():
    assert is_new_block_prose("Replace with:")
    assert is_new_block_prose("with:")
    assert is_new_block_prose("to:")
    assert is_new_block_prose("Here is the new version:")


def test_neutral_prose():
    assert not is_old_block_prose("Just some prose.")
    assert not is_new_block_prose("Just some prose.")


def test_partial_replacement_detected():
    assert is_partial_replacement("Add the early-out. The rest is unchanged.")
    assert is_partial_replacement("Only the start of the function is below.")
    assert not is_partial_replacement("Replace the entire block.")


def test_scoped_replace_by_body():
    body = '<details><summary>Inpainting</summary></details>'
    prose = "Replace the entire Inpainting block with:"
    assert is_scoped_replace_by_body(prose, body)

    # Non-scope body
    assert not is_scoped_replace_by_body(prose, "const x = 1;")


# ---------------------------------------------------------------------------
# 2. Pair mis-detection
# ---------------------------------------------------------------------------

def test_pg_base_shaped_pairing():
    """Reproduces the pg_base case: [old CSS], [new CSS], [new <details>].

    The buggy version paired (new CSS, new <details>).  The fix
    pairs (old CSS, new CSS) and leaves the <details> block for
    scoped detection.
    """
    old_css = _mk_block(
        0, "pg_base.py",
        "  #inpaintControls textarea {\n    width: 100%;\n  }\n",
        prose="**CSS.** Find:")
    new_css = _mk_block(
        1, "pg_base.py",
        "  #inpaintPrompt {\n    width: 100%;\n  }\n",
        prose="Replace with:")
    new_details = _mk_block(
        2, "pg_base.py",
        '<details><summary>Inpainting</summary>\nbody\n</details>\n',
        prose="**Panel body.** Replace the entire Inpainting <details> for Inpainting with:")

    blocks = [old_css, new_css, new_details]
    blocks_by_idx = {b.idx: b for b in blocks}
    detect_all(blocks, blocks_by_idx)

    assert old_css.paired_with == 1, (
        f"expected (0,1) paired, got {old_css.paired_with}")
    assert new_css.paired_with == 0, (
        f"expected (0,1) paired, got {new_css.paired_with}")
    # new_details must NOT be paired with new_css (already-paired).
    # And it should have been caught by scoped detection.
    assert new_details.paired_with == -1, (
        f"expected new_details scoped (-1), got "
        f"{new_details.paired_with}")
    assert new_details.scope_name == "Inpainting"


def test_find_with_pairing():
    """Simple case: Find: + Replace with: → pair."""
    old = _mk_block(0, "a.js", "old_body", prose="Find:")
    new = _mk_block(1, "a.js", "new_body", prose="Replace with:")
    blocks = [old, new]
    blocks_by_idx = {0: old, 1: new}
    detect_all(blocks, blocks_by_idx)
    assert old.paired_with == 1
    assert new.paired_with == 0


def test_body_version_heuristic():
    a = "// Hover cursor.\nif (r && !_ctx.active) {"
    b = "// Hover cursor.\n// Skipped.\nif (r && !_ctx.active && !x) {"
    assert _bodies_look_like_versions(a, b)
    assert not _bodies_look_like_versions(
        "css css css", "<details>html html</details>")


# ---------------------------------------------------------------------------
# 3. Drift detection
# ---------------------------------------------------------------------------

def test_pair_already_applied():
    base = "prefix\nNEW_CONTENT\nsuffix\n"
    assert _pair_already_applied(base, "OLD_CONTENT", "NEW_CONTENT")
    assert not _pair_already_applied(base, "OLD_CONTENT", "DIFF_CONTENT")
    # Both present → not a clean drift
    both = "OLD_CONTENT\nNEW_CONTENT\n"
    assert not _pair_already_applied(both, "OLD_CONTENT", "NEW_CONTENT")


def test_try_replace_pair_drift_returns_base():
    """State drift: new is present, old is not.  Returns base unchanged
    so caller marks already-present."""
    base = "some content\n#inpaintPrompt {\n  color: red;\n}\n"
    old = "#inpaintControls textarea {\n  width: 100%;\n}"
    new = "#inpaintPrompt {\n  color: red;\n}"
    # Note: `new` is fully in base; `old` isn't.
    result = _try_replace_pair(base, old, new)
    assert result is base, (
        f"expected unchanged base for drift, got "
        f"{result!r}")


# ---------------------------------------------------------------------------
# 4. Partial splice
# ---------------------------------------------------------------------------

def test_new_body_completeness():
    base_scope = "export function foo() {\n  body();\n}"
    complete = "export function foo() {\n  other();\n}"
    prefix = "export function foo() {\n  early();\n  body();"
    assert _new_body_is_complete_for_scope(base_scope, complete)
    assert not _new_body_is_complete_for_scope(base_scope, prefix)


def test_scoped_partial_splice():
    base_scope = (
        "export function foo(r) {\n"
        "  if (!r) return;\n"
        "\n"
        "  ensureDefaults();\n"
        "\n"
        "  doWork();\n"
        "}"
    )
    new_body = (
        "export function foo(r) {\n"
        "  if (flag) return;\n"
        "  if (!r) return;\n"
        "\n"
        "  ensureDefaults();"
    )
    result = _try_scoped_partial_splice(base_scope, new_body)
    assert result is not None
    assert "if (flag) return;" in result
    assert "doWork();" in result, (
        f"tail should survive splice:\n{result}")
    # Both braces balanced
    assert result.count("{") == result.count("}")


def test_scoped_partial_splice_rejects_pure_deletion():
    """A new body that is a strict subset of base should not splice."""
    base_scope = "function foo() {\n  a();\n  b();\n  c();\n}"
    new_body = "function foo() {\n  a();\n  b();"
    # SequenceMatcher would only see 'delete' ops — added_lines == 0
    result = _try_scoped_partial_splice(base_scope, new_body)
    assert result is None, (
        f"pure-deletion prefix should not splice, got {result!r}")


# ---------------------------------------------------------------------------
# 5. Fuzzy line matching (token-equivalent whitespace drift)
# ---------------------------------------------------------------------------

def test_fuzzy_line_match_token_equivalent():
    """Simulates the pg_inpaint case: tokens equal, whitespace differs."""
    from deeppeek.recover.apply_patches import _try_fuzzy_replace
    base = (
        "export function buildContextMaskB64() {\n"
        "  const W = board.imageWidth,\tH = board.imageHeight;\n"
        "  return 1;\n"
        "}\n"
    )
    old = (
        "export function buildContextMaskB64() {\n"
        "  const W = board.imageWidth, H = board.imageHeight;\n"
        "  return 1;\n"
        "}\n"
    )
    new = (
        "export function buildContextMaskB64() {\n"
        "  if (guard) return;\n"
        "  const W = board.imageWidth, H = board.imageHeight;\n"
        "  return 1;\n"
        "}\n"
    )
    result = _try_fuzzy_replace(base, old, new)
    assert result is not None, (
        "expected fuzzy match to succeed with token-equivalent lines")
    assert "if (guard) return;" in result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        ("old prose Find:", test_old_prose_find),
        ("new prose with:", test_new_prose_with),
        ("neutral prose", test_neutral_prose),
        ("partial replacement detected",
         test_partial_replacement_detected),
        ("scoped replace by body", test_scoped_replace_by_body),
        ("pg_base shaped pairing", test_pg_base_shaped_pairing),
        ("find+with pairing", test_find_with_pairing),
        ("body version heuristic", test_body_version_heuristic),
        ("pair already applied", test_pair_already_applied),
        ("try replace pair drift returns base",
         test_try_replace_pair_drift_returns_base),
        ("new body completeness", test_new_body_completeness),
        ("scoped partial splice", test_scoped_partial_splice),
        ("scoped partial splice rejects pure deletion",
         test_scoped_partial_splice_rejects_pure_deletion),
        ("fuzzy line match token equivalent",
         test_fuzzy_line_match_token_equivalent),
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
        print(f"\n{failed}/{len(tests)} test(s) failed")
        return 1
    print(f"\nAll {len(tests)} robustness tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
