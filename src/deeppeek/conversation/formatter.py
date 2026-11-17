import re
from typing import List

from .models import Message

USER_HEADER = "User"
ASSISTANT_HEADER = "Assistant"
THOUGHTS_HEADER = "Thought Process"


def close_unclosed_fenced_code_blocks(markdown: str) -> str:
    if not isinstance(markdown, str) or not markdown:
        return markdown
    in_fence = False
    for line in markdown.splitlines():
        m = re.match(r"^ {0,3}(```+)", line)
        if not m:
            continue
        if len(m.group(1)) == 3:
            in_fence = not in_fence
    return markdown + "\n```" if in_fence else markdown


def _format_thinking(thinking: str) -> str:
    thinking = close_unclosed_fenced_code_blocks(thinking)
    quoted = "\n".join("> " + line for line in thinking.split("\n"))
    return f"### {THOUGHTS_HEADER}\n\n{quoted}"


def format_message(m: Message, include_thinking: bool, include_hints: bool) -> str:
    if m.role == "user":
        return f"## {USER_HEADER}\n\n{m.content}"

    parts = []
    if include_hints and m.hint:
        parts.append(f"**{m.hint.strip()}**")
    if include_thinking and m.thinking:
        parts.append(_format_thinking(m.thinking))
    if m.content:
        parts.append(close_unclosed_fenced_code_blocks(m.content))
    if not parts:
        return ""
    body = "\n\n".join(p for p in parts if p).strip()
    return f"## {ASSISTANT_HEADER}\n\n{body}"


def format_conversation(
    title,
    url,
    messages: List[Message],
    include_thinking: bool = True,
    include_hints: bool = True,
) -> str:
    header = ""
    if title and url:
        escaped = title.replace("\\", "\\\\").replace("]", "\\]")
        header = f"# [{escaped}]({url})\n\n"
    elif title:
        header = f"# {title}\n\n"

    body_parts = [format_message(m, include_thinking, include_hints) for m in messages]
    body_parts = [b for b in body_parts if b]
    return header + "\n\n---\n\n".join(body_parts) if body_parts else header
