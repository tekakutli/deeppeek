"""Unit tests for `_has_deletion_signal`.

The narrowing (2026-11-01) — deletion verbs must be at line-start
followed by whitespace, or followed by a demonstrative, or part of a
fixed two-word phrase — closes the `Delete-and-recreate` false
positive that sent the `comfyui.py` fix down a doomed deletion path.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import (
    _has_deletion_signal,
    _is_deletion_instruction,
)


# ---------------------------------------------------------------------------
# Positive: prose that clearly instructs deletion
# ---------------------------------------------------------------------------

def test_line_start_delete_with_space():
    assert _has_deletion_signal("Delete the panelState block:") is True


def test_line_start_remove_with_space():
    assert _has_deletion_signal("Remove the following lines:") is True


def test_line_start_after_newline():
    assert _has_deletion_signal(
        "Some context.\nDelete this:"
    ) is True


def test_line_start_with_indent():
    assert _has_deletion_signal("  Delete the block:") is True


def test_line_start_with_colon():
    assert _has_deletion_signal("Remove:") is True


def test_delete_demonstrative_anywhere():
    assert _has_deletion_signal(
        "First thing: please delete this block"
    ) is True


def test_remove_the_anywhere():
    assert _has_deletion_signal(
        "You should remove the following declaration."
    ) is True


def test_no_longer_phrase():
    assert _has_deletion_signal("This is no longer needed.") is True


def test_get_rid_of_phrase():
    assert _has_deletion_signal("Get rid of the helper:") is True


def test_strip_out_phrase():
    assert _has_deletion_signal("Strip out the unused code:") is True


def test_take_out_phrase():
    assert _has_deletion_signal("Take out the debug prints:") is True


def test_revert_this():
    assert _has_deletion_signal("Revert this block:") is True


# ---------------------------------------------------------------------------
# Negative: prose that mentions deletion words but is *not* an
# instruction to delete the block
# ---------------------------------------------------------------------------

def test_delete_and_recreate_compound_verb():
    """The specific comfyui.py false positive.  `delete-and-recreate`
    has `-` after `delete`, not whitespace — the narrowed rule does
    not fire."""
    assert _has_deletion_signal(
        "Delete-and-recreate the layer instead of updating in place"
    ) is False


def test_deletion_in_descriptive_prose():
    """Prose describing code that deletes something, not instructing
    the patcher to delete the block."""
    assert _has_deletion_signal(
        "The cleanup function deletes stale layers after commit"
    ) is False


def test_wiped_the_deck():
    """'wipe' in 'wiped the deck' — demonstrative follows a
    different word, not the deletion verb directly."""
    assert _has_deletion_signal(
        "The refactor wiped the deck clean"
    ) is False


def test_dropping_hint():
    """'dropping' is not 'drop'; compound suffix defeats line-start
    match and no demonstrative follows."""
    assert _has_deletion_signal(
        "Dropping support for old browsers"
    ) is False


def test_delete_in_backticks():
    """A quoted command name is not a directive."""
    assert _has_deletion_signal(
        "Run `delete` on the record"
    ) is False


def test_delete_without_space_after():
    """'delete()' — no whitespace after the verb."""
    assert _has_deletion_signal(
        "Call delete() to remove the item"
    ) is False


# ---------------------------------------------------------------------------
# _is_deletion_instruction composes correctly
# ---------------------------------------------------------------------------

def test_is_deletion_instruction_pure_deletion_diff():
    """A pure-deletion diff is a deletion instruction regardless of
    prose."""
    body = "-removed_line_1()\n-removed_line_2()\n-removed_line_3()\n"
    assert _is_deletion_instruction(body, "") is True


def test_is_deletion_instruction_diff_not_deletion():
    """A mixed diff with `+` lines is a transform, not a deletion."""
    body = "-old()\n+new()\n"
    assert _is_deletion_instruction(body, "") is False


def test_is_deletion_instruction_comfyui_case():
    """The motivating case: prose contains 'delete-and-recreate',
    body is a fully-formed function.  `_is_deletion_instruction` must
    return False so the dispatcher tries normal heuristics."""
    body = (
        "def comfy_set_layer_pixels(self, params):\n"
        "    return {'success': True}\n"
    )
    prose = "Delete-and-recreate the layer instead of updating in place."
    assert _is_deletion_instruction(body, prose) is False


def test_is_deletion_instruction_real_deletion_still_works():
    """Regression guard: real deletion prose still fires."""
    body = "def old_helper():\n    return 1\n"
    prose = "Delete this block:"
    assert _is_deletion_instruction(body, prose) is True


def main():
    tests = [
        ("line start delete with space",
         test_line_start_delete_with_space),
        ("line start remove with space",
         test_line_start_remove_with_space),
        ("line start after newline", test_line_start_after_newline),
        ("line start with indent", test_line_start_with_indent),
        ("line start with colon", test_line_start_with_colon),
        ("delete demonstrative anywhere",
         test_delete_demonstrative_anywhere),
        ("remove the anywhere", test_remove_the_anywhere),
        ("no longer phrase", test_no_longer_phrase),
        ("get rid of phrase", test_get_rid_of_phrase),
        ("strip out phrase", test_strip_out_phrase),
        ("take out phrase", test_take_out_phrase),
        ("revert this", test_revert_this),
        ("delete and recreate compound verb",
         test_delete_and_recreate_compound_verb),
        ("deletion in descriptive prose",
         test_deletion_in_descriptive_prose),
        ("wiped the deck", test_wiped_the_deck),
        ("dropping hint", test_dropping_hint),
        ("delete in backticks", test_delete_in_backticks),
        ("delete without space after", test_delete_without_space_after),
        ("is_deletion_instruction pure deletion diff",
         test_is_deletion_instruction_pure_deletion_diff),
        ("is_deletion_instruction diff not deletion",
         test_is_deletion_instruction_diff_not_deletion),
        ("is_deletion_instruction comfyui case",
         test_is_deletion_instruction_comfyui_case),
        ("is_deletion_instruction real deletion still works",
         test_is_deletion_instruction_real_deletion_still_works),
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
        print(f"\n{failed}/{len(tests)} deletion-signal test(s) failed")
        return 1
    print(f"\nAll {len(tests)} deletion-signal tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
