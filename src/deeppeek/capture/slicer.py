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
) -> List[Message]:
    if not messages:
        return []

    last = len(messages) - 1

    # --last-regex PATTERN — anchor at the LAST user message whose
    # content matches PATTERN, slice to end.  More specific than
    # --last N (which counts user turns by position).  Use when you
    # want "everything from the most recent mention of X onward",
    # regardless of how many turns back that was.
    #
    # Priority: --last-regex > --last > start/end anchors.  The
    # regex form is the most specific selector and short-circuits
    # everything else.  An invalid pattern is a hard error (exit 3);
    # a valid pattern with no matches warns and returns the whole
    # conversation — matching the existing convention for
    # --start-prompt that fails to match.
    if last_regex is not None:
        try:
            pat = re.compile(last_regex)
        except re.error as e:
            print(f"error: --last-regex is not a valid regex: {e}",
                  file=sys.stderr)
            raise SystemExit(3)

        match_idx = None
        for i, m in enumerate(messages):
            if m.role == "user" and pat.search(m.content or ""):
                match_idx = i

        if match_idx is None:
            print(
                f"warning: --last-regex {last_regex!r} matched no "
                f"user prompt; returning whole conversation",
                file=sys.stderr,
            )
            start = 0
        else:
            start = match_idx
        end = last
        start = max(0, start - context)
        end = min(last, end + context)
        return messages[start:end + 1]

    # --last N — last N user turns (each turn = 1 user message +
    # its reply, extending to end of conversation).
    if last_user_turns is not None:
        if last_user_turns <= 0:
            return []
        user_indices = [i for i, m in enumerate(messages) if m.role == "user"]
        if not user_indices:
            print(
                "warning: --last given but no user messages in conversation",
                file=sys.stderr,
            )
            return []
        if len(user_indices) <= last_user_turns:
            start = 0
        else:
            start = user_indices[-last_user_turns]
        end = last
        start = max(0, start - context)
        end = min(last, end + context)
        return messages[start:end + 1]

    # Start (anchor resolution)
    if start_index is not None:
        start = max(0, min(start_index, last))
    elif start_prompt is not None:
        start = None
        for i, m in enumerate(messages):
            if m.role == "user" and _matches(m.content, start_prompt, match_mode):
                start = i
                break
        if start is None:
            print("warning: start prompt not found; starting at first message",
                  file=sys.stderr)
            start = 0
    else:
        start = 0

    # End (inclusive, extend to the assistant reply when the anchor
    # is a user message)
    if end_index is not None:
        end = max(0, min(end_index, last))
    elif end_prompt is not None:
        last_match = None
        for i, m in enumerate(messages):
            if m.role == "user" and _matches(m.content, end_prompt, match_mode):
                last_match = i
        if last_match is None:
            print("warning: end prompt not found; ending at last message",
                  file=sys.stderr)
            end = last
        else:
            if last_match + 1 <= last and messages[last_match + 1].role == "assistant":
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
