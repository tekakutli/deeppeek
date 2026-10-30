"""Console summary printing for pass 9.

CHURNY.  Every "New in 2026-XX" annotation that adds a summary line
lands here.  The output is what the operator reads at the end of a
run — the only part of the pass whose *format* is a user-facing
contract (so it tends to grow before it stabilises).

`print_summary(state)` reads the same state dict that
`_manifest.build_manifest` consumes.  Nothing here writes files; the
warnings/manifest paths it mentions are already on disk by the time
it runs.

The per-patch fail-closed detail block is the one heavy section: it
loops every LOW-trust record and prints one PATCH_FAILED / PAIR_FAILED
line per failure with its `context_before` truncated to 200 chars.
That is the console-visible diagnostic the operator actually acts on.
"""

import sys
from pathlib import Path

from ..core import _PLACEMENT_FAILURES
from ._text import _patch_first_line, _patch_context


def print_summary(state: dict) -> None:
    """Print the end-of-run summary to stderr.

    Reads the following keys from `state`:

      records, written_count, skipped, all_deleted, deleted_applied,
      deleted_missing, missing, llm, cross_validate,
      cross_file_duplicates, apply_to, files_dir, reports_dir,
      untouched_files, untouched_low, overwritten_files,
      overwritten_low, preexisting_skipped_count, by_heuristic,
      by_llm, by_preexisting, escalated, overridden,
      orphans_attributed, unattributed, total_block_count.
    """
    records = state["records"]
    skipped = state["skipped"]
    llm = state["llm"]
    cross_validate = state["cross_validate"]
    cross_file_duplicates = state["cross_file_duplicates"]
    apply_to = state["apply_to"]
    files_dir = state["files_dir"]
    reports_dir = state["reports_dir"]
    written_count = state["written_count"]

    # ---- skipped-file summary by reason class ---------------------
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
            tail = (f" … (+{len(paths)-5} more)"
                    if len(paths) > 5 else "")
            print(f"    {label}: {len(paths)}  ({head}{tail})",
                  file=sys.stderr)
        print(f"    Full list: {reports_dir / 'warnings.txt'}",
              file=sys.stderr)

    # ---- missing modules from cross-validation -------------------
    xv = cross_validate.get("problems", [])
    if xv:
        missing_mods = sorted({
            p["module"] for p in xv
            if p["type"] == "missing_module"
        })
        if missing_mods:
            print(f"  missing modules ({len(missing_mods)}): "
                  f"{', '.join(missing_mods)}", file=sys.stderr)

    # ---- main write line ----------------------------------------
    if apply_to is not None:
        print(f"\nApplied {written_count} file(s) to {files_dir}/")
        print(f"  reports: {reports_dir}/")
    else:
        print(f"\nWrote {written_count} files to {files_dir}/")
    print(f"  skipped:  {len(skipped)}"
          + ("  (see warnings.txt)" if skipped else ""))

    # ---- fail-closed summary ------------------------------------
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
            if (frec.is_deleted
                    or getattr(frec, "sanity_rejected", False)):
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
                print(f"    PAIR_FAILED {fpath} (old={oi}): "
                      f"{reason}", file=sys.stderr)

    # ---- reconciliation summary --------------------------------
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

    # ---- stale-base hint ---------------------------------------
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

    # ---- preexisting / deleted / preserved counts --------------
    preexisting_skipped_count = state["preexisting_skipped_count"]
    if preexisting_skipped_count:
        print(f"  preexisting: {preexisting_skipped_count} file(s) "
              f"left as-is (unchanged by the conversation)")

    deleted_applied = state["deleted_applied"]
    deleted_missing = state["deleted_missing"]
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

    untouched_files = state["untouched_files"]
    untouched_low = state["untouched_low"]
    overwritten_files = state["overwritten_files"]
    overwritten_low = state["overwritten_low"]
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

    # ---- classification / graph / cross-dup / paths ------------
    by_heuristic = state["by_heuristic"]
    by_llm = state["by_llm"]
    by_preexisting = state["by_preexisting"]
    escalated = state["escalated"]
    overridden = state["overridden"]
    orphans_attributed = state["orphans_attributed"]
    unattributed = state["unattributed"]
    total_block_count = state["total_block_count"]
    all_deleted = state["all_deleted"]
    missing = state["missing"]

    print(f"  classified: {by_heuristic} heuristic / {by_llm} LLM"
          + (f" / {by_preexisting} preexisting"
             if by_preexisting else "")
          + f" (escalated {escalated}/{total_block_count}, "
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
          + (f", {len(deleted_applied)} applied)"
             if apply_to is not None else ")"))
    if missing:
        print(f"  missing:  {reports_dir / 'MISSING_FILES.txt'} "
              f"({len(missing)} files referenced but never recovered)")
    if llm is not None:
        print(f"  LLM:      {llm.calls_made} live, "
              f"{llm.calls_cached} cached, "
              f"{llm.calls_refused} refused")
    print(f"  warnings: {reports_dir / 'warnings.txt'}")
    print(f"  manifest: {reports_dir / 'manifest.json'}")
