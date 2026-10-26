"""`deeppeek scripts` — run any script under the project's scripts/ dir.

Discovers *.py files recursively under <repo>/scripts/ and dispatches
to them by their relative path or by an index number shown in the
menu:

    deeppeek scripts                    # numbered menu of all scripts
    deeppeek scripts <subdir>           # numbered menu of that subdir
    deeppeek scripts <n>                # run the nth entry of the root menu
    deeppeek scripts <subdir> <n>       # run the nth entry of that subdir
    deeppeek scripts <path> [args...]   # run <path>.py with args
    deeppeek scripts <subdir> <name> [args...]

No script list is hardcoded.  Discovery runs at invocation time by
walking scripts/ — adding a file (or a whole subdirectory) makes it
immediately runnable.

The script is run as a subprocess with the current Python interpreter
(sys.executable), inheriting the caller's cwd and environment:

  · the script's own `if __name__ == "__main__"` entry point fires,
  · its `__file__`-relative path resolution works,
  · its cwd-relative behaviour works,
  · exit codes propagate to deeppeek's exit code,
  · stdin/stdout/stderr stay attached, so interactive scripts and
    Ctrl+C behave exactly as if invoked directly.

Everything after the script name is passed to it verbatim — flags
like --merge or -a cable are forwarded unchanged.

`scripts/` is located by walking up from this file to the nearest
directory containing pyproject.toml + src/deeppeek/ + scripts/.
That's a source checkout, so this works for `pipx install --editable`
and `pip install -e .`.  A frozen wheel has no such tree; the command
says so and suggests the editable-install fix.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# argparse registration (help listing only; dispatch bypasses argparse)
# ---------------------------------------------------------------------------

def register(sub) -> None:
    """Add `scripts` to the root parser's subcommand list.

    The real dispatch happens in `cli.main`, which intercepts
    `argv[0] == "scripts"` *before* `parse_args`, so flags after the
    script name reach the script unchanged.  Registration exists only
    so `deeppeek --help` lists `scripts` as a valid subcommand.
    """
    sub.add_parser(
        "scripts",
        help="Run a script from the project's scripts/ directory "
             "(e.g. `deeppeek scripts dumps dumps_evolve`)",
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_scripts_root() -> Optional[Path]:
    """Return <source-tree>/scripts, or None when not in a source checkout.

    Walks up from this file looking for the project root, identified
    by all three of: pyproject.toml, src/deeppeek/, scripts/.  Requiring
    all three rules out false positives (a stray ~/scripts/ above a
    pipx site-packages).  A frozen wheel has no pyproject.toml, so this
    safely returns None there.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (
            (parent / "pyproject.toml").is_file()
            and (parent / "src" / "deeppeek").is_dir()
            and (parent / "scripts").is_dir()
        ):
            return parent / "scripts"
    return None


def _all_scripts_ordered(scripts_root: Path) -> list[tuple[str, Path]]:
    """Return [(display_path, actual_path)] sorted deterministically.

    `display_path` has no .py suffix, e.g. "dumps/dumps_evolve" or
    "status".  Order is lexicographic on the relative path, so the
    numeric indices are stable across runs and reproducible.
    """
    ordered: list[tuple[str, Path]] = []
    for py in sorted(scripts_root.rglob("*.py")):
        if not py.is_file():
            continue
        rel = py.relative_to(scripts_root)
        if rel.name == "__init__.py":
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        display = rel.with_suffix("").as_posix()
        ordered.append((display, py))
    return ordered


# ---------------------------------------------------------------------------
# Path → target resolution
# ---------------------------------------------------------------------------

def resolve(parts: list[str],
            scripts_root: Path) -> tuple[str, Optional[Path], list[str]]:
    """Resolve a path-argv to (kind, target, remaining_args).

    kind is "file" (run the target), "dir" (list the target), or "none"
    (nothing matched).  Longest-prefix-first: at each length try
    `<prefix>.py` as a file, then `<prefix>` as a file (if it ends in
    .py), then `<prefix>` as a directory.  First hit wins; unconsumed
    trailing parts become the script's arguments.
    """
    for i in range(len(parts), 0, -1):
        prefix = parts[:i]
        rel_str = "/".join(prefix)

        as_file = scripts_root / (rel_str + ".py")
        if as_file.is_file():
            return ("file", as_file, parts[i:])

        raw = scripts_root / rel_str
        if raw.is_file() and raw.suffix == ".py":
            return ("file", raw, parts[i:])

        if raw.is_dir():
            return ("dir", raw, parts[i:])

    return ("none", None, parts)


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------

def _print_missing_scripts_root() -> None:
    print("error: could not locate the project's scripts/ directory.",
          file=sys.stderr)
    print("  this command needs a source checkout — scripts/ is not "
          "shipped inside the wheel.", file=sys.stderr)
    print("  install editable to enable it:", file=sys.stderr)
    print("    pipx install --editable /path/to/deeppeek --force",
          file=sys.stderr)


def _print_root_menu(scripts_root: Optional[Path]) -> None:
    if scripts_root is None:
        _print_missing_scripts_root()
        return
    ordered = _all_scripts_ordered(scripts_root)
    if not ordered:
        print(f"no scripts under {scripts_root}")
        return
    width = len(str(len(ordered)))
    for i, (display, _) in enumerate(ordered, 1):
        print(f"  {i:>{width}}  {display}")
    print()
    print("  deeppeek scripts <n|path> [args...]")


def _print_dir_menu(target: Path, scripts_root: Path) -> None:
    rel = target.relative_to(scripts_root)
    rel_str = rel.as_posix() if rel != Path(".") else ""

    entries = sorted(
        (p.stem, p) for p in target.glob("*.py")
        if p.name != "__init__.py" and p.is_file()
    )
    subdirs = sorted(
        p.name for p in target.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if not entries and not subdirs:
        print(f"no scripts under {rel_str or '.'}")
        return

    if entries:
        width = len(str(len(entries)))
        for i, (name, _) in enumerate(entries, 1):
            print(f"  {i:>{width}}  {name}")
        print()

    if subdirs:
        print(f"  subdirs:  {'  '.join(s + '/' for s in subdirs)}")
        print()

    prefix = f"deeppeek scripts {rel_str}" if rel_str else "deeppeek scripts"
    print(f"  {prefix} <n|name> [args...]")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_script(script_path: Path, args: list[str]) -> int:
    """Run `script_path` with `args`, inheriting stdin/out/err and cwd.

    No capture, no cwd change, no environment manipulation.  The child
    behaves exactly as if the user had invoked it directly with
    `python <script> <args>` from the same terminal.
    """
    cmd = [sys.executable, str(script_path), *args]
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        print(f"error: python interpreter not found: {sys.executable}",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return result.returncode


def _run_by_number(scripts_root: Path, n: int,
                   extra_args: list[str], verbose: bool) -> int:
    ordered = _all_scripts_ordered(scripts_root)
    if not (1 <= n <= len(ordered)):
        print(f"error: {n} out of range (1..{len(ordered)})",
              file=sys.stderr)
        return 1
    _, path = ordered[n - 1]
    if verbose:
        print(f"$ {sys.executable} {path} {' '.join(extra_args)}".rstrip(),
              file=sys.stderr)
    return run_script(path, extra_args)


def _run_by_number_in_dir(scripts_root: Path, dir_path: Path, n: int,
                          extra_args: list[str], verbose: bool) -> int:
    entries = sorted(
        (p.stem, p) for p in dir_path.glob("*.py")
        if p.name != "__init__.py" and p.is_file()
    )
    if not (1 <= n <= len(entries)):
        print(f"error: {n} out of range (1..{len(entries)})",
              file=sys.stderr)
        return 1
    _, path = entries[n - 1]
    if verbose:
        print(f"$ {sys.executable} {path} {' '.join(extra_args)}".rstrip(),
              file=sys.stderr)
    return run_script(path, extra_args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def dispatch(argv: list[str], verbose: bool = False) -> int:
    """Handle `deeppeek scripts ...`.

    Called by `cli.main` *before* argparse runs on the full argv, so
    all flags after the script name reach the script unchanged.
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_root_menu(find_scripts_root())
        return 0

    scripts_root = find_scripts_root()
    if scripts_root is None:
        _print_missing_scripts_root()
        return 1

    # Numeric shortcut at root scope.  Only fires when `argv[0]` isn't
    # the literal name of an existing script/dir (so a real script
    # called "42.py" would still be runnable as `deeppeek scripts 42`).
    if (
        argv[0].isdigit()
        and not (scripts_root / argv[0]).exists()
        and not (scripts_root / (argv[0] + ".py")).exists()
    ):
        return _run_by_number(
            scripts_root, int(argv[0]), argv[1:], verbose)

    kind, target, remaining = resolve(argv, scripts_root)

    if kind == "none":
        print(f"error: no script or directory matches: {' '.join(argv)}",
              file=sys.stderr)
        print("  run `deeppeek scripts` for the menu", file=sys.stderr)
        return 1

    if kind == "dir" and target is not None:
        # `-h` / `--help` (or nothing at all) means "show this scope".
        if not remaining or remaining[0] in ("-h", "--help"):
            _print_dir_menu(target, scripts_root)
            return 0
        # Numeric shortcut inside this subdir.
        if remaining[0].isdigit():
            return _run_by_number_in_dir(
                scripts_root, target, int(remaining[0]),
                remaining[1:], verbose)
        rel_str = target.relative_to(scripts_root).as_posix()
        print(f"error: no script '{remaining[0]}' under '{rel_str}/'",
              file=sys.stderr)
        return 1

    if kind == "file" and target is not None:
        if verbose:
            cmd = " ".join([sys.executable, str(target), *remaining])
            print(f"$ {cmd}", file=sys.stderr)
        return run_script(target, remaining)

    print(f"internal error: unexpected resolve result: kind={kind!r}",
          file=sys.stderr)
    return 1


def run(args) -> int:
    """Argparse fallback.  Unreachable in normal operation — `cli.main`
    intercepts `scripts` before parse_args.  Present so a future
    refactor that forgets to intercept produces a clear error instead
    of a KeyError."""
    print(
        "error: `scripts` reached argparse; cli.main should have "
        "intercepted it.  Report this bug.",
        file=sys.stderr,
    )
    return 1
