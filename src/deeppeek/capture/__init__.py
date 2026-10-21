"""capture — receive a DeepSeek conversation from the browser extension.

Public API:

  CaptureConfig, CaptureResult
  capture_conversation(cfg) -> CaptureResult   # in memory, no disk
  capture_to_markdown(cfg)  -> Path            # writes to disk
  default_output_path(title, as_json) -> Path  # auto-naming helper

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
