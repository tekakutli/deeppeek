"""recover.pipeline — orchestration of passes 1-9.

Input is either a dump file on disk (dump_path) or a string already
in memory (dump_text).  The manifest records `<in-memory>` when the
source was dump_text.

Output is either the standard `out_dir` layout (files/,
files_low_trust/, reports beside them) or — when `apply_to` is set —
an in-place apply against an existing directory: recovered files go
directly into apply_to, files marked [DEL] are deleted from it, and
all reports are written beside them.

LAYOUT (derived, not user-set)
==============================
The effective output layout is derived:

    --apply-to set   → "header"   (preserve the target tree)
    neither          → "flat"     (basenames only)

PREE-XISTING BASELINES + PATH RESOLUTION (--apply-to only)
==========================================================
When `--apply-to DIR` is set:

  1. `_load_preexisting_blocks` scans DIR and creates one synthetic
     full-print block per source file, with canonical_path and
     output_path both = the real relative path from DIR.

  2. `resolve_paths` (in `recover.resolve`) then walks the
     conversation blocks and tries to match each `header_path`
     against the pre-existing file index.  It handles the case the
     user actually sees: the conversation uses "recover/pipeline.py"
     in one message, "pipeline.py" in another, and
     "tests/recover/test_recover.py" in a third — the resolver
     recognises all three as pointing to specific real files.  When
     two pre-existing files share a basename (multiple `__init__.py`,
     multiple `pipeline.py` in different subpackages), the resolver
     scores candidates by content similarity and context hints, and
     commits only when a clear winner exists.

  3. `pass3_group` then groups records by `output_path`, so a
     conversation block and its matching pre-existing baseline end
     up in the same FileRecord.

  4. Pass 9 writes to `apply_to / output_path`, i.e. to the real
     subdirectory.  And the pre-existing-baseline skip ensures files
     the conversation didn't touch are left alone.
"""
import hashlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
from .validate import pass7_cross_validate, pass8_verify
from .write import pass9_write


@dataclass
class RecoverConfig:
    dump_path: Optional[Path] = None
    dump_text: Optional[str] = None
    out_dir: Path = field(default_factory=lambda: Path("./recovered"))
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
        print("  (create it first, or omit --apply-to to use ./recovered/)",
              file=sys.stderr)
        return 3
    if not cfg.apply_to.is_dir():
        print(f"error: --apply-to path is not a directory: {cfg.apply_to}",
              file=sys.stderr)
        return 3
    return None


# ---------------------------------------------------------------------------
# Pre-existing baseline loader
# ---------------------------------------------------------------------------

_PREEXISTING_EXCLUDE_DIRS = {
    "node_modules", "__pycache__", "dist", "build", "target",
    "venv", ".venv", ".tox", ".nox", ".git",
}
_PREEXISTING_MAX_FILE_SIZE = 1_048_576  # 1 MiB


def _load_preexisting_blocks(apply_to: Path) -> list[Block]:
    """Scan `apply_to` and return one synthetic 'full print' block per
    source-like file, ready to be prepended to the conversation's
    blocks.

    Each block carries its real relative path in both canonical_path
    and output_path — this is the identity used for grouping and the
    target used for writing.
    """
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

    err = _validate_apply_to(cfg)
    if err is not None:
        return err

    layout = "header" if cfg.apply_to is not None else "flat"

    dump_sha = hashlib.sha256(dump_text.encode()).hexdigest()

    if cfg.apply_to is None:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        aux_base = cfg.out_dir
    else:
        aux_base = cfg.apply_to / ".deeppeek"
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
        print(f"[startup] LLM mode: {cfg.llm_mode} (model={cfg.llm_model})",
              file=sys.stderr)

    if cfg.apply_to is not None:
        print(f"[startup] apply mode: {cfg.apply_to} "
              f"(layout={layout})", file=sys.stderr)
    else:
        print(f"[startup] layout: {layout}", file=sys.stderr)

    t_start = time.monotonic()

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
    pass2_classify(blocks, llm, cfg.llm_mode, cfg.escalate_below,
                   cfg.llm_verbose)
    n_full = sum(1 for b in blocks if b.kind == "full")
    n_partial = sum(1 for b in blocks if b.kind == "partial")
    n_other = sum(1 for b in blocks if b.kind == "not_a_file")
    n_meta = sum(1 for b in blocks if b.kind == "recovered_meta")
    n_pre_full = sum(1 for b in blocks
                     if b.classified_by == "preexisting")
    print(f"  full: {n_full}  partial: {n_partial}  "
          f"other: {n_other}  recovered-meta: {n_meta}"
          + (f"  preexisting: {n_pre_full}" if n_pre_full else ""))

    # ---- NEW: resolve each block's output path ----
    print("[pass 2.5] resolving output paths...")
    resolutions = resolve_paths(blocks, cfg.apply_to, cfg.trace)
    n_resolved = sum(1 for r in resolutions
                     if not r["reason"].startswith("unresolved"))
    n_unresolved = sum(1 for r in resolutions
                       if r["reason"].startswith("unresolved"))
    print(f"  {n_resolved} resolved, {n_unresolved} unresolved")

    print("[pass 3] grouping by file...")
    records = pass3_group(blocks, deleted_files)
    n_pre_rec = sum(1 for r in records.values() if r.preexisting_base)
    print(f"  {len(records)} distinct files with headers"
          + (f" ({n_pre_rec} preexisting baselines picked up later)"
             if n_pre_rec else ""))

    if n_orphans:
        print(f"[pass 4] attributing {n_orphans} orphan fence(s)...")
        blocks_by_idx_all = {b.idx: b for b in blocks}
        pass4_attribute_orphans(blocks, blocks_by_idx_all, records, cfg.trace)
        attributed = sum(1 for b in blocks if b.is_orphan and b.attributed_to)
        print(f"  {attributed}/{n_orphans} orphans attributed")

    print("[pass 5] deduplicating multi-print files...")
    blocks_by_idx = {b.idx: b for b in blocks}
    pass5_deduplicate(records, blocks_by_idx, cfg.trace)
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
    pass6_apply_patches(records, blocks_by_idx, llm)

    print("[pass 7] cross-validating imports...")
    cross_validate = pass7_cross_validate(records)
    xv = cross_validate.get("problems", [])
    if xv:
        print(f"  {len(xv)} import problem(s):")
        for p in xv[:10]:
            if p["type"] == "missing_module":
                print(f"    [{p['from']}] imports '{p['module']}' — not recovered")
            elif p["type"] == "missing_export":
                print(f"    [{p['from']}] imports '{p['name']}' from "
                      f"'{p['target']}' — not exported")
        if len(xv) > 10:
            print(f"    ... and {len(xv) - 10} more (see warnings.txt)")
    else:
        print("  clean")

    print("[pass 8] verifying...")
    pass8_verify(records)

    print("[pass 9] writing files...")
    rc = pass9_write(
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
    )

    print(f"  total time: {time.monotonic() - t_start:.1f}s")
    return rc
