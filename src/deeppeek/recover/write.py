"""
recover.write — Pass 9 (write files, manifest, warnings).

New in 2026-11-01: `sanity_rejected` branch.  A file marked with
`sanity_rejected = True` by `apply_patches` is treated exactly like
a `LOW`-trust file: not written to the target tree, reported in
`warnings.txt`, and preserved as-is.

New in 2026-11-01 (b): trust reason names the specific check
(`patches_incomplete`, `pairs_incomplete`, `ast_parse_failed`).
Unattributed orphans report their first content line.

New in 2026-11-01 (c): `patch_failure_reasons` in `decisions[path]`.
Each failed patch records its `idx`, `first_line`, `context_before`,
and `reason`.  `warnings.txt` gets a `[PATCH_FAILED]` line per failed
patch.  The final summary prints one PATCH_FAILED / PAIR_FAILED line
per failure so the user sees the diagnostics in the console without
opening files.

New in 2026-11-01 (d): `patch_failure_reasons` also records
`context_before` (truncated to 800 chars).  When a heuristic refuses
a patch because the prose didn't match — e.g. an "insert between the
Image and Masks `<details>` blocks" phrase that didn't parse — the
manifest carries the actual prose the heuristic saw, so the failure
can be diagnosed from the manifest alone rather than by re-reading
the conversation dump.

New in 2026-11-01 (e): `_compute_block_disposition` now respects
trust.  A block in a rejected-record previously showed as "applied"
because the record's *state* was FULL_PLUS_PATCH; that misreads the
outcome when trust was LOW and `written == False`.  The disposition
now falls back to "block_unapplied" / "block_failed" / "block_partial"
when the record was not actually written.

New in 2026-11-01 (f): reconciliation feedback.  Two additions:

  · A `reconciled_to_base` record — one where pass 8.7 excluded every
    applied item and the record's content equals the pre-existing
    base — is skipped silently, with the exclusions recorded in
    `warnings.txt` and the `reconciled` field on the manifest
    decision.  This is the correct outcome for a record whose only
    changes were misattributed: the target file's pre-existing
    content is preserved, nothing is overwritten, and the audit
    trail still says which block was refused and why.

  · `preexisting_unmodified` now treats `skipped_misattributed` and
    `skipped_unplaceable` as no-op statuses (alongside
    `already-present`).  A pre-existing baseline whose only "patches"
    were excluded by reconciliation has *not* been modified — the
    skip is correct and the warning is legible.

New in 2026-11-01 (g): the final summary reports reconciliation
counts alongside the fail-closed count, so the operator sees
"reconciled: N records, M items excluded" when pass 8.7 rescued a
run that would otherwise have been fail-closed end to end.

New in 2026-10-28e: centralised placement-failure reporting.
`patch_failures` and `patch_failure_reasons` now use the shared
`_PLACEMENT_FAILURES` set from `core.py`, so
`skipped_structural_damage` — a patch that the structural sanity
gate refused and which therefore never landed — is reported alongside
`failed` and `skipped_unplaceable` instead of silently dropping out
of both lists.  The FAIL-CLOSED summary loop uses the same set.

PRE-EXISTING SKIP AGGREGATION (2026-11-05)
==========================================
The `preexisting_unmodified` branch no longer emits one
`[PREEXISTING]` line per pre-existing file.  A single aggregate
`[preexisting] N file(s) left unchanged (not part of this run)`
line is written near the end of `warnings.txt`.

Motivating run (2026-10-29): a mid-size `--apply-to` target tree
had ~280 untouched pre-existing source files.  Before this change,
`warnings.txt` was ~500 lines, of which ~280 were
`[path] [PREEXISTING] pre-existing file with no conversation
changes; left as-is` — pure noise, differing only in the file path.
After: one aggregate line; per-file detail preserved in
`manifest.decisions[path]` (which already has `state`,
`preexisting_base`, `verification`, `warnings`).

The `[PRESERVED]` summary line that follows it serves a different
purpose: it counts files that were already in the *output directory*
before this run, whether or not they were part of the conversation
(this includes files whose records were never created).  The two
lines together describe the complete "untouched" surface without
per-file spam.

MISSING-FILES SCOPE (2026-11-01b)
=================================
`_scan_mentioned_files` scopes to source extensions
(`_MENTION_EXTENSIONS`) rather than all recoverable extensions
(`SOURCE_EXTENSIONS`).  Data files (`.json`, `.yaml`, `.toml`), docs
(`.md`, `.rst`), and scripts (`.sh`, `.bash`) are excluded from the
"referenced but never recovered" report: they are config or output,
not code dependencies, and their appearance in a conversation dump
(a `project_dump_*.md` that embeds every history record, for
instance) fills `MISSING_FILES.txt` with noise.

Shell scripts are a borderline case — `.sh` appears in the
`SOURCE_EXTENSIONS` set because deeppeek recovers them as source
files, but they are excluded from the MISSING report because they
are almost always invoked rather than imported.  An imported-from
shell script is not a thing; if you want to recover a specific one,
name it explicitly in the conversation.

PROSE-ONLY MENTION SCAN (2026-11-05)
====================================
`_scan_mentioned_files` now scans **prose** (`Block.context_before`)
only.  The previous version accepted the raw dump text and matched
filenames anywhere in it — every `from .foo import X` in a recovered
source body, every mention in a `recovered_meta` project dump.  On
the 2026-11-05 conversation that meant 21 deeppeek-source files (the
embedded `deeppeek_dump.md` block) got reported as "referenced but
never recovered", despite being present on disk in the target tree.

The scan now:

  · skips pre-existing baselines (their bodies are the target's
    own code; a mention there is the target talking to itself);
  · skips `recovered_meta` blocks (their bodies are entire project
    dumps; a mention there is a nested recovery exercise, not this
    conversation's target);
  · reads only `context_before`, so the "human intent" signal — a
    filename in the prose of a chat message — is the only signal;
  · filters further to files that are *not* already on disk in the
    target tree.

Combined, that produces zero false-positive MISSING_FILES entries
for a conversation whose `recovered_meta` block embeds a full repo
dump, and preserves every genuinely-missing entry from a conversation
that names a file by prose and never prints it.

WARNINGS AGGREGATION (2026-11-05)
=================================
`warnings.txt` used to emit one line per record per warning class:
200 untouched records with the same "structural-equivalence check
raised: name '_python_signatures' is not defined" message produced
200 lines, all identical apart from the path.

`_aggregate_warnings` collapses identical-class warnings into:

    [<first_path>] [WARN] <text> (+199 more file(s) with this
    warning)

One readable line.  Every distinct warning text is preserved in
full, so the operator never has to search for a specific record's
warning — they read one aggregate line and, if needed, open
`manifest.decisions[path].warnings` for the full per-record text.

The helper iterates its input exactly once, accumulating grouped
entries into `warn_groups` and preserving non-warning lines verbatim
into `result` — a prior version appended non-warning lines during
classification *and* during reconstruction, doubling every
non-`[WARN]` line in the output.  The variable rename from
`preserved` to `result` makes the single-write pattern obvious.

TOOL_BUG WARNINGS TAG (2026-10-29)
==================================
When a patch carries `patch["tool_bug"]` (recorded by
`_try_deterministic_patch`'s `_TOOL_BUG_EXCEPTIONS` branch), the
warnings writer emits `[TOOL_BUG]` instead of `[PATCH_FAILED]`, and
the manifest's `patch_failure_reasons` entry gains a `tool_bug`
field.  The fail-closed summary uses `TOOL_BUG` as its tag.  This
distinguishes "body shape the heuristics couldn't place" from
"deeppeek module raised" in both the human-readable warnings and
the machine-readable manifest.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core import (
    Block, FileRecord,
    SKIP_MISSING_FILES, SOURCE_EXTENSIONS,
    _PLACEMENT_FAILURES,
    _basename,
)
from .llm import LLMClient
from .validate import _py_imports, _is_likely_local


_NO_OP_STATUSES = frozenset({
    "already-present",
    "skipped_misattributed",
    "skipped_unplaceable",
})


# "Referenced but never recovered" is scoped to source-code extensions
# only.  Data files, docs, and shell scripts are excluded from the
# MISSING report because they are almost never imported — their
# appearance in a conversation dump is incidental, not a dependency
# signal.  See module docstring, MISSING-FILES SCOPE section.
_MENTION_EXTENSIONS = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".rs", ".go", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".java", ".rb", ".php",
})


def _scan_mentioned_files(blocks) -> set:
    """Return basenames of source files named in **prose**, not code.

    Filenames in a code body are imports; `_aggregate_missing_imports`
    already accounts for those.  Filenames in *prose* are the
    human intent: "this conversation is about `foo.py` and `bar.py`".

    The previous version accepted the raw dump text and matched
    filenames inside every recovered source file's body — every
    `from .foo import X` in the recovered content produced a
    spurious "missing" entry.  On the 2026-11-05 conversation, the
    embedded `deeppeek_dump.md` alone produced 21 false positives.
    """
    mentioned: set = set()
    for b in blocks:
        if b.classified_by == "preexisting":
            continue
        if b.kind == "recovered_meta":
            continue
        prose = b.context_before or ""
        for m in re.finditer(r'[A-Za-z0-9_./\-]+', prose):
            tok = m.group(0)
            if not any(tok.endswith(ext)
                       for ext in _MENTION_EXTENSIONS):
                continue
            base = os.path.basename(tok)
            if base and len(base) > 3:
                mentioned.add(base)
    return mentioned


def _aggregate_missing_imports(records: dict) -> set:
    """Return basenames of local modules that are imported by a
    recovered file but not themselves recovered.

    Dotted paths (``from pg_comfy.image import X``) are resolved
    against the **top-level** module first, because that is the
    level at which deeppeek can decide "recovered / not recovered":

      · ``pg_comfy.py`` is recovered  → ``pg_comfy.image`` resolves
        (whether the sub is a real file, an ``__init__.py`` package,
        or an attribute) — do not report a missing file.
      · ``pg_comfy/__init__.py`` is recovered  → the package exists;
        check for the submodule file specifically.
      · Neither is recovered  → report ``pg_comfy.py`` (once), not
        ``image.py``.
    """
    recovered_files = {
        _basename(p) for p, r in records.items()
        if r.content is not None
    }
    recovered_paths = {
        p.replace("\\", "/").lstrip("./")
        for p, r in records.items()
        if r.content is not None
    }

    expected: set = set()
    for path, rec in records.items():
        if not path.endswith(".py"):
            continue
        if rec.content is None:
            continue
        for module, _name in _py_imports(rec.content):
            if not _is_likely_local(module, records):
                continue
            clean = module.lstrip(".")
            if not clean:
                continue
            parts = clean.split(".")

            top_name = parts[0]
            top_file = top_name + ".py"
            top_pkg  = top_name + "/__init__.py"

            if top_file in recovered_paths:
                continue
            if top_pkg in recovered_paths:
                if len(parts) > 1:
                    sub_file = "/".join(parts) + ".py"
                    if sub_file in recovered_paths:
                        continue
                    expected.add(os.path.basename(sub_file))
                continue
            expected.add(top_file)

    return expected - recovered_files


def _recovered_name_prefixes(records: dict) -> set:
    prefixes: set = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        base = _basename(path)
        if not base or "." not in base:
            continue
        stem = base.rsplit(".", 1)[0]
        if "_" not in stem:
            continue
        prefix = stem.split("_", 1)[0] + "_"
        if len(prefix) > 2:
            prefixes.add(prefix)
    return prefixes


def _recovered_extensions(records: dict) -> set:
    exts: set = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        ext = Path(path).suffix.lower()
        if ext:
            exts.add(ext)
    return exts


def _apply_deletions(apply_to: Path,
                     all_deleted: list,
                     warnings_lines: list) -> tuple:
    applied: list = []
    missing: list = []
    for name in all_deleted:
        base = os.path.basename(name)
        candidates = []
        if base and base != name:
            candidates.append(apply_to / base)
        candidates.append(apply_to / name)

        target = None
        for c in candidates:
            if c.exists() and c.is_file():
                target = c
                break

        if target is None and base:
            matches = [
                p for p in apply_to.rglob(base)
                if p.is_file() and p.name == base
                and "deeppeek" not in p.parts
                and ".deeppeek" not in p.parts
            ]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                rel_matches = [
                    m.relative_to(apply_to).as_posix() for m in matches
                ]
                warnings_lines.append(
                    f"[{name}] [AMBIGUOUS] {len(matches)} files match "
                    f"basename ({rel_matches}); not deleted")
                missing.append(name)
                continue

        if target is None:
            missing.append(name)
            continue

        try:
            target.unlink()
            applied.append(name)
            warnings_lines.append(
                f"[{name}] [DELETED] removed from target tree")
        except Exception as e:
            warnings_lines.append(
                f"[{name}] [ERROR] could not delete: {e}")
            missing.append(name)
    return applied, missing


def _trust_reason(rec: FileRecord) -> str:
    """Human-readable string listing which check(s) triggered LOW trust."""
    reasons = []
    if rec.verification.get("patches_incomplete"):
        reasons.append("patches_incomplete")
    if rec.verification.get("pairs_incomplete"):
        reasons.append("pairs_incomplete")
    ast_check = rec.verification.get("ast_parse", "")
    if isinstance(ast_check, str) and ast_check.startswith("FAIL"):
        reasons.append("ast_parse_failed")
    if reasons:
        return " [" + ", ".join(reasons) + "]"
    return ""


def _compute_block_disposition(b, records):
    """One-word summary of a block's fate.  Used in
    `_debug.blocks` and (indirectly) by the warnings
    summarizer.

    Trust-aware: a block in a rejected-record previously showed as
    "applied" because the record's *state* was FULL_PLUS_PATCH; that
    misreads the outcome when trust was LOW and `written == False`.
    The disposition now reflects what actually happened:

      · `applied` — record written with this block's patch applied.
      · `block_skipped_misattributed` / `block_skipped_unplaceable` —
        pass 8.7 excluded the block and the rest of the record
        wrote cleanly.
      · `block_skipped_structural_damage` — the structural sanity
        gate refused the patch; see apply_patches._check_structural_sanity.
      · `block_failed` — patch failed and no reconciliation could
        recover the record.
      · `block_unapplied` — record fail-closed; block's patch may
        have applied but the file was not written.
      · `block_partial` — pair member in a record that was not
        written.
    """
    if getattr(b, "disposition", None):
        return b.disposition
    if b.classified_by == "preexisting":
        return "preexisting"
    if b.is_orphan:
        if b.attributed_to:
            rec = records.get(b.attributed_to)
            if not rec or rec.state not in (
                    "SINGLE_FULL", "FULL_PLUS_PATCH",
                    "MULTI_FULL"):
                return "orphan_attributed_skipped"
            _own_patch = next(
                (p for p in rec.patches
                 if p.get("idx") == b.idx),
                None)
            if (_own_patch is not None
                    and _own_patch.get("applied_by")
                    in ("deterministic", "llm")):
                return "orphan_attributed_applied"
            return "orphan_attributed_skipped"
        if b.kind == "not_a_file":
            return "orphan_not_code"
        if b.kind == "recovered_meta":
            return "orphan_meta"
        if b.attributed_by == "empty":
            return "orphan_empty"
        if b.attributed_by == "blockquote_skip":
            return "blockquote_skip"
        return "orphan_unattributed"
    if getattr(b, "paired_with", None) is not None:
        # Pair member: report the record-level outcome so a pair in
        # a written record is distinguishable from one in a
        # skipped record.
        rec = records.get(b.output_path) if b.output_path else None
        if rec is None:
            return "paired"
        if rec.trust == "LOW" and not getattr(rec, "_was_written", False):
            return "block_partial"
        return "paired"
    if b.output_path is None:
        return "unresolved"
    rec = records.get(b.output_path)
    if rec is None:
        return "unmatched"
    if rec.state not in (
            "SINGLE_FULL", "FULL_PLUS_PATCH", "MULTI_FULL"):
        if rec.state == "PARTIAL_ONLY":
            return "partial"
        return "unapplied"

    # Look at the block's own patch status first — this is what
    # reconciliation changes.
    _own = next(
        (p for p in rec.patches if p.get("idx") == b.idx),
        None)
    if _own is not None:
        status = _own.get("applied_by")
        if status == "skipped_misattributed":
            return "block_skipped_misattributed"
        if status == "skipped_unplaceable":
            return "block_skipped_unplaceable"
        if status == "skipped_structural_damage":
            return "block_skipped_structural_damage"
        if status == "failed":
            return "block_failed" if rec.trust == "LOW" else "applied"

    if rec.trust == "LOW" and not getattr(rec, "_was_written", False):
        return "block_unapplied"
    if getattr(rec, "sanity_rejected", False):
        return "block_unapplied"
    return "applied"


def _orphan_first_line(block: Block) -> str:
    """First non-empty body line of an orphan block, truncated."""
    if not block.body:
        return "(empty)"
    for ln in block.body.splitlines():
        s = ln.strip()
        if s:
            if len(s) > 60:
                s = s[:57] + "..."
            return s
    return "(empty)"


def _patch_first_line(patch: dict, limit: int = 60) -> str:
    """First non-empty line of a patch's body, truncated."""
    body = patch.get("body", "") or ""
    for ln in body.splitlines():
        s = ln.strip()
        if s:
            return s[:limit]
    return ""


def _patch_context(patch: dict, limit: int = 800) -> str:
    """Return the patch's `context_before`, truncated to `limit`
    characters with a trailing ellipsis marker when longer.

    FIX (2026-11-01, d): when a heuristic refuses a patch because
    the surrounding prose didn't match any of its patterns, the
    manifest needs to show the prose the heuristic saw.  Without
    it, the diagnostic string ("no heuristic matched…") is an
    opaque verdict that can't be acted on without re-reading the
    dump.  `context_before` closes that gap.
    """
    ctx = (patch.get("context_before") or "").strip()
    if len(ctx) <= limit:
        return ctx
    return ctx[:limit - 3] + "..."


# Warning-class key for aggregation.  Two warnings are considered the
# same class if their whitespace-normalized text agrees up to the first
# 60 characters, with variable fragments — file paths, backticked
# names, line numbers, and numeric counts — replaced by a placeholder.
# The 2026-11-05 run had 200 records emitting the *same* structural-
# equivalence warning because a helper name was undefined; that is
# one bug, and it should occupy one line in warnings.txt.
_WARN_CLASS_VAR_RE = re.compile(
    r'`[^`]+`'
    r'|\bline \d+\b'
    r'|\b\d+\b'
    r'|\[[^\]\n]+\]'
)


def _warn_class_key(text: str) -> str:
    """Stable class key for a warning text.

    Strips the variable parts — backticked names, line numbers, bare
    numbers, bracketed paths — and truncates to 60 characters.  Two
    warnings with different variable fragments but the same shape
    produce the same key, so they aggregate; two warnings with
    genuinely different text produce different keys and stay
    separate.
    """
    return _WARN_CLASS_VAR_RE.sub("_", text)[:60]


def _aggregate_warnings(warnings_lines: list) -> list:
    """Collapse runs of identical-class warnings into one line + count.

    Preserves the per-record `[path] [WARN] <text>` lines for warnings
    whose text differs, but a warning class that fires on N records
    becomes:

        [<path>] [WARN] <text> (+{N-1} more file(s) with this warning)

    Non-`[WARN]` lines are preserved verbatim in their original order
    — this function only touches the `[<path>] [WARN] <text>` shape.

    The function iterates its input exactly **once**, accumulating
    grouped entries into `warn_groups` and pushing every line —
    aggregated or verbatim — into the single output list.  A prior
    version appended non-warning lines during classification *and*
    during reconstruction, doubling every non-`[WARN]` line in the
    output; the 2026-11-05 test run reported one duplicate
    `[preexisting]` line per untouched summary block.

    Motivating case (2026-11-05): 200 records emitted the same
    `structural-equivalence check raised: name '_python_signatures'
    is not defined` warning because `structural_diff` was broken;
    that is one bug and should occupy one line.
    """
    warn_pattern = re.compile(r'^\[([^\]]+)\] \[WARN\] (.+)$')
    warn_groups: dict[str, list] = {}
    order: list[tuple[str, str]] = []

    for line in warnings_lines:
        m = warn_pattern.match(line)
        if not m:
            order.append(("other", line))
            continue
        path, text = m.group(1), m.group(2)
        key = _warn_class_key(text)
        if key not in warn_groups:
            warn_groups[key] = []
            order.append(("warn", key))
        warn_groups[key].append((path, text))

    result: list = []
    for kind, val in order:
        if kind == "other":
            result.append(val)
            continue
        entries = warn_groups[val]
        if len(entries) == 1:
            path, text = entries[0]
            result.append(f"[{path}] [WARN] {text}")
        else:
            sample_path, sample_text = entries[0]
            result.append(
                f"[{sample_path}] [WARN] {sample_text} "
                f"(+{len(entries) - 1} more file(s) with this warning)"
            )
    return result


def pass9_write(records: dict,
                blocks: list,
                deleted_files: list,
                out_dir: Path,
                layout: str,
                min_trust: str,
                dump_display: str,
                dump_size: int,
                dump_sha: str,
                dump_text: str,
                llm: Optional[LLMClient],
                llm_mode: str,
                cross_validate: dict,
                apply_to: Optional[Path] = None,
                resolutions: Optional[list] = None,
                cross_file_duplicates: Optional[list] = None,
                verbose: bool = False,
                run_errors: Optional[list] = None,
                pass_timings: Optional[dict] = None) -> int:
    if apply_to is not None:
        files_dir = apply_to
        aux_dir = apply_to / "deeppeek"
        low_dir = aux_dir / "low_trust"
        reports_dir = aux_dir
    else:
        files_dir = out_dir / "files"
        low_dir = out_dir / "files_low_trust"
        aux_dir = out_dir / "llm_audit"
        reports_dir = out_dir

    if not records:
        print("  [!] pipeline produced zero file records — the "
              "conversation may have had no parseable code blocks, "
              "OR an upstream pass silently discarded them.  Check "
              "the block list and warnings.txt.",
              file=sys.stderr)

    _run_errors = run_errors or []

    files_dir.mkdir(parents=True, exist_ok=True)
    aux_dir.mkdir(parents=True, exist_ok=True)

    pre_existing_files: set = (
        {p.name for p in files_dir.iterdir() if p.is_file()}
        if files_dir.exists() else set()
    )
    pre_existing_low: set = (
        {p.name for p in low_dir.iterdir() if p.is_file()}
        if low_dir.exists() else set()
    )

    trust_rank = {"SKIP": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
    min_rank = trust_rank[min_trust]

    warnings_lines: list = []
    for _err in _run_errors:
        warnings_lines.append(
            f"[PASS_FAILED] {_err['pass']}: {_err['error']}")
        for _line in (_err.get("traceback") or "").splitlines():
            warnings_lines.append(f"  {_line}")
    written: dict = {}
    skipped: list = []
    written_count = 0
    exit_code = 0
    written_out_names: dict = {}
    actually_written: set = set()
    actually_written_low: set = set()
    preexisting_skipped_count = 0

    for path, rec in sorted(records.items()):
        if layout == "flat":
            out_name = os.path.basename(path)
        else:
            out_name = path

        for w in rec.warnings:
            warnings_lines.append(f"[{path}] [WARN] {w}")

        for r in rec.verification.get("pair_records", []):
            if r.get("applied_by") != "failed":
                continue
            pair = r.get("pair", {})
            oi = pair.get("old_idx")
            ni = pair.get("new_idx")
            scope = pair.get("scope_hint", "replace")
            reason = r.get("reason", "unknown")
            kind = "scoped" if oi == -1 else "replace"

            if "ellipsis" in reason.lower():
                warnings_lines.append(
                    f"[{path}] [PAIR_ELIDED] {kind} pair "
                    f"(old={oi}, new={ni}): {reason}")
            elif ("not found in base" in reason
                  or "state drift" in reason):
                warnings_lines.append(
                    f"[{path}] [STATE_DRIFT] {kind} pair "
                    f"(old={oi}, new={ni}): {reason}")
                warnings_lines.append(
                    f"[{path}] [STATE_DRIFT] this usually means "
                    f"the base is missing a prerequisite change "
                    f"from an earlier conversation; verify the "
                    f"base state and reapply the prerequisite "
                    f"before retrying.")
            else:
                warnings_lines.append(
                    f"[{path}] [PAIR_FAILED] {kind} pair "
                    f"(old={oi}, new={ni}, scope={scope}): {reason}")

        for p in rec.patches:
            if p.get("applied_by") not in (
                    "failed", "skipped_structural_damage"):
                continue
            if p.get("applied_by") == "skipped_structural_damage":
                first_line = _patch_first_line(p, limit=70)
                reason = p.get("failure_reason", "unknown")
                warnings_lines.append(
                    f"[{path}] [STRUCT_DAMAGE] idx={p['idx']} "
                    f"(first line: {first_line!r}): {reason}")
                continue
            # NEW (2026-10-29): tool bugs are code-level failures —
            # missing module, renamed function, undefined name —
            # that aborted the heuristic chain.  Distinguish them
            # from body-shape failures so the user knows whether to
            # look at the conversation or at deeppeek itself.
            if p.get("tool_bug"):
                first_line = _patch_first_line(p, limit=70)
                reason = p["tool_bug"]
                warnings_lines.append(
                    f"[{path}] [TOOL_BUG] idx={p['idx']} "
                    f"(first line: {first_line!r}): {reason} "
                    f"— this is a deeppeek internal error, not a "
                    f"body-shape failure; report the exception "
                    f"type and message to deeppeek")
                continue
            first_line = _patch_first_line(p, limit=70)
            reason = p.get("failure_reason", "unknown")
            warnings_lines.append(
                f"[{path}] [PATCH_FAILED] idx={p['idx']} "
                f"(first line: {first_line!r}): {reason}")
            _ctx = _patch_context(p)
            if _ctx:
                warnings_lines.append(
                    f"[{path}] [PATCH_FAILED_CONTEXT] idx={p['idx']}: "
                    f"{_ctx!r}")

        for p in rec.patches:
            if not p.get("filtered_duplicate_lines"):
                continue
            status = p.get("applied_by", "?")
            fallback = " (fallback: applied unfiltered)" \
                if p.get("filter_fallback") else ""
            warnings_lines.append(
                f"[{path}] [FILTERED] idx={p['idx']} "
                f"filtered duplicate block(s); final status: "
                f"{status}{fallback}")

        if rec.is_deleted:
            warnings_lines.append(
                f"[{path}] [INFO] file deleted by conversation")
            continue

        if getattr(rec, "sanity_rejected", False):
            reason = rec.verification.get(
                "sanity_reason",
                "sanity gate rejected write (over-deletion, empty result, "
                "or unrelated content)")
            warnings_lines.append(
                f"[{path}] [SANITY] {reason}; target file preserved")

            if rec.content is not None:
                rejected_dir = aux_dir / "rejected"
                rejected_dir.mkdir(parents=True, exist_ok=True)
                rejected_target = rejected_dir / out_name
                rejected_target.parent.mkdir(parents=True, exist_ok=True)
                rejected_content = rec.content
                if not rejected_content.endswith("\n"):
                    rejected_content += "\n"
                rejected_target.write_text(rejected_content)

            if getattr(rec, "repair_content", None):
                low_dir.mkdir(parents=True, exist_ok=True)
                repaired_name = f"{out_name}.repaired"
                repaired_path = low_dir / repaired_name
                repaired_path.parent.mkdir(parents=True, exist_ok=True)
                repaired_path.write_text(rec.repair_content)
                closers = rec.verification.get(
                    "sanity_repair_closers", "?")
                warnings_lines.append(
                    f"[{path}] [REPAIR] suggestion written to "
                    f"{'deeppeek/low_trust/' if apply_to is not None else 'files_low_trust/'}"
                    f"{repaired_name} (+{closers} closer(s); "
                    f"review before adopting)")
                if verbose:
                    print(f"  [repair] {path}: suggestion at "
                          f"{repaired_path}", file=sys.stderr)

            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "sanity_rejected": True,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (sanity): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue

        # FIX (2026-11-01, f): reconciled-to-base early skip.  When
        # pass 8.7 excluded every applied item and the rebuilt content
        # equals the pre-existing base, the target file is already
        # what we would write.  Skip cleanly; the per-item exclusions
        # are already in `warnings.txt` and the `reconciled` field
        # documents the audit trail.
        if (rec.verification.get("reconciled_to_base")
                and rec.preexisting_base):
            reason = ("reconciled to base: applied items were "
                      "excluded as misattributed or unplaceable; "
                      "no net change")
            warnings_lines.append(f"[{path}] [NO_CHANGE] {reason}")
            skipped.append({
                "path": path, "state": rec.state, "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (no change): {reason}",
                      file=sys.stderr)
            preexisting_skipped_count += 1
            continue

        if rec.state in ("PARTIAL_ONLY", "MISSING"):
            reason = f"no full print (state={rec.state})"
            warnings_lines.append(
                f"[{path}] [{rec.state}] {reason}; not written")
            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} ({rec.state}): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue

        if rec.content is None:
            reason = "content is None after patching"
            warnings_lines.append(
                f"[{path}] [ERROR] {reason}; not written")
            skipped.append({
                "path": path,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (no content): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue

        # FIX (2026-11-01, f): no-op statuses now include the
        # reconciliation skip statuses.  A pre-existing baseline
        # whose only "patches" were excluded by pass 8.7 has not
        # been modified; skip cleanly.
        preexisting_unmodified = (
            rec.preexisting_base
            and not any(
                p.get("applied_by") not in _NO_OP_STATUSES
                for p in rec.patches
            )
            and not any(
                r.get("applied_by") not in _NO_OP_STATUSES
                for r in rec.verification.get("pair_records", [])
            )
        )
        if preexisting_unmodified:
            reason = ("pre-existing file with no conversation "
                      "changes; left as-is")
            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (preexisting): {reason}",
                      file=sys.stderr)
            preexisting_skipped_count += 1
            # NOTE (2026-11-05): no per-file `[PREEXISTING]` warning
            # line — aggregated below into a single summary line.
            continue

        if out_name in written_out_names:
            prev_src = written_out_names[out_name]
            reason = (
                f"basename collision with {prev_src!r} "
                f"(layout={layout})"
            )
            warnings_lines.append(
                f"[{path}] [ERROR] {reason}; not written")
            skipped.append({
                "path": path,
                "state": rec.state,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if verbose:
                print(f"  SKIPPED {path} (collision): {reason}",
                      file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        written_out_names[out_name] = path

        if trust_rank.get(rec.trust, 0) < min_rank:
            why = _trust_reason(rec)
            reason = f"trust={rec.trust}{why} below --min-trust {min_trust}"
            warnings_lines.append(
                f"[{path}] [{rec.trust}]{why} {reason}")
            skipped.append({
                "path": path,
                "trust": rec.trust,
                "reason": reason,
                "source_blocks": rec.source_blocks,
                "warnings": rec.warnings,
                "verification": rec.verification,
            })
            if rec.content is not None:
                low_dir.mkdir(parents=True, exist_ok=True)
                low_target = low_dir / out_name
                low_target.parent.mkdir(parents=True, exist_ok=True)
                low_content = rec.content
                if not low_content.endswith("\n"):
                    low_content += "\n"
                low_target.write_text(low_content)
                actually_written_low.add(out_name)
                if apply_to is not None:
                    warnings_lines.append(
                        f"[{path}] [{rec.trust}] -> deeppeek/low_trust/")
                else:
                    warnings_lines.append(
                        f"[{path}] [{rec.trust}] -> files_low_trust/")
            if verbose:
                dest = ("deeppeek/low_trust/" if apply_to is not None
                        else "files_low_trust/")
                print(f"  SKIPPED {path} ({rec.trust}){why}: {reason} "
                      f"-> {dest}", file=sys.stderr)
                for w in rec.warnings[:3]:
                    print(f"    warning: {w}", file=sys.stderr)
            exit_code = max(exit_code, 1)
            continue

        if rec.trust == "LOW":
            exit_code = max(exit_code, 1)

        target = files_dir / out_name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = rec.content
        if not content.endswith("\n"):
            content += "\n"
        target.write_text(content)
        actually_written.add(out_name)

        # Mark the record as actually written so _compute_block_disposition
        # can distinguish "state says written" from "write actually ran".
        try:
            rec._was_written = True
        except Exception:
            pass

        sha = hashlib.sha256(content.encode()).hexdigest()
        manifest_base = apply_to if apply_to is not None else out_dir
        try:
            rel_path = str(target.relative_to(manifest_base))
        except ValueError:
            rel_path = str(target)
        written[path] = {
            "state": rec.state,
            "source_blocks": rec.source_blocks,
            "all_full_idxs": rec.all_full_idxs,
            "chosen_full_idx": rec.chosen_full_idx,
            "dedup_reason": rec.dedup_reason,
            "last_full_idx": rec.last_full_idx,
            "patches": [
                {
                    "idx": p["idx"],
                    "applied_by": p.get("applied_by", "?"),
                    **({"filtered_duplicate_lines": True}
                       if p.get("filtered_duplicate_lines") else {}),
                    **({"filter_fallback": True}
                       if p.get("filter_fallback") else {}),
                }
                for p in rec.patches
            ],
            "pairs": [
                {"old_idx": p["old_idx"], "new_idx": p["new_idx"],
                 "scope_hint": p["scope_hint"]}
                for p in rec.pairs
            ],
            "is_new": rec.is_new,
            "is_deleted": False,
            "trust": rec.trust,
            "preexisting_base": rec.preexisting_base,
            "output_path": rel_path,
            "line_count": content.count("\n"),
            "sha256": sha,
            "verification": rec.verification,
        }
        written_count += 1

    deleted_from_records = {p for p, r in records.items() if r.is_deleted}
    all_deleted = sorted(set(deleted_files) | deleted_from_records)

    deleted_applied: list = []
    deleted_missing: list = []
    if apply_to is not None:
        deleted_applied, deleted_missing = _apply_deletions(
            apply_to, all_deleted, warnings_lines)

    deleted_lines = [
        "# Files marked [DEL]/[DELETED]/[REMOVED] or `rm` in the conversation.",
        "# In standard mode this is informational only.",
        "# In --apply-to mode, the deleted-applied section was actually",
        "# removed from the target tree.",
        "",
        "[marked-deleted]",
    ]
    for name in all_deleted:
        deleted_lines.append(name)
    if apply_to is not None:
        deleted_lines.append("")
        deleted_lines.append("[deleted-applied]")
        for name in deleted_applied:
            deleted_lines.append(name)
        if deleted_missing:
            deleted_lines.append("")
            deleted_lines.append("[deleted-not-found]")
            for name in deleted_missing:
                deleted_lines.append(name)
    (reports_dir / "deleted.txt").write_text(
        "\n".join(deleted_lines) + "\n")

    for gp in cross_validate.get("problems", []):
        if gp["type"] == "missing_module":
            warnings_lines.append(
                f"[graph] {gp['from']} imports module "
                f"'{gp['module']}' — not recovered")
        elif gp["type"] == "missing_export":
            warnings_lines.append(
                f"[graph] {gp['from']} imports '{gp['name']}' from "
                f"'{gp['target']}' — not exported")
    if cross_validate.get("problems"):
        exit_code = max(exit_code, 4)

    unattributed = [b for b in blocks
                    if b.is_orphan and not b.attributed_to
                    and b.attributed_by not in (
                        "empty", "no_candidates", "skipped_meta",
                        "not_a_file", "blockquote_skip",
                    )]
    for b in unattributed:
        first_line = _orphan_first_line(b)
        warnings_lines.append(
            f"[orphan] idx={b.idx} (lines {b.line_start}..{b.line_end}) "
            f"could not be attributed to any file "
            f"(lang={b.fence_lang}, {b.body_line_count} line(s), "
            f"first: {first_line!r})")

    # NEW (2026-11-05): prose-only scan.  See module docstring,
    # PROSE-ONLY MENTION SCAN section.
    mentioned = _scan_mentioned_files(blocks)

    recovered_exts = _recovered_extensions(records)
    if recovered_exts:
        mentioned = {
            name for name in mentioned
            if any(name.endswith(ext) for ext in recovered_exts)
        }

    imported_missing = _aggregate_missing_imports(records)
    recovered = {_basename(p) for p, r in records.items()
                 if r.content is not None}
    deleted_set = set(deleted_files)

    project_prefixes = _recovered_name_prefixes(records)
    if project_prefixes:
        mentioned = {
            name for name in mentioned
            if any(name.startswith(p) for p in project_prefixes)
        }
    else:
        mentioned = set()

    # NEW (2026-11-05): filter further to files that are not already
    # present in the target tree.  A file that exists on disk was not
    # "missed" by the recovery — it was just not touched by this run.
    on_disk: set = set()
    if apply_to is not None and apply_to.is_dir():
        for p in apply_to.rglob("*"):
            if p.is_file():
                on_disk.add(p.name)

    missing = (
        (mentioned | imported_missing) - recovered - deleted_set
        - SKIP_MISSING_FILES - on_disk
    )

    if missing:
        lines = [
            "The following source files are referenced in the transcript",
            "(either named in prose, or imported by a recovered file)",
            "but were never printed as recoverable code blocks:",
            "",
        ]
        for name in sorted(missing):
            lines.append(f"## {name}")
            lines.append("")
        (reports_dir / "MISSING_FILES.txt").write_text(
            "\n".join(lines) + "\n")
        warnings_lines.append(
            f"[missing] {len(missing)} file(s) referenced but not recovered "
            f"— see MISSING_FILES.txt")
        exit_code = max(exit_code, 2)
    else:
        (reports_dir / "MISSING_FILES.txt").write_text(
            "# No missing files.\n"
            "# This file is written on every --apply-to run.  An "
            "empty or all-comment body means the transcript\n"
            "# did not reference any source file that failed to "
            "recover.\n")

    untouched_files = pre_existing_files - actually_written
    untouched_low = pre_existing_low - actually_written_low
    overwritten_files = pre_existing_files & actually_written
    overwritten_low = pre_existing_low & actually_written_low

    if untouched_files or untouched_low:
        total = len(untouched_files) + len(untouched_low)
        warnings_lines.append(
            f"[preserved] {total} pre-existing file(s) in the output "
            f"directory left untouched (not part of this run)")
        if apply_to is None:
            for name in sorted(untouched_files):
                warnings_lines.append(f"  files/{name}")
            for name in sorted(untouched_low):
                warnings_lines.append(f"  files_low_trust/{name}")
        else:
            warnings_lines.append(
                "  (per-file listing suppressed in --apply-to mode; "
                "the target tree owns its own files)")

    # NEW (2026-11-05): pre-existing-skip aggregation.  One line for
    # the whole run instead of one `[PREEXISTING]` line per file.
    # Per-file detail remains in `manifest.decisions[path]`, which has
    # the record's state, preexisting_base flag, verification, and
    # warnings.
    if preexisting_skipped_count:
        warnings_lines.append(
            f"[preexisting] {preexisting_skipped_count} pre-existing "
            f"file(s) left unchanged (not part of this run)"
        )

    if cross_file_duplicates:
        for _d in cross_file_duplicates:
            _files = ", ".join(_d["files"])
            warnings_lines.append(
                f"[CROSS_FILE_DUP] {_d['kind']} "
                f"{_d['name']!r} ({_d['line_count']} lines) "
                f"newly-added to: {_files}")

    if overwritten_files or overwritten_low:
        warnings_lines.append(
            "[overwritten] pre-existing files overwritten by this run:")
        for name in sorted(overwritten_files):
            warnings_lines.append(f"  {name}")
        for name in sorted(overwritten_low):
            warnings_lines.append(f"  deeppeek/low_trust/{name}")

    repairs_manifest = {}
    for rec_path, rec in records.items():
        if not getattr(rec, "repair_content", None):
            continue
        rec_out_name = (
            os.path.basename(rec_path) if layout == "flat"
            else rec_path
        )
        repairs_manifest[rec_path] = {
            "failure_reason": rec.verification.get("sanity_reason"),
            "closers_added": rec.verification.get(
                "sanity_repair_closers"),
            "suggested_file": f"{rec_out_name}.repaired",
        }
    (reports_dir / "repairs.json").write_text(
        json.dumps(repairs_manifest, indent=2))

    # NEW (2026-11-05): aggregate repeated warnings.  See module
    # docstring, WARNINGS AGGREGATION section.
    warnings_lines = _aggregate_warnings(warnings_lines)

    warnings_text = "\n".join(warnings_lines)
    if warnings_text:
        warnings_text += "\n"
    (reports_dir / "warnings.txt").write_text(warnings_text)

    escalations = []
    for b in blocks:
        if b.escalation_reason is None:
            continue
        escalations.append({
            "idx": b.idx,
            "path": b.canonical_path,
            "fence_lang": b.fence_lang,
            "reason": b.escalation_reason,
            "heuristic_kind": b.heuristic_kind,
            "llm_kind": b.llm_kind,
            "final_kind": b.kind,
            "override_reason": b.override_reason,
            "classified_by": b.classified_by,
        })

    by_heuristic = sum(1 for b in blocks if b.classified_by == "heuristic")
    by_llm = sum(1 for b in blocks if b.classified_by == "llm")
    by_preexisting = sum(1 for b in blocks if b.classified_by == "preexisting")
    escalated = sum(1 for b in blocks if b.escalation_reason is not None)
    overridden = sum(1 for b in blocks if b.override_reason is not None)
    orphans_attributed = sum(
        1 for b in blocks if b.is_orphan and b.attributed_to)

    decisions = {}
    for path, rec in records.items():
        patch_idxs = [p["idx"] for p in rec.patches]
        source_idxs = list(rec.source_blocks)
        for pidx in patch_idxs:
            if pidx not in source_idxs:
                source_idxs.append(pidx)

        pair_records = rec.verification.get("pair_records", [])
        pair_failures = []
        state_drift_count = 0
        for r in pair_records:
            if r.get("applied_by") == "failed":
                pair = r.get("pair", {})
                reason = r.get("reason", "unknown")
                if ("not found in base" in reason
                        or "state drift" in reason):
                    state_drift_count += 1
                pair_failures.append({
                    "old_idx": pair.get("old_idx"),
                    "new_idx": pair.get("new_idx"),
                    "scope_hint": pair.get("scope_hint"),
                    "reason": reason,
                })
        sanity_reason = (
            rec.verification.get("sanity_reason")
            if getattr(rec, "sanity_rejected", False) else None
        )

        patch_failure_reasons = [
            {
                "idx": p["idx"],
                "first_line": _patch_first_line(p, limit=70),
                "context_before": _patch_context(p, limit=800),
                "reason": p.get("failure_reason", "unknown"),
                **({"tool_bug": p["tool_bug"]}
                   if p.get("tool_bug") else {}),
            }
            for p in rec.patches
            if p.get("applied_by") in _PLACEMENT_FAILURES
        ]

        decisions[path] = {
            "state": rec.state,
            "source_block_idxs": source_idxs,
            "chosen_full_idx": rec.chosen_full_idx,
            "chosen_by": rec.dedup_reason,
            "trust": rec.trust,
            "preexisting_base": rec.preexisting_base,
            "sanity_rejected": getattr(rec, "sanity_rejected", False),
            "sanity_reason": sanity_reason,
            "written": path in written,
            "patch_count": len(rec.patches),
            "pair_count": len(rec.pairs),
            "patch_failures": [
                p["idx"] for p in rec.patches
                if p.get("applied_by") in _PLACEMENT_FAILURES
            ],
            "patch_failure_reasons": patch_failure_reasons,
            "pair_failures": pair_failures,
            "state_drift_count": state_drift_count,
            "verification": rec.verification,
            "warnings": rec.warnings,
        }

    ast_info = None
    try:
        from .ast_anchor import ast_status
        ast_info = ast_status()
    except Exception:
        pass

    manifest = {
        "dump": {
            "path": dump_display,
            "sha256": dump_sha,
            "byte_len": dump_size,
        },
        "run": {
            "timestamp": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds"),
            "mode": "apply" if apply_to is not None else "recover",
            "apply_to": str(apply_to) if apply_to is not None else None,
            "layout": layout,
            "llm_mode": llm_mode,
            "llm_model": llm.model if llm else None,
            "llm_calls": {
                "live": llm.calls_made if llm else 0,
                "cached": llm.calls_cached if llm else 0,
                "refused": llm.calls_refused if llm else 0,
            },
            "ast": ast_info,
            "errors": _run_errors,
            "pass_timings": (
                {k: round(v, 3) for k, v in
                 sorted(pass_timings.items(), key=lambda kv: -kv[1])}
                if pass_timings else None
            ),
            "block_classification": {
                "total": len(blocks),
                "escalated_to_llm": escalated,
                "overridden_by_hard_rules": overridden,
                "classified_by_heuristic": by_heuristic,
                "classified_by_llm": by_llm,
                "classified_by_preexisting": by_preexisting,
                "orphans_attributed": orphans_attributed,
            },
            "escalations": escalations,
            "resolutions": resolutions or [],
            "cross_validate": cross_validate,
            "missing_files": sorted(missing) if missing else [],
            "deleted_applied": deleted_applied,
            "deleted_not_found": deleted_missing,
            "preexisting_unchanged": preexisting_skipped_count,
        },
        "files": written,
        "skipped": skipped,
        "decisions": decisions,
        "deleted": all_deleted,
        "cross_file_duplicates": cross_file_duplicates or [],
        "_debug": {
            "blocks": [
                {
                    "idx": b.idx,
                    "header_path": b.header_path,
                    "output_path": b.output_path,
                    "attributed_to": b.attributed_to,
                    "attributed_by": b.attributed_by,
                    "is_orphan": b.is_orphan,
                    "in_blockquote": b.in_blockquote,
                    "kind": b.kind,
                    "paired_with": getattr(b, "paired_with", None),
                    "classified_by": b.classified_by,
                    "line_start": b.line_start,
                    "line_end": b.line_end,
                    "disposition": _compute_block_disposition(b, records),
                }
                for b in blocks
            ],
        },
    }
    (reports_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if skipped:
        by_reason: dict = {
            "no_full":     [],
            "low_trust":   [],
            "sanity":      [],
            "no_content":  [],
            "collision":   [],
            "preexisting": [],
            "no_change":   [],
            "other":       [],
        }
        for s in skipped:
            r = s.get("reason", "")
            if "no full print" in r:
                by_reason["no_full"].append(s["path"])
            elif "sanity gate rejected" in r or "truncation" in r:
                by_reason["sanity"].append(s["path"])
            elif "trust=" in r:
                by_reason["low_trust"].append(s["path"])
            elif "content is None" in r:
                by_reason["no_content"].append(s["path"])
            elif "basename collision" in r:
                by_reason["collision"].append(s["path"])
            elif "reconciled to base" in r:
                by_reason["no_change"].append(s["path"])
            elif "pre-existing" in r:
                by_reason["preexisting"].append(s["path"])
            else:
                by_reason["other"].append(s["path"])

        print(f"\n  Skipped {len(skipped)} file(s):", file=sys.stderr)
        for label, paths in by_reason.items():
            if not paths:
                continue
            head = ", ".join(paths[:5])
            tail = f" … (+{len(paths)-5} more)" if len(paths) > 5 else ""
            print(f"    {label}: {len(paths)}  ({head}{tail})",
                  file=sys.stderr)
        print(f"    Full list: {reports_dir / 'warnings.txt'}",
              file=sys.stderr)

    xv = cross_validate.get("problems", [])
    if xv:
        missing_mods = sorted({
            p["module"] for p in xv if p["type"] == "missing_module"
        })
        if missing_mods:
            print(f"  missing modules ({len(missing_mods)}): "
                  f"{', '.join(missing_mods)}", file=sys.stderr)

    if apply_to is not None:
        print(f"\nApplied {written_count} file(s) to {files_dir}/")
        print(f"  reports: {reports_dir}/")
    else:
        print(f"\nWrote {written_count} files to {files_dir}/")
    print(f"  skipped:  {len(skipped)}"
          + ("  (see warnings.txt)" if skipped else ""))

    fail_closed_count = sum(
        1 for rec in records.values()
        if rec.trust == "LOW"
        and not rec.is_deleted
        and not getattr(rec, "sanity_rejected", False)
        and rec.content is not None
    )
    if fail_closed_count:
        print(f"  FAIL-CLOSED: {fail_closed_count} file(s) with "
              f"incomplete application — target preserved, content in "
              f"{'deeppeek/low_trust/' if apply_to else 'files_low_trust/'}",
              file=sys.stderr)
        print(f"    (use --min-trust LOW to force-write them)",
              file=sys.stderr)
        for fpath, frec in sorted(records.items()):
            if frec.trust != "LOW":
                continue
            if frec.is_deleted or getattr(frec, "sanity_rejected", False):
                continue
            if frec.content is None:
                continue
            for pf in frec.patches:
                if pf.get("applied_by") not in _PLACEMENT_FAILURES:
                    continue
                first_line = _patch_first_line(pf, limit=60)
                if pf.get("tool_bug"):
                    tag = "TOOL_BUG"
                    reason = pf["tool_bug"]
                elif pf.get("applied_by") == "skipped_structural_damage":
                    tag = "STRUCT_DAMAGE"
                    reason = pf.get("failure_reason", "unknown")
                else:
                    tag = "PATCH_FAILED"
                    reason = pf.get("failure_reason", "unknown")
                print(f"    {tag} {fpath} idx={pf['idx']}: "
                      f"{first_line!r} — {reason}",
                      file=sys.stderr)
                ctx = _patch_context(pf, limit=200)
                if ctx:
                    print(f"      context: {ctx!r}", file=sys.stderr)
            for r in frec.verification.get("pair_records", []):
                if r.get("applied_by") != "failed":
                    continue
                pair = r.get("pair", {})
                oi = pair.get("old_idx")
                reason = r.get("reason", "unknown")
                print(f"    PAIR_FAILED {fpath} (old={oi}): {reason}",
                      file=sys.stderr)

    # FIX (2026-11-01, g): reconciliation summary.  Reports how many
    # records pass 8.7 rescued and how many items it excluded, so the
    # operator sees "reconciled: N records, M items excluded" when
    # the run landed cleanly only because reconciliation dropped
    # specific items.  Silent when no record was reconciled.
    n_reconciled_records = sum(
        1 for r in records.values()
        if "reconciled" in r.verification
    )
    if n_reconciled_records:
        total_excluded = sum(
            len(r.verification["reconciled"]["excluded"])
            for r in records.values()
            if "reconciled" in r.verification
        )
        print(f"  reconciled: {n_reconciled_records} record(s), "
              f"{total_excluded} item(s) excluded "
              f"(see warnings.txt for details)",
              file=sys.stderr)

    stale_files = []
    for fpath, frec in records.items():
        pf = frec.verification.get("pair_records") or []
        failed = [r for r in pf if r.get("applied_by") == "failed"]
        if not failed:
            continue
        if all(("not found in base" in r.get("reason", "")
                or "state drift" in r.get("reason", ""))
               for r in failed):
            if not frec.patches or all(
                p.get("applied_by") in ("failed", "already-present")
                for p in frec.patches
            ):
                stale_files.append(fpath)
    if stale_files:
        print(f"  [hint] {len(stale_files)} file(s) look like "
              f"their base predates this conversation "
              f"({', '.join(sorted(stale_files))}); consider "
              f"applying the previous conversation first.",
              file=sys.stderr)

    if preexisting_skipped_count:
        print(f"  preexisting: {preexisting_skipped_count} file(s) left "
              f"as-is (unchanged by the conversation)")

    if deleted_applied:
        print(f"  deleted:  {len(deleted_applied)} file(s) removed "
              f"from target tree")
        for name in deleted_applied[:5]:
            print(f"    {name}")
        if len(deleted_applied) > 5:
            print(f"    ... +{len(deleted_applied) - 5} more")
    if deleted_missing and apply_to is not None:
        print(f"  del-miss: {len(deleted_missing)} file(s) marked "
              f"[DEL] but not present in target tree")

    total_preserved = len(untouched_files) + len(untouched_low)
    if total_preserved:
        print(f"  preserved: {total_preserved} pre-existing file(s) "
              f"left untouched (not part of this run)")
        if apply_to is None:
            shown = 0
            for name in sorted(untouched_files)[:5]:
                print(f"    files/{name}")
                shown += 1
            for name in sorted(untouched_low)[:5]:
                print(f"    files_low_trust/{name}")
                shown += 1
            if total_preserved > shown:
                print(f"    ... +{total_preserved - shown} more "
                      f"(see warnings.txt)")
    total_overwritten = len(overwritten_files) + len(overwritten_low)
    if total_overwritten:
        print(f"  overwrote: {total_overwritten} pre-existing file(s) "
              f"that shared a name with this run's output")

    print(f"  classified: {by_heuristic} heuristic / {by_llm} LLM"
          + (f" / {by_preexisting} preexisting" if by_preexisting else "")
          + f" (escalated {escalated}/{len(blocks)}, "
          f"{overridden} overridden by hard rules)")
    print(f"  orphans:  {orphans_attributed} attributed to a file"
          + (f", {len(unattributed)} unattributed (see warnings.txt)"
             if unattributed else ""))
    print(f"  graph:    {len(xv)} import problem(s)"
          if xv else "  graph:    clean")
    if cross_file_duplicates:
        print(f"  cross-dup: {len(cross_file_duplicates)} "
              f"newly-added function/class body(ies) in multiple "
              f"files (see warnings.txt)")
    print(f"  deleted:  {reports_dir / 'deleted.txt'} "
          f"({len(all_deleted)} marked"
          + (f", {len(deleted_applied)} applied)" if apply_to is not None
             else f")"))
    if missing:
        print(f"  missing:  {reports_dir / 'MISSING_FILES.txt'} "
              f"({len(missing)} files referenced but never recovered)")
    if llm is not None:
        print(f"  LLM:      {llm.calls_made} live, "
              f"{llm.calls_cached} cached, "
              f"{llm.calls_refused} refused")
    print(f"  warnings: {reports_dir / 'warnings.txt'}")
    print(f"  manifest: {reports_dir / 'manifest.json'}")
    return exit_code
