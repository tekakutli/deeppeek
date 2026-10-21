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
) -> List[Message]:
    if not messages:
        return []

    last = len(messages) - 1

    # Start
    if start_index is not None:
        start = max(0, min(start_index, last))
    elif start_prompt is not None:
        start = None
        for i, m in enumerate(messages):
            if m.role == "user" and _matches(m.content, start_prompt, match_mode):
                start = i
                break
        if start is None:
            print(
                f"warning: start prompt not found; starting at first message",
                file=sys.stderr,
            )
            start = 0
    else:
        start = 0

    # End (inclusive)
    if end_index is not None:
        end = max(0, min(end_index, last))
    elif end_prompt is not None:
        last_match = None
        for i, m in enumerate(messages):
            if m.role == "user" and _matches(m.content, end_prompt, match_mode):
                last_match = i
        if last_match is None:
            print(
                "warning: end prompt not found; ending at last message",
                file=sys.stderr,
            )
            end = last
        else:
            # Include the assistant reply immediately following that user turn
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
    return messages[start : end + 1]
