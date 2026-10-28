"""Tests for `attribute._pick_by_content`.

The motivating case: `pg_base.py` exists seven times under
`playgrounds/*/pg_base.py`.  Prose attribution and anchor attribution
both produce multiple candidates with identical basename positions;
nearest-mention cannot break the tie.  Content scoring uses the
block's own identifiers and structure to pick the correct one.

The scoring formula in `_pick_by_content` uses:
  · line density (verbatim line overlap)
  · decl overlap (top-level declaration overlap)
  · token overlap (quoted-vs-bare identifier overlap, min-normalized)
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.attribute import _pick_by_content
from deeppeek.recover.resolve import _score


def _base_block(path, body, idx=0):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _rec(path, content=None, last_full_idx=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    rec.last_full_idx = last_full_idx
    rec.preexisting_base = True
    return rec


def _orphan(body, idx=0):
    return Block(
        idx=idx, header_path=None, modifier=None, fence_lang="js",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=True, is_code_file=True, kind="partial",
    )


# ---------------------------------------------------------------------------
# _pick_by_content
# ---------------------------------------------------------------------------

def test_pick_by_content_picks_line_density():
    """The block's *lines* are verbatim in the correct candidate.
    Realistic case: a reprint of the same code the candidate contains."""
    body = (
        "function existing() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function also_existing() {\n"
        "  return 2;\n"
        "}\n"
    )
    block = _orphan(body)

    board_base = _base_block("pg_state.py", (
        "function unrelated() {\n"
        "  return 99;\n"
        "}\n"
    ))
    inpaint_base = _base_block("pg_state.py", body)
    records = {
        "playgrounds/board/pg_state.py":
            _rec("playgrounds/board/pg_state.py", last_full_idx=0),
        "playgrounds/inpaint/pg_state.py":
            _rec("playgrounds/inpaint/pg_state.py", last_full_idx=1),
    }
    blocks_by_idx = {0: board_base, 1: inpaint_base}
    candidates = [
        "playgrounds/board/pg_state.py",
        "playgrounds/inpaint/pg_state.py",
    ]

    result = _pick_by_content(block, candidates, records, blocks_by_idx)
    assert result == "playgrounds/inpaint/pg_state.py", (
        f"expected inpaint candidate, got {result!r}")


def test_pick_by_content_picks_decl_overlap():
    body = (
        "export function fittedImageRect() {\n"
        "  return {x: 0, y: 0};\n"
        "}\n"
        "\n"
        "export function clampPan() {\n"
        "  return;\n"
        "}\n"
    )
    block = _orphan(body)

    board_base = _base_block("pg_state.py", (
        "export const view = { zoom: 1 };\n"
        "export function drawInpaintContext() {}\n"
    ))
    inpaint_base = _base_block("pg_state.py", (
        "export function fittedImageRect() {\n"
        "  return null;\n"
        "}\n"
        "export function clampPan() {\n"
        "  return;\n"
        "}\n"
    ))
    records = {
        "playgrounds/board/pg_state.py":
            _rec("playgrounds/board/pg_state.py", last_full_idx=0),
        "playgrounds/inpaint/pg_state.py":
            _rec("playgrounds/inpaint/pg_state.py", last_full_idx=1),
    }
    blocks_by_idx = {0: board_base, 1: inpaint_base}
    candidates = [
        "playgrounds/board/pg_state.py",
        "playgrounds/inpaint/pg_state.py",
    ]

    result = _pick_by_content(block, candidates, records, blocks_by_idx)
    assert result == "playgrounds/inpaint/pg_state.py", (
        f"expected inpaint candidate (decl overlap), got {result!r}")


def test_pick_by_content_picks_identifier_overlap():
    """The motivating case: JS block vs HTML bases.  No shared lines,
    no shared decls — only shared identifiers appear via the HTML
    bases' `id="..."` attributes."""
    body = (
        "const inpaintPromptEl = document.getElementById(\"inpaintPrompt\");\n"
        "if (inpaintPromptEl) {\n"
        "  inpaintPromptEl.value = board.inpaintPrompt || \"\";\n"
        "}\n"
        "const inpaintFullCanvasEl = document.getElementById(\"inpaintFullCanvas\");\n"
        "if (inpaintFullCanvasEl) {\n"
        "  inpaintFullCanvasEl.addEventListener(\"change\", () => {});\n"
        "}\n"
    )
    block = _orphan(body)

    board_base = _base_block("pg_base.py", (
        "<div class=\"board\">\n"
        "  <canvas id=\"c\"></canvas>\n"
        "</div>\n"
    ))
    inpaint_base = _base_block("pg_base.py", (
        "<div class=\"inpaint\">\n"
        "  <input type=\"checkbox\" id=\"inpaintFullCanvas\">\n"
        "  <textarea id=\"inpaintPrompt\"></textarea>\n"
        "</div>\n"
    ))
    records = {
        "playgrounds/board/pg_base.py":
            _rec("playgrounds/board/pg_base.py", last_full_idx=0),
        "playgrounds/inpaint/pg_base.py":
            _rec("playgrounds/inpaint/pg_base.py", last_full_idx=1),
    }
    blocks_by_idx = {0: board_base, 1: inpaint_base}
    candidates = [
        "playgrounds/board/pg_base.py",
        "playgrounds/inpaint/pg_base.py",
    ]

    result = _pick_by_content(block, candidates, records, blocks_by_idx)
    assert result == "playgrounds/inpaint/pg_base.py", (
        f"expected inpaint candidate (identifier overlap), got "
        f"{result!r}")


def test_pick_by_content_ambiguous_returns_none():
    body = "function foo() {\n  return 1;\n}\n"
    block = _orphan(body)
    base = _base_block("x.js", "function foo() {\n  return 1;\n}\n")
    records = {
        "a/x.js": _rec("a/x.js", last_full_idx=0),
        "b/x.js": _rec("b/x.js", last_full_idx=0),
    }
    blocks_by_idx = {0: base}
    candidates = ["a/x.js", "b/x.js"]

    result = _pick_by_content(block, candidates, records, blocks_by_idx)
    assert result is None, (
        f"expected None on ambiguity, got {result!r}")


def test_pick_by_content_single_candidate_returns_it():
    body = "function foo() { return 1; }\n"
    block = _orphan(body)
    base = _base_block("x.js", "function other() { return 2; }\n")
    records = {"x.js": _rec("x.js", last_full_idx=0)}
    blocks_by_idx = {0: base}
    assert _pick_by_content(block, ["x.js"], records, blocks_by_idx) == "x.js"


def test_pick_by_content_missing_base_returns_none():
    block = _orphan("function foo() {}\n")
    records = {"x.js": _rec("x.js", last_full_idx=99)}
    assert _pick_by_content(
        block, ["x.js"], records, {}) is None


def test_pick_by_content_empty_body_returns_none():
    block = _orphan("")
    assert _pick_by_content(block, ["x.js"], {}, {}) is None


# ---------------------------------------------------------------------------
# resolve._score
# ---------------------------------------------------------------------------

def test_score_prefers_decl_overlap():
    body = (
        "export function fittedImageRect() { return null; }\n"
        "export function clampPan() { return; }\n"
    )
    block = _orphan(body)
    block.kind = "partial"

    correct = _base_block("pg_state.py", body)
    wrong = _base_block("pg_state.py", (
        "export const view = { zoom: 1 };\n"
    ))
    correct_score = _score(block, "playgrounds/inpaint/pg_state.py",
                            correct)
    wrong_score = _score(block, "playgrounds/board/pg_state.py",
                          wrong)
    assert correct_score - wrong_score > 20, (
        f"expected a strong margin, got {correct_score - wrong_score}")


def test_score_prefers_line_density():
    body = (
        "line_1();\n"
        "line_2();\n"
        "line_3();\n"
        "line_4();\n"
    )
    block = _orphan(body)
    block.kind = "partial"

    correct = _base_block("x.js", body + "other();\n")
    wrong = _base_block("x.js", "unrelated_a();\nunrelated_b();\n")
    correct_score = _score(block, "a/x.js", correct)
    wrong_score = _score(block, "b/x.js", wrong)
    assert correct_score - wrong_score > 20


def test_score_exact_path_match_dominates():
    block = _orphan("unrelated();\n")
    block.header_path = "playgrounds/inpaint/pg_state.py"
    block.kind = "partial"
    correct = _base_block("pg_state.py", "unrelated();\n")
    wrong = _base_block("pg_state.py",
                        "export function unrelated() {}\n")
    correct_score = _score(block, "playgrounds/inpaint/pg_state.py",
                            correct)
    wrong_score = _score(block, "playgrounds/board/pg_state.py", wrong)
    assert correct_score > wrong_score


def main():
    tests = [
        ("pick by content picks line density",
         test_pick_by_content_picks_line_density),
        ("pick by content picks decl overlap",
         test_pick_by_content_picks_decl_overlap),
        ("pick by content picks identifier overlap",
         test_pick_by_content_picks_identifier_overlap),
        ("pick by content ambiguous returns none",
         test_pick_by_content_ambiguous_returns_none),
        ("pick by content single candidate returns it",
         test_pick_by_content_single_candidate_returns_it),
        ("pick by content missing base returns none",
         test_pick_by_content_missing_base_returns_none),
        ("pick by content empty body returns none",
         test_pick_by_content_empty_body_returns_none),
        ("score prefers decl overlap",
         test_score_prefers_decl_overlap),
        ("score prefers line density",
         test_score_prefers_line_density),
        ("score exact path match dominates",
         test_score_exact_path_match_dominates),
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
        print(f"\n{failed}/{len(tests)} content-tiebreaker test(s) "
              f"failed")
        return 1
    print(f"\nAll {len(tests)} content-tiebreaker tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
