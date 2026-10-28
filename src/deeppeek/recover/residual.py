"""
recover.residual — post-apply residual emission.

After the pipeline has run to completion — all patches attempted,
all pairs applied, all reconciliation decisions made, all files
either written to the target tree or fail-closed to
`low_trust/` — emit a smaller markdown document containing only
the blocks that did *not* reach a successful placement.

The residual is intended as a second, smaller `conversation.md`:
same prose, same fences, same `## File:` headers, but stripped of
every block that already landed.  The workflow it enables is:

    deeppeek recover conversation.md --apply-to target
    # edit the residual (or the target) as needed
    deeppeek recover deeppeek/residual.md --apply-to target
    # repeat until residual is empty or stable

A *stable* residual — same size, same failures, no matter how many
times the user re-runs — is the signal that the remaining blocks
need manual editing.  Every failed or excluded block carries a
`<!-- deeppeek: ... -->` comment above it explaining its outcome;
those comments survive markdown round-trip but are invisible when
the file is rendered.

HONEST-REPORTING GUARD (2026-10-28 fix)
=======================================
A `no_candidates` attribution — which fires when `existing_paths`
was empty in `pass4_attribute_orphans` — used to be in the residual
skip set.  Combined with the pipeline's pre-attribution pruning of
preexisting baselines (fixed in `pipeline.py`), a conversation
whose headers did not parse as file headers produced zero records,
zero attributions, and an **empty residual that claimed success**
while writing nothing.

The skip set no longer contains `no_candidates` (a block that could
not be attributed because there were no candidates is a failure, not
a by-design skip).  Additionally, `_build_residual` has a
top-of-function guard: if `records` is empty *and* the conversation
carried substantive code blocks (non-pre-existing, non-meta,
non-prose, non-blockquote), those blocks are all listed in the
residual with `outcome=orphan_unattributed` and a note explaining
that zero records were created.  The pipeline's real failure mode
is now visible, not silently absorbed.

WHAT GOES IN
============
  · `failed` — patch could not be applied by any heuristic.
  · `skipped_structural_damage` — patch was applied in-memory but
    refused by the structural sanity gate (see
    `apply_patches._check_structural_sanity`); the file that would
    have resulted is not what the patch's author meant.
  · `skipped_unplaceable` — patch/pair was excluded by pass 8.7
    reconciliation as unreconcilable (was failed or would corrupt).
  · `skipped_weak_attribution` — attribution was too weak to apply.
  · `applied_record_fail_closed` — patch applied in-memory but the
    record fail-closed; content lives in `low_trust/` only.
  · `orphan_unattributed` — orphan fence with no owning file.

WHAT DOES NOT GO IN
===================
  · `placed` — the block's content is in the target tree.
  · `skipped_misattributed` — the block's content already exists
    elsewhere; it's a duplicate, not a missing change.  Retargeting
    it (or re-applying it to the file it was originally attributed
    to) would *worsen* the target.
  · `already-present` — was a no-op.
  · `preexisting` — the base itself; the target already has it.
  · `recovered_meta`, `not_a_file`, `blockquote_skip`, `orphan_prose`,
    `orphan_meta`, `orphan_not_code` — thought-fragment or prose
    noise, not code changes.
  · Base content of a fail-closed record — the record's base is
    the pre-existing target content.  Only the patches/pairs that
    *tried to change* it are candidates for the residual.

FORMAT NOTES
============
The fence width is computed dynamically: if the block body contains
a three-backtick run, the fence widens to ` ```` ` (or wider) so
the residual itself round-trips through the extractor without
mangling.  The HTML comment's `--` sequences are rewritten as
`- -` so the comment stays well-formed; the same convention used
by `docs/EMISSION.md` for embedded delimiters.

The `context_before` prose is included verbatim (up to the same
1000-char cap the extractor used).  If a block required more prose
than that to be placeable, it was not placeable in the original
either; the residual inherits the extractor's fidelity, not more.
"""

import re
import sys
from pathlib import Path
from typing import Optional

from .core import Block, FileRecord


_TRUST_RANK = {"SKIP": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

# Skip-set entries that legitimately indicate "not a block we want to
# surface in the residual".  `no_candidates` was removed: a block that
# could not be attributed because `existing_paths` was empty is a
# pipeline failure, not a benign skip, and must surface.  (2026-10-28
# fix; see module docstring, HONEST-REPORTING GUARD.)
_SKIP_ORPHAN_ATTR = frozenset({
    "empty", "not_a_file",
    "blockquote_skip", "skipped_meta",
})

_SKIP_KINDS = frozenset({"recovered_meta", "not_a_file"})

_NOTE_MAX_CHARS = 500


def _trust_ok(trust: str, min_trust: str) -> bool:
    return _TRUST_RANK.get(trust, 0) >= _TRUST_RANK.get(min_trust, 2)


def _note_safe(note: Optional[str]) -> str:
    """Sanitize a note for inclusion in an HTML comment."""
    if not note:
        return ""
    s = note.replace("--", "- -")  # keep HTML comment well-formed
    s = s.replace("\n", " ").strip()
    if len(s) > _NOTE_MAX_CHARS:
        s = s[: _NOTE_MAX_CHARS - 3] + "..."
    return s


def _block_outcome(b: Block, rec: Optional[FileRecord],
                   min_trust: str) -> tuple:
    """Return `(outcome, note)` for a block's fate, or `(None, None)`
    to mean "do not include in the residual".

    Every branch below either (a) has enough information to classify
    the block as placed / skipped-by-design, or (b) returns a
    specific non-placed outcome that will surface in the residual.
    There is no "I don't know" fall-through to `None, None`; if the
    block is genuinely unclassifiable it lands in the residual with
    a `not_written` note, because a block whose outcome is unknown
    is exactly what the residual exists to surface.
    """
    # Pre-existing content is the target's own file — never residual.
    if b.classified_by == "preexisting":
        return None, None

    # Blockquote content is thought-fragment by default; only
    # strong attribution (exact `prose` + commit language) makes
    # it a residual candidate.  Mirrors the same predicate used
    # by `pass5_deduplicate`, so re-running the residual would
    # hit the same filter and produce the same skip.
    if b.in_blockquote:
        from .dedup import _has_strong_blockquote_attribution
        try:
            strong = _has_strong_blockquote_attribution(b)
        except Exception:
            strong = False
        if not strong:
            return None, None

    if b.kind in _SKIP_KINDS:
        return None, None

    if rec is None:
        if b.is_orphan and not b.attributed_to:
            if b.attributed_by in _SKIP_ORPHAN_ATTR:
                return None, None
            return "orphan_unattributed", None
        return None, None

    # PARTIAL_ONLY / MISSING records: the pipeline never found a
    # full-print block to anchor anything.  Every source block of
    # such a record failed to reach the target tree — include it.
    if rec.state in ("PARTIAL_ONLY", "MISSING"):
        return ("no_full_print",
                f"record state is {rec.state}; no full-print block "
                f"was found to anchor this content")

    fail_closed = (not _trust_ok(rec.trust, min_trust)
                   or getattr(rec, "sanity_rejected", False))
    low_name = Path(rec.path).name

    # The record's own chosen full-print block.  If the record was
    # written, its content IS the target's content (skip).  If the
    # record fail-closed, the content lives only in `low_trust/`
    # (include — this is what the user needs to edit and re-run).
    is_base = (rec.last_full_idx is not None
               and b.idx == rec.last_full_idx)
    if is_base:
        if fail_closed:
            reason = (rec.verification.get("sanity_reason")
                      or f"record trust={rec.trust}")
            return "base_not_written", reason
        return None, None

    # A pre-full partial is superseded by the base by construction;
    # the base's content covers it.  Never residual.
    if rec.last_full_idx is not None and b.idx < rec.last_full_idx:
        return None, None

    patch = next(
        (p for p in rec.patches if p.get("idx") == b.idx), None)
    pair_rec = None
    for r in rec.verification.get("pair_records", []):
        pair = r.get("pair", {})
        if b.idx in (pair.get("old_idx"), pair.get("new_idx")):
            pair_rec = r
            break

    if patch is not None:
        status = patch.get("applied_by")
        if status in ("deterministic", "llm"):
            if fail_closed:
                return ("applied_record_fail_closed",
                        f"patch applied but record fail-closed; "
                        f"content lives in low_trust/{low_name}")
            return None, None
        if status == "already-present":
            return None, None
        if status == "skipped_misattributed":
            return None, None
        if status == "skipped_unplaceable":
            return ("skipped_unplaceable",
                    patch.get("failure_reason")
                    or "reconciliation excluded this patch")
        if status == "skipped_structural_damage":
            # NEW (2026-10-28e): structural sanity gate refused the
            # patch; the record fail-closed.  Report it as its own
            # outcome so the residual names the specific cause
            # (e.g. "patch injected module-level statement(s) at
            # column 0: ['try']") rather than a generic not_written.
            return ("skipped_structural_damage",
                    patch.get("failure_reason")
                    or "structural sanity gate refused the patch")
        if status == "failed":
            return "failed", patch.get("failure_reason")
        if status == "skipped_weak_attribution":
            return ("skipped_weak_attribution",
                    "body_header attribution was too weak to place")
        return "not_written", None

    if pair_rec is not None:
        status = pair_rec.get("applied_by")
        if status == "deterministic":
            if fail_closed:
                return ("applied_record_fail_closed",
                        f"pair applied but record fail-closed; "
                        f"content lives in low_trust/{low_name}")
            return None, None
        if status == "already-present":
            return None, None
        if status == "skipped_misattributed":
            return None, None
        if status == "skipped_unplaceable":
            return ("skipped_unplaceable",
                    pair_rec.get("reason")
                    or "reconciliation excluded this pair")
        if status == "failed":
            return "failed", pair_rec.get("reason")
        return "not_written", None

    # Reached only when a block claims a record's source but has no
    # per-block status.  This can happen in a few edge cases (a
    # source block attributed after pass 5, a record whose state
    # was mutated by a late pass, etc.).  Include it with a
    # generic reason so the failure surfaces rather than silently
    # disappearing.
    return ("not_written",
            f"source block of record state={rec.state}; no "
            f"per-block outcome recorded")


def _safe_fence(body: str) -> str:
    """Return a fence wide enough to enclose `body`.

    Standard markdown: 3 backticks unless the body itself contains
    a longer run.  Dynamic width is what keeps the residual
    round-trippable through `pass1_extract` — a body containing a
    raw three-backtick fence would otherwise close the enclosing
    fence early and fragment the residual on the next run.
    """
    max_run = 0
    for m in re.finditer(r"`+", body):
        if len(m.group(0)) > max_run:
            max_run = len(m.group(0))
    width = max(3, max_run + 1)
    return "`" * width


def _format_block(b: Block, outcome: str, note: Optional[str]) -> str:
    """Render one block in conversation.md format with a
    `<!-- deeppeek: ... -->` metadata comment above it.
    """
    lines = []

    meta_parts = [f"idx={b.idx}", f"outcome={outcome}"]
    if note:
        meta_parts.append(f"note={_note_safe(note)!r}")
    if b.line_start is not None and b.line_start >= 0:
        meta_parts.append(f"line={b.line_start}..{b.line_end}")
    if b.in_blockquote:
        meta_parts.append("from_thinking=true")
    lines.append(f"<!-- deeppeek: {' '.join(meta_parts)} -->")
    lines.append("")

    if b.context_before:
        lines.append(b.context_before.rstrip())
        lines.append("")

    header_path = b.header_path
    if header_path is None and b.attributed_to:
        header_path = b.attributed_to
    if header_path:
        lines.append(f"## File: `{header_path}`")
        lines.append("")

    lang = (b.fence_lang or "").strip()
    fence = _safe_fence(b.body)
    opener = fence + (lang if lang else "")
    lines.append(opener)
    lines.append(b.body.rstrip())
    lines.append(fence)
    lines.append("")

    return "\n".join(lines)


def _is_substantive_code_block(b: Block) -> bool:
    """True iff `b` is a non-noise, non-preexisting code block.

    Used by the honest-reporting guard: on a zero-record run, only
    blocks that a user would actually want to see in a residual get
    listed.  Prose/UI blocks, blockquoted thought fragments, and
    meta dumps are excluded — they were never code the pipeline
    could have applied.
    """
    if b.classified_by == "preexisting":
        return False
    if b.kind in _SKIP_KINDS:
        return False
    if b.in_blockquote:
        return False
    if not b.body.strip():
        return False
    return True


def _build_residual(records: dict,
                    blocks: list,
                    dump_display: str,
                    min_trust: str) -> tuple:
    """Return `(content, entry_count)` for the residual markdown."""
    entries = []
    for b in blocks:
        # Prefer the block's declared output path; fall back to the
        # orphan attribution target.
        rec = None
        if b.output_path and b.output_path in records:
            rec = records[b.output_path]
        elif b.attributed_to and b.attributed_to in records:
            rec = records[b.attributed_to]

        outcome, note = _block_outcome(b, rec, min_trust)
        if outcome is None:
            continue
        entries.append((b, outcome, note))

    # HONEST-REPORTING GUARD (2026-10-28): if the pipeline produced
    # zero records AND there are substantive code blocks in the
    # conversation, report every such block honestly.  This catches
    # the pipeline-failure case (pre-existing baselines pruned before
    # attribution, leaving `existing_paths = []`) which used to
    # produce an empty residual claiming "all placed" — the failure
    # was completely invisible.
    if not entries and not records:
        for b in blocks:
            if not _is_substantive_code_block(b):
                continue
            entries.append((
                b, "orphan_unattributed",
                "no records were created; this block's content was "
                "not recovered and no pre-existing file was available "
                "for attribution",
            ))

    header = [
        "# Residual from deeppeek recovery",
        "",
        f"# Source: {dump_display}",
        f"# {len(entries)} block(s) did not reach a successful "
        f"placement against the target tree.",
        "",
        "# Blocks whose content already exists in the target tree",
        "# (skipped_misattributed) are omitted — their payload is",
        "# redundant and would duplicate or overwrite correct content.",
        "# Every included block carries a `<!-- deeppeek: ... -->`",
        "# comment explaining its outcome.",
        "",
        "# Re-running deeppeek on this file attempts only these blocks.",
        "# A residual of the same size across re-runs means the remaining",
        "# failures require manual editing.",
        "",
    ]

    if not entries:
        return "\n".join(header + ["*All blocks reached a successful placement.*", ""]), 0

    body = []
    for b, outcome, note in entries:
        body.append(_format_block(b, outcome, note))

    return "\n".join(header) + "\n" + "\n".join(body), len(entries)


def emit_residual(records: dict,
                  blocks: list,
                  dump_display: str,
                  min_trust: str,
                  out_path: Optional[Path] = None,
                  verbose: bool = False) -> str:
    """Emit the residual markdown to `out_path` (or stdout if
    `out_path` resolves to `-`), and return its content as a string.

    Called from `pipeline.run` after `pass9_write` completes; runs
    unconditionally so the artifact always exists for inspection.
    Never raises into the caller — any error is caught by the
    pipeline's per-pass boundary.
    """
    content, n_entries = _build_residual(
        records, blocks, dump_display, min_trust)

    if out_path is None:
        return content

    if str(out_path) == "-":
        sys.stdout.write(content)
        sys.stdout.flush()
        return content

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    if verbose:
        print(f"  [residual] wrote {out_path} "
              f"({n_entries} block(s) unplaced)", file=sys.stderr)
    return content
# ===========================================================================
#  Reading: parse an existing residual.md
# ===========================================================================
#
# The inverse of `_format_block`.  Two consumers:
#
#   · `deeppeek residual`        — list files in a residual.md
#   · `deeppeek prompt reprint`  — build the file list the AI is
#                                  asked to reprint
#
# Markers are the exact ones `_format_block` emits:
#
#   <!-- deeppeek: idx=N outcome=X [note='...'] [line=A..B] -->
#   [context_before prose]
#   ## File: `path`
#   [fenced body]

_BLOCK_MARKER_RE = re.compile(r'^<!-- deeppeek:\s', re.MULTILINE)
_FENCE_OPENER_RE = re.compile(r'^(`{3,}|~{3,})', re.MULTILINE)
_FILE_HEADER_RE = re.compile(r'^## File:\s*`([^`\n]+)`\s*$', re.MULTILINE)
_OUTCOME_RE = re.compile(r'\boutcome=(\w+)')


def parse_residual_entries(text: str) -> list:
    """Parse a residual.md back into per-block entries.

    Returns a list of `(path_or_None, outcome_or_None)` tuples, in
    block order.  `path` is None for blocks with no `## File:` header
    (unattributed orphans); `outcome` is None when the marker comment
    carries no `outcome=` field.

    The `## File:` header is only looked for *before* the first fence
    opener in each block — a `## File:` line inside the recovered
    file's own body is content, not a residual header.  This matters
    for residuals of markdown files, where the body can itself
    contain headers.
    """
    positions = [m.start() for m in _BLOCK_MARKER_RE.finditer(text)]
    if not positions:
        return []
    positions.append(len(text))

    entries = []
    for i in range(len(positions) - 1):
        chunk = text[positions[i]:positions[i + 1]]
        marker_line = chunk.split("\n", 1)[0]
        outcome_match = _OUTCOME_RE.search(marker_line)
        outcome = outcome_match.group(1) if outcome_match else None

        fence_match = _FENCE_OPENER_RE.search(chunk)
        head = chunk[:fence_match.start()] if fence_match else chunk
        file_match = _FILE_HEADER_RE.search(head)
        path = file_match.group(1) if file_match else None

        entries.append((path, outcome))
    return entries


def unique_residual_paths(text: str) -> list:
    """Deduplicated, sorted list of file paths referenced by the
    residual.  Unattributed blocks (no `## File:`) are ignored.
    """
    seen = set()
    for p, _ in parse_residual_entries(text):
        if p is not None:
            seen.add(p)
    return sorted(seen)
