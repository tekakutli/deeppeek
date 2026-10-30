"""recover.pipeline — the 9-pass orchestrator.

Input is either a dump file on disk (dump_path) or a string already
in memory (dump_text).  The manifest records `<in-memory>` when the
source was dump_text.

Output is either the standard `out_dir` layout (files/,
files_low_trust/, reports beside them) or — when `apply_to` is set —
an in-place apply against an existing directory: recovered files go
directly into apply_to, files marked [DEL] are deleted from it, and
all reports are written into `apply_to/deeppeek/`.

PARTS
=====
  orchestration.py    RecoverConfig, pass-runner with timing hook,
                      dump I/O, apply_to validation.
  preexisting.py      pre-existing baseline loader, target-tree
                      directory scan.
  startup_check.py    import-failure + unresolved-name detection at
                      pipeline startup.  Skippable via
                      `--skip-import-check` for latency.
  equivalence.py      Pass 8.10 structural-equivalence check.

Everything below this docstring is `run()` — the sequential
invocation of passes 1-9 with the appropriate exception boundary,
timing, and reporting.

PRE-EXISTING BASELINES
======================
When `--apply-to` is provided, `preexisting.load_preexisting_blocks`
walks the target tree and synthesises one `Block` per source file
with `classified_by="preexisting"` and `kind="full"`.  Those
baselines are prepended to the conversation's blocks and act as the
base content for the matching records.

Earlier revisions tried to prune untouched pre-existing baselines
between pass 4 and pass 5 to save memory.  The pruning loop was
broken: it checked `rec.preexisting_base`, a field set by pass 5's
`pass5_deduplicate`, from code that runs *before* pass 5.  On the
first pass every pre-existing record has `preexisting_base=False`,
so `del records[path]` never fired — but the pruning of the
underlying `blocks` list did.  The records survived with empty
`source_blocks`, landed as `state=MISSING` in pass 5, and produced
one `[MISSING] no full print` warning per untouched pre-existing
file (hundreds in the 2026-10-29 run).

The pruning is now removed.  Untouched pre-existing files go
through passes 5–9 normally; `write.preexisting_unmodified` in
pass 9 identifies them and skips the write.  Total cost: one extra
`ast.parse`/memory copy per pre-existing file — a few hundred
milliseconds for a mid-size repo, versus a manifest that was ~95%
noise.

PER-PASS EXCEPTION BOUNDARIES
=============================
Every pass from 2.5 onward runs inside `_run_pass`, which catches
exceptions, logs them with a traceback to stderr, records them in
`run_errors`, and returns a safe default.

Pass 1 (parsing) and pass 3 (grouping) are NOT wrapped — a crash
there means the input is truly unusable and the right answer is to
abort.

`pass9_write` is wrapped in its own try/except so a bug in the
writer never produces a silent no-op.

PROFILE (2026-11-05)
====================
When `--profile` is set, `run()` records each pass's wall-clock time
into a local dict.  It prints the top 8 passes to stderr at end and
persists the full dict to `manifest.json` under
`run.pass_timings` (flows through `pass9_write(pass_timings=timings)`).

Pass 9 (write) is timed by the outer `try/finally` but is *not* in
the manifest's `pass_timings` (the manifest is written *by* pass 9,
before its own duration is known).  It appears in the `total time`
line and in the stderr profile printout.
"""

import hashlib
import sys
import time
import traceback
from typing import Optional

from .llm import LLMClient
from .extract import pass1_extract, pass3_group
from .classify import pass2_classify
from .resolve import resolve_paths
from .attribute import pass4_attribute_orphans
from .dedup import pass5_deduplicate
from .apply_patches import pass6_apply_patches
from .validate import (
    pass7_cross_validate, pass7_5_js_validate,
    pass7_6_demote_on_missing_export,
    pass7_6b_demote_on_missing_python_export,
    pass7_7_repair_js_imports,
    pass7_8_demote_on_any_finding,
)
from .verify import pass8_verify, pass8_5_check_symbols
from .reconcile import pass8_7_reconcile
from .reverify import reverify_reconciled
from .shield import pass8_8_shield_preexisting
from .write import pass9_write
from .topic import infer_topic_weights

# New pipeline structure
from .orchestration import (
    RecoverConfig,
    _run_pass, _run_pass_no_return,
    _resolve_dump, _validate_apply_to, _wipe_deeppeek_dir,
)
from .preexisting import scan_apply_to
from .startup_check import validate_pipeline_imports
from .equivalence import check_structural_equivalence
# Re-export the moved startup helpers under their pre-refactor names.
# These lived in this module before the 2026-11-05 split; tests and
# any out-of-tree callers import them from here.  Keep the aliases
# stable even as the modules they live in grow.
from .startup_check import (
    validate_pipeline_imports as _validate_pipeline_imports,
    _find_unresolved_module_names,
)
from .equivalence import (
    check_structural_equivalence as _check_structural_equivalence,
)

# Re-export RecoverConfig for `from deeppeek.recover.pipeline import RecoverConfig`.
__all__ = ["RecoverConfig", "run"]


def run(cfg: RecoverConfig) -> int:
    try:
        dump_text, dump_display, dump_size = _resolve_dump(cfg)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 3

    if cfg.dump_path is not None:
        try:
            from .last_conversation import save_last
            _saved = save_last(dump_text)
            if cfg.verbose:
                print(f"[last] saved conversation to {_saved}",
                      file=sys.stderr)
        except Exception as _e:
            print(f"  [!] could not save last conversation: {_e}",
                  file=sys.stderr)

    err = _validate_apply_to(cfg)
    if err is not None:
        return err

    layout = "header" if cfg.apply_to is not None else "flat"

    dump_sha = hashlib.sha256(dump_text.encode()).hexdigest()

    if cfg.apply_to is None:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        aux_base = cfg.out_dir
    else:
        aux_base = cfg.apply_to / "deeppeek"
        _wipe_deeppeek_dir(aux_base)
        aux_base.mkdir(parents=True, exist_ok=True)

    llm: Optional[LLMClient] = None
    if cfg.llm_mode != "off":
        cache = cfg.cache_dir or (aux_base / "cache" / "llm")
        llm = LLMClient(cfg.llm_url, cfg.llm_model, cache,
                        max_calls=cfg.llm_max_calls,
                        timeout=cfg.llm_timeout)
        if cfg.llm_ping:
            print(f"[startup] probing LLM at {cfg.llm_url} ...",
                  file=sys.stderr)
            if not llm.probe():
                print("  [startup] LLM unreachable — downgrading to off",
                      file=sys.stderr)
                llm = None
                cfg.llm_mode = "off"
            else:
                print("  [startup] LLM reachable", file=sys.stderr)

    if cfg.llm_mode == "off":
        print("[startup] LLM mode: off (0 calls will be made)",
              file=sys.stderr)
    else:
        print(f"[startup] LLM mode: {cfg.llm_mode} "
              f"(model={cfg.llm_model})", file=sys.stderr)

    if cfg.apply_to is not None:
        print(f"[startup] apply mode: {cfg.apply_to} "
              f"(layout={layout}, reports in "
              f"{cfg.apply_to / 'deeppeek'})",
              file=sys.stderr)
    else:
        print(f"[startup] layout: {layout}", file=sys.stderr)

    # Grammar availability diagnostic.
    try:
        from .ast_anchor import ast_status
        _ast = ast_status()
        _missing = [g for g in ("html", "css")
                    if not _ast.get(g)]
        if _missing:
            print(f"[startup] tree-sitter grammar missing: "
                  f"{', '.join(_missing)} — structural checks for "
                  f"embedded {', '.join(_missing).upper()} "
                  f"will fall back to line-based heuristics; install "
                  f"`tree-sitter-{'` and `tree-sitter-'.join(_missing)}` "
                  f"(or run .venv/bin/pip install -e '.[ast]')",
                  file=sys.stderr)
    except Exception:
        pass

    # Startup import sanity check.
    if not cfg.skip_import_check:
        _import_failures = validate_pipeline_imports()
        if _import_failures:
            print(f"[startup] {len(_import_failures)} pipeline "
                  f"module(s) failed to import — pipeline "
                  f"will not run:", file=sys.stderr)
            for _f in _import_failures:
                print(f"  {_f}", file=sys.stderr)
            print("[startup] fix the modules above, then re-run.",
                  file=sys.stderr)
            return 1
    else:
        print("[startup] --skip-import-check: skipping pipeline "
              "import validation", file=sys.stderr)

    t_start = time.monotonic()
    timings: Optional[dict] = {} if cfg.profile else None

    run_errors: list = []

    print("[pass 1] extracting blocks (markdown AST)...")
    blocks, deleted_files = pass1_extract(dump_text)
    n_orphans = sum(1 for b in blocks if b.is_orphan)
    print(f"  {len(blocks)} fenced blocks ({n_orphans} orphans), "
          f"{len(deleted_files)} [DEL] markers")

    existing_dirs: set = set()
    on_disk_names: set = set()
    if cfg.apply_to is not None:
        pre, existing_dirs, on_disk_names = scan_apply_to(cfg.apply_to)
        if pre:
            print(f"  + {len(pre)} pre-existing baseline file(s) "
                  f"from --apply-to", file=sys.stderr)
            blocks = pre + blocks
            for i, b in enumerate(blocks):
                b.idx = i

    print(f"[pass 2] classifying blocks (mode={cfg.llm_mode}, "
          f"escalate_below={cfg.escalate_below})...")
    _run_pass_no_return(
        "2 (classify)", pass2_classify, run_errors,
        blocks, llm, cfg.llm_mode, cfg.escalate_below, cfg.llm_verbose,
        timings=timings,
    )
    n_full = sum(1 for b in blocks if b.kind == "full")
    n_partial = sum(1 for b in blocks if b.kind == "partial")
    n_other = sum(1 for b in blocks if b.kind == "not_a_file")
    n_meta = sum(1 for b in blocks if b.kind == "recovered_meta")
    n_pre_full = sum(1 for b in blocks
                     if b.classified_by == "preexisting")
    print(f"  full: {n_full}  partial: {n_partial}  "
          f"other: {n_other}  recovered-meta: {n_meta}"
          + (f"  preexisting: {n_pre_full}" if n_pre_full else ""))

    topic_weights: dict = {}
    if cfg.apply_to is not None:
        print("[pass 2.1] inferring conversation topic weights...")
        topic_weights = _run_pass(
            "2.1 (topic)", infer_topic_weights, {},
            run_errors, dump_text, blocks, existing_dirs,
            timings=timings,
        ) or {}
        if topic_weights:
            _top = sorted(topic_weights.items(),
                          key=lambda kv: -kv[1])[:5]
            print(f"  {len(topic_weights)} candidate dir(s); top: "
                  + ", ".join(f"{d} ({w:.1f})" for d, w in _top))
        else:
            print("  no topic signal")

    print("[pass 2.5] resolving output paths...")
    resolutions = _run_pass(
        "2.5 (resolve)", resolve_paths, [],
        run_errors, blocks, cfg.apply_to, cfg.trace,
        topic_weights=topic_weights,
        timings=timings,
    )
    n_resolved = sum(1 for r in resolutions
                     if not r["reason"].startswith("unresolved"))
    n_unresolved = sum(1 for r in resolutions
                       if r["reason"].startswith("unresolved"))
    n_topic_aided = sum(1 for r in resolutions
                        if "topic-aided" in r.get("reason", ""))
    print(f"  {n_resolved} resolved, {n_unresolved} unresolved"
          + (f" ({n_topic_aided} topic-aided)" if n_topic_aided else ""))

    print("[pass 3] grouping by file...")
    records = pass3_group(blocks, deleted_files)
    n_pre_rec = sum(1 for r in records.values() if r.preexisting_base)
    print(f"  {len(records)} distinct files with headers"
          + (f" ({n_pre_rec} preexisting baselines picked up later)"
             if n_pre_rec else ""))

    if n_orphans:
        print(f"[pass 4] attributing {n_orphans} orphan fence(s)...")
        blocks_by_idx_all = {b.idx: b for b in blocks}
        _run_pass_no_return(
            "4 (attribute)", pass4_attribute_orphans, run_errors,
            blocks, blocks_by_idx_all, records, cfg.trace,
            topic_weights=topic_weights,
            timings=timings,
        )
        attributed = sum(1 for b in blocks if b.is_orphan and b.attributed_to)
        print(f"  {attributed}/{n_orphans} orphans attributed")

    print("[pass 5] deduplicating multi-print files...")
    blocks_by_idx = {b.idx: b for b in blocks}
    _run_pass_no_return(
        "5 (deduplicate)", pass5_deduplicate, run_errors,
        records, blocks_by_idx, cfg.trace,
        timings=timings,
    )
    state_counts = {}
    pre_base_count = 0
    for r in records.values():
        state_counts[r.state] = state_counts.get(r.state, 0) + 1
        if r.preexisting_base:
            pre_base_count += 1
    for st, n in sorted(state_counts.items()):
        print(f"  {st}: {n}")
    if pre_base_count:
        print(f"  preexisting_baseline: {pre_base_count}")

    print("[pass 6] applying patches...")
    _p6_errors_before = len(run_errors)
    _run_pass_no_return(
        "6 (apply)", pass6_apply_patches, run_errors,
        records, blocks_by_idx, llm,
        timings=timings,
    )
    if len(run_errors) > _p6_errors_before:
        _fc_count = 0
        for _fc_path, _fc_rec in records.items():
            if _fc_rec.state in ("SINGLE_FULL", "MULTI_FULL",
                                 "FULL_PLUS_PATCH"):
                if _fc_rec.trust == "SKIP":
                    _fc_rec.trust = "LOW"
                    _fc_rec.warnings.append(
                        "pass 6 crashed; record fail-closed defensively")
                    _fc_count += 1
        if _fc_count:
            print(f"  [!] pass 6 crashed; {_fc_count} record(s) "
                  f"fail-closed defensively so the target is "
                  f"preserved", file=sys.stderr)

    print("[pass 7] cross-validating imports...")
    cross_validate = _run_pass(
        "7 (cross-validate)", pass7_cross_validate,
        {"exports": {}, "imports": {}, "problems": []},
        run_errors, records, blocks_by_idx=blocks_by_idx,
        timings=timings,
    )
    xv = cross_validate.get("problems", [])
    if xv:
        print(f"  {len(xv)} import problem(s):")
        for p in xv[:10]:
            if p["type"] == "missing_module":
                print(f"    [{p['from']}] imports '{p['module']}' — "
                      f"not recovered")
            elif p["type"] == "missing_export":
                print(f"    [{p['from']}] imports '{p['name']}' from "
                      f"'{p['target']}' — not exported")
        if len(xv) > 10:
            print(f"    ... and {len(xv) - 10} more (see warnings.txt)")
    else:
        print("  clean")

    print("[pass 7.5] cross-validating JS imports/exports...")
    js_problems = _run_pass(
        "7.5 (js-validate)", pass7_5_js_validate, [],
        run_errors, records,
        timings=timings,
    ) or []
    if js_problems:
        cross_validate["problems"] = (
            cross_validate.get("problems", []) + js_problems)
        print(f"  {len(js_problems)} JS import problem(s):")
        for p in js_problems[:10]:
            if p["type"] == "missing_js_module":
                print(f"    [{p['from']}] imports '{p['module']}' — "
                      f"not recovered")
            elif p["type"] == "missing_js_export":
                print(f"    [{p['from']}] imports '{p['name']}' from "
                      f"'{p['target']}' — not exported")
        if len(js_problems) > 10:
            print(f"    ... and {len(js_problems) - 10} more")
    else:
        print("  clean")

    if js_problems:
        _repaired, _remaining = _run_pass(
            "7.7 (repair)", pass7_7_repair_js_imports,
            ([], js_problems),
            run_errors, records, js_problems, cfg.trace,
            timings=timings,
        ) or ([], js_problems)
        if _repaired:
            _repaired_probs = {id(r["problem"]) for r in _repaired}
            cross_validate["problems"] = [
                p for p in cross_validate.get("problems", [])
                if id(p) not in _repaired_probs
            ]
            cross_validate["repaired"] = [
                {"problem": r["problem"], "repair": r["repair"],
                 "target": r["target"]} for r in _repaired
            ]
            for r in _repaired:
                print(f"    [repair] {r['target']}: {r['repair']}",
                      file=sys.stderr)
            js_problems = _remaining
            print(f"  {len(_repaired)} JS problem(s) auto-repaired",
                  file=sys.stderr)

    if js_problems:
        n_export_demoted = _run_pass(
            "7.6 (missing-export)",
            pass7_6_demote_on_missing_export, 0,
            run_errors, records, js_problems, cfg.trace,
            timings=timings,
        ) or 0
        if n_export_demoted:
            print(f"  {n_export_demoted} record(s) demoted to LOW "
                  f"for missing JS export(s)", file=sys.stderr)

    if cross_validate.get("problems"):
        n_py_export_demoted = _run_pass(
            "7.6b (missing-python-export)",
            pass7_6b_demote_on_missing_python_export, 0,
            run_errors, records, cross_validate["problems"], cfg.trace,
            timings=timings,
        ) or 0
        if n_py_export_demoted:
            print(f"  {n_py_export_demoted} record(s) demoted to LOW "
                  f"for missing Python export(s)", file=sys.stderr)

    if cross_validate.get("problems"):
        n_universal_demoted = _run_pass(
            "7.8 (universal-demote)",
            pass7_8_demote_on_any_finding, 0,
            run_errors, records, cross_validate["problems"], cfg.trace,
            timings=timings,
        ) or 0
        if n_universal_demoted:
            print(f"  {n_universal_demoted} record-demotion(s) from "
                  f"the universal safety net", file=sys.stderr)

    print("[pass 8] verifying...")
    _run_pass_no_return(
        "8 (verify)", pass8_verify, run_errors,
        records, blocks_by_idx=blocks_by_idx,
        timings=timings,
    )

    print("[pass 8.5] checking symbol resolution...")
    _run_pass_no_return(
        "8.5 (symbols)", pass8_5_check_symbols, run_errors,
        records, blocks_by_idx=blocks_by_idx,
        timings=timings,
    )

    print("[pass 8.4] linting @command decorator args...")
    from .lint import pass8_4_lint_command_args
    n_lint_warned = _run_pass(
        "8.4 (lint)", pass8_4_lint_command_args, 0,
        run_errors, records,
        timings=timings,
    ) or 0
    if n_lint_warned:
        print(f"  {n_lint_warned} function(s) declared an unused "
              f"@command arg (see warnings.txt)", file=sys.stderr)
    else:
        print("  all declared args are read")

    print("[pass 8.4b] linting added parameters for dead forwarding...")
    from .lint import pass8_4b_check_added_unused_params
    n_param_warned = _run_pass(
        "8.4b (added-param)", pass8_4b_check_added_unused_params, 0,
        run_errors, records, blocks_by_idx,
        timings=timings,
    ) or 0
    if n_param_warned:
        print(f"  {n_param_warned} function(s) gained a parameter "
              f"never referenced in the body (see warnings.txt)",
              file=sys.stderr)
    else:
        print("  no added-parameter gaps")

    print("[pass 8.9] checking prose-promise coverage...")
    from .prose_promise import pass8_9_check_prose_promises
    n_promise_warned = _run_pass(
        "8.9 (prose-promise)", pass8_9_check_prose_promises, 0,
        run_errors, records,
        timings=timings,
    ) or 0
    if n_promise_warned:
        print(f"  {n_promise_warned} record(s) had prose-promise gaps "
              f"(see warnings.txt)", file=sys.stderr)
    else:
        print("  no promise gaps detected")

    print("[pass 8.7d] verifying already-present patches...")
    from .consistency import pass8_7d_verify_already_present
    n_already_unverified = _run_pass(
        "8.7d (verify-already-present)",
        pass8_7d_verify_already_present, 0,
        run_errors, records, blocks_by_idx,
        timings=timings,
    ) or 0
    if n_already_unverified:
        print(f"  {n_already_unverified} already-present claim(s) "
              f"contradicted by the file; marked failed",
              file=sys.stderr)
    else:
        print("  all already-present claims verified")

    print("[pass 8.7] reconciling fail-closed records...")
    n_reconciled = _run_pass(
        "8.7 (reconcile)", pass8_7_reconcile, 0,
        run_errors, records, blocks_by_idx, cfg.trace,
        timings=timings,
    ) or 0
    if n_reconciled:
        print(f"  {n_reconciled} record(s) reconciled "
              f"(excluded misattributed/unplaceable items)",
              file=sys.stderr)

    print("[pass 8.7b] re-verifying reconciled records...")
    n_reverified = _run_pass(
        "8.7b (re-verify)", reverify_reconciled, 0,
        run_errors, records, blocks_by_idx,
        timings=timings,
    ) or 0
    if n_reverified:
        print(f"  {n_reverified} reconciled record(s) re-demoted "
              f"by post-reconcile verification", file=sys.stderr)
    else:
        print("  no re-demotions")

    print("[pass 8.7c] verifying applied patches landed...")
    from .verify_applied import pass8_7c_verify_applied_patches
    n_verify_warned = _run_pass(
        "8.7c (verify-applied)", pass8_7c_verify_applied_patches, 0,
        run_errors, records, blocks_by_idx,
        timings=timings,
    ) or 0
    if n_verify_warned:
        print(f"  {n_verify_warned} patch(es) with missing content "
              f"lines (informational)", file=sys.stderr)
    else:
        print("  all applied patches present in final content")

    print("[pass 8.10] structural-equivalence check...")
    _structural_changes = _run_pass(
        "8.10 (structural-diff)", check_structural_equivalence, {},
        run_errors, records, blocks_by_idx,
        timings=timings,
    ) or {}
    _n_structural = sum(len(c) for c in _structural_changes.values())
    if _n_structural:
        print(f"  {_n_structural} structural divergence(s) introduced "
              f"by patches — demoting affected records to LOW",
              file=sys.stderr)
        for _p, _changes in _structural_changes.items():
            for _c in _changes[:3]:
                print(f"    {_p}: {_c.kind} {_c.signature!r} "
                      f"({_c.detail})", file=sys.stderr)
    else:
        print("  no unexpected structural divergences")

    if cfg.apply_to is not None and cfg.shield_preexisting:
        print("[pass 8.8] shielding target-tree overwrites...")
        n_shielded = _run_pass(
            "8.8 (shield)", pass8_8_shield_preexisting, 0,
            run_errors, records, cfg.apply_to, cfg.trace,
            timings=timings,
        ) or 0
        if n_shielded:
            print(f"  [shield] {n_shielded} record(s) failed closed "
                  f"to protect the target tree", file=sys.stderr)
        else:
            print("  no overwrites blocked")

    from .cross_file import find_cross_file_duplicates
    cross_file_dups = _run_pass(
        "8.6 (cross-file)", find_cross_file_duplicates, [],
        run_errors, records, blocks_by_idx,
        timings=timings,
    ) or []
    if cross_file_dups:
        print(f"  {len(cross_file_dups)} cross-file duplicate(s) "
              f"(see warnings.txt)")

    print("[pass 9] writing files...")
    _t9 = time.monotonic() if timings is not None else 0.0
    try:
        rc = pass9_write(
            cross_file_duplicates=cross_file_dups,
            records=records,
            blocks=blocks,
            deleted_files=deleted_files,
            out_dir=cfg.out_dir,
            apply_to=cfg.apply_to,
            layout=layout,
            min_trust=cfg.min_trust,
            dump_display=dump_display,
            dump_size=dump_size,
            dump_sha=dump_sha,
            dump_text=dump_text,
            llm=llm,
            llm_mode=cfg.llm_mode,
            cross_validate=cross_validate,
            resolutions=resolutions,
            verbose=cfg.trace,
            run_errors=run_errors,
            pass_timings=timings,
            on_disk_names=on_disk_names,
        )
    except Exception as e:
        print(f"  [!] pass 9 (write) raised: {type(e).__name__}: {e}",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        if cfg.apply_to is not None:
            print(f"  [!] target tree untouched.  Reports may be "
                  f"incomplete in {cfg.apply_to / 'deeppeek'}/",
                  file=sys.stderr)
        else:
            print(f"  [!] files may be partially written to "
                  f"{cfg.out_dir}/", file=sys.stderr)
        return 1
    finally:
        if timings is not None:
            timings["9 (write)"] = time.monotonic() - _t9

    _residual_paths_for_stdout: Optional[list] = None
    try:
        from .residual import emit_residual, unique_residual_paths
        _residual_path = cfg.residual_path
        if _residual_path is None:
            _residual_path = (
                cfg.apply_to / "deeppeek" / "residual.md"
                if cfg.apply_to is not None
                else cfg.out_dir / "residual.md"
            )
        _residual_content = emit_residual(
            records=records,
            blocks=blocks,
            dump_display=dump_display,
            min_trust=cfg.min_trust,
            out_path=_residual_path,
            verbose=cfg.trace,
        )
        if str(_residual_path) != "-":
            _residual_paths_for_stdout = unique_residual_paths(
                _residual_content or "")
    except Exception as e:
        print(f"  [!] residual emission failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)

    _total = time.monotonic() - t_start

    if timings is not None:
        print("[profile] top passes:", file=sys.stderr)
        for _name, _secs in sorted(
                timings.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {_secs:6.2f}s  {_name}", file=sys.stderr)
        _covered = sum(timings.values())
        print(f"  {_covered:6.2f}s  (sum of timed passes; "
              f"total wall {_total:.2f}s)", file=sys.stderr)

        try:
            from ._ast import parse_cache_stats
            _cache = parse_cache_stats()
            _calls = _cache["hits"] + _cache["misses"]
            _ratio = (_cache["hits"] / _calls * 100) if _calls else 0.0
            print(f"[profile] parse cache: {_cache['hits']} hits, "
                  f"{_cache['misses']} misses ({_ratio:.0f}% hit), "
                  f"{_cache['size']}/{_cache['maxsize']} entries",
                  file=sys.stderr)
        except Exception:
            pass

    print(f"  total time: {_total:.1f}s")
    if run_errors:
        print(f"  [!] {len(run_errors)} pass(es) crashed — see "
              f"warnings.txt for the traceback",
              file=sys.stderr)

    if _residual_paths_for_stdout is not None:
        print("=== files not fully recovered (see residual.md) ===")
        for _p in _residual_paths_for_stdout:
            print(_p)
        print(f"=== {len(_residual_paths_for_stdout)} file(s) "
              f"not fully recovered ===")

    return rc
