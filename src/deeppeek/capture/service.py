"""High-level capture: run the local HTTP server, block on a payload,
slice, render, and either return the text in memory or write it to
disk.

Two entry points for callers:

  capture_conversation(cfg)   — capture + slice + render, return a
                                CaptureResult without touching disk.
  capture_to_markdown(cfg)    — capture + slice + render + write to
                                cfg.output (auto-naming if None),
                                return the written Path.

Also exports default_output_path(title, as_json) — the timestamped
filename generator — so the CLI can produce the same name for
capture -o auto.
"""
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Conversation
from .server import ExportServer
from .slicer import slice_messages
from .formatter import format_conversation
from .latex import convert_latex_delimiters


@dataclass
class CaptureConfig:
    port: int = 8765
    watch: bool = False
    output: Optional[Path] = None
    start_prompt: Optional[str] = None
    end_prompt: Optional[str] = None
    start_index: Optional[int] = None
    end_index: Optional[int] = None
    match_mode: str = "substring"
    context: int = 0
    include_thinking: bool = True
    include_hints: bool = True
    latex: bool = False
    as_json: bool = False
    timeout: Optional[float] = None
    last_user_turns: Optional[int] = None
    last_regex: Optional[str] = None
    until_user_turn: Optional[int] = None
    until_regex: Optional[str] = None
    include_forked: bool = False


@dataclass
class CaptureResult:
    text: str
    title: Optional[str]
    message_count: int


def _safe_title(title: Optional[str], maxlen: int = 30) -> str:
    if not title:
        return ""
    s = re.sub(r"[^a-zA-Z0-9-_\s]", "", title)
    s = re.sub(r"\s+", "_", s)
    return s[:maxlen].rstrip("_").strip()


def default_output_path(title: Optional[str], as_json: bool) -> Path:
    """Produce a timestamped default output filename.

    Used by capture -o auto and by capture_to_markdown when
    cfg.output is None.  Example: DeepSeek_2026-10-21_15-42-03.md
    or DeepSeek_<sanitized-title>_2026-10-21_15-42-03.md if the
    conversation had a title.
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
    safe = _safe_title(title)
    ext = ".json" if as_json else ".md"
    suffix = f"_{safe}" if safe else ""
    return Path(f"DeepSeek{suffix}_{stamp}{ext}")


def _print_prompt_index(messages, label: str) -> None:
    prompts = [m for m in messages if m.role == "user"]
    if not prompts:
        print(f"  {label}: (none)", file=sys.stderr)
        return
    print(f"  {label} ({len(prompts)}):", file=sys.stderr)
    for i, p in enumerate(prompts):
        preview = (p.content or "").replace("\n", " ").strip()[:80]
        if len(p.content or "") > 80:
            preview += "…"
        print(f"    [{i}] {preview}", file=sys.stderr)


def capture_payload(cfg: CaptureConfig) -> Conversation:
    server = ExportServer(
        port=cfg.port, watch_mode=cfg.watch,
        include_forked=cfg.include_forked,
    )
    server.start()
    print(f"Listening on http://127.0.0.1:{cfg.port}", file=sys.stderr)
    print("Open Zen to chat.deepseek.com and click the DEEPPEEK toolbar button.",
          file=sys.stderr)
    print("Press Ctrl+C to cancel.", file=sys.stderr)
    try:
        payload = server.wait_for_result(timeout=cfg.timeout)
    finally:
        server.stop()
    return Conversation.from_json(payload)


def render_conversation(conversation: Conversation,
                        cfg: CaptureConfig) -> CaptureResult:
    print(f"  captured {len(conversation.messages)} message(s)",
          file=sys.stderr)
    _print_prompt_index(conversation.messages, "prompts in full capture")

    sliced = slice_messages(
        conversation.messages,
        start_prompt=cfg.start_prompt,
        end_prompt=cfg.end_prompt,
        start_index=cfg.start_index,
        end_index=cfg.end_index,
        match_mode=cfg.match_mode,
        context=cfg.context,
        last_user_turns=cfg.last_user_turns,
        last_regex=cfg.last_regex,
        until_user_turn=cfg.until_user_turn,
        until_regex=cfg.until_regex,
    )

    sliced_flag = (
        cfg.last_user_turns is not None
        or cfg.last_regex is not None
        or cfg.until_user_turn is not None
        or cfg.until_regex is not None
        or cfg.start_prompt is not None
        or cfg.end_prompt is not None
        or cfg.start_index is not None
        or cfg.end_index is not None
    )
    if sliced_flag:
        print(f"  sliced to {len(sliced)} message(s)", file=sys.stderr)
        _print_prompt_index(sliced, "prompts after slice")

    if cfg.as_json:
        text = json.dumps(
            {
                "title": conversation.title,
                "url": conversation.url,
                "messages": [m.to_dict() for m in sliced],
            },
            indent=2,
            ensure_ascii=False,
        )
    else:
        text = format_conversation(
            conversation.title,
            conversation.url,
            sliced,
            include_thinking=cfg.include_thinking,
            include_hints=cfg.include_hints,
        )
        if cfg.latex:
            text = convert_latex_delimiters(text)

    return CaptureResult(
        text=text,
        title=conversation.title,
        message_count=len(sliced),
    )


def capture_conversation(cfg: CaptureConfig) -> CaptureResult:
    conversation = capture_payload(cfg)
    return render_conversation(conversation, cfg)


def capture_to_markdown(cfg: CaptureConfig) -> Path:
    result = capture_conversation(cfg)
    output_path = cfg.output
    if output_path is None:
        output_path = default_output_path(result.title, cfg.as_json)
    output_path.write_text(result.text, encoding="utf-8")
    print(f"Wrote {output_path} ({result.message_count} message(s))",
          file=sys.stderr)
    return output_path
