"""conversation — capture a DeepSeek conversation to markdown.

Formerly `deeppeek.capture`.  Trimmed to the conversation-only
surface: no recovery, no apply-to, no metadata.  The `conversation`
command consumes this package; nothing else does.

Public API:

    CaptureConfig, CaptureResult
    capture_conversation(cfg)     → CaptureResult   (no disk write)
    capture_payload(cfg)          → Conversation
    render_conversation(c, cfg)   → CaptureResult
    default_output_path(title, as_json) → Path
    capture_to_markdown(cfg)      → Path            (kept for callers)

Plus the low-level pieces (Conversation, Message, format_conversation,
slice_messages) for tests and one-off scripts.
"""
from .models import Conversation, Message
from .service import (
    CaptureConfig,
    CaptureResult,
    capture_conversation,
    capture_payload,
    capture_to_markdown,
    default_output_path,
    render_conversation,
)
from .formatter import format_conversation
from .slicer import slice_messages

__all__ = [
    "Conversation", "Message",
    "CaptureConfig", "CaptureResult",
    "capture_conversation", "capture_payload", "capture_to_markdown",
    "default_output_path",
    "render_conversation",
    "format_conversation", "slice_messages",
]
