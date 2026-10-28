"""Unit tests for `_already_in_other_file`'s extension to non-pre-
existing records (Fix 5)."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.dedup import _already_in_other_file


def _block(body, idx=0):
    return Block(
        idx=idx, header_path=None, modifier=None, fence_lang="js",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=True, is_code_file=True, kind="partial",
    )


def _rec(path, content=None, last_full_idx=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    rec.last_full_idx = last_full_idx
    return rec


def test_finds_in_preexisting_content():
    block = _block("function helper() {\n  return 1;\n}\n")
    other = _rec("other.js",
                 content="function helper() {\n  return 1;\n}\n")
    other.preexisting_base = True
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) == "other.js"


def test_finds_in_conversation_content():
    """Non-preexisting record: content is set."""
    block = _block("function helper() {\n  return 1;\n}\n")
    other = _rec("other.js",
                 content="function helper() {\n  return 1;\n}\n")
    other.preexisting_base = False
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) == "other.js"


def test_finds_in_last_full_base_when_content_none():
    """Non-preexisting record: content None, but last_full_idx base is
    present in blocks_by_idx."""
    block = _block("function helper() {\n  return 1;\n}\n")
    other = _rec("other.js", content=None, last_full_idx=5)
    other.preexisting_base = False
    base_block = Block(
        idx=5, header_path="other.js", modifier=None, fence_lang="js",
        body="function helper() {\n  return 1;\n}\n",
        body_line_count=3, context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="other.js", output_path="other.js",
        classified_by="preexisting",
    )
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    blocks_by_idx = {5: base_block}
    assert _already_in_other_file(
        block, "current.js", records, blocks_by_idx) == "other.js"


def test_missing_base_skipped():
    """Other record has content=None and last_full_idx points to a
    missing block → skipped (no crash)."""
    block = _block("function helper() {\n  return 1;\n}\n")
    other = _rec("other.js", content=None, last_full_idx=5)
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) is None


def test_no_match_returns_none():
    block = _block("function totallyNew() {\n  return 999;\n}\n")
    other = _rec("other.js",
                 content="function helper() {\n  return 1;\n}\n")
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) is None


def test_short_body_not_checked():
    """Bodies < 20 chars are not tested (avoid false positives on
    trivial shapes)."""
    block = _block("x = 1;\n")
    other = _rec("other.js", content="x = 1;\n")
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) is None


def test_skips_current_path():
    """A block matching its own record doesn't count as 'in another
    file'."""
    block = _block("function helper() {\n  return 1;\n}\n")
    current = _rec("current.js",
                   content="function helper() {\n  return 1;\n}\n")
    records = {"current.js": current}
    assert _already_in_other_file(
        block, "current.js", records, {}) is None


def test_fuzzy_match_above_threshold():
    block = _block(
        "function helper() {\n"
        "  return 1;\n"
        "}\n"
    )
    other = _rec("other.js",
                 content=(
                     "function helper() {\n"
                     "  return 1;\n"
                     "}\n"
                 ))
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) == "other.js"


def test_fuzzy_match_below_threshold():
    block = _block(
        "function helper() {\n"
        "  totally_different();\n"
        "  also_different();\n"
        "  and_more();\n"
        "}\n"
    )
    other = _rec("other.js",
                 content=(
                     "function helper() {\n"
                     "  return 1;\n"
                     "}\n"
                 ))
    records = {"current.js": _rec("current.js", content=""),
               "other.js": other}
    assert _already_in_other_file(
        block, "current.js", records, {}) is None


def main():
    tests = [
        ("finds in preexisting content",
         test_finds_in_preexisting_content),
        ("finds in conversation content",
         test_finds_in_conversation_content),
        ("finds in last full base when content none",
         test_finds_in_last_full_base_when_content_none),
        ("missing base skipped", test_missing_base_skipped),
        ("no match returns none", test_no_match_returns_none),
        ("short body not checked", test_short_body_not_checked),
        ("skips current path", test_skips_current_path),
        ("fuzzy match above threshold", test_fuzzy_match_above_threshold),
        ("fuzzy match below threshold", test_fuzzy_match_below_threshold),
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
        print(f"\n{failed}/{len(tests)} cross-file dedup test(s) failed")
        return 1
    print(f"\nAll {len(tests)} cross-file dedup tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
