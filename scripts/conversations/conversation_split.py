#!/usr/bin/env python3
"""
conversation_split.py — split a markdown conversation into parts under
a hard byte limit.

DECISION TREE
=============

The splitter walks the conversation from the beginning, greedily packing
units into a chunk until the next unit would push it over the hard byte
limit, then cuts and continues.  Units are chosen at the least
disruptive granularity available, and if a single unit is still larger
than the limit, the splitter recurses on *that unit* at the next level
down.  The levels, from least to most disruptive:

    0. turn pairs        — a User+Assistant block kept together
    1. speaker blocks    — an individual ## User or ## Assistant block
    2. sub-blocks        — an ### subsection inside a speaker block
                           (this is where ### Thought Process fires)
    3. paragraphs        — blank-line separated
    4. sentences         — . ! ? boundaries
    5. words             — whitespace boundaries
    6. characters        — hard byte cut, respects UTF-8 codepoints

After the greedy pass, adjacent chunks are coalesced whenever their
combined size still fits under the limit.  That post-pass prevents
thin orphan parts left behind when the packer had to flush a leading
buffer to make room for an oversized unit, or when a lone subsection
header was separated from an oversized body.

THOUGHT PROCESS
===============

`### Thought Process` is caught by level 2 — the generic `### `
sub-block rule.  Thought and reply therefore stay together (via the
level-1 speaker-block grouping) until the Assistant block is too big.
When it does split, the thought and reply separate; the reply half
recurses at paragraphs/sentences/etc.  `--handles` marks the reply
half with a continuation marker so it doesn't read as a bare
paragraph.

HANDLES
=======

With `--handles`, each boundary between chunk N and chunk N+1 carries
a shared identifier `hN` on both sides:

    tail of chunk N:  <!-- ─── handle hN ─── end of part N/M · continues as part N+1/M ─── -->
    head of chunk N+1: <!-- ─── handle hN ─── start of part N+1/M · continued from part N/M ─── -->

Chunk 1 has no head; chunk M has no tail.  The `hN` lets a reader
(or an LLM) match chunk N+1's opening to chunk N's closing.  Without
`--handles`, chunks are pure slices of the input and concatenate
back byte-for-byte.

Handles add roughly 200 bytes of UTF-8 overhead per boundary (two
`───` glyphs are 9 bytes each, and there are six of them per pair of
markers, plus the part numbers).  When `--handles` is on and the limit
is comfortably larger than that overhead, `HANDLE_HEADROOM_BYTES` is
reserved from the content limit so the final parts — markers included —
stay under the user-requested limit.  For tiny limits where the
headroom would dominate, the raw limit is kept and any post-handle
oversize triggers the warning at the end of `main()`.

USAGE
=====

    conversation_split.py conversation1.md --handles
       → fit to < 125 KiB per part, with handle markers

    conversation_split.py conversation1.md --max-part-bytes 200000
       → fit to < 200 KB, no handles (pure slices)

    conversation_split.py conversation1.md -n 9 --handles
       → target 9 parts, with handles

    conversation_split.py conversation1.md --dry-run
    conversation_split.py conversation1.md -o /tmp/parts/ --handles
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DEFAULT_MAX_PART_BYTES = 125 * 1024

# Conservative upper bound on the byte overhead added by a matched pair of
# handle markers (`start of part ...` + `end of part ...`).  Measured worst
# case: ~190 bytes for two-digit part numbers; padded to 256 so the final
# files stay under the user-requested limit even with three-digit part
# counts or future changes to the marker format.
HANDLE_HEADROOM_BYTES = 256


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


# ---------------------------------------------------------------------------
# Parsing — top-level speaker blocks.
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"(?m)^## (?:User|Assistant)\s*$")


def _parse_blocks(text: str) -> list[dict]:
    positions = [m.start() for m in _HEADER_RE.finditer(text)]
    if not positions:
        return [{"speaker": None, "text": text}]

    blocks: list[dict] = []
    if positions[0] > 0:
        blocks.append({"speaker": None, "text": text[:positions[0]]})

    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        block_text = text[start:end]
        header_line = block_text.split("\n", 1)[0]
        speaker = "User" if "User" in header_line else "Assistant"
        blocks.append({"speaker": speaker, "text": block_text})

    return blocks


# ---------------------------------------------------------------------------
# Per-level unit splitters.
# ---------------------------------------------------------------------------

def _units_turn_pairs(text: str) -> list[str]:
    blocks = _parse_blocks(text)
    if not blocks:
        return [text]
    preamble = ""
    i = 0
    if blocks[0]["speaker"] is None:
        preamble = blocks[0]["text"]
        i = 1

    pairs: list[str] = []
    while i < len(blocks):
        if (blocks[i]["speaker"] == "User"
                and i + 1 < len(blocks)
                and blocks[i + 1]["speaker"] == "Assistant"):
            pairs.append(blocks[i]["text"] + blocks[i + 1]["text"])
            i += 2
        else:
            pairs.append(blocks[i]["text"])
            i += 1

    if pairs and preamble:
        pairs[0] = preamble + pairs[0]
    elif preamble:
        pairs.append(preamble)
    return pairs


def _units_speaker_blocks(text: str) -> list[str]:
    blocks = _parse_blocks(text)
    preamble = ""
    units: list[str] = []
    for b in blocks:
        if b["speaker"] is None:
            preamble = b["text"]
        else:
            units.append(b["text"])
    if units and preamble:
        units[0] = preamble + units[0]
    elif preamble:
        units.append(preamble)
    return units


def _units_sub_blocks(text: str) -> list[str]:
    """Split a single speaker block at ### subsection boundaries.

    Catches '### Thought Process' as well as any other ### header.
    The ## header stays with the first sub-block; subsequent sub-blocks
    are bare (the --handles tail/head markers on the resulting chunks
    provide the 'continued' context).

    Note: `re.split(r"(?m)(?=^### )", body)` emits an empty leading
    element whenever `body` itself starts with `### `, because the
    zero-width lookahead matches at position 0.  Without filtering,
    `parts[0] = header + parts[0]` would promote that empty element
    into a header-only unit ("## Assistant\\n\\n"), which then gets
    flushed by itself when the following body is oversized — producing
    a ~13-byte orphan chunk.  Filter empties *before* prepending the
    header, and return `[text]` unchanged if the split yielded nothing
    (a single subsection doesn't need level-2 splitting).
    """
    m = re.match(r"(## (?:User|Assistant)[^\n]*\n\n?)(.*)", text, re.DOTALL)
    if not m:
        return [text]
    header, body = m.group(1), m.group(2)
    parts = [p for p in re.split(r"(?m)(?=^### )", body) if p]
    if len(parts) <= 1:
        return [text]
    parts[0] = header + parts[0]
    return parts


def _rejoin_with_separator(parts: list[str]) -> list[str]:
    """Pair (content, separator) chunks produced by `re.split` with a
    capturing group, then drop any empties.

    `re.split(r"(\\s+)", " abc def")` returns `['', ' ', 'abc', ' ',
    'def']`: the leading empty string exists because the splitter
    captured the whitespace at the front.  Filtering empties *before*
    pairing keeps units aligned to content boundaries and prevents a
    stray whitespace-only unit from being emitted alone.
    """
    parts = [p for p in parts if p]
    if not parts:
        return []
    units: list[str] = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts):
            units.append(parts[i] + parts[i + 1])
            i += 2
        else:
            units.append(parts[i])
            i += 1
    return [u for u in units if u]


def _units_paragraphs(text: str) -> list[str]:
    return _rejoin_with_separator(re.split(r"(\n{2,})", text))


def _units_sentences(text: str) -> list[str]:
    return _rejoin_with_separator(re.split(r"((?<=[.!?])\s+)", text))


def _units_words(text: str) -> list[str]:
    return _rejoin_with_separator(re.split(r"(\s+)", text))


_UNIT_FNS = [
    _units_turn_pairs,
    _units_speaker_blocks,
    _units_sub_blocks,
    _units_paragraphs,
    _units_sentences,
    _units_words,
]

_NUM_LEVELS = 7


# ---------------------------------------------------------------------------
# Recursive splitting.
# ---------------------------------------------------------------------------

def _split_chars(text: str, max_chunk_bytes: int) -> list[str]:
    """Split `text` into chunks of at most `max_chunk_bytes` bytes,
    never breaking a UTF-8 codepoint.

    Walks codepoints and tracks byte count incrementally.  If a single
    codepoint is larger than `max_chunk_bytes` (e.g. a 4-byte emoji
    with a 2-byte limit) it goes into its own chunk — oversized but
    valid UTF-8.  This avoids the previous `errors="replace"` fallback,
    which silently corrupted multi-byte characters and could emit
    replacement chars in the middle of a split.
    """
    if max_chunk_bytes < 1:
        max_chunk_bytes = 1
    chunks: list[str] = []
    buf: list[str] = []
    buf_bytes = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if buf_bytes + ch_bytes > max_chunk_bytes:
            if buf:
                chunks.append("".join(buf))
            buf = [ch]
            buf_bytes = ch_bytes
        else:
            buf.append(ch)
            buf_bytes += ch_bytes
    if buf:
        chunks.append("".join(buf))
    return chunks


def _split_at_level(text: str, limit: int, level: int) -> list[str]:
    if _byte_len(text) < limit:
        return [text]

    if level >= _NUM_LEVELS - 1:
        return _split_chars(text, max(1, limit - 1))

    units = [u for u in _UNIT_FNS[level](text) if u]
    if not units:
        # Defensive: no splitter produced anything usable.  Falling
        # through to chars guarantees termination.
        return _split_chars(text, max(1, limit - 1))
    if len(units) <= 1:
        return _split_at_level(text, limit, level + 1)

    chunks: list[str] = []
    buffer = ""
    buffer_bytes = 0
    for unit in units:
        unit_bytes = _byte_len(unit)
        if buffer_bytes + unit_bytes < limit:
            buffer += unit
            buffer_bytes += unit_bytes
        else:
            if buffer:
                chunks.append(buffer)
            if unit_bytes < limit:
                buffer = unit
                buffer_bytes = unit_bytes
            else:
                chunks.extend(_split_at_level(unit, limit, level + 1))
                buffer = ""
                buffer_bytes = 0
    if buffer:
        chunks.append(buffer)
    return chunks


def _coalesce(chunks: list[str], limit: int) -> list[str]:
    """Merge adjacent chunks whose combined byte size is still < limit.

    The greedy packer in `_split_at_level` flushes its buffer the moment
    the next unit is oversized, so a single oversized unit can leave a
    tiny orphan in front of it (the buffer) and another behind it (the
    recursive remainder).  Rejoining adjacent chunks when they fit
    together removes those orphans without changing the byte-for-byte
    content or exceeding the per-chunk limit.

    Example: splitting [98K, 272B, 201B, 125K, 3K, 51K] with limit
    128,000 gives [98.5K, 125K, 54K] — three clean parts.
    """
    cleaned = [c for c in chunks if c]
    if len(cleaned) <= 1:
        return cleaned
    merged = [cleaned[0]]
    merged_bytes = [_byte_len(cleaned[0])]
    for c in cleaned[1:]:
        c_bytes = _byte_len(c)
        if merged_bytes[-1] + c_bytes < limit:
            merged[-1] += c
            merged_bytes[-1] += c_bytes
        else:
            merged.append(c)
            merged_bytes.append(c_bytes)
    return merged


def split_conversation(text: str, max_bytes: int) -> list[str]:
    if _byte_len(text) < max_bytes:
        return [text]
    chunks = _split_at_level(text, max_bytes, 0)
    return _coalesce(chunks, max_bytes)


# ---------------------------------------------------------------------------
# Handle injection.
# ---------------------------------------------------------------------------

def _add_handles(chunks: list[str]) -> list[str]:
    """Wrap each chunk with head/tail markers indicating serial
    continuity.

    Chunk 1 has no head.  Chunk M has no tail.  Chunk K (1 < K < M)
    ends with handle h{K}, and chunk K+1 begins with the same h{K},
    so both sides of the boundary carry the same identifier.
    """
    total = len(chunks)
    if total <= 1:
        return list(chunks)
    result: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        head = ""
        tail = ""
        if i > 1:
            head = (
                f"<!-- ─── handle h{i-1} ─── start of part {i}/{total} · "
                f"continued from part {i-1}/{total} ─── -->\n\n"
            )
        if i < total:
            tail = (
                f"\n\n<!-- ─── handle h{i} ─── end of part {i}/{total} · "
                f"continues as part {i+1}/{total} ─── -->\n"
            )
        result.append(head + chunk + tail)
    return result


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="conversation_split.py",
        description=(
            "Split a markdown conversation into parts under a hard byte "
            "limit.  Uses a priority-ordered decision tree: turn pairs "
            "kept intact first, then speaker blocks, then ### sub-blocks "
            "(including ### Thought Process), then paragraphs, then "
            "sentences, then words, then raw characters.  Greedy packing "
            "from the start of the conversation; oversized units recurse "
            "on the next level; adjacent chunks are coalesced afterwards "
            "so no orphan thin parts survive."
        ),
    )
    ap.add_argument("conversation", type=Path, metavar="CONVERSATION",
                    help="input markdown conversation")
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "-n", "--parts", type=int, default=None, metavar="N",
        help="target number of parts.  Effective byte limit becomes "
             "total_bytes/N, capped at --max-part-bytes.",
    )
    group.add_argument(
        "--max-part-bytes", type=int, default=None, metavar="N",
        help=f"hard byte limit per part "
             f"(default: {DEFAULT_MAX_PART_BYTES:,}).",
    )
    ap.add_argument(
        "--handles", action="store_true",
        help="wrap each part with handle markers that signal serial "
             "continuation from the previous part.  Chunk K ends with "
             "handle hK; chunk K+1 begins with the same hK — so both "
             "sides of a boundary carry the same identifier.  Without "
             "this flag, chunks are pure slices of the input and "
             "concatenate back byte-for-byte.",
    )
    ap.add_argument(
        "-o", "--output-dir", type=Path, default=None, metavar="DIR",
        help="directory for the split parts "
             "(default: alongside CONVERSATION)",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan but don't write any files")
    args = ap.parse_args(argv)

    if not args.conversation.is_file():
        print(f"error: not a file: {args.conversation}", file=sys.stderr)
        return 1

    # newline="" preserves CRLF and lone-CR line endings, so the
    # "chunks concatenate back byte-for-byte" guarantee holds for any
    # valid UTF-8 input, not just LF-only files.
    try:
        with args.conversation.open(
            mode="r", encoding="utf-8", newline=""
        ) as f:
            text = f.read()
    except OSError as e:
        print(f"error: cannot read {args.conversation}: {e}",
              file=sys.stderr)
        return 1
    except UnicodeDecodeError as e:
        print(f"error: {args.conversation} is not valid UTF-8: {e}",
              file=sys.stderr)
        return 1

    total_bytes = _byte_len(text)
    if total_bytes == 0:
        print("error: conversation file is empty", file=sys.stderr)
        return 1

    max_bytes = (args.max_part_bytes if args.max_part_bytes is not None
                 else DEFAULT_MAX_PART_BYTES)
    if max_bytes < 2:
        print("error: --max-part-bytes must be >= 2", file=sys.stderr)
        return 1

    if args.parts is not None:
        if args.parts < 1:
            print("error: --parts must be >= 1", file=sys.stderr)
            return 1
        effective_limit = max(2, total_bytes // args.parts)
        effective_limit = min(effective_limit, max_bytes)
    else:
        effective_limit = max_bytes

    # Reserve room for handle markers so the *final* parts (markers
    # included) stay under max_bytes.  Only subtract when the limit is
    # large enough that the headroom won't dominate; otherwise leave
    # the raw limit in place and let the oversize warning fire at the
    # end of main().
    if args.handles and effective_limit > 2 * HANDLE_HEADROOM_BYTES:
        effective_limit -= HANDLE_HEADROOM_BYTES

    parts = split_conversation(text, effective_limit)
    if args.handles:
        parts = _add_handles(parts)
    num_parts = len(parts)

    oversized = [i for i, p in enumerate(parts, 1)
                 if _byte_len(p) >= max_bytes]
    if oversized:
        print(
            f"warning: {len(oversized)} part(s) are >= {max_bytes:,} "
            f"bytes: indices {oversized[:5]}"
            + (" ..." if len(oversized) > 5 else ""),
            file=sys.stderr,
        )

    orig_name = args.conversation.name
    stem = args.conversation.stem
    suffix = args.conversation.suffix

    if args.handles:
        limit_desc = (
            f"limit: < {max_bytes:,} bytes incl. handles, "
            f"< {effective_limit:,} bytes content"
        )
    else:
        limit_desc = f"limit: < {max_bytes:,} bytes"

    print(
        f"Split {orig_name} ({total_bytes:,} bytes) into "
        f"{num_parts} part(s) [{limit_desc}"
        + (", handles on" if args.handles else "") + "]",
        file=sys.stderr,
    )

    def _report(part: str, out_path: Path) -> None:
        n = _byte_len(part)
        pct = 100.0 * n / max(total_bytes, 1)
        print(f"  {out_path.name}: {n:,} bytes ({pct:.1f}%)",
              file=sys.stderr)

    if args.dry_run:
        for i, part in enumerate(parts, 1):
            name = f"{stem}_part{i}of{num_parts}{suffix}"
            out_path = (args.output_dir / name if args.output_dir
                        else args.conversation.with_name(name))
            _report(part, out_path)
        print("[dry run — no files written]", file=sys.stderr)
        return 0

    if args.output_dir is not None:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"error: cannot create {args.output_dir}: {e}",
                  file=sys.stderr)
            return 1

    for i, part in enumerate(parts, 1):
        name = f"{stem}_part{i}of{num_parts}{suffix}"
        out_path = (args.output_dir / name if args.output_dir
                    else args.conversation.with_name(name))
        try:
            # newline="" writes exactly the characters we hand it,
            # no platform translation — this is what makes the
            # handles-off "byte-for-byte concatenation" guarantee
            # actually hold on every platform.
            with out_path.open(
                mode="w", encoding="utf-8", newline=""
            ) as f:
                f.write(part)
        except OSError as e:
            print(f"error: cannot write {out_path}: {e}",
                  file=sys.stderr)
            return 1
        _report(part, out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
