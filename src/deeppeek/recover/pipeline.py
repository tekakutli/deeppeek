"""recover.pipeline — orchestration of passes 1-9.

This is the old `main()` body, decoupled from argparse.  A caller
builds a RecoverConfig and calls run(); the CLI adapter in
commands/recover.py is the only expected caller, but tests may
call this directly.
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
    dump_path: Path
    out_dir: Path
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


def run(cfg: RecoverConfig) -> int:
    if not cfg.dump_path.exists():
        print(f"error: dump not found: {cfg.dump_path}", file=sys.stderr)
        return 3

    dump_text = cfg.dump_path.read_text()
    dump_sha = hashlib.sha256(dump_text.encode()).hexdigest()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)

    llm: Optional[LLMClient] = None
    if cfg.llm_mode != "off":
        cache = cfg.cache_dir or (cfg.out_dir / "cache" / "llm")
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
    rc = pass9_write(records, blocks, deleted_files, cfg.out_dir,
                     cfg.layout, cfg.remap, cfg.min_trust, cfg.dump_path,
                     dump_sha, dump_text, llm, cfg.llm_mode,
                     cross_validate, cfg.trace)

    print(f"  total time: {time.monotonic() - t_start:.1f}s")
    return rc
