"""Flatten OpenAI messages into a single prompt for the web composer."""
from typing import Iterable


_SEPARATOR = "\n\n---\n\n"


def serialize_messages(messages: Iterable[dict]) -> str:
    """Flatten messages into one string.  Content only, joined by
    _SEPARATOR.  No system override prefix, no role banners, no
    wrapping of any kind."""
    parts: list[str] = []
    for m in messages:
        content = _content_str(m.get("content"))
        if not content.strip():
            continue
        parts.append(content)
    return _SEPARATOR.join(parts)


def _content_str(content) -> str:
    """Coerce OpenAI's ``content`` (string OR list of content-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(part.get("text") or "")
        return "\n".join(out)
    return str(content)
