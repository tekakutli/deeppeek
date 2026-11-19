#!/usr/bin/env python3
"""edit_prompts.py — prompt building + response parsing.

Sibling of `extract_edit_requests.py`.  Provides:

  · classifier prompt (build/parse) for the ambiguity path
  · implement-edits shared context + per-target prompt, where the
    context is built ONCE and reused for every target; only the
    trailing "give me <this-file>" line varies per call
  · `_build_purged_conversation`     — full-file blocks stripped,
                                       edit fragments inlined
  · `_build_current_files_section`   — current content of every
                                       resolved target
  · `_build_implement_context`       — combines the two; ONE string,
                                       same for all implement calls

No dependencies on the rest of the pipeline; importable standalone.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# ---- classifier prompt -------------------------------------------

_PROMPT_TEMPLATE = """\
You are a code-archaeologist.  Given a block of code extracted from a
chat log, decide whether the block is an EDIT REQUEST — a code block
the surrounding conversation is asking to APPLY TO AN EXISTING FILE
— or something else (a brand-new file, an isolated snippet for
reference, a throwaway illustration).  If it is an edit request,
identify the target file being edited.

An EDIT REQUEST typically:
  · Has surrounding context saying "add this to X.py", "modify the
    Y function", "insert this in Z", "update the ... method",
    "here's the patch", "here's the fix", "apply these changes",
    "the relevant change is", "the affected code is"
  · Contains diff markers: `<<<<<<<` / `=======` / `>>>>>>>`,
    `--- a/path` / `+++ b/path`, `@@ -N,M +N,M @@`
  · Starts mid-definition (indented) or has elision markers
    ("# ... existing code", "...", "rest of function")
  · Is a single function / class / statement with no module-level
    setup
  · Explicitly names a target file in the context

NOT an EDIT REQUEST typically:
  · Is a complete new file: has a module docstring at column 0,
    imports, complete top-level structure
  · Context says "create a new file", "save as foo.py", "here's the
    complete implementation", "## 1/3 — `new/path/file.py`"
  · Is an illustrative snippet ("here's an example of how X works")
    with no target-file signal

The heuristic analysis below has been run against this block and its
context.  Treat those signals as STRONG evidence — they are derived
from `ast` parsing, diff-marker detection, and header detection, not
guesses.  If they say "edit" with high confidence, you should agree
unless you see a clear reason not to.  If they say "not an edit" with
high confidence, you should agree unless you see a clear reason not
to.

Language: {lang}
Original filename: {orig_filename}

Heuristic pre-analysis:
{heuristic_hints}

Block content:
<block>
{content}
</block>

Surrounding context (block appears where you see {handle}):
<context>
{context}
</context>

Respond with STRICT JSON only (no prose, no markdown fences):
{{
  "is_edit_request": <true|false>,
  "target_file": <"path/to/file.ext" or null>,
  "edit_kind": <"patch"|"fragment"|"replacement"|"insertion"|null>,
  "reasoning": "<one sentence>",
  "agrees_with_heuristic": <true|false>
}}

Target-file rules:
  · Prefer a path explicitly named in the surrounding context
  · For unified diffs, use the `+++ b/...` path (or `--- a/...`)
  · Include directory prefixes only when the context implies them
  · No leading "/" and no absolute paths
  · If the block is not an edit request, set target_file to null
"""


def _build_heuristic_hints(analysis: dict) -> str:
    lines = [f"score: {analysis['score']:+.2f} "
             f"(confidence: {analysis['confidence']})"]
    if analysis.get("edit_kind"):
        lines.append(f"edit_kind: {analysis['edit_kind']}")
    if analysis["signals"]:
        lines.append("signals:")
        for s in analysis["signals"]:
            sign = "+" if s.weight > 0 else ""
            detail = f"  ({s.detail})" if s.detail else ""
            lines.append(f"  {sign}{s.weight:.2f}  {s.name}{detail}")
    if analysis["target_candidates"]:
        lines.append("target candidates:")
        for c in analysis["target_candidates"][:5]:
            lines.append(
                f"  {c['name']}  "
                f"(source: {c['source']}, "
                f"confidence: {c['confidence']})")
    return "\n".join(lines)


def _build_prompt(block: dict, context: str, analysis: dict) -> str:
    return _PROMPT_TEMPLATE.format(
        lang=block.get("lang") or "(unknown)",
        orig_filename=block.get("filename") or "?",
        heuristic_hints=_build_heuristic_hints(analysis),
        content=block.get("content") or "",
        handle=block.get("handle") or "?",
        context=context,
    )


def _parse_json_response(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            inner = s[nl + 1:]
            if inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3].rstrip()
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
    start = s.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


# ---- implement-edits: shared context ----------------------------

def _read_block_content(raw_dir: Path, rec: dict) -> str:
    name = rec.get("block_filename") or ""
    if not name:
        return ""
    try:
        return (raw_dir / name).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _build_purged_conversation(
    stripped_text: str,
    handle_by_index: dict[int, str],
    records: list[dict],
    raw_dir: Path,
) -> str:
    """Build the shared purged conversation.

    Every line that contains a block handle is rewritten:
      · classification == "full_file_skip"  →  "(full-file block NNN
        omitted — not an edit)".
      · is_edit_request  →  the block content, inlined as a fenced
        block and prefixed by an HTML comment naming the block, its
        edit_kind, and its target file.
      · otherwise  →  the placeholder line is kept verbatim.

    The result is target-independent: it contains every edit fragment
    in place, so a single context string can serve every implement
    call.
    """
    rec_by_handle: dict[str, dict] = {}
    for rec in records:
        h = handle_by_index.get(rec["index"], "")
        if h:
            rec_by_handle[h] = rec

    out: list[str] = []
    for line in stripped_text.splitlines():
        hit: Optional[dict] = None
        for handle, rec in rec_by_handle.items():
            if handle in line:
                hit = rec
                break
        if hit is None:
            out.append(line)
            continue

        cls = hit.get("classification")
        if cls == "full_file_skip":
            out.append(
                f"(full-file block {hit['index']:03d} omitted — "
                f"not an edit)")
            continue

        if hit.get("is_edit_request"):
            content = _read_block_content(raw_dir, hit)
            lang = hit.get("lang") or ""
            out.append(
                f"<!-- edit fragment for "
                f"{hit.get('target_file') or '?'} "
                f"(block {hit['index']:03d}, "
                f"kind: {hit.get('edit_kind') or '?'}) -->")
            out.append("```" + lang)
            out.append(content.rstrip("\n"))
            out.append("```")
            continue

        out.append(line)
    return "\n".join(out)


def _build_current_files_section(
    retrieved_by_target: dict[str, dict],
) -> str:
    """Current content of every uniquely-resolved target file.

    Same for every implement call — the LLM sees the full working set
    once and is asked (per call) to edit exactly one of them.
    """
    parts: list[str] = []
    for target, res in retrieved_by_target.items():
        if res.get("status") != "resolved":
            continue
        content = res.get("content") or ""
        lang = Path(target).suffix.lstrip(".") or ""
        parts.append(f"### {target}")
        parts.append("```" + lang)
        parts.append(content.rstrip("\n"))
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def _build_implement_context(
    stripped_text: str,
    handle_by_index: dict[int, str],
    records: list[dict],
    raw_dir: Path,
    retrieved_by_target: dict[str, dict],
) -> str:
    """Build the SHARED context used for every implement-edits call.

    Returned string is byte-for-byte identical for every target — only
    the surrounding prompt template changes between calls.
    """
    conv = _build_purged_conversation(
        stripped_text, handle_by_index, records, raw_dir)
    files = _build_current_files_section(retrieved_by_target)
    return (
        "<conversation>\n"
        f"{conv}\n"
        "</conversation>\n\n"
        "<current-files>\n"
        f"{files}\n"
        "</current-files>\n"
    )


# ---- implement-edits: per-target prompt -------------------------

_IMPLEMENT_TEMPLATE = """\
You are a precise code editor.  Below is a conversation in which
edits to some files were requested (full-file blocks have been
omitted; edit fragments are shown inline with the target file they
belong to), followed by the current content of every file that is
to be edited.

Implement ALL the edits that were requested for {target}.  Do NOT
invent edits that were not asked for.  Preserve the rest of the
file verbatim.

{context}

Give me {target}, with the implemented edits, printed fully, no
ellisions.
"""


def _build_implement_prompt(target: str, context: str) -> str:
    """Per-target prompt.  Only `{target}` varies between calls."""
    return _IMPLEMENT_TEMPLATE.format(target=target, context=context)


def _extract_edited_file_from_response(text: str) -> Optional[str]:
    """Return the largest fenced block from `text` (or None)."""
    if not text:
        return None
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            j = i + 1
            body: list[str] = []
            while j < len(lines) and not lines[j].lstrip().startswith(
                    "```"):
                body.append(lines[j])
                j += 1
            if body:
                blocks.append("\n".join(body))
            i = j + 1
        else:
            i += 1
    if not blocks:
        return None
    return max(blocks, key=len)
