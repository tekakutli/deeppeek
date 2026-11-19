"""extract_code_blocks.py — pull fenced code blocks out of a markdown-ish file.

Filters out blocks that belong to a "thought process" section (as
opposed to the actual assistant response), which is what DeepSeek
chat exports render for model reasoning.

THOUGHT-PROCESS DETECTION
=========================
Two independent signals, combined with OR:

  1. Section-aware.  A `### Thought Process` heading (also matches
     "Thinking", "Reasoning", "Internal Monologue", "Chain of
     Thought" — case-insensitive, level 1-6) opens a thinking
     section.  It closes at the next `---` horizontal rule, or at
     a level-1 / level-2 heading (`# `, `## `), or at a `## User` /
     `## Assistant` turn marker (which also resets per-turn state).
     Only one thinking section is honored per Assistant turn — a
     second `### Thought Process` heading in the same response is
     ignored (defends against the phrase appearing inside the
     response as a sub-heading).

  2. Blockquote heuristic.  Any block with `blockquote > 0` is
     marked thinking regardless of section detection.  DeepSeek
     renders thinking as a blockquote, so this catches the common
     format even when the heading isn't present or the section-end
     detection misfires.  Belt and suspenders.

Each CodeBlock gets a `thinking: bool`.  Default behavior: keep
only `thinking == False` blocks (actual-response code).  See flags.

No regexes.  Markdown fence detection is a small line-based state
machine with CommonMark-compatible rules (3+ backticks or tildes,
matching char, trailing-whitespace-tolerant closing, optional up-to-3
leading spaces, blockquote awareness).  Language validation is
best-effort and never fatal:

  · Python blocks parse with `ast.parse`.  Fragmentary or
    intentionally-incomplete snippets are reported but still written.
  · Non-Python blocks try `tree_sitter_languages` when the package and
    the relevant grammar are installed.  Absent that, no validation is
    attempted and the block is written as-is.

Usage
-----
    python extract_code_blocks.py INPUT.md [OUTPUT_DIR] [flags]

    # Default: OUTPUT_DIR = INPUT_stem + "_blobs" next to INPUT.
    # Default: only actual-response blocks are extracted; thinking
    # blocks are recorded in the review log but not written as files.

Every run writes, inside OUTPUT_DIR:

  · one file per *kept* (non-thinking) block, named
        block_<idx>_<lang>.<ext>
  · one stripped copy of the original file, named
        <input_stem>_stripped<ext>
    in which every KEPT block has been replaced by a single
    placeholder line containing its handle:
        {{block_000_python.py}}
    Blocks NOT kept (thinking blocks, or blocks removed by any
    filter) remain verbatim in the stripped file — the invariant is
    "stripped = original minus kept blocks".
  · manifest.json — array of one JSON object per *kept* block.
  · the review log (see below) lists ALL blocks (kept + skipped),
    so the filter is verifiable at a glance.

REVIEW LOG
==========
Written by default to:

    /tmp/extract_review_<input_stem>_<YYYYmmdd_HHMMSS>.log

Contents:
  · header (input path, output dir, timestamp)
  · summary: total / kept / skipped counts, per-language
    distribution for kept, parse ok/fail, thinking count
  · one section per block (including thinking ones, marked
    [THINKING]), with filename, handle, line range, char count,
    parse status, and a truncated preview.

Override with `--review-log PATH`, or `--review-log -` for stdout.
Suppress with `--no-review-log`.

Flags
-----
    --include-thinking        Include thinking blocks (old behavior).
    --only-thinking           Only include thinking blocks (debug).
    --no-validate             Skip ast / tree-sitter passes.
    --list                    Print the summary to stderr and exit
                              without writing any blob / stripped /
                              manifest files.  Review log still
                              written if --review-log PATH was given
                              (and is not '-').
    -q, --quiet               Silence the per-block summary on stderr.
    --review-log PATH         Review log path.  Default:
                              /tmp/extract_review_<stem>_<ts>.log.
                              Pass '-' for stdout.
    --no-review-log           Don't write the review log.
    --preview-lines N         Lines of content per block in the
                              review log (default: 8).
    --preview-width N         Max chars per preview line before
                              truncation with '…' (default: 200).
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---- data model --------------------------------------------------

@dataclass
class CodeBlock:
    index:       int
    lang:        str
    info:        str
    start_line:  int
    end_line:    int
    body_start:  int
    content:     str
    blockquote:  int = 0
    thinking:    bool = False
    filename:    str = ""
    handle:      str = ""
    parse_ok:    Optional[bool] = None
    parse_error: Optional[str]  = None


# ---- heading / boundary helpers (no regex) -----------------------

def _heading_info(line: str):
    """Return (level, text) if line is a markdown heading, else None.

    A heading is 1-6 '#' chars at line start (up to 3 leading spaces
    allowed), followed by whitespace or end-of-line, then the text.
    """
    s = line.lstrip(" ")
    # Reject if stripping ate more than 3 spaces.
    if len(line) - len(s) > 3:
        return None
    if not s or s[0] != "#":
        return None
    i = 0
    while i < len(s) and s[i] == "#":
        i += 1
    if i == 0 or i > 6:
        return None
    if i < len(s) and s[i] not in (" ", "\t"):
        return None
    return i, s[i:].strip()


_THINKING_KEYWORDS = (
    "thought process", "thinking", "reasoning",
    "internal monologue", "internal reasoning",
    "chain of thought", "chain-of-thought",
)


def _is_thinking_heading(htext: str) -> bool:
    lower = htext.strip().lower()
    if not lower:
        return False
    return any(kw in lower for kw in _THINKING_KEYWORDS)


def _is_turn_heading(htext: str) -> bool:
    """Is this a `## User` / `## Assistant` style turn header?"""
    return htext.strip().lower() in ("user", "assistant", "human", "system")


def _is_boundary_line(line: str) -> bool:
    """Is this a horizontal rule / turn separator?"""
    return line.strip() in ("---", "***", "___")


# ---- fence scanner (no regex) ------------------------------------

def _strip_indent(line: str, max_indent: int = 3) -> str:
    n = 0
    for ch in line:
        if ch == " " and n < max_indent:
            n += 1
        else:
            break
    return line[n:]


def _fence_info(line: str):
    stripped = _strip_indent(line)
    if not stripped:
        return None
    ch = stripped[0]
    if ch not in ("`", "~"):
        return None
    n = 0
    for c in stripped:
        if c == ch:
            n += 1
        else:
            break
    if n < 3:
        return None
    return ch, n, stripped[n:].strip()


def _is_closing_fence(line: str, opener_char: str, opener_len: int) -> bool:
    stripped = _strip_indent(line).rstrip()
    if not stripped:
        return False
    ch = stripped[0]
    if ch != opener_char:
        return False
    n = 0
    for c in stripped:
        if c == ch:
            n += 1
        else:
            break
    if n < opener_len:
        return False
    return stripped[n:].strip() == ""


def _blockquote_depth(line: str) -> int:
    i = 0
    for _ in range(3):
        if i < len(line) and line[i] == " ":
            i += 1
        else:
            break
    d = 0
    while i < len(line) and line[i] == ">":
        d += 1
        i += 1
        if i < len(line) and line[i] == " ":
            i += 1
    return d


def _strip_blockquote(line: str, depth: int) -> str:
    if depth == 0:
        return line
    i = 0
    for _ in range(3):
        if i < len(line) and line[i] == " ":
            i += 1
        else:
            break
    d = 0
    while d < depth and i < len(line) and line[i] == ">":
        d += 1
        i += 1
        if i < len(line) and line[i] == " ":
            i += 1
    if d == 0:
        return line
    return line[i:]


# ---- thought-process mask ---------------------------------------

def _compute_thinking_mask(text: str) -> list[bool]:
    """Return a per-line boolean: is this line inside a thought-
    process section (detected via heading boundaries)?

    Headings, fences, and horizontal rules are all evaluated on the
    blockquote-stripped line, so quoted content doesn't confuse the
    state machine.  Headings inside a fence are ignored (they're
    code, not document structure).
    """
    lines = text.splitlines(keepends=True)
    mask: list[bool] = []

    in_thinking = False
    seen_thinking_in_turn = False
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        d = _blockquote_depth(line)
        effective = _strip_blockquote(line, d) if d else line

        if in_fence:
            if _is_closing_fence(effective, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
            mask.append(in_thinking)
            continue

        fi = _fence_info(effective)
        if fi is not None:
            fence_char, fence_len, _ = fi
            in_fence = True
            mask.append(in_thinking)
            continue

        info = _heading_info(effective)
        if info is not None:
            level, htext = info
            if _is_turn_heading(htext) and level <= 2:
                in_thinking = False
                seen_thinking_in_turn = False
            elif (_is_thinking_heading(htext)
                  and not seen_thinking_in_turn):
                in_thinking = True
                seen_thinking_in_turn = True
            elif (in_thinking and level <= 2
                  and not _is_thinking_heading(htext)):
                in_thinking = False
        elif _is_boundary_line(effective) and in_thinking:
            in_thinking = False

        mask.append(in_thinking)

    return mask


# ---- block extraction --------------------------------------------

def extract_blocks(text: str) -> list[CodeBlock]:
    thinking_mask = _compute_thinking_mask(text)
    lines = text.splitlines(keepends=True)
    blocks: list[CodeBlock] = []

    state = "OUTSIDE"
    fence_char = ""
    fence_len = 0
    fence_info = ""
    bq_depth = 0
    block_start = 0
    body: list[str] = []

    def _finish(end_line: int, terminated: bool) -> None:
        start = block_start - 1        # 0-based line index
        end = end_line                 # 0-based, inclusive
        seg = thinking_mask[start:end]
        section_thinking = any(seg) if seg else False
        thinking = section_thinking or (bq_depth > 0)
        info = fence_info
        if not terminated:
            info = (info + " [UNTERMINATED]").strip()
        blocks.append(CodeBlock(
            index=len(blocks),
            lang=(fence_info.split()[0] if fence_info else ""),
            info=info,
            start_line=block_start,
            end_line=end_line,
            body_start=block_start + 1,
            content="".join(body),
            blockquote=bq_depth,
            thinking=thinking,
        ))

    for i, raw in enumerate(lines, start=1):
        if state == "OUTSIDE":
            d = _blockquote_depth(raw)
            effective = _strip_blockquote(raw, d) if d else raw
            fi = _fence_info(effective)
            if fi is not None:
                fence_char, fence_len, fence_info = fi
                bq_depth = d
                state = "INSIDE"
                block_start = i
                body = []
            continue

        body_line = _strip_blockquote(raw, bq_depth) if bq_depth else raw
        if _is_closing_fence(body_line, fence_char, fence_len):
            _finish(i, terminated=True)
            state = "OUTSIDE"
            fence_char = fence_info = ""
            fence_len = 0
            bq_depth = 0
            body = []
        else:
            body.append(body_line)

    if state == "INSIDE":
        _finish(len(lines), terminated=False)

    return blocks


# ---- handles -----------------------------------------------------

_EXT = {
    "python": "py", "py": "py", "py3": "py", "python3": "py",
    "javascript": "js", "js": "js", "jsx": "jsx",
    "typescript": "ts", "ts": "ts", "tsx": "tsx",
    "json": "json", "yaml": "yaml", "yml": "yml",
    "css": "css", "scss": "scss",
    "html": "html", "htm": "html",
    "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "rust": "rs", "go": "go",
    "c": "c", "cpp": "cpp", "c++": "cpp",
    "java": "java", "kotlin": "kt", "ruby": "rb",
    "markdown": "md", "md": "md", "text": "txt",
    "": "txt",
}


def _safe_ext(lang: str) -> str:
    lang = (lang or "").lower()
    if lang in _EXT:
        return _EXT[lang]
    s = "".join(c for c in lang if c.isalnum())
    return s if 0 < len(s) <= 4 else "txt"


def _safe_tag(lang: str) -> str:
    if not lang:
        return "raw"
    cleaned = "".join(c for c in lang if c.isalnum() or c in "-_")
    return cleaned[:16] or "raw"


def _populate_handles(blocks: list[CodeBlock]) -> None:
    for b in blocks:
        ext = _safe_ext(b.lang)
        tag = _safe_tag(b.lang)
        b.filename = f"block_{b.index:03d}_{tag}.{ext}"
        b.handle = "{{" + b.filename + "}}"


# ---- language validation -----------------------------------------

def _validate_python(block: CodeBlock) -> None:
    if block.lang.lower() not in ("python", "py", "python3", "py3"):
        return
    try:
        ast.parse(block.content)
        block.parse_ok = True
    except SyntaxError as e:
        block.parse_ok = False
        block.parse_error = (
            f"python: {e.msg} (line {e.lineno}, col {e.offset})")


_TS_LANG = {
    "js": "javascript", "javascript": "javascript",
    "jsx": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "tsx": "tsx",
    "json": "json",
    "yaml": "yaml", "yml": "yaml",
    "css": "css", "scss": "scss",
    "html": "html", "htm": "html",
    "bash": "bash", "sh": "bash", "shell": "bash",
    "sql": "sql",
    "rust": "rust", "go": "go", "java": "java",
    "c": "c", "cpp": "cpp", "c++": "cpp",
    "ruby": "ruby", "rb": "ruby",
}


def _try_treesitter(block: CodeBlock) -> None:
    grammar = _TS_LANG.get(block.lang.lower())
    if grammar is None:
        return
    try:
        from tree_sitter_languages import get_parser
    except ImportError:
        return
    try:
        parser = get_parser(grammar)
        tree = parser.parse(block.content.encode("utf-8"))
        root = tree.root_node
        block.parse_ok = not root.has_error
        if root.has_error:
            block.parse_error = f"{grammar}: tree-sitter reported errors"
    except Exception:
        pass


def validate(blocks: list[CodeBlock]) -> None:
    for b in blocks:
        _validate_python(b)
        if b.parse_ok is None:
            _try_treesitter(b)


# ---- blob + stripped writer --------------------------------------

def _build_stripped(text: str, blocks: list[CodeBlock]) -> str:
    """Replace each kept block (fences + body) with its handle line.

    Only blocks in `blocks` (the *kept* set) are replaced; skipped
    blocks (e.g. thinking) remain verbatim.  Blockquote depth is
    preserved on the handle line.
    """
    lines = text.splitlines(keepends=True)
    by_start = {b.start_line: b for b in blocks}
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        b = by_start.get(i + 1)
        if b is not None:
            prefix = "> " * b.blockquote
            out.append(prefix + b.handle + "\n")
            i = b.end_line
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def write_blobs(blocks: list[CodeBlock],
                out_dir: Path,
                original_text: str,
                input_stem: str,
                input_ext: str):
    """Write blobs + stripped file + manifest for `blocks`.

    `blocks` must already be the KEPT set (thinking already filtered
    or not, depending on flags).  The stripped file leaves skipped
    blocks verbatim in the output.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    blob_paths: list[Path] = []
    for b in blocks:
        p = out_dir / b.filename
        p.write_text(b.content, encoding="utf-8")
        blob_paths.append(p)

    stripped = _build_stripped(original_text, blocks)
    stripped_path = out_dir / f"{input_stem}_stripped{input_ext}"
    stripped_path.write_text(stripped, encoding="utf-8")

    (out_dir / "manifest.json").write_text(
        json.dumps([asdict(b) for b in blocks], indent=2),
        encoding="utf-8",
    )
    return blob_paths, stripped_path


# ---- review log --------------------------------------------------

def _build_review_log(all_blocks: list[CodeBlock], kept: list[CodeBlock],
                      *, input_path=None, out_dir=None,
                      preview_lines: int = 8,
                      preview_width: int = 200) -> str:
    out: list[str] = []

    def _w(s: str = "") -> None:
        out.append(s)

    kept_idx = {b.index for b in kept}
    skipped = len(all_blocks) - len(kept)

    _w("=" * 78)
    _w("EXTRACTION REVIEW")
    _w("=" * 78)
    if input_path is not None:
        _w(f"input:     {input_path}")
    if out_dir is not None:
        _w(f"out_dir:   {out_dir}")
    _w(f"timestamp: {datetime.now().isoformat(timespec='seconds')}")
    _w(f"blocks:    {len(all_blocks)} total, {len(kept)} kept, "
       f"{skipped} skipped")
    _w("")

    by_lang: dict[str, int] = {}
    for b in kept:
        key = b.lang or "(none)"
        by_lang[key] = by_lang.get(key, 0) + 1
    if by_lang:
        _w("kept by language:")
        for lang in sorted(by_lang):
            _w(f"  {lang:<24} {by_lang[lang]}")
        _w("")

    ok = sum(1 for b in kept if b.parse_ok is True)
    bad = sum(1 for b in kept if b.parse_ok is False)
    if ok or bad:
        parts = []
        if ok:
            parts.append(f"{ok} ok")
        if bad:
            parts.append(f"{bad} with errors")
        _w(f"parse (kept only): {', '.join(parts)}")
        _w("")

    thinking = sum(1 for b in all_blocks if b.thinking)
    if thinking:
        _w(f"thinking blocks: {thinking} of {len(all_blocks)} "
           f"(filtered out by default; use --include-thinking "
           f"to keep)")
        _w("")

    _w("=" * 78)

    for b in all_blocks:
        _w("")
        tag = ""
        if b.index not in kept_idx:
            tag = "  [SKIPPED]"
        if b.thinking:
            tag += "  [THINKING]"
        header = (f"block {b.index:03d}{tag}  "
                  f"lang={b.lang or '(none)'}  "
                  f"lines {b.start_line}-{b.end_line}  "
                  f"{len(b.content)} chars")
        if b.blockquote:
            header += f"  (bq×{b.blockquote})"
        _w(header)
        _w(f"  filename: {b.filename}")
        _w(f"  handle:   {b.handle}")
        if b.parse_ok is True:
            _w("  parse:    ok")
        elif b.parse_ok is False:
            _w(f"  parse:    FAILED — {b.parse_error}")
        _w("  ---")

        content_lines = b.content.splitlines()
        show = preview_lines
        if b.index not in kept_idx:
            # Smaller preview for skipped blocks — enough to verify
            # the filter is right, not enough to clutter.
            show = min(preview_lines, 2)
        for ln in content_lines[:show]:
            if len(ln) > preview_width:
                ln = ln[:preview_width] + "…"
            _w(f"  | {ln}")
        extra = len(content_lines) - show
        if extra > 0:
            suffix = ""
            if b.index not in kept_idx:
                suffix = " (skipped)"
            _w(f"  | ... ({extra} more line"
               f"{'s' if extra != 1 else ''}){suffix}")

    return "\n".join(out) + "\n"


def write_review_log(all_blocks: list[CodeBlock],
                     kept: list[CodeBlock],
                     path: Path, *,
                     input_path=None, out_dir=None,
                     preview_lines: int = 8,
                     preview_width: int = 200) -> Path:
    text = _build_review_log(
        all_blocks, kept,
        input_path=input_path,
        out_dir=out_dir,
        preview_lines=preview_lines,
        preview_width=preview_width,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- CLI ---------------------------------------------------------

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _safe_stem(stem: str) -> str:
    out = "".join(
        c if (c.isalnum() or c in "-_") else "_" for c in stem
    )
    return out or "input"


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_code_blocks.py",
        description=(
            "Extract fenced code blocks from a markdown-ish file.  "
            "Thought-process blocks (DeepSeek's `### Thought "
            "Process` section, also matched by blockquote) are "
            "excluded by default."
        ),
    )
    p.add_argument("input", metavar="INPUT", help="input file to scan")
    p.add_argument("output_dir", metavar="OUTPUT_DIR", nargs="?",
                   default=None,
                   help="output directory (default: "
                        "<input_stem>_blobs/ next to INPUT)")
    p.add_argument("--include-thinking", action="store_true",
                   help="include thought-process blocks (old "
                        "behavior)")
    p.add_argument("--only-thinking", action="store_true",
                   help="only include thought-process blocks "
                        "(debug)")
    p.add_argument("--no-validate", action="store_true",
                   help="skip ast / tree-sitter validation")
    p.add_argument("--list", action="store_true",
                   help="print the summary to stderr and exit "
                        "without writing blob / stripped / "
                        "manifest files")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="silence the per-block summary on stderr")
    p.add_argument("--review-log", metavar="PATH", default=None,
                   help="review log path (default: "
                        "/tmp/extract_review_<stem>_<ts>.log).  "
                        "Pass '-' for stdout.")
    p.add_argument("--no-review-log", action="store_true",
                   help="don't write the review log")
    p.add_argument("--preview-lines", type=int, default=8, metavar="N",
                   help="lines of content per block in the review "
                        "log (default: 8)")
    p.add_argument("--preview-width", type=int, default=200,
                   metavar="CHARS",
                   help="max chars per preview line in the review "
                        "log before truncation with '…' "
                        "(default: 200)")
    # Deprecated — this is now the default.  Accepted for backwards
    # compatibility, ignored.
    p.add_argument("--top-level-only", action="store_true",
                   help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    parser = _build_arg_parser()
    ns = parser.parse_args(argv)

    src = Path(ns.input)
    if not src.is_file():
        print(f"error: not a file: {src}", file=sys.stderr)
        return 1

    out_dir = (Path(ns.output_dir) if ns.output_dir
               else src.parent / (src.stem + "_blobs"))

    text = _read_text(src)
    all_blocks = extract_blocks(text)

    # Partition on the thinking flag.
    if ns.include_thinking and not ns.only_thinking:
        kept = list(all_blocks)
    elif ns.only_thinking:
        kept = [b for b in all_blocks if b.thinking]
    else:
        kept = [b for b in all_blocks if not b.thinking]

    # Reindex kept blocks so their filenames are 0..N-1 with no
    # gaps.  `all_blocks` retains original indices for the review
    # log; `kept` gets a fresh contiguous sequence.
    for i, b in enumerate(kept):
        b.index = i

    if not ns.no_validate:
        validate(kept)

    _populate_handles(kept)

    # Per-block stderr summary (kept blocks only, unless -q or -v
    # variant — we print all here with markers so the user sees
    # what was filtered).
    if not ns.quiet:
        kept_ids = {id(b) for b in kept}
        for b in all_blocks:
            is_kept = id(b) in kept_ids
            status = ""
            if is_kept and b.parse_ok is True:
                status = "  [parse ok]"
            elif is_kept and b.parse_ok is False:
                status = f"  [parse error: {b.parse_error}]"
            bq = f" (bq×{b.blockquote})" if b.blockquote else ""
            tag = ""
            if not is_kept:
                tag = "  [SKIPPED]"
            if b.thinking:
                tag += "  [THINKING]"
            handle = b.handle
            if not is_kept:
                # Show what its handle WOULD be, for reference.
                ext = _safe_ext(b.lang)
                tag2 = _safe_tag(b.lang)
                handle = f"{{{{block_{b.index:03d}_{tag2}.{ext}}}}}"
            print(
                f"block {b.index:03d}  {b.lang or '(none)':<12}"
                f"lines {b.start_line}-{b.end_line}  "
                f"{len(b.content):>6} chars{bq}  {handle}"
                f"{tag}{status}",
                file=sys.stderr)

    thinking_skipped = len(all_blocks) - len(kept)

    # --list mode: summary only, no blob writes.  Review log still
    # written if the user explicitly named a path.
    if ns.list:
        if (ns.review_log and ns.review_log != "-"
                and not ns.no_review_log):
            review_path = Path(ns.review_log)
            write_review_log(
                all_blocks, kept, review_path,
                input_path=src, out_dir=out_dir,
                preview_lines=ns.preview_lines,
                preview_width=ns.preview_width,
            )
            print(f"review log → {review_path}", file=sys.stderr)
        print(f"\n{len(kept)} block(s) kept of "
              f"{len(all_blocks)} total"
              f" ({thinking_skipped} skipped)",
              file=sys.stderr)
        if thinking_skipped and not ns.include_thinking:
            print("(skipped blocks are thought-process content; "
                  "pass --include-thinking to include them)",
                  file=sys.stderr)
        return 0

    blob_paths, stripped_path = write_blobs(
        kept, out_dir, text, src.stem, src.suffix or ".txt")

    # Review log (lists all blocks, including skipped, with markers).
    if not ns.no_review_log:
        if ns.review_log == "-":
            sys.stdout.write(_build_review_log(
                all_blocks, kept,
                input_path=src, out_dir=out_dir,
                preview_lines=ns.preview_lines,
                preview_width=ns.preview_width,
            ))
        else:
            if ns.review_log:
                review_path = Path(ns.review_log)
            else:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                review_path = (
                    Path("/tmp")
                    / f"extract_review_{_safe_stem(src.stem)}_"
                      f"{ts}.log"
                )
            write_review_log(
                all_blocks, kept, review_path,
                input_path=src, out_dir=out_dir,
                preview_lines=ns.preview_lines,
                preview_width=ns.preview_width,
            )
            print(f"review log → {review_path}", file=sys.stderr)

    print(f"\n{len(kept)} block(s) kept of {len(all_blocks)} total"
          f" → {out_dir}/", file=sys.stderr)
    if thinking_skipped and not ns.include_thinking:
        print(f"({thinking_skipped} thought-process block(s) skipped; "
              f"--include-thinking to include)",
              file=sys.stderr)
    print(f"stripped file → {stripped_path}", file=sys.stderr)

    if len(all_blocks) and not len(kept):
        print(
            "\nwarning: all blocks were filtered out.  If your file "
            "does not use the standard DeepSeek thought-process "
            "format (blockquoted or under a '### Thought Process' "
            "heading), pass --include-thinking to inspect them.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
