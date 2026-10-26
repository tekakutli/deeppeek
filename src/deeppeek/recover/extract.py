"""
recover.extract — Pass 1 (markdown AST extraction) and Pass 3 (group
by resolved output path).

STABLE.  The markdown-it based extractor has been correct since the
first revision.  The grouping pass now keys records by the resolved
`output_path` (set by `resolve.resolve_paths` between classify and
group), which is what makes `--apply-to` write to the correct
subdirectories and what makes conversation blocks merge with their
matching pre-existing baselines.
"""

import re
from typing import Optional

from markdown_it import MarkdownIt

from .core import Block, FileRecord, SOURCE_EXTENSIONS, _del_marker_names


def _walk_block_tokens(tokens):
    for tok in tokens:
        yield tok
        if tok.children and tok.type != "inline":
            yield from _walk_block_tokens(tok.children)


def _parse_file_header(text: str) -> Optional[tuple[str, Optional[str]]]:
    s = text.strip()
    # Markdown link: [text](url) -> text.  The link text may
    # itself carry backticks, which the strip below removes.
    link_m = re.match(r"^\[([^\]]+)\]\([^)]+\)\s*$", s)
    if link_m:
        s = link_m.group(1).strip()
    s = re.sub(r"[*`]", "", s)

    m = re.match(
        r"^(?:file|path|source)\s*:\s*"
        r"([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]+)"
        r"(?:\s*(?:\(([^)]+)\)|—(.+?)|-(.+?)))?\s*$",
        s, re.IGNORECASE,
    )
    if m:
        mod = m.group(2) or m.group(3) or m.group(4)
        return m.group(1), (mod.strip() if mod else None)

    m = re.match(r"^==+\s*(\S+)\s*==+$", s)
    if m:
        return m.group(1), None

    m = re.match(r"^\[([^\]]+)\]$", s)
    if m and "." in m.group(1):
        return m.group(1), None

    m = re.match(
        r"^(?:(?:\d+)\s*[.):\-]\s*)?"
        r"([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]{1,6})"
        r"\s*(?:(?:—|–|\s-\s|\(|\[)\s*"
        r"(.+?)\s*(?:\)|\])?)?\s*$",
        s,
    )
    if m:
        path = m.group(1)
        ext = "." + path.split(".")[-1].lower()
        if ext in SOURCE_EXTENSIONS:
            mod = m.group(2)
            return path, (mod.strip() if mod else None)

    m = re.match(r"^([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]{1,5})$", s)
    if m:
        ext = "." + m.group(1).split(".")[-1].lower()
        if ext in SOURCE_EXTENSIONS or "/" in m.group(1):
            return m.group(1), None

    return None


import re as _re


_DUMP_WRAPPER_RE = _re.compile(
    r"\[file name\]:\s*(\S+)\s*\n"
    r"(?:.*?\n)*?"
    r"\[file content begin\]\s*\n"
    r"(.*?)"
    r"\n\[file content end\]",
    _re.DOTALL,
)


def _find_dump_wrapper_spans(text):
    """Return [(start_char, end_char)] spans of dump wrappers."""
    spans = []
    for m in _DUMP_WRAPPER_RE.finditer(text):
        filename = m.group(1) or ""
        body = m.group(2) or ""
        looks_dump = (
            "dump" in filename.lower()
            or "SCANNED DIRECTORY" in body
        )
        if not looks_dump:
            continue
        spans.append((m.start(), m.end()))
    return spans


def _char_spans_to_line_spans(text, char_spans):
    line_spans = []
    for start, end in char_spans:
        start_line = text.count("\n", 0, start)
        end_line = text.count("\n", 0, end)
        line_spans.append((start_line, end_line))
    return line_spans


def _block_in_any_span(block_line_start, block_line_end, line_spans):
    for span_start, span_end in line_spans:
        if (block_line_start >= span_start
                and block_line_start <= span_end):
            return True
    return False


def pass1_extract(dump_text: str) -> tuple[list[Block], list[str]]:
    md = MarkdownIt()
    tokens = md.parse(dump_text)

    blocks: list[Block] = []
    current_header: Optional[tuple[str, Optional[str]]] = None
    current_context_lines: list[str] = []

    def _make_block(header, lang, body, line_start, line_end, ctx, bq):
        return Block(
            idx=len(blocks),
            header_path=header[0] if header else None,
            modifier=header[1] if header else None,
            fence_lang=lang,
            body=body,
            body_line_count=body.count("\n") + (1 if body else 0),
            context_before=ctx[:1000],
            line_start=line_start,
            line_end=line_end,
            is_orphan=header is None,
            in_blockquote=bq,
        )

    all_tokens = list(_walk_block_tokens(tokens))
    blockquote_depth = 0
    i = 0
    while i < len(all_tokens):
        tok = all_tokens[i]

        # Track blockquote nesting.  The token stream is flat:
        # blockquote_open precedes the blockquoted content's
        # tokens (including fences), blockquote_close follows
        # them.  Fences at depth > 0 are parts of the AI's
        # reasoning (deeppeek's formatter wraps thinking in
        # `> ` quotes).
        if tok.type == "blockquote_open":
            blockquote_depth += 1
            i += 1
            continue
        if tok.type == "blockquote_close":
            blockquote_depth = max(0, blockquote_depth - 1)
            i += 1
            continue

        if tok.type == "heading_open":
            if (i + 1 < len(all_tokens)
                    and all_tokens[i + 1].type == "inline"):
                raw = all_tokens[i + 1].content
                parsed = _parse_file_header(raw)
                if parsed is not None:
                    current_header = parsed
                    current_context_lines = []
                else:
                    current_context_lines.append(raw)
                    if len(current_context_lines) > 40:
                        current_context_lines = current_context_lines[-40:]
            i += 3
            continue

        if tok.type == "inline":
            current_context_lines.append(tok.content)
            if len(current_context_lines) > 40:
                current_context_lines = current_context_lines[-40:]
            i += 1
            continue

        if tok.type == "fence":
            info = (tok.info or "").strip()
            lang = info.split()[0] if info else ""
            m = tok.map or [0, 0]
            body = tok.content
            if body.endswith("\n"):
                body = body[:-1]
            # Fix D: 15-line context so `FILE i/N:` markers
            # written by dump.py output (which are three lines
            # above the fence) are visible to RECOVERED_META_RE.
            # The extra lines are cheap; the false-negative they
            # prevent is a silent corruption.
            ctx = "\n".join(current_context_lines[-15:])
            blocks.append(_make_block(
                current_header, lang, body, m[0], m[1], ctx,
                bq=(blockquote_depth > 0),
            ))
            current_context_lines = []
            i += 1
            continue

        i += 1

    # Fix C: dump-wrapper spans are marked as recovered_meta.
    # Their inner blocks were already extracted by the markdown
    # parser as ordinary fences; we are re-tagging, not
    # re-parsing.
    wrapper_char_spans = _find_dump_wrapper_spans(dump_text)
    wrapper_line_spans = _char_spans_to_line_spans(
        dump_text, wrapper_char_spans)
    if wrapper_line_spans:
        for b in blocks:
            if _block_in_any_span(b.line_start, b.line_end,
                                   wrapper_line_spans):
                b.kind = "recovered_meta"
                b.is_code_file = False
                b.override_reason = "dump wrapper"

    deleted = _del_marker_names(dump_text)
    return blocks, sorted(deleted)


def pass3_group(blocks: list[Block],
                deleted_files: list[str]) -> dict[str, FileRecord]:
    """Group blocks into FileRecords keyed by `output_path`.

    `output_path` is set by `resolve.resolve_paths` for every non-
    orphan block.  Preexisting baselines carry their real path there;
    conversation blocks carry their resolved path (which either
    matches a pre-existing file — in which case they merge into that
    file's record — or falls back to the literal header_path).

    Orphans are skipped here; pass 4 attributes them to a record, and
    the assignment (`Block.attributed_to`) uses the record's
    `output_path` as the key, so patch content lands on the right
    file without a separate path-resolution step.
    """
    by_path: dict[str, list[Block]] = {}
    for b in blocks:
        if b.is_orphan:
            continue
        if not b.is_code_file:
            continue
        if b.kind in ("not_a_file", "recovered_meta"):
            continue
        key = b.output_path or b.canonical_path
        if not key:
            continue
        by_path.setdefault(key, []).append(b)

    records: dict[str, FileRecord] = {}
    deleted_set = set(deleted_files)

    for path, file_blocks in by_path.items():
        file_blocks.sort(key=lambda x: x.idx)
        rec = FileRecord(path=path, state="MISSING")
        rec.source_blocks = [b.idx for b in file_blocks]
        rec.is_new = any(
            b.modifier and "new" in b.modifier.lower() for b in file_blocks
        )
        rec.is_deleted = path in deleted_set
        records[path] = rec

    return records
