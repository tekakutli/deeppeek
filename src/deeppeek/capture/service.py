"""High-level capture: run the local HTTP server, block on a payload,
render it to markdown/JSON, write to disk."""
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


def _safe_title(title: Optional[str], maxlen: int = 30) -> str:
    if not title:
        return ""
    s = re.sub(r"[^a-zA-Z0-9-_\s]", "", title)
    s = re.sub(r"\s+", "_", s)
    return s[:maxlen].rstrip("_").strip()


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


def capture_to_markdown(cfg: CaptureConfig) -> Path:
    server = ExportServer(port=cfg.port, watch_mode=cfg.watch)
    server.start()
    print(f"Listening on http://127.0.0.1:{cfg.port}", file=sys.stderr)
    print("Open Zen to chat.deepseek.com and click the DEEPPEEK toolbar button.",
          file=sys.stderr)
    print("Press Ctrl+C to cancel.", file=sys.stderr)
    try:
        payload = server.wait_for_result(timeout=cfg.timeout)
    finally:
        server.stop()

    conversation = Conversation.from_json(payload)
    print(f"  captured {len(conversation.messages)} message(s)", file=sys.stderr)
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
    )

    if cfg.last_user_turns is not None or cfg.start_prompt or cfg.end_prompt \
            or cfg.start_index is not None or cfg.end_index is not None:
        print(f"  sliced to {len(sliced)} message(s)", file=sys.stderr)
        _print_prompt_index(sliced, "prompts after slice")

    if cfg.as_json:
        out = json.dumps(
            {
                "title": conversation.title,
                "url": conversation.url,
                "messages": [m.to_dict() for m in sliced],
            },
            indent=2,
            ensure_ascii=False,
        )
    else:
        out = format_conversation(
            conversation.title,
            conversation.url,
            sliced,
            include_thinking=cfg.include_thinking,
            include_hints=cfg.include_hints,
        )
        if cfg.latex:
            out = convert_latex_delimiters(out)

    output_path = cfg.output
    if output_path is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
        safe = _safe_title(conversation.title)
        ext = ".json" if cfg.as_json else ".md"
        suffix = f"_{safe}" if safe else ""
        output_path = Path(f"DeepSeek{suffix}_{stamp}{ext}")

    output_path.write_text(out, encoding="utf-8")
    print(f"Wrote {output_path} ({len(sliced)} message(s))", file=sys.stderr)
    return output_path
