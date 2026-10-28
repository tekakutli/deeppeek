"""recover.pipeline — orchestration of passes 1-9.

Input is either a dump file on disk (dump_path) or a string already
in memory (dump_text).  The manifest records `<in-memory>` when the
source was dump_text.

Output is either the standard `out_dir` layout (files/,
files_low_trust/, reports beside them) or — when `apply_to` is set —
an in-place apply against an existing directory: recovered files go
directly into apply_to, files marked [DEL] are deleted from it, and
all reports are written into `apply_to/deeppeek/`.

LAST-CONVERSATION SAVE
======================
When the input came from a file (cfg.dump_path is not None), the
dump text is saved to `<cwd>/.deeppeek/last.md` before recovery
begins, so `deeppeek restore` can copy it back later.  The
`.deeppeek/` directory is self-gitignored (it contains its own
`.gitignore` with `*`).  Only the most recent dump is kept — each
run overwrites the previous one.  In-memory inputs (`capture`
without `-o`) do not trigger the save: there is nothing on disk to
round-trip.  See `recover/last_conversation.py`.

ROBUSTNESS: PER-PASS EXCEPTION BOUNDARIES
=========================================
Every pass from 2.5 onward runs inside `_run_pass`, which catches
exceptions, logs them with a traceback to stderr, records them in
`run_errors`, and returns a safe default.  The consequence:

  · A crash in ANY pass does not abort the pipeline.
  · `pass9_write` ALWAYS runs when `records` is non-empty.
  · The manifest ALWAYS gets written, with the crashed pass named
    in `run.errors` and `warnings.txt`.
  · Files that were successfully recovered before the crash still
    land on disk (or in `low_trust/`).

`pass1_extract` (parsing) and `pass3_group` (grouping) are NOT
wrapped — a crash there means the input is truly unusable, and the
right answer is to abort with a clear error, not to write an empty
manifest.  These two passes are also the simplest in the pipeline;
their absence of exception handling is a deliberate trade-off, not
an oversight.

`pass9_write` itself is wrapped in a top-level try/except that
catches a crash and prints a message, so a bug in the writer never
produces a silent no-op.

CROSS-VALIDATE TRUST DEMOTION (FIX 2)
=====================================
Pass 7 (Python imports/exports) and pass 7.5 (JS imports/exports)
both emit problems into `run.cross_validate.problems`.  Historically
those findings were informational: they set the exit code and
appeared in the manifest, but the affected records were still
written at their original trust.  That is how a broken
`pg_state.py` (declared `_rotateMask` but didn't export it, while
`pg_masks.py` imported it) got written at HIGH trust and blanked
the playground on load.

Pass 7.7 (`pass7_7_repair_js_imports`) attempts a mechanical repair
first: when the target declares the symbol but does not export it,
it rewrites the declaration to add `export`.  If repair succeeds
the problem is removed from the list and the target stays at its
original trust.

Pass 7.6 (`pass7_6_demote_on_missing_export`) is the fallback: it
demotes both sides of an unrepairable finding so the target file
isn't written broken.  Under default `--min-trust MEDIUM` neither
is written; the target tree is preserved and `warnings.txt` tells
the user exactly which `export` is missing.

Pass 7.6b (NEW) does the same for Python exports.  The
`comfyui_krita.py` failure in the wild: `handlers/comfyui.py`
imports `build_krita_resources` from a target that was truncated
to two functions; both records shipped at HIGH trust and the
daemon crashed on reload.  Pass 7.6b demotes both sides so the
target tree survives intact and the manifest names the missing
symbol.

Pass 7.8 (NEW) is the universal safety net.  Any `missing_export`
or `missing_js_export` finding that survives the specific passes
above still demotes both sides.  When cross-validate gains a new
finding type in a future change, 7.8 is the pass that makes it a
trust signal even if no specific handler was added.

SHIELD (PASS 8.8, NEW)
======================
Even after dedup, cross-validate demotion, and reconcile, a bug in
any of those layers could let a record through whose content is
provably incomplete.  Pass 8.8 compares the recovered content
against what is *actually on disk* in the target tree, and refuses
to write a file that would delete substantive top-level
declarations.  The `comfyui_krita.py` truncation is the motivating
case — twenty of twenty-two top-level functions would have been
deleted.  See `shield.py` for the full account.

RE-VERIFY RECONCILED (PASS 8.7b, 2026-10-28b)
=============================================
The verify-reconcile-verify invariant:

    every pass that can demote a record must be re-run after every
    pass that can promote it, on the promoted content, before the
    promotion is committed.

Pass 8.4 (lint) and pass 8.5 (symbols) both demote.  Pass 8.7
(reconcile) promotes.  Reconcile's own replay is now lint-aware
(see `reconcile.py`), so the specific `--output_rotation` case is
caught inside 8.7.  But that fix relies on `_count_failures` being
kept in sync with every demoting pass; adding a future demoting
pass would require updating that function.  Pass 8.7b makes the
invariant mechanical: `reverify.reverify_reconciled` re-runs every
demoting pass on every promoted record.  A reconciled record that
still fails a demoting check is demoted again; it does not ship.

RESIDUAL EMISSION
=================
After pass 9 writes its output and returns cleanly, a residual
markdown file is emitted alongside the manifest: a smaller
`conversation.md` containing only the blocks that did NOT reach a
successful placement.  The residual is intended as the input to a
subsequent `deeppeek recover` run once the user has edited the
failed blocks (or the target).  It never affects the primary run's
output — it is purely additive.  See `residual.py`.

CRASH REPORTING
===============
A crashed pass reports:

    [!] pass 2.5 (resolve) raised: <one-line error>
    <traceback>
    [!] continuing with a safe default; downstream passes will see
        the pre-pass state

The safe defaults per pass:

    pass 2.5   → [] (no resolutions)
    pass 4     → (no-op; orphans stay unattributed)
    pass 5     → (no-op; records keep whatever state pass 3 gave them)
    pass 6     → records keep base content (no patches applied)
    pass 7     → {"exports": {}, "imports": {}, "problems": []}
    pass 7.5   → [] (no JS problems)
    pass 7.6   → 0 (no demotions)
    pass 7.6b  → 0 (no demotions)
    pass 7.7   → (original problem list unchanged)
    pass 7.8   → 0 (no demotions)
    pass 8     → (no-op; no per-file verification)
    pass 8.5   → (no-op; no symbol demotions)
    pass 8.7   → 0 (no reconciliations)
    pass 8.7b  → 0 (no re-demotions)
    pass 8.8   → 0 (no shield demotions)
    pass 9     → caught by the writer's own try/except

These defaults are conservative: a crashed pass cannot accidentally
make the result WORSE than "do nothing for that pass".
"""
import hashlib
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .core import (
    Block,
    DEFAULT_ESCALATE, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT, DEFAULT_LLM_URL,
    SOURCE_EXTENSIONS,
)
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
    pass8_verify, pass8_5_check_symbols,
)
from .reconcile import pass8_7_reconcile
from .reverify import reverify_reconciled
from .shield import pass8_8_shield_preexisting
from .write import pass9_write


@dataclass
class RecoverConfig:
    dump_path: Optional[Path] = None
    dump_text: Optional[str] = None
    out_dir: Path = field(default_factory=lambda: Path("./deeppeek_recovered"))
    apply_to: Optional[Path] = None
    min_trust: str = "MEDIUM"
    llm_mode: str = "off"
    llm_url: str = DEFAULT_LLM_URL
    llm_model: str = DEFAULT_LLM_MODEL
    llm_timeout: int = DEFAULT_LLM_TIMEOUT
    escalate_below: float = DEFAULT_ESCALATE
    llm_max_calls: Optional[int] = None
    llm_ping: bool = True
    cache_dir: Optional[Path] = None
    verbose: bool = False
    trace: bool = False
    llm_verbose: bool = False
    # Residual output.  None → reports_dir/residual.md (the default
    # location, next to manifest.json and warnings.txt).  Path("-") →
    # stdout, for shell pipelines.  Any other path → that file.
    residual_path: Optional[Path] = None
    # Pass 8.8 shield: refuse to overwrite a target-tree file with
    # recovered content that would delete substantive top-level
    # declarations.  On by default; escape hatch is `--min-trust LOW`
    # (which lets LOW records write), or explicit
    # shield_preexisting=False for programmatic callers.
    shield_preexisting: bool = True


# ---------------------------------------------------------------------------
# Per-pass exception boundary
# ---------------------------------------------------------------------------

def _run_pass(pass_name: str,
              pass_fn: Callable,
              default,
              errors: list,
              *args,
              **kwargs):
    """Run `pass_fn(*args, **kwargs)`.  If it raises, log a traceback
    to stderr, record the error in `errors`, and return `default`.

    The whole point of this wrapper: a pass that crashes on one
    pathological input must not stop the pipeline from writing the
    files it has already decided on.  See the module docstring.
    """
    try:
        return pass_fn(*args, **kwargs)
    except Exception as e:
        msg = f"pass {pass_name} raised: {type(e).__name__}: {e}"
        print(f"  [!] {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print(f"  [!] continuing with a safe default; downstream "
              f"passes will see the pre-pass state", file=sys.stderr)
        errors.append({
            "pass": pass_name,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })
        return default


def _run_pass_no_return(pass_name: str,
                        pass_fn: Callable,
                        errors: list,
                        *args,
                        **kwargs) -> None:
    """Same as `_run_pass` but for no-op passes (mutate in place).
    Records the crash and returns None; no default needed."""
    try:
        pass_fn(*args, **kwargs)
    except Exception as e:
        msg = f"pass {pass_name} raised: {type(e).__name__}: {e}"
        print(f"  [!] {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print(f"  [!] continuing with a safe default; downstream "
              f"passes will see the pre-pass state", file=sys.stderr)
        errors.append({
            "pass": pass_name,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_dump(cfg: RecoverConfig) -> tuple[str, str, int]:
    if cfg.dump_text is not None:
        text = cfg.dump_text
        return text, "<in-memory>", len(text.encode("utf-8"))
    if cfg.dump_path is None:
        raise SystemExit(
            "error: RecoverConfig needs either dump_path or dump_text"
        )
    if not cfg.dump_path.exists():
        raise SystemExit(f"error: dump not found: {cfg.dump_path}")
    text = cfg.dump_path.read_text()
    return text, str(cfg.dump_path), cfg.dump_path.stat().st_size


def _validate_apply_to(cfg: RecoverConfig) -> Optional[int]:
    if cfg.apply_to is None:
        return None
    if not cfg.apply_to.exists():
        print(f"error: --apply-to directory does not exist: {cfg.apply_to}",
              file=sys.stderr)
        print("  (create it first, or omit --apply-to to use "
              "./deeppeek_recovered/)",
              file=sys.stderr)
        return 3
    if not cfg.apply_to.is_dir():
        print(f"error: --apply-to path is not a directory: {cfg.apply_to}",
              file=sys.stderr)
        return 3
    return None


def _wipe_deeppeek_dir(aux_base: Path) -> None:
    """Empty the reports directory, preserving `cache/`."""
    if not aux_base.exists():
        return
    for child in aux_base.iterdir():
        if child.name == "cache":
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pre-existing baseline loader
# ---------------------------------------------------------------------------

_PREEXISTING_EXCLUDE_DIRS = {
    "node_modules", "__pycache__", "dist", "build", "target",
    "venv", ".venv", ".tox", ".nox", ".git",
    "deeppeek",
    ".deeppeek",
}
_PREEXISTING_MAX_FILE_SIZE = 1_048_576  # 1 MiB


def _load_preexisting_blocks(apply_to: Path) -> list[Block]:
    """Scan `apply_to` and return one synthetic 'full print' block per
    source-like file, ready to be prepended to the conversation's
    blocks."""
    blocks: list[Block] = []
    if not apply_to.is_dir():
        return blocks

    for p in sorted(apply_to.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(apply_to)
        rel_parts = rel.parts

        if any(part.startswith(".") for part in rel_parts):
            continue
        if any(part in _PREEXISTING_EXCLUDE_DIRS for part in rel_parts):
            continue
        if p.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            if p.stat().st_size > _PREEXISTING_MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        try:
            raw = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        body = raw.replace("\r\n", "\n")
        if body.endswith("\n"):
            body = body[:-1]

        rel_str = rel.as_posix()
        blocks.append(Block(
            idx=0,
            header_path=rel_str,
            modifier=None,
            fence_lang="",
            body=body,
            body_line_count=body.count("\n") + 1,
            context_before="",
            line_start=-1,
            line_end=-1,
            is_orphan=False,
            is_code_file=True,
            kind="full",
            canonical_path=rel_str,
            output_path=rel_str,
            confidence=1.0,
            classified_by="preexisting",
            heuristic_kind="full",
        ))
    return blocks


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

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

    t_start = time.monotonic()
    run_errors: list = []

    print("[pass 1] extracting blocks (markdown AST)...")
    blocks, deleted_files = pass1_extract(dump_text)
    n_orphans = sum(1 for b in blocks if b.is_orphan)
    print(f"  {len(blocks)} fenced blocks ({n_orphans} orphans), "
          f"{len(deleted_files)} [DEL] markers")

    if cfg.apply_to is not None:
        pre = _load_preexisting_blocks(cfg.apply_to)
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

    print("[pass 2.5] resolving output paths...")
    resolutions = _run_pass(
        "2.5 (resolve)", resolve_paths, [],
        run_errors, blocks, cfg.apply_to, cfg.trace,
    )
    n_resolved = sum(1 for r in resolutions
                     if not r["reason"].startswith("unresolved"))
    n_unresolved = sum(1 for r in resolutions
                       if r["reason"].startswith("unresolved"))
    print(f"  {n_resolved} resolved, {n_unresolved} unresolved")

    if cfg.apply_to is not None:
        touched = {b.output_path for b in blocks
                   if b.classified_by != "preexisting" and b.output_path}
        before = len(blocks)
        blocks = [b for b in blocks
                  if b.classified_by != "preexisting"
                  or b.output_path in touched]
        for i, b in enumerate(blocks):
            b.idx = i
        n_pruned = before - len(blocks)
        if n_pruned:
            print(f"  pruned {n_pruned} untouched pre-existing "
                  f"baseline(s)", file=sys.stderr)

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
        )
        attributed = sum(1 for b in blocks if b.is_orphan and b.attributed_to)
        print(f"  {attributed}/{n_orphans} orphans attributed")

    print("[pass 5] deduplicating multi-print files...")
    blocks_by_idx = {b.idx: b for b in blocks}
    _run_pass_no_return(
        "5 (deduplicate)", pass5_deduplicate, run_errors,
        records, blocks_by_idx, cfg.trace,
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
        run_errors, records,
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

    # ---- pass 7.7 (wrapped) — repair JS export findings ----
    if js_problems:
        _repaired, _remaining = _run_pass(
            "7.7 (repair)", pass7_7_repair_js_imports,
            ([], js_problems),
            run_errors, records, js_problems, cfg.trace,
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

    # ---- pass 7.6 (wrapped) — demote on missing JS export ----
    if js_problems:
        n_export_demoted = _run_pass(
            "7.6 (missing-export)",
            pass7_6_demote_on_missing_export, 0,
            run_errors, records, js_problems, cfg.trace,
        ) or 0
        if n_export_demoted:
            print(f"  {n_export_demoted} record(s) demoted to LOW "
                  f"for missing JS export(s)", file=sys.stderr)

    # ---- pass 7.6b (wrapped) — demote on missing Python export ----
    if cross_validate.get("problems"):
        n_py_export_demoted = _run_pass(
            "7.6b (missing-python-export)",
            pass7_6b_demote_on_missing_python_export, 0,
            run_errors, records, cross_validate["problems"], cfg.trace,
        ) or 0
        if n_py_export_demoted:
            print(f"  {n_py_export_demoted} record(s) demoted to LOW "
                  f"for missing Python export(s)", file=sys.stderr)

    # ---- pass 7.8 (wrapped) — universal finding demotion ----
    if cross_validate.get("problems"):
        n_universal_demoted = _run_pass(
            "7.8 (universal-demote)",
            pass7_8_demote_on_any_finding, 0,
            run_errors, records, cross_validate["problems"], cfg.trace,
        ) or 0
        if n_universal_demoted:
            print(f"  {n_universal_demoted} record-demotion(s) from "
                  f"the universal safety net", file=sys.stderr)

    print("[pass 8] verifying...")
    _run_pass_no_return(
        "8 (verify)", pass8_verify, run_errors,
        records, blocks_by_idx=blocks_by_idx,
    )

    print("[pass 8.5] checking symbol resolution...")
    _run_pass_no_return(
        "8.5 (symbols)", pass8_5_check_symbols, run_errors,
        records, blocks_by_idx=blocks_by_idx,
    )

    print("[pass 8.4] linting @command decorator args...")
    from .lint import pass8_4_lint_command_args
    n_lint_demoted = _run_pass(
        "8.4 (lint)", pass8_4_lint_command_args, 0,
        run_errors, records,
    ) or 0
    if n_lint_demoted:
        print(f"  {n_lint_demoted} record(s) demoted to LOW for "
              f"unused @command args", file=sys.stderr)

    print("[pass 8.7] reconciling fail-closed records...")
    n_reconciled = _run_pass(
        "8.7 (reconcile)", pass8_7_reconcile, 0,
        run_errors, records, blocks_by_idx, cfg.trace,
    ) or 0
    if n_reconciled:
        print(f"  {n_reconciled} record(s) reconciled "
              f"(excluded misattributed/unplaceable items)",
              file=sys.stderr)

    # ---- pass 8.7b (NEW) — re-verify reconciled records ----
    # See the module docstring, RE-VERIFY RECONCILED section, and
    # `reverify.py`.  Runs immediately after reconcile so a promotion
    # cannot commit content that a demoting pass would still reject.
    print("[pass 8.7b] re-verifying reconciled records...")
    n_reverified = _run_pass(
        "8.7b (re-verify)", reverify_reconciled, 0,
        run_errors, records, blocks_by_idx,
    ) or 0
    if n_reverified:
        print(f"  {n_reverified} reconciled record(s) re-demoted "
              f"by post-reconcile verification", file=sys.stderr)
    else:
        print("  no re-demotions")

    # ---- pass 8.8 (wrapped) — shield pre-existing overwrites ----
    if cfg.apply_to is not None and cfg.shield_preexisting:
        print("[pass 8.8] shielding target-tree overwrites...")
        n_shielded = _run_pass(
            "8.8 (shield)", pass8_8_shield_preexisting, 0,
            run_errors, records, cfg.apply_to, cfg.trace,
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
    ) or []
    if cross_file_dups:
        print(f"  {len(cross_file_dups)} cross-file duplicate(s) "
              f"(see warnings.txt)")

    print("[pass 9] writing files...")
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

    try:
        from .residual import emit_residual
        _residual_path = cfg.residual_path
        if _residual_path is None:
            _residual_path = (
                cfg.apply_to / "deeppeek" / "residual.md"
                if cfg.apply_to is not None
                else cfg.out_dir / "residual.md"
            )
        emit_residual(
            records=records,
            blocks=blocks,
            dump_display=dump_display,
            min_trust=cfg.min_trust,
            out_path=_residual_path,
            verbose=cfg.trace,
        )
    except Exception as e:
        print(f"  [!] residual emission failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)

    print(f"  total time: {time.monotonic() - t_start:.1f}s")
    if run_errors:
        print(f"  [!] {len(run_errors)} pass(es) crashed — see "
              f"warnings.txt for the traceback",
              file=sys.stderr)
    return rc
