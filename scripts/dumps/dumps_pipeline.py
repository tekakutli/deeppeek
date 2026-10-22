#!/usr/bin/env python3
"""
dumps_pipeline.py — one-shot chain:
    dumps_compare.py  →  dumps_diff.py  →  cleanup  →  wl-cp-serial.py

Edit PREVIOUS_COMMIT / CURRENT_COMMIT below. Optionally pass -a/--affix AFFIX
to restrict dump.py to filenames containing AFFIX (forwarded as DEFAULT_AFFIX
via dumps_compare's OVERRIDES). Pass an empty string to disable any affix.

Final state of the working directory: exactly three files, all sharing a
common prefix, with no "_changed" marker:

    <prefix>_previous.md
    <prefix>_current.md
    <prefix>_common.md

The two full dumps produced by dumps_compare.py are deleted after diffing.

The three trimmed dumps are then queued into wl-cp-serial, in that order.
Use `wl-cp-serial --next` (or send SIGUSR1 to the worker) to advance
between files while pasting.

Requires dumps_compare.py, dumps_diff.py, and wl-cp-serial.py to live in the
same directory as this file.
"""

import argparse
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these
# ---------------------------------------------------------------------------
PREVIOUS_COMMIT = "HEAD~1"
CURRENT_COMMIT  = "HEAD"

SCRIPT_DIR     = Path(__file__).resolve().parent
COMPARE_SCRIPT = SCRIPT_DIR / "dumps_compare.py"
DIFF_SCRIPT    = SCRIPT_DIR / "dumps_diff.py"
SERIAL_SCRIPT  = SCRIPT_DIR / "wl-cp-serial.py"
# ---------------------------------------------------------------------------


def import_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # does not run main()
    return mod


def snapshot_files(directory: Path) -> set[Path]:
    try:
        return {p for p in directory.iterdir() if p.is_file()}
    except OSError:
        return set()


def run_dumps_compare(affix: str | None):
    """Import dumps_compare, pin commits + (optional) affix, run main(),
    and identify the two dump files it wrote.

    Returns (rc, prev_path, curr_path, dc) where `dc` is the loaded module,
    so the caller can read PREVIOUS_SUFFIX / CURRENT_SUFFIX from it.
    """
    dc = import_from_path("dumps_compare_module", COMPARE_SCRIPT)

    dc.PREVIOUS_COMMIT = PREVIOUS_COMMIT
    dc.CURRENT_COMMIT  = CURRENT_COMMIT
    if affix is not None:
        dc.OVERRIDES = {**dc.OVERRIDES, "DEFAULT_AFFIX": affix}

    cwd        = Path.cwd()
    before     = snapshot_files(cwd)
    start_time = time.time()

    saved_argv = sys.argv
    sys.argv = [str(COMPARE_SCRIPT)]
    try:
        rc = dc.main()
    finally:
        sys.argv = saved_argv

    after = snapshot_files(cwd)
    fresh: list[Path] = []
    for f in after:
        if f not in before:
            fresh.append(f)
        elif f.stat().st_mtime >= start_time:
            fresh.append(f)

    prev_suffix = getattr(dc, "PREVIOUS_SUFFIX", "_previous")
    curr_suffix = getattr(dc, "CURRENT_SUFFIX",  "_current")

    prev_path = next((f for f in fresh if prev_suffix in f.stem), None)
    curr_path = next((f for f in fresh if curr_suffix in f.stem), None)
    return rc, prev_path, curr_path, dc


def run_dumps_diff(dump1: Path, dump2: Path):
    r = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), str(dump1), str(dump2)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        if r.stdout: print(r.stdout, file=sys.stderr)
        if r.stderr: print(r.stderr, file=sys.stderr)
        raise RuntimeError(f"dumps_diff.py exited with {r.returncode}")

    # Mirrors dumps_diff's default naming so we don't have to parse its output.
    return (
        dump1.with_name(f"{dump1.stem}_changed{dump1.suffix}"),
        dump2.with_name(f"{dump2.stem}_changed{dump2.suffix}"),
        dump1.with_name(f"common{dump1.suffix}"),
    )


def derive_shared_prefix(prev_path: Path, curr_path: Path, dc) -> str:
    """Recover the shared prefix ('project_dump_<dirname>') by stripping the
    known _previous / _current suffixes from the two dump filenames."""
    prev_suffix = getattr(dc, "PREVIOUS_SUFFIX", None)
    curr_suffix = getattr(dc, "CURRENT_SUFFIX",  None)
    if prev_suffix and prev_path.stem.endswith(prev_suffix):
        return prev_path.stem[: -len(prev_suffix)]
    if curr_suffix and curr_path.stem.endswith(curr_suffix):
        return curr_path.stem[: -len(curr_suffix)]
    return prev_path.stem  # fallback — won't share the ideal prefix


def cleanup(prev_path: Path, curr_path: Path,
            diff_prev: Path, diff_curr: Path, diff_common: Path,
            dc) -> tuple[Path, Path, Path]:
    """Delete the two full dumps, drop the `_changed` marker from the two
    trimmed dumps, and rename `common.md` to `<prefix>_common.md`.

    Returns (prev_trimmed, curr_trimmed, common_trimmed) using the final
    names. Ordering matters: full dumps are removed first so the rename
    targets (`<prefix>_previous.md`, `<prefix>_current.md`) are free.
    """
    prefix       = derive_shared_prefix(prev_path, curr_path, dc)
    prev_suffix  = getattr(dc, "PREVIOUS_SUFFIX", "_previous")
    curr_suffix  = getattr(dc, "CURRENT_SUFFIX",  "_current")
    suffix       = diff_prev.suffix  # e.g. ".md"

    final_prev   = diff_prev.with_name(f"{prefix}{prev_suffix}{suffix}")
    final_curr   = diff_curr.with_name(f"{prefix}{curr_suffix}{suffix}")
    final_common = diff_common.with_name(f"{prefix}_common{suffix}")

    # 1. Remove the two full dumps so their names become available.
    prev_path.unlink(missing_ok=True)
    curr_path.unlink(missing_ok=True)

    # 2. Drop the `_changed` marker from the two trimmed dumps.
    if diff_prev != final_prev:
        diff_prev.replace(final_prev)
    if diff_curr != final_curr:
        diff_curr.replace(final_curr)

    # 3. Rename `common.md` to share the prefix.
    if diff_common != final_common:
        diff_common.replace(final_common)

    return final_prev, final_curr, final_common


def queue_with_serial(files: list[Path]) -> None:
    """Hand the three trimmed dumps to wl-cp-serial.py in order.

    wl-cp-serial.py accepts explicit file arguments and keeps them in the
    given order, so no staging is needed — argument order IS queue order.
    It daemonizes itself; the first file goes on the clipboard immediately,
    and the user advances with `wl-cp-serial --next` (or SIGUSR1).
    """
    if not files:
        raise RuntimeError("nothing to queue")
    r = subprocess.run(
        [sys.executable, str(SERIAL_SCRIPT), *[str(f) for f in files]],
    )
    if r.returncode != 0:
        raise RuntimeError(f"wl-cp-serial.py exited with {r.returncode}")


def banner(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="dumps_pipeline.py",
        description="Chain dumps_compare.py → dumps_diff.py → wl-cp-serial.py.",
    )
    ap.add_argument("-a", "--affix", default=None, metavar="AFFIX",
                    help="Forwarded to dump.py as DEFAULT_AFFIX, restricting "
                         "the dump to filenames containing AFFIX. "
                         "Pass an empty string to disable any affix.")
    args = ap.parse_args()

    for p, label in (
        (COMPARE_SCRIPT, "dumps_compare.py"),
        (DIFF_SCRIPT,    "dumps_diff.py"),
        (SERIAL_SCRIPT,  "wl-cp-serial.py"),
    ):
        if not p.is_file():
            print(f"error: {label} not found at {p}", file=sys.stderr)
            return 1

    # 1/4 ──────────────────────────────────────────────────────────────
    banner("1/4", "dumps_compare.py")
    try:
        rc, prev_path, curr_path, dc = run_dumps_compare(args.affix)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if rc != 0:
        return rc
    if prev_path is None or curr_path is None:
        print("error: could not locate the two dump files written by "
              "dumps_compare.py", file=sys.stderr)
        return 1
    print(f"  previous: {prev_path.name}", file=sys.stderr)
    print(f"  current:  {curr_path.name}", file=sys.stderr)

    # 2/4 ──────────────────────────────────────────────────────────────
    banner("2/4", "dumps_diff.py")
    try:
        diff_prev, diff_curr, diff_common = run_dumps_diff(prev_path, curr_path)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # 3/4 ──────────────────────────────────────────────────────────────
    banner("3/4", "cleanup — rename + remove full dumps")
    try:
        final_prev, final_curr, final_common = cleanup(
            prev_path, curr_path, diff_prev, diff_curr, diff_common, dc,
        )
    except OSError as e:
        print(f"error: cleanup failed: {e}", file=sys.stderr)
        return 1
    for f in (final_prev, final_curr, final_common):
        print(f"  kept: {f.name}", file=sys.stderr)

    # 4/4 ──────────────────────────────────────────────────────────────
    banner("4/4", "wl-cp-serial.py — queueing the three trimmed dumps")
    try:
        queue_with_serial([final_prev, final_curr, final_common])
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("  queued; paste with Ctrl+V, advance with "
          "`wl-cp-serial --next`", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
