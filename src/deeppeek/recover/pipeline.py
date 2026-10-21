"""recover.pipeline — orchestration of passes 1-9.

Input is either a dump file on disk (dump_path) or a string already
in memory (dump_text) — the recover pipeline accepts both so callers
can choose whether the intermediate markdown lives on disk at all.
The manifest's `dump.path` field records "<in-memory>" when the
source was dump_text.

Output is either the standard `out_dir` layout (files/, files_low_trust/,
reports beside them) or — when `apply_to` is set — an in-place apply
against an existing directory: recovered files go directly into
apply_to, files marked [DEL] are deleted from it, and all reports are
written beside them.
"""
import hashlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .core import (
    DEFAULT_ESCALATE, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT, DEFAULT_LLM_URL,
)
from .llm import LLMClient
from .extract import pass1_extract, pass3_group
from .classify import pass2_classify
from .attribute import pass4_attribute_orphans
from .dedup import pass5_deduplicate
from .apply_patches import pass6_apply_patches
from .validate import pass7_cross_validate, pass8_verify
from .write import pass9_write


@dataclass
class RecoverConfig:
    # Exactly one of these must be set:
    dump_path: Optional[Path] = None
    dump_text: Optional[str] = None

    # Standard output directory (files/, files_low_trust/, reports).
    # Ignored when apply_to is set.
    out_dir: Path = field(default_factory=lambda: Path("./recovered"))

    # Apply mode: recovered files go directly into this existing
    # directory; deleted-marked files are removed from it; reports go
    # alongside.  Must exist and be a directory.
    apply_to: Optional[Path] = None

    layout: str = "flat"
    remap: dict = field(default_factory=dict)
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
    """Return (dump_text, dump_display, dump_size_bytes)."""
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
    """Return exit code on error, None on success."""
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


def run(cfg: RecoverConfig) -> int:
    try:
        dump_text, dump_display, dump_size = _resolve_dump(cfg)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 3

    err = _validate_apply_to(cfg)
    if err is not None:
        return err

    dump_sha = hashlib.sha256(dump_text.encode()).hexdigest()

    if cfg.apply_to is None:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        aux_base = cfg.out_dir
    else:
        # In apply mode we don't touch out_dir at all; ancillary data
        # (cache, llm_audit) goes under the target tree's hidden dir.
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
        print(f"[startup] apply mode: {cfg.apply_to}", file=sys.stderr)

    t_start = time.monotonic()

    print("[pass 1] extracting blocks (markdown AST)...")
    blocks, deleted_files = pass1_extract(dump_text)
    n_orphans = sum(1 for b in blocks if b.is_orphan)
    print(f"  {len(blocks)} fenced blocks ({n_orphans} orphans), "
          f"{len(deleted_files)} [DEL] markers")

    print(f"[pass 2] classifying blocks (mode={cfg.llm_mode}, "
          f"escalate_below={cfg.escalate_below})...")
    pass2_classify(blocks, llm, cfg.llm_mode, cfg.escalate_below,
                   cfg.llm_verbose)
    n_full = sum(1 for b in blocks if b.kind == "full")
    n_partial = sum(1 for b in blocks if b.kind == "partial")
    n_other = sum(1 for b in blocks if b.kind == "not_a_file")
    n_meta = sum(1 for b in blocks if b.kind == "recovered_meta")
    print(f"  full: {n_full}  partial: {n_partial}  "
          f"other: {n_other}  recovered-meta: {n_meta}")

    print("[pass 3] grouping by file...")
    records = pass3_group(blocks, deleted_files)
    print(f"  {len(records)} distinct files with headers")

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
    for r in records.values():
        state_counts[r.state] = state_counts.get(r.state, 0) + 1
    for st, n in sorted(state_counts.items()):
        print(f"  {st}: {n}")

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
        layout=cfg.layout,
        remap=cfg.remap,
        min_trust=cfg.min_trust,
        dump_display=dump_display,
        dump_size=dump_size,
        dump_sha=dump_sha,
        dump_text=dump_text,
        llm=llm,
        llm_mode=cfg.llm_mode,
        cross_validate=cross_validate,
        verbose=cfg.trace,
    )

    print(f"  total time: {time.monotonic() - t_start:.1f}s")
    return rc
