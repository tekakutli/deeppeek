#!/usr/bin/env python3
"""Launch aider against a deeppeek daemon; start one if not already up."""
import os
import signal
import subprocess
import sys
import time
import urllib.request
import shutil
from pathlib import Path

PORT, LOG = 8766, "/tmp/deeppeek-daemon.log"
BASE = f"http://127.0.0.1:{PORT}/v1"


def up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/models", timeout=1)
        return True
    except Exception:
        return False


def _find_aider_python() -> str | None:
    """Find the Python interpreter that has aider installed."""
    aider_bin = shutil.which("aider")
    if not aider_bin:
        print("deepaider: 'aider' command not found in PATH", file=sys.stderr)
        return None

    try:
        with open(aider_bin, 'rb') as f:
            first_line = f.readline().decode('utf-8', errors='ignore').strip()
        if first_line.startswith('#!'):
            interpreter = first_line[2:].strip()
            if ' ' in interpreter:
                parts = interpreter.split()
                if parts[0].endswith('env'):
                    interpreter = shutil.which(parts[1])
                else:
                    interpreter = parts[0]
            if interpreter and Path(interpreter).exists():
                return interpreter
    except Exception:
        pass

    home = Path.home()
    common = []
    if sys.platform == "darwin":
        common.append(home / "Library/Application Support/pipx/venvs/aider-chat/bin/python")
    common.append(home / ".local/pipx/venvs/aider-chat/bin/python")
    for p in common:
        if p.exists():
            return str(p)
    return None


def main(argv: list[str]) -> int:
    args = list(argv)

    keep = "--keep-daemon" in argv
    no_architect = "--no-architect" in argv
    no_reflection_patch = "--no-reflection-patch" in argv

    for flag in ("--keep-daemon", "--no-architect", "--no-reflection-patch"):
        if flag in args:
            args.remove(flag)

    pid: int | None = None
    if not up():
        with open(LOG, "w") as log:
            pid = subprocess.Popen(
                ["deeppeek", "daemon", "--port", str(PORT)],
                stdout=log, stderr=subprocess.STDOUT,
            ).pid
        for _ in range(70):
            if up():
                break
            time.sleep(0.5)
        else:
            print(f"deepaider: daemon not ready; see {LOG}", file=sys.stderr)
            os.kill(pid, signal.SIGTERM)
            return 1

    env = {**os.environ,
           "OPENAI_API_BASE": BASE, "OPENAI_BASE_URL": BASE,
           "OPENAI_API_KEY": "dummy"}

    aider_args = ["--model", "openai/deepseek-web"]
    if not no_architect and "--architect" not in args:
        aider_args += ["--architect"]
    if not any(a.startswith(("--edit-format", "--editor-edit-format"))
               for a in args):
        aider_args += ["--editor-edit-format", "whole"]
    aider_args += args

    if not no_reflection_patch:
        aider_python = _find_aider_python()
        if aider_python:
            patch_script = r"""
import sys

def patch():
    try:
        from aider.coders.base_coder import Coder

        # Layer 1: cap the reflection counter.
        Coder.max_reflections = 0

        # Layer 2: neutralise send_message so it never leaves a
        # reflected_message behind.  This is the root cause: every
        # reflection (edit-format mismatch, lint error, test error)
        # is armed inside send_message.  Clearing it here means the
        # loop in run_one has nothing to consume, regardless of
        # which coder instance (architect, editor, or a newly
        # created one) is running.
        original_send_message = Coder.send_message

        def patched_send_message(self, *args, **kwargs):
            try:
                return original_send_message(self, *args, **kwargs)
            finally:
                self.reflected_message = None

        patched_send_message.__wrapped__ = original_send_message
        Coder.send_message = patched_send_message

        # Layer 3: belt-and-braces override of run_one that never
        # re-enters the reflection loop, even if reflected_message
        # somehow gets set by a path that bypasses send_message.
        original_run_one = Coder.run_one

        def patched_run_one(self, user_message, preproc):
            self.init_before_message()
            if preproc:
                message = self.preproc_user_input(user_message)
            else:
                message = user_message
            self.reflected_message = None
            list(self.send_message(message))
            # Intentionally ignore self.reflected_message.

        patched_run_one.__wrapped__ = original_run_one
        Coder.run_one = patched_run_one

        print("deepaider: disabled aider reflection loop (multi-layer patch)",
              file=sys.stderr)
        return True
    except Exception as e:
        print(f"deepaider: failed to patch aider: {e}", file=sys.stderr)
        return False

if patch():
    from aider.main import main as aider_main
    try:
        sys.exit(aider_main(sys.argv[1:]))
    except SystemExit as e:
        sys.exit(e.code if e.code is not None else 0)
else:
    sys.exit(1)
"""
            cmd = [aider_python, "-c", patch_script] + aider_args
            try:
                result = subprocess.run(cmd, env=env)
                return result.returncode
            except OSError as e:
                print(f"deepaider: failed to launch aider: {e}", file=sys.stderr)
        else:
            print("deepaider: could not find aider interpreter, running unpatch",
                  file=sys.stderr)

    cmd = ["aider"] + aider_args
    try:
        return subprocess.run(cmd, env=env).returncode
    finally:
        if pid and not keep:
            os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
