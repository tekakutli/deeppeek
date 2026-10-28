"""
recover.extract — Pass 1 (markdown AST extraction) and Pass 3 (group
by resolved output path).

STABLE.  The markdown-it based extractor has been correct since the
first revision.  The grouping pass now keys records by the resolved
`output_path` (set by `resolve.resolve_paths` between classify and
group), which is what makes `--apply-to` write to the correct
subdirectories and what makes conversation blocks merge with their
matching pre-existing baselines.

HEADING-AS-SECTION-BOUNDARY (2026-10-28 fix)
============================================
An H1 or H2 that does NOT parse as a file header resets
`current_header` to `None`.  Without this, a header like

    ## Fix 4 — rotation doesn't shrink (`pg_state.py`)

sets `current_header = pg_state.py`, and the *next* H2

    ## Fix 5 — text input for rotation

(which names no file) leaves `current_header` at `pg_state.py`, so
every fence under Fix 5 inherits a header that targets a file it
doesn't belong to.  Fix 5's three fences actually target
`pg_base.py` / `pg_panel_scrubs.py` / `pg_panel_bindings.py`; only
their prose names the target.  The inheritance caused them to be
attributed as headed partials against `pg_state.py` and driven to
`PARTIAL_ONLY`.

H3+ subsections do NOT reset — `## File: foo.js` followed by
`### (a) Add a field` should keep the file header (fixture 02
relies on this).

H2 SCOPE PROPAGATION (2026-10-29)
=================================
Pass 1 also extracts a *scope hint* from non-file H1/H2 headings —
"in `handle_inpaint`", "find the `comfy_generate` command", etc. —
and attaches it to every subsequent fence in that section as
`Block.scope_hint`.  Attribution (pass 4) falls back to this hint
when the fence's own prose does not name a scope.

Motivating case: block idx 634 in the 2026-10-28 run.  Its prose
was "And in every failure return:" — no scope named.  The enclosing
H2 was "## Fix 3 — surface per-command errors in `handle_inpaint`".
Before this fix, the hint was lost: the block orphaned, went to the
residual as `orphan_unattributed`, and the `errors` return never
landed in `inpaint_server.py`.

The hint is a *fallback*, not an override: a fence whose own prose
names a different scope uses the prose's scope.  Only when the
fence's prose is silent does the section's scope hint apply.
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


# NEW: single-filename fallback regex.  Matches a bounded source-file
# path anywhere in the header text.  Bounded by `\b` so it does not
# eat the trailing `)` of `(utils/foo.py)`, and by the extension list
# so it does not fire on `.md` references in prose (`.md` is in the
# list but `## Fix 1 — …` phrasing without a filename produces zero
# matches, which the fallback correctly refuses).
_SINGLE_FILENAME_RE = re.compile(
    r'\b([A-Za-z0-9_./\-]+\.'
    r'(?:py|js|mjs|cjs|jsx|ts|tsx|md|json|yaml|yml|toml|sh|bash|'
    r'html|css|scss|rs|go|rb|php|java|kt|c|cc|cpp|h|hpp))\b',
)


# NEW (2026-10-29): scope hint from section headings.
#
# Matches the two most common shapes the assistant uses to introduce
# a code block whose target is a function/class, when that
# introduction happens in a section heading rather than in the
# fence's own prose:
#
#   "## Fix 3 — surface per-command errors in `handle_inpaint`"
#   "## Rewrite of `runInpaint`"
#   "### (a) Add a field to `MyHandler`"
#
# Only H1/H2 headings are scanned (H3+ subsections are too narrow to
# carry a section-wide hint).  A heading that names no scope returns
# "" and does not override an earlier hint in the enclosing section.
_SCOPE_IN_HEADING_RE = re.compile(
    r'\b(?:in|inside|within|to)\s+`([A-Za-z_][\w$]*)`'
    r'|\bfind\s+the\s+`([A-Za-z_][\w$]*)`'
    r'|\bof\s+`([A-Za-z_][\w$]*)`',
    re.IGNORECASE,
)


def _extract_scope_hint(heading_text: str) -> str:
    """Return a scope name mentioned in a section heading, or "".

    The three alternatives in `_SCOPE_IN_HEADING_RE` cover:

      · "in `X`", "inside `X`", "within `X`", "to `X`" — the
        standard "this section is about X" form.
      · "find the `X`" — the imperative form.
      · "of `X`" — the descriptive form ("Rewrite of `runInpaint`").

    First match wins; a heading that names multiple scopes returns
    the first.  Returns "" when no scope is named.
    """
    m = _SCOPE_IN_HEADING_RE.search(heading_text or "")
    if not m:
        return ""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


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

    # NEW: natural-language header fallback.  A heading that contains
    # exactly one source-extension filename anywhere in its text is a
    # file header.  Catches the assistant's natural shapes:
    #     "## Fix 1 — mask encoding (`utils/comfyui_krita.py`)"
    #     "## Rewrite of `board.py`"
    #     "**pg_state.py.** fittedImageRect and clampPan:"
    # Zero matches → not a file header (correctly falls through to
    # prose-only parsing, preserving `## File: ` and `== file ==`
    # conventions).  Two or more matches → not a file header either
    # (ambiguous; the prose or the block body should carry the
    # attribution signal instead).
    matches = _SINGLE_FILENAME_RE.findall(s)
    if len(matches) == 1:
        return matches[0], None

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
    # NEW (2026-10-29): scope hint from the enclosing H1/H2 heading.
    # Reset only when a new non-file H1/H2 appears; H3+ subsections
    # inherit the enclosing H2's hint.
    current_scope: str = ""
    current_context_lines: list[str] = []

    def _make_block(header, lang, body, line_start, line_end, ctx, bq,
                    scope_hint=""):
        b = Block(
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
        # Attach the section heading's scope hint only to orphan
        # blocks.  Headed blocks carry their own attribution signal
        # (the header_path) — the scope hint is a fallback for blocks
        # whose only attribution route is prose.
        if scope_hint and not header:
            b.scope_hint = scope_hint
            b.scope_name = scope_hint
        return b

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
                    # NEW: an H1 or H2 that does NOT name a file is a
                    # section boundary.  Without resetting, the next
                    # fence inherits the *previous* H2's file header.
                    #
                    # Concretely: the natural-language header shape
                    #   "## Fix 4 — rotation doesn't shrink (`pg_state.py`)"
                    # sets current_header=pg_state.py; a later
                    #   "## Fix 5 — text input for rotation"
                    # (which names no file) then leaves current_header
                    # at pg_state.py, so every fence under Fix 5 gets
                    # the wrong header.  Fix 5's three fences actually
                    # target pg_base.py / pg_panel_scrubs.py /
                    # pg_panel_bindings.py, and their correct target
                    # is expressed only in their prose.
                    #
                    # Reset on H1/H2 only.  H3+ are subsections of the
                    # current file section (e.g. "### (a) Add a field"
                    # under "## File: foo.js") and must preserve the
                    # header.  Existing fixtures rely on this — see
                    # tests/recover/fixtures/02_subsection_patches.md.
                    if tok.tag in ("h1", "h2"):
                        current_header = None
                        # NEW: pick up a scope hint from this
                        # section heading, e.g.
                        # "## Fix 3 — surface errors in `handle_inpaint`"
                        # yields "handle_inpaint", which later
                        # orphans in this section fall back on.
                        _hint = _extract_scope_hint(raw)
                        if _hint:
                            current_scope = _hint
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
                scope_hint=current_scope,
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
