import re
import sys
from typing import List, Optional

from .models import Message


def _matches(text: str, needle: str, mode: str) -> bool:
    if mode == "exact":
        return text == needle
    if mode == "regex":
        try:
            return re.search(needle, text) is not None
        except re.error:
            return False
    return needle in text


def slice_messages(
    messages: List[Message],
    start_prompt: Optional[str] = None,
    end_prompt: Optional[str] = None,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    match_mode: str = "substring",
    context: int = 0,
    last_user_turns: Optional[int] = None,
    last_regex: Optional[str] = None,
    until_user_turn: Optional[int] = None,
    until_regex: Optional[str] = None,
) -> List[Message]:
    if not messages:
        return []

    last = len(messages) - 1

    # --until and --until-regex are EXCLUSIVE tail-trims for the
    # slice chosen by --last or --last-regex.  Exclusive means
    # the anchor turn itself is removed from the slice, not just
    # everything after it: --until K keeps user turns
    # [slice_start, K-1].
    #
    # Both flags require a turn-anchored start (--last N or
    # --last-regex PATTERN).  Trimming the tail of a
    # --start-prompt / --start-index slice would need a different
    # turn-counting rule and is not supported.
    if until_user_turn is not None and until_regex is not None:
        raise SystemExit(
            "error: --until and --until-regex are mutually exclusive")
    trim_requested = (until_user_turn is not None
                      or until_regex is not None)
    in_turn_slice = (last_regex is not None
                     or last_user_turns is not None)
    if trim_requested and not in_turn_slice:
        if until_user_turn is not None:
            flag = "--until"
        else:
            flag = "--until-regex"
        raise SystemExit(
            f"error: {flag} requires --last N or --last-regex "
            f"PATTERN (there must be a slice to trim)")

    user_indices = [i for i, m in enumerate(messages)
                    if m.role == "user"]

    # ------------------------------------------------------------------
    # Turn-anchored slice: --last-regex (last matching user turn to
    # end) or --last N (last N user turns to end).  Both paths
    # produce (start, start_user_turn) and then fall into the
    # shared exclusive tail-trim.
    # ------------------------------------------------------------------
    if in_turn_slice:
        if last_regex is not None:
            try:
                pat = re.compile(last_regex)
            except re.error as e:
                print(
                    f"error: --last-regex is not a valid regex: {e}",
                    file=sys.stderr)
                raise SystemExit(3)

            match_idx = None
            for i, m in enumerate(messages):
                if m.role == "user" and pat.search(m.content or ""):
                    match_idx = i

            if match_idx is None:
                print(
                    f"warning: --last-regex {last_regex!r} matched "
                    f"no user prompt; returning whole conversation",
                    file=sys.stderr)
                start = 0
                start_user_turn = 0
            else:
                start = match_idx
                start_user_turn = user_indices.index(match_idx)
            end = last
        else:  # last_user_turns is not None
            if last_user_turns <= 0:
                return []
            if not user_indices:
                print(
                    "warning: --last given but no user messages in "
                    "conversation", file=sys.stderr)
                return []
            if len(user_indices) <= last_user_turns:
                start_user_turn = 0
            else:
                start_user_turn = len(user_indices) - last_user_turns
            start = user_indices[start_user_turn]
            end = last

        # ---- exclusive tail trim ----
        if until_user_turn is not None:
            if until_user_turn < 0:
                raise SystemExit(
                    f"error: --until must be >= 0, got "
                    f"{until_user_turn}")
            if until_user_turn < start_user_turn:
                raise SystemExit(
                    f"error: --until {until_user_turn} is before "
                    f"the start of the slice (start user turn "
                    f"index is {start_user_turn})")
            if until_user_turn == start_user_turn:
                raise SystemExit(
                    f"error: --until {until_user_turn} excludes "
                    f"the start of the slice (user turn "
                    f"{start_user_turn}); no turns would remain")
            if until_user_turn >= len(user_indices):
                print(
                    f"warning: --until {until_user_turn} has no "
                    f"user turn to exclude ({len(user_indices)} "
                    f"user turn(s) in conversation); no trim "
                    f"applied", file=sys.stderr)
                # end stays `last`
            else:
                end_user_turn = until_user_turn - 1
                end = user_indices[end_user_turn]
                if (end + 1 <= last
                        and messages[end + 1].role == "assistant"):
                    end = end + 1
        elif until_regex is not None:
            try:
                upat = re.compile(until_regex)
            except re.error as e:
                print(
                    f"error: --until-regex is not a valid regex: "
                    f"{e}", file=sys.stderr)
                raise SystemExit(3)

            match_idx = None
            for i, m in enumerate(messages):
                if m.role == "user" and upat.search(m.content or ""):
                    match_idx = i

            if match_idx is None:
                print(
                    f"warning: --until-regex {until_regex!r} "
                    f"matched no user prompt; no trim applied",
                    file=sys.stderr)
                # end stays `last`
            else:
                matched_turn = user_indices.index(match_idx)
                if matched_turn < start_user_turn:
                    raise SystemExit(
                        f"error: --until-regex {until_regex!r} "
                        f"matched only before the start of the "
                        f"slice (matched user turn "
                        f"{matched_turn}, slice starts at user "
                        f"turn {start_user_turn})")
                if matched_turn == start_user_turn:
                    raise SystemExit(
                        f"error: --until-regex {until_regex!r} "
                        f"matched the start of the slice (user "
                        f"turn {start_user_turn}); excluding it "
                        f"would leave no turns")
                end_user_turn = matched_turn - 1
                end = user_indices[end_user_turn]
                if (end + 1 <= last
                        and messages[end + 1].role == "assistant"):
                    end = end + 1

        start = max(0, start - context)
        end = min(last, end + context)
        return messages[start:end + 1]

    # ------------------------------------------------------------------
    # Legacy: --start-prompt / --start-index / --end-prompt /
    # --end-index.  Neither tail-trim flag applies here (validated
    # above).
    # ------------------------------------------------------------------
    if start_index is not None:
        start = max(0, min(start_index, last))
    elif start_prompt is not None:
        start = None
        for i, m in enumerate(messages):
            if m.role == "user" and _matches(m.content, start_prompt,
                                             match_mode):
                start = i
                break
        if start is None:
            print(
                "warning: start prompt not found; starting at first "
                "message", file=sys.stderr)
            start = 0
    else:
        start = 0

    if end_index is not None:
        end = max(0, min(end_index, last))
    elif end_prompt is not None:
        last_match = None
        for i, m in enumerate(messages):
            if m.role == "user" and _matches(m.content, end_prompt,
                                             match_mode):
                last_match = i
        if last_match is None:
            print(
                "warning: end prompt not found; ending at last "
                "message", file=sys.stderr)
            end = last
        else:
            if (last_match + 1 <= last
                    and messages[last_match + 1].role == "assistant"):
                end = last_match + 1
            else:
                end = last_match
    else:
        end = last

    if start > end:
        raise SystemExit(f"start index {start} > end index {end}")

    start = max(0, start - context)
    end = min(last, end + context)
    return messages[start:end + 1]
