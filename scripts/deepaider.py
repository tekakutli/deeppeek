#!/usr/bin/env python3
"""Launch aider against a deeppeek daemon; start one if not already up.

Usage (three entry points, all equivalent):

    deeppeek scripts deepaider [args...]      # source checkout only
    ~/.local/bin/deepaider [args...]          # symlink into this file
    python3 scripts/deepaider.py [args...]

The launcher runs `dump.py --as-read-args -r` in the current working
directory and injects the resulting `--read PATH` pairs into aider's
argv.  This means aider's model sees the whole project (per dump.py's
DEFAULT_EXTENSIONS / DEFAULT_EXCLUDE_DIRS) as read-only context on
every turn — no "Add file to the chat?" roundtrips, no architect-mode
re-fires with no new files.  Skip with `--no-dump`.

Any arguments not consumed by this launcher are passed through to
`aider` verbatim.

Launcher-consumed flags (not forwarded to aider):

    --keep-daemon    leave the daemon running when aider exits
    --no-dump        skip the `dump.py --as-read-args` step
    --no-architect   don't auto-add `--architect`

Unless the caller passes `--architect` themselves (and didn't pass
`--no-architect`), architect mode is enabled by default.  Unless the
caller passes an edit-format flag, the architect-mode editor is pinned
to the reliable `whole` format.

Exit code is aider's exit code (or 1 on startup failure).
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT, LOG = 8766, "/tmp/deeppeek-daemon.log"
BASE = f"http://127.0.0.1:{PORT}/v1"

# dump.py lives beside this script under scripts/dumps/.  Resolving the
# symlink (Path(__file__).resolve()) makes this work from
# ~/.local/bin/deepaider even when the real file is elsewhere.
SCRIPT_DIR = Path(__file__).resolve().parent
DUMP_SCRIPT = SCRIPT_DIR / "dumps" / "dump.py"


def up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/models", timeout=1)
        return True
    except Exception:
        return False


def run_dump() -> list[str]:
    """Run `dump.py --as-read-args -r` and return the parsed aider args.

    Returns `["--read", path1, "--read", path2, ...]`.  Empty list on
    any failure (dump.py missing, non-zero exit, no matching files) —
    the caller treats that as "no injection" and continues.

    Parsing: dump.py emits `--read path1 --read path2 ...` as one line
    on stdout.  Splitting on the literal token `"--read "` (note the
    trailing space) is robust to paths containing spaces, since a real
    path would have to contain the string `"--read "` to be mis-split —
    which cannot happen in any sane directory layout.
    """
    if not DUMP_SCRIPT.is_file():
        print(
            f"deepaider: dump script not found at {DUMP_SCRIPT}; "
            f"skipping --read injection",
            file=sys.stderr,
        )
        return []

    cmd = [sys.executable, str(DUMP_SCRIPT), "--as-read-args", "-r"]
    print(f"deepaider: running dump: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        print(
            f"deepaider: could not run dump.py ({e}); "
            f"skipping --read injection",
            file=sys.stderr,
        )
        return []

    if result.returncode != 0:
        print(
            f"deepaider: dump.py exited {result.returncode}; "
            f"continuing without --read injection",
            file=sys.stderr,
        )
        for line in (result.stderr or "").splitlines()[:5]:
            print(f"  {line}", file=sys.stderr)
        return []

    parts = result.stdout.split("--read ")
    paths = [p.strip() for p in parts if p.strip()]
    if not paths:
        print("deepaider: dump produced 0 files; no injection",
              file=sys.stderr)
        return []

    args: list[str] = []
    for p in paths:
        args.extend(["--read", p])

    print(
        f"deepaider: dumped {len(paths)} file(s) for --read injection",
        file=sys.stderr,
    )
    return args


def main(argv: list[str]) -> int:
    args = list(argv)

    # ---- launcher-consumed flags ----
    keep = "--keep-daemon" in args
    if keep:
        args.remove("--keep-daemon")

    no_dump = "--no-dump" in args
    if no_dump:
        args.remove("--no-dump")

    no_architect = "--no-architect" in args
    if no_architect:
        args.remove("--no-architect")

    # ---- daemon: start if not already up ----
    pid: int | None = None
    if not up():
        with open(LOG, "w") as log:
            pid = subprocess.Popen(
                ["deeppeek", "daemon", "--port", str(PORT)],
                stdout=log, stderr=subprocess.STDOUT,
            ).pid
        for _ in range(70):  # ~35 s
            if up():
                break
            time.sleep(0.5)
        else:
            print(f"deepaider: daemon not ready; see {LOG}", file=sys.stderr)
            subprocess.run(["tail", "-n", "20", LOG], file=sys.stderr)
            os.kill(pid, signal.SIGTERM)
            return 1

    # ---- dump: inject --read args for the whole project ----
    read_args = [] if no_dump else run_dump()

    # ---- build aider argv ----
    env = {**os.environ,
           "OPENAI_API_BASE": BASE, "OPENAI_BASE_URL": BASE,
           "OPENAI_API_KEY": "dummy"}

    cmd = ["aider", "--model", "openai/deepseek-web"]
    if not no_architect and "--architect" not in args:
        cmd += ["--architect"]
    if not any(a.startswith(("--edit-format", "--editor-edit-format"))
               for a in args):
        cmd += ["--editor-edit-format", "whole"]
    # Read args go before user args so the user's own flags (including
    # any additional --read they pass manually) can override or extend.
    cmd += read_args
    cmd += args

    try:
        return subprocess.run(cmd, env=env).returncode
    finally:
        if pid and not keep:
            os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
