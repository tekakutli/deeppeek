"""Tests for slice_messages with the --until / --until-regex flags.

--until K trims the tail of a --last / --last-regex slice so it
ends JUST BEFORE user turn K (0-indexed, exclusive of K).  Keeps
user turns [start_user_turn, K-1].

--until-regex PATTERN is the pattern-based mirror: it ends just
before the LAST user turn matching PATTERN.  Exclusive of the
matched turn.

Both require a turn-anchored start (--last N or --last-regex) and
are mutually exclusive with each other.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.capture.models import Message
from deeppeek.capture.slicer import slice_messages


def _mk_messages(n_turns):
    """Build 2*n messages: user, assistant, user, assistant, ...

    User turns are at even indices; their assistant replies are at
    odd indices.  Content is q<i>/a<i> so tests can verify which
    turns ended up in the slice.
    """
    msgs = []
    for i in range(n_turns):
        msgs.append(Message(index=len(msgs), role="user", content=f"q{i}"))
        msgs.append(Message(index=len(msgs), role="assistant", content=f"a{i}"))
    return msgs


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_until_without_last_errors():
    msgs = _mk_messages(5)
    try:
        slice_messages(msgs, until_user_turn=2)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "--until requires --last" in str(e)


def test_until_regex_without_last_errors():
    msgs = _mk_messages(5)
    try:
        slice_messages(msgs, until_regex="q2")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "--until-regex requires --last" in str(e)


def test_until_and_until_regex_are_mutually_exclusive():
    msgs = _mk_messages(5)
    try:
        slice_messages(msgs, last_user_turns=3,
                       until_user_turn=2, until_regex="q2")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "mutually exclusive" in str(e)


def test_until_before_start_errors():
    """--until K where K < start user-turn: error."""
    msgs = _mk_messages(10)
    try:
        slice_messages(msgs, last_user_turns=3, until_user_turn=5)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "before the start" in str(e)


def test_until_at_start_errors():
    """--until K where K == start user-turn: excluding it leaves
    no turns, error."""
    msgs = _mk_messages(10)
    try:
        slice_messages(msgs, last_user_turns=3, until_user_turn=7)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "excludes the start" in str(e)


def test_until_negative_errors():
    msgs = _mk_messages(10)
    try:
        slice_messages(msgs, last_user_turns=3, until_user_turn=-1)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "--until must be >= 0" in str(e)


def test_until_regex_invalid_errors():
    """Invalid regex exits with code 3."""
    import contextlib, io
    msgs = _mk_messages(5)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            slice_messages(msgs, last_user_turns=3, until_regex="[")
            assert False, "expected SystemExit"
        except SystemExit as e:
            assert e.code == 3, f"expected exit code 3, got {e.code!r}"
    assert "not a valid regex" in err.getvalue()


def test_until_regex_before_start_errors():
    msgs = _mk_messages(10)
    try:
        slice_messages(msgs, last_user_turns=3, until_regex="q2")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "matched only before the start" in str(e)


def test_until_regex_at_start_errors():
    """--until-regex matching the slice's start turn: excluding it
    leaves no turns, error."""
    msgs = _mk_messages(10)
    try:
        slice_messages(msgs, last_user_turns=3, until_regex="q7")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "matched the start" in str(e)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_last_only_backward_compat():
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=3)
    assert len(sliced) == 6


def test_last_regex_only_backward_compat():
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_regex="q7")
    assert len(sliced) == 6
    assert sliced[0].content == "q7"


# ---------------------------------------------------------------------------
# --until (integer) — EXCLUSIVE of turn K
# ---------------------------------------------------------------------------

def test_last_until_one_turn():
    """--last 3 --until 8 on 10 turns: start=7, keeps turn 7 only
    (turn 8 is excluded)."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=3, until_user_turn=8)
    assert len(sliced) == 2
    assert sliced[0].content == "q7"
    assert sliced[1].content == "a7"


def test_last_until_two_turns():
    """--last 3 --until 9 on 10 turns: keeps turns 7,8 (turn 9
    excluded)."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=3, until_user_turn=9)
    assert len(sliced) == 4
    assert sliced[0].content == "q7"
    assert sliced[-1].content == "a8"


def test_last_until_full_slice_is_noop():
    """--last 3 --until 10 on 10 turns: K >= M so no trim, keeps
    turns 7,8,9 (same as --last 3 alone)."""
    msgs = _mk_messages(10)
    a = slice_messages(msgs, last_user_turns=3)
    b = slice_messages(msgs, last_user_turns=3, until_user_turn=10)
    assert len(a) == len(b) == 6
    assert [m.content for m in a] == [m.content for m in b]


def test_until_exceeds_caps_to_last_turn():
    """--until 100 on a 10-turn conversation: no trim."""
    msgs = _mk_messages(10)
    a = slice_messages(msgs, last_user_turns=3)
    b = slice_messages(msgs, last_user_turns=3, until_user_turn=100)
    assert len(a) == len(b) == 6


def test_until_with_fewer_turns_than_last():
    """--last 10 --until 2 on 5 turns: start=0, keeps turns 0,1
    (exclusive of 2)."""
    msgs = _mk_messages(5)
    sliced = slice_messages(msgs, last_user_turns=10, until_user_turn=2)
    assert len(sliced) == 4
    assert sliced[0].content == "q0"
    assert sliced[-1].content == "a1"


def test_until_one_turn_from_start():
    """--last 5 --until 1 on 5 turns: start=0, keeps turn 0 only
    (exclusive of 1)."""
    msgs = _mk_messages(5)
    sliced = slice_messages(msgs, last_user_turns=5, until_user_turn=1)
    assert len(sliced) == 2
    assert sliced[0].content == "q0"
    assert sliced[1].content == "a0"


# ---------------------------------------------------------------------------
# --until with --last-regex
# ---------------------------------------------------------------------------

def test_until_with_last_regex_works():
    """--last-regex 'q1' anchors at turn 1; --until 3 keeps turns 1,2."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_regex="q1", until_user_turn=3)
    assert len(sliced) == 4
    assert sliced[0].content == "q1"
    assert sliced[-1].content == "a2"


def test_until_regex_with_last_regex_works():
    """--last-regex 'q2' --until-regex 'q5' keeps turns 2,3,4."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_regex="q2", until_regex="q5")
    assert len(sliced) == 6
    assert sliced[0].content == "q2"
    assert sliced[-1].content == "a4"


# ---------------------------------------------------------------------------
# --until-regex (pattern-based) — EXCLUSIVE of matched turn
# ---------------------------------------------------------------------------

def test_until_regex_one_turn():
    """--last 3 --until-regex 'q8' on 10 turns: keeps turn 7 only
    (turn 8 excluded)."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=3, until_regex="q8")
    assert len(sliced) == 2
    assert sliced[0].content == "q7"
    assert sliced[1].content == "a7"


def test_until_regex_two_turns():
    """--last 3 --until-regex 'q9' on 10 turns: keeps turns 7,8
    (turn 9 excluded)."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=3, until_regex="q9")
    assert len(sliced) == 4
    assert sliced[0].content == "q7"
    assert sliced[-1].content == "a8"


def test_until_regex_no_match_noop():
    """--until-regex matching nothing warns and does NOT trim."""
    msgs = _mk_messages(10)
    a = slice_messages(msgs, last_user_turns=3)
    b = slice_messages(msgs, last_user_turns=3, until_regex="zzz")
    assert len(a) == len(b) == 6


def test_until_regex_last_match_wins():
    """PATTERN 'q' matches every user turn; last match is turn 9.
    --last 5 → start=5, keeps turns 5,6,7,8 (exclusive of 9)."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=5, until_regex="q")
    assert len(sliced) == 8
    assert sliced[0].content == "q5"
    assert sliced[-1].content == "a8"


def test_until_regex_partial_match():
    """Substring 'q5' matches user turn 5; keeps turns 0..4."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=10, until_regex="q5")
    assert len(sliced) == 10
    assert sliced[0].content == "q0"
    assert sliced[-1].content == "a4"


def test_until_regex_ignores_assistant_messages():
    """Pattern 'a5' matches no user turn → no trim."""
    msgs = _mk_messages(10)
    sliced = slice_messages(msgs, last_user_turns=10, until_regex="a5")
    assert len(sliced) == 20


def main():
    tests = [
        ("until without last errors", test_until_without_last_errors),
        ("until-regex without last errors",
         test_until_regex_without_last_errors),
        ("until and until-regex are mutually exclusive",
         test_until_and_until_regex_are_mutually_exclusive),
        ("until before start errors", test_until_before_start_errors),
        ("until at start errors", test_until_at_start_errors),
        ("until negative errors", test_until_negative_errors),
        ("until-regex invalid errors", test_until_regex_invalid_errors),
        ("until-regex before start errors",
         test_until_regex_before_start_errors),
        ("until-regex at start errors",
         test_until_regex_at_start_errors),
        ("last only backward compat", test_last_only_backward_compat),
        ("last-regex only backward compat",
         test_last_regex_only_backward_compat),
        ("last until one turn", test_last_until_one_turn),
        ("last until two turns", test_last_until_two_turns),
        ("last until full slice is noop",
         test_last_until_full_slice_is_noop),
        ("until exceeds caps to last turn",
         test_until_exceeds_caps_to_last_turn),
        ("until with fewer turns than last",
         test_until_with_fewer_turns_than_last),
        ("until one turn from start", test_until_one_turn_from_start),
        ("until with last-regex works",
         test_until_with_last_regex_works),
        ("until-regex with last-regex works",
         test_until_regex_with_last_regex_works),
        ("until-regex one turn", test_until_regex_one_turn),
        ("until-regex two turns", test_until_regex_two_turns),
        ("until-regex no match noop",
         test_until_regex_no_match_noop),
        ("until-regex last match wins",
         test_until_regex_last_match_wins),
        ("until-regex partial match",
         test_until_regex_partial_match),
        ("until-regex ignores assistant messages",
         test_until_regex_ignores_assistant_messages),
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
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\nAll {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
