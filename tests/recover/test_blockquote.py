"""Regression tests for blockquoted-fence handling.

deeppeek's own formatter (formatter.py) wraps AI thought content in
markdown blockquotes.  A blockquoted fence is therefore part of the
AI's reasoning, not its final answer.  Three behaviours matter:

  1. Unattributed blockquoted fences must NOT be reported as orphan
     warnings.  They are thinking-fragment noise (the AI exploring
     code it doesn't commit to), and warning about them clutters
     warnings.txt with blocks the user can't act on.

  2. Blockquoted fences with weak attribution (body_header, anchor,
     subset, prose_nearest) must NOT be applied as patches.  Only
     EXACT prose attribution — the AI naming exactly one target file
     in surrounding prose — is strong enough to justify applying a
     blockquoted fence.

     `prose_nearest` was previously accepted alongside `prose`; the
     distinction matters when the prose names MULTIPLE files and the
     tool guesses the closest mention.  Thought-process code that
     straddles several files is exactly what should not be applied
     on a guess.  The specific failure that motivated the tightening:
     a prose_nearest blockquoted block was applied to pg_inpaint.py
     AFTER a correct scoped-replace pair had already fixed the file,
     nesting `buildContextMaskB64` inside itself.  Balanced-brace
     clean, syntactically valid JavaScript, silently wrong.

  3. Blockquoted fences with exact-prose attribution ARE applied,
     matching the previous behaviour.  This is the case where the
     AI is explicitly saying "Update file X with this code".

The motivating case for the blockquote handling: conversation2.md,
where every assistant message was a thought-process blockquote.  The
previous behaviour extracted four thinking fragments (blocks 26, 28,
30, 32) as unattributable orphans and produced two warnings for a
fifth (block 25) that failed after being tried.  The fix eliminates
all six warnings without changing which files land on disk.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.extract import pass1_extract
from deeppeek.recover.attribute import pass4_attribute_orphans
from deeppeek.recover.dedup import pass5_deduplicate
from deeppeek.recover.write import _compute_block_disposition


# ---------------------------------------------------------------------------
# pass1_extract: blockquote tracking
# ---------------------------------------------------------------------------

def test_extract_top_level_fence_not_blockquoted():
    dump = (
        "## File: `foo.py`\n"
        "\n"
        "```python\n"
        "def foo():\n"
        "    pass\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 1
    assert blocks[0].in_blockquote is False


def test_extract_blockquoted_fence_marked():
    dump = (
        "### Thought Process\n"
        "\n"
        "> Here is the fix:\n"
        "> ```js\n"
        "> const x = 1;\n"
        "> ```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 1, (
        f"expected 1 block, got {len(blocks)}: "
        f"{[(b.fence_lang, b.body[:30]) for b in blocks]}")
    assert blocks[0].in_blockquote is True
    assert "const x = 1;" in blocks[0].body


def test_extract_mixed_blockquoted_and_top_level():
    dump = (
        "### Thought Process\n"
        "\n"
        "> ```js\n"
        "> // thinking fragment\n"
        "> ```\n"
        "\n"
        "## Answer\n"
        "\n"
        "```js\n"
        "// committed code\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 2
    assert blocks[0].in_blockquote is True
    assert blocks[1].in_blockquote is False


def test_extract_nested_blockquotes():
    dump = (
        "> > ```js\n"
        "> > deeply_quoted();\n"
        "> > ```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 1
    assert blocks[0].in_blockquote is True


# ---------------------------------------------------------------------------
# pass4_attribute_orphans: blockquoted + unattributed -> blockquote_skip
# ---------------------------------------------------------------------------

def test_blockquoted_unattributed_skipped_silently():
    bq = Block(
        idx=0, header_path=None, modifier=None, fence_lang="js",
        body="if (cond) {\n  doThing();\n}\n",
        body_line_count=3, context_before="",
        line_start=0, line_end=3, is_orphan=True,
        is_code_file=True, kind="partial", in_blockquote=True,
    )
    base = Block(
        idx=1, header_path="a.js", modifier=None, fence_lang="js",
        body="existing();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.source_blocks = [1]
    pass4_attribute_orphans(
        [bq, base], {0: bq, 1: base},
        {"a.js": rec}, verbose=False,
    )
    assert bq.attributed_by == "blockquote_skip"


def test_non_blockquoted_unattributed_still_warned():
    nb = Block(
        idx=0, header_path=None, modifier=None, fence_lang="js",
        body="if (cond) {\n  doThing();\n}\n",
        body_line_count=3, context_before="",
        line_start=0, line_end=3, is_orphan=True,
        is_code_file=True, kind="partial", in_blockquote=False,
    )
    base = Block(
        idx=1, header_path="a.js", modifier=None, fence_lang="js",
        body="existing();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.source_blocks = [1]
    pass4_attribute_orphans(
        [nb, base], {0: nb, 1: base},
        {"a.js": rec}, verbose=False,
    )
    assert nb.attributed_by == "unattributed"


def test_blockquoted_with_prose_still_attributed():
    bq = Block(
        idx=0, header_path=None, modifier=None, fence_lang="js",
        body="newFunc();\n", body_line_count=1,
        context_before="Update `a.js` with:",
        line_start=0, line_end=2, is_orphan=True,
        is_code_file=True, kind="partial", in_blockquote=True,
    )
    base = Block(
        idx=1, header_path="a.js", modifier=None, fence_lang="js",
        body="existing();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.source_blocks = [1]
    pass4_attribute_orphans(
        [bq, base], {0: bq, 1: base},
        {"a.js": rec}, verbose=False,
    )
    assert bq.attributed_to == "a.js"
    assert bq.attributed_by in ("prose", "prose_nearest")


# ---------------------------------------------------------------------------
# pass5_deduplicate: blockquoted + weak attribution -> not applied
# ---------------------------------------------------------------------------

def test_blockquoted_weak_attr_not_patched():
    """A blockquoted orphan with weak attribution (body_header) must
    NOT become a patch.

    Base is at idx=0 to mirror `_load_preexisting_blocks`, which
    prepends preexisting baselines before the conversation blocks.
    The orphan is therefore at idx=1, *after* the chosen full -- so
    the `trailing` filter in pass5 is satisfied and the blockquote
    filter is the one doing the exclusion.  Without the swap, the
    test would pass because `p > chosen_idx` excludes pre-full
    blocks regardless of attribution strength, which would let a
    regression in the blockquote filter go unnoticed."""
    base = Block(
        idx=0, header_path="a.js", modifier=None, fence_lang="js",
        body="existing();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
        classified_by="preexisting",
    )
    bq = Block(
        idx=1, header_path=None, modifier=None, fence_lang="js",
        body="// a.js fragment\nnewFunc();\n", body_line_count=2,
        context_before="", line_start=0, line_end=2, is_orphan=True,
        is_code_file=True, kind="partial",
        in_blockquote=True,
        attributed_to="a.js", attributed_by="body_header",
        output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.source_blocks = [0]
    rec.preexisting_base = True
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0
    pass5_deduplicate({"a.js": rec}, {0: base, 1: bq}, verbose=False)
    assert not any(p["idx"] == bq.idx for p in rec.patches), (
        f"blockquoted body_header patch should be filtered; "
        f"bq.idx={bq.idx}, patches={rec.patches}")


def test_blockquoted_prose_attr_still_patched():
    """A blockquoted orphan with STRONG attribution (prose) MUST
    become a patch.

    The prose attribution -- "Update `a.js`:" appearing right above
    the blockquoted fence -- is the AI's explicit intent to commit
    code.  The blockquote is only a formatting artifact of the GUI
    wrapping the AI's thought stream; it is not a signal that the
    code is throwaway.  This test is the regression guard: if a
    future change tightens the blockquote filter to exclude all
    blockquoted blocks, seven real recoveries from conversation2.md
    break silently."""
    base = Block(
        idx=0, header_path="a.js", modifier=None, fence_lang="js",
        body="existing();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
        classified_by="preexisting",
    )
    bq = Block(
        idx=1, header_path=None, modifier=None, fence_lang="js",
        body="newFunc();\n", body_line_count=1,
        context_before="Update `a.js`:", line_start=0, line_end=2,
        is_orphan=True, is_code_file=True, kind="partial",
        in_blockquote=True,
        attributed_to="a.js", attributed_by="prose",
        output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.source_blocks = [0]
    rec.preexisting_base = True
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0
    pass5_deduplicate({"a.js": rec}, {0: base, 1: bq}, verbose=False)
    assert any(p["idx"] == bq.idx for p in rec.patches), (
        f"blockquoted prose-attributed patch should be applied; "
        f"bq.idx={bq.idx}, patches={rec.patches}")


def test_blockquoted_prose_nearest_not_patched():
    """A blockquoted orphan with `prose_nearest` attribution must NOT
    be applied.

    `prose_nearest` means the prose named MULTIPLE files and the tool
    guessed the closest one.  Thought-process code routinely straddles
    several files; the guess is unreliable — especially for a
    blockquoted block, which is already speculative by convention.

    The specific regression: a prose_nearest blockquoted block was
    applied to pg_inpaint.py *after* a correct scoped-replace pair
    had already fixed the file, nesting `buildContextMaskB64` inside
    itself.  The nesting was balanced-brace clean — syntactically
    valid JavaScript, silently wrong."""
    base = Block(
        idx=0, header_path="a.js", modifier=None, fence_lang="js",
        body="existing();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
        classified_by="preexisting",
    )
    bq = Block(
        idx=1, header_path=None, modifier=None, fence_lang="js",
        body="// a.js fragment\nnewFunc();\n", body_line_count=2,
        # Ambiguous prose — mentions two files.
        context_before="Update `a.js` and `b.js` with:",
        line_start=0, line_end=2, is_orphan=True,
        is_code_file=True, kind="partial",
        in_blockquote=True,
        attributed_to="a.js", attributed_by="prose_nearest",
        output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.source_blocks = [0]
    rec.preexisting_base = True
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0
    pass5_deduplicate({"a.js": rec}, {0: base, 1: bq}, verbose=False)
    assert not any(p["idx"] == bq.idx for p in rec.patches), (
        f"prose_nearest blockquoted should be skipped; "
        f"bq.idx={bq.idx}, patches={rec.patches}")


# ---------------------------------------------------------------------------
# write.py: blockquote_skip disposition
# ---------------------------------------------------------------------------

def test_disposition_blockquote_skip():
    b = Block(
        idx=0, header_path=None, modifier=None, fence_lang="js",
        body="fragment();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=True, is_code_file=True, kind="partial",
        in_blockquote=True, attributed_by="blockquote_skip",
    )
    assert _compute_block_disposition(b, {}) == "blockquote_skip"


# ---------------------------------------------------------------------------
# via_llm reporting fix
# ---------------------------------------------------------------------------

def test_via_llm_not_set_without_llm_patch():
    from deeppeek.recover.apply_patches import pass6_apply_patches
    base_body = "export function existing() {\n  return 1;\n}\n"
    base_block = Block(
        idx=0, header_path="a.js", modifier=None, fence_lang="js",
        body=base_body, body_line_count=3,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
        classified_by="preexisting",
    )
    fragment = Block(
        idx=1, header_path=None, modifier=None, fence_lang="js",
        body="orphan_fragment();\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=True, is_code_file=True, kind="partial",
        attributed_by="body_header",
    )
    rec = FileRecord(path="a.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [{
        "idx": 1,
        "body": "orphan_fragment();\n",
        "context_before": "",
        "fence_lang": "js",
    }]
    pass6_apply_patches({"a.js": rec}, {0: base_block, 1: fragment}, None)
    assert not rec.verification.get("via_llm"), (
        f"via_llm should be absent when no LLM applied a patch; "
        f"verification={rec.verification}")


def main():
    tests = [
        ("extract top-level not blockquoted",
         test_extract_top_level_fence_not_blockquoted),
        ("extract blockquoted marked",
         test_extract_blockquoted_fence_marked),
        ("extract mixed blockquoted and top-level",
         test_extract_mixed_blockquoted_and_top_level),
        ("extract nested blockquotes",
         test_extract_nested_blockquotes),
        ("blockquoted unattributed skipped silently",
         test_blockquoted_unattributed_skipped_silently),
        ("non-blockquoted unattributed still warned",
         test_non_blockquoted_unattributed_still_warned),
        ("blockquoted with prose still attributed",
         test_blockquoted_with_prose_still_attributed),
        ("blockquoted weak attr not patched",
         test_blockquoted_weak_attr_not_patched),
        ("blockquoted prose attr still patched",
         test_blockquoted_prose_attr_still_patched),
        ("blockquoted prose_nearest not patched",
         test_blockquoted_prose_nearest_not_patched),
        ("disposition blockquote_skip",
         test_disposition_blockquote_skip),
        ("via_llm not set without llm patch",
         test_via_llm_not_set_without_llm_patch),
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
        print(f"\n{failed}/{len(tests)} blockquote test(s) failed")
        return 1
    print(f"\nAll {len(tests)} blockquote tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
