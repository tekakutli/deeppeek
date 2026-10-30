"""Manifest dict assembly.

CHURNY.  The manifest's shape is consumed by external tools; every
"New in 2026-XX" annotation that adds a verification field or a
failure-reason entry lands here.  Keeping the assembly isolated from
`pass9.py` means a new field is a 1-line change to one function, not
a scuba dive through the write loop.

`build_manifest(state)` takes the pass-9 state dict and returns the
manifest dict.  `pass9_write` is responsible for `json.dumps` and
writing the file; this module never touches the filesystem.
"""

from datetime import datetime, timezone

from ._disposition import _compute_block_disposition


def build_manifest(state: dict) -> dict:
    """Assemble the manifest dict from the pass-9 state.

    `state` provides (in addition to what `_summary.print_summary`
    reads):

      written, skipped, decisions, all_deleted,
      cross_file_duplicates, dump_display, dump_size, dump_sha,
      apply_to, layout, llm, llm_mode, run_errors, pass_timings,
      blocks, resolutions, cross_validate, missing,
      deleted_applied, deleted_missing, preexisting_skipped_count,
      records (for `_debug.blocks`).
    """
    blocks = state["blocks"]
    records = state["records"]
    llm = state["llm"]

    # Grammar availability — same probe as `pipeline.run`'s startup
    # diagnostic.  Imported lazily so the `[ast]` extra is not
    # required to load this module.
    ast_info = None
    try:
        from ..ast_anchor import ast_status
        ast_info = ast_status()
    except Exception:
        pass

    # Block-classification counts.  Cheap pass over `blocks`; the
    # per-classification counters `state` already carries were
    # computed in `pass9_write` because they also feed the summary
    # printout, so just read them here.
    manifest = {
        "dump": {
            "path": state["dump_display"],
            "sha256": state["dump_sha"],
            "byte_len": state["dump_size"],
        },
        "run": {
            "timestamp": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds"),
            "mode": ("apply" if state["apply_to"] is not None
                     else "recover"),
            "apply_to": (str(state["apply_to"])
                         if state["apply_to"] is not None
                         else None),
            "layout": state["layout"],
            "llm_mode": state["llm_mode"],
            "llm_model": llm.model if llm else None,
            "llm_calls": {
                "live": llm.calls_made if llm else 0,
                "cached": llm.calls_cached if llm else 0,
                "refused": llm.calls_refused if llm else 0,
            },
            "ast": ast_info,
            "errors": state["run_errors"],
            "pass_timings": (
                {k: round(v, 3) for k, v in
                 sorted(state["pass_timings"].items(),
                        key=lambda kv: -kv[1])}
                if state["pass_timings"] else None
            ),
            "block_classification": {
                "total": len(blocks),
                "escalated_to_llm": state["escalated"],
                "overridden_by_hard_rules": state["overridden"],
                "classified_by_heuristic": state["by_heuristic"],
                "classified_by_llm": state["by_llm"],
                "classified_by_preexisting":
                    state["by_preexisting"],
                "orphans_attributed": state["orphans_attributed"],
            },
            "escalations": state["escalations"],
            "resolutions": state["resolutions"] or [],
            "cross_validate": state["cross_validate"],
            "missing_files": (
                sorted(state["missing"])
                if state["missing"] else []
            ),
            "deleted_applied": state["deleted_applied"],
            "deleted_not_found": state["deleted_missing"],
            "preexisting_unchanged":
                state["preexisting_skipped_count"],
        },
        "files": state["written"],
        "skipped": state["skipped"],
        "decisions": state["decisions"],
        "deleted": state["all_deleted"],
        "cross_file_duplicates":
            state["cross_file_duplicates"] or [],
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
                    "paired_with":
                        getattr(b, "paired_with", None),
                    "classified_by": b.classified_by,
                    "line_start": b.line_start,
                    "line_end": b.line_end,
                    "disposition":
                        _compute_block_disposition(b, records),
                }
                for b in blocks
            ],
        },
    }
    return manifest
