#!/usr/bin/env python3
"""Launch aider against a deeppeek daemon; start one if not already up.

Usage (three entry points, all equivalent):

    deeppeek scripts deepaider [args...]      # source checkout only
    ~/.local/bin/deepaider [args...]          # symlink into this file
    python3 scripts/deepaider.py [args...]

Any arguments not consumed by this launcher are passed through to
`aider` verbatim.  `--keep-daemon` is consumed here (not forwarded).
Unless the caller passes `--architect` themselves, architect mode is
enabled by default; unless the caller passes an edit-format flag,
the architect-mode editor is pinned to the reliable `whole` format.

Exit code is aider's exit code (or 1 on startup failure).
"""
import os, signal, subprocess, sys, time, urllib.request

PORT, LOG = 8766, "/tmp/deeppeek-daemon.log"
BASE = f"http://127.0.0.1:{PORT}/v1"


def up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/models", timeout=1)
        return True
    except Exception:
        return False


def main(argv: list[str]) -> int:
    args = list(argv)
    keep = "--keep-daemon" in args
    if keep:
        args.remove("--keep-daemon")

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

    env = {**os.environ,
           "OPENAI_API_BASE": BASE, "OPENAI_BASE_URL": BASE,
           "OPENAI_API_KEY": "dummy"}
    cmd = ["aider", "--model", "openai/deepseek-web"]
    if "--architect" not in args:
        cmd += ["--architect"]
    if not any(a.startswith(("--edit-format", "--editor-edit-format"))
               for a in args):
        cmd += ["--editor-edit-format", "whole"]
    cmd += args

    try:
        return subprocess.run(cmd, env=env).returncode
    finally:
        if pid and not keep:
            os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
