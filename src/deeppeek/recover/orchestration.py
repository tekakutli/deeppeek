"""recover.orchestration — RecoverConfig, per-pass exception boundary,
and dump/apply_to I/O helpers.

STABLE.  Nothing here changes when pass logic changes.

The exception boundary (`_run_pass` / `_run_pass_no_return`) is the
single place every pass is wrapped so one pass crashing on one
pathological input does not stop the pipeline from writing the files
it has already decided on.

Config (`RecoverConfig`) is a plain dataclass; adding a new flag is
a one-line change that never touches the pass logic.

Dump I/O (`_resolve_dump`, `_validate_apply_to`, `_wipe_deeppeek_dir`)
is pure filesystem plumbing.

TIMING HOOK (2026-11-05)
========================
Both pass-runner helpers accept an optional `timings` dict.  When
provided, they record the wall-clock duration of each pass into
`timings[pass_name]` (accumulating, so a pass wrapped twice reports
the total).  `run()` builds this dict when `--profile` is set and
persists it in the manifest via `pass9_write(pass_timings=timings)`.

When `timings is None` the helpers are a straight-through
try/except — no measured overhead beyond the `is None` check.

The `timings` kwarg is reserved by the orchestration layer.  A pass
function that legitimately wants to receive its own `timings` dict
must be renamed at the pass-runner call site; no current pass does.
"""

import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .core import (
    DEFAULT_ESCALATE, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT,
    DEFAULT_LLM_URL,
)


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
    residual_path: Optional[Path] = None
    shield_preexisting: bool = True
    profile: bool = False
    skip_import_check: bool = False


def _run_pass(pass_name: str,
              pass_fn: Callable,
              default,
              errors: list,
              *args,
              timings: Optional[dict] = None,   # orchestration-reserved
              **kwargs):
    """Run `pass_fn(*args, **kwargs)`.  If it raises, log a traceback
    to stderr, record the error in `errors`, and return `default`.

    When `timings` is provided, record wall-clock duration into
    `timings[pass_name]` (accumulating across multiple calls).
    """
    t0 = time.monotonic() if timings is not None else 0.0
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
    finally:
        if timings is not None:
            timings[pass_name] = timings.get(pass_name, 0.0) + (
                time.monotonic() - t0)


def _run_pass_no_return(pass_name: str,
                        pass_fn: Callable,
                        errors: list,
                        *args,
                        timings: Optional[dict] = None,   # orchestration-reserved
                        **kwargs) -> None:
    """Same as `_run_pass` but for mutate-in-place passes."""
    t0 = time.monotonic() if timings is not None else 0.0
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
    finally:
        if timings is not None:
            timings[pass_name] = timings.get(pass_name, 0.0) + (
                time.monotonic() - t0)


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