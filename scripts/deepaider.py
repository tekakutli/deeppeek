#!/usr/bin/env python3
"""Launch aider against a deeppeek daemon; start one if not already up.

Timeout
-------
LLM thinking on the DeepSeek web UI can exceed the client-side
timeouts that litellm/OpenAI impose. Three different timers exist:

  * aider's own          --timeout       (whole response, seconds)
  * litellm              request_timeout (global default, env REQUEST_TIMEOUT)
  * litellm (streaming)  stream_timeout  (first chunk)

All three are set from the same value here. Default 600 s. Override with
`--timeout N` or `DEEPAIDER_TIMEOUT=N`.

IMPORTANT: the error

    model produced no output within 120s — ... IndexedDB ... Check the tab

is emitted by the *daemon*, not litellm. The real fix is in
`src/deeppeek/daemon/server.py`: it must emit SSE keepalives on the
HTTP stream while the browser is still thinking, otherwise the client
first-chunk timer fires before the daemon has anything to send.

Context (read-only) files
-------------------------
DEEPAIDER_CONTEXT_FILES is an os.pathsep-separated list of files/dirs
passed to aider as `--read`. Missing paths warn and skip.

Examples:
    DEEPAIDER_CONTEXT_FILES=README.md:docs/ deepaider.py --architect
    export DEEPAIDER_CONTEXT_FILES="project_dump_kritomatic.md:playgrounds/project_dump_playgrounds.md"
"""
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

DEFAULT_TIMEOUT = 600
CONTEXT_ENV = "DEEPAIDER_CONTEXT_FILES"


def up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/models", timeout=1)
        return True
    except Exception:
        return False


def _find_aider_python() -> str | None:
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


def _resolve_timeout(args: list[str]) -> int:
    for i, a in enumerate(args):
        if a == "--timeout" and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                pass
        if a.startswith("--timeout="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                pass

    env_val = os.environ.get("DEEPAIDER_TIMEOUT")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            print(
                f"deepaider: ignoring non-integer DEEPAIDER_TIMEOUT="
                f"{env_val!r}",
                file=sys.stderr,
            )

    return DEFAULT_TIMEOUT


def _resolve_context_files() -> list[Path]:
    raw = os.environ.get(CONTEXT_ENV, "")
    if not raw:
        return []

    paths: list[Path] = []
    seen: set[Path] = set()
    for entry in raw.split(os.pathsep):
        s = entry.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            s = s[1:-1].strip()
        if not s:
            continue

        p = Path(s).expanduser()
        try:
            p = p.resolve()
        except OSError as e:
            print(
                f"deepaider: cannot resolve {s!r}: {e} — skipping",
                file=sys.stderr,
            )
            continue

        if not p.exists():
            print(
                f"deepaider: {CONTEXT_ENV} entry not found, skipping: {p}",
                file=sys.stderr,
            )
            continue

        if p in seen:
            continue
        seen.add(p)
        paths.append(p)

    return paths


def main(argv: list[str]) -> int:
    args = list(argv)
    keep = "--keep-daemon" in argv
    no_reflection_patch = "--no-reflection-patch" in argv
    for flag in ("--keep-daemon", "--no-reflection-patch"):
        if flag in args:
            args.remove(flag)

    timeout = _resolve_timeout(args)
    print(f"deepaider: LLM timeout = {timeout}s", file=sys.stderr)

    context_files = _resolve_context_files()
    for p in context_files:
        print(f"deepaider: context (read-only): {p}", file=sys.stderr)

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

    # All client-side timeout layers. REQUEST_TIMEOUT is the env var
    # litellm's constants.py actually reads (LITELLM_TIMEOUT is not).
    env = {
        **os.environ,
        "OPENAI_API_BASE": BASE,
        "OPENAI_BASE_URL": BASE,
        "OPENAI_API_KEY": "dummy",
        "REQUEST_TIMEOUT": str(timeout),
        "LITELLM_TIMEOUT": str(timeout),
        "OPENAI_API_TIMEOUT": str(timeout),
        "OPENAI_TIMEOUT": str(timeout),
    }

    aider_args = ["--model", "openai/deepseek-web", "--no-auto-commits"]

    if not any(a == "--timeout" or a.startswith("--timeout=")
               for a in args):
        aider_args += ["--timeout", str(timeout)]

    for p in context_files:
        aider_args += ["--read", str(p)]

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
        import litellm
        litellm.request_timeout = LITELLM_REQUEST_TIMEOUT_PLACEHOLDER

        from aider.coders.base_coder import Coder

        Coder.max_reflections = 0

        original_send_message = Coder.send_message

        def patched_send_message(self, *args, **kwargs):
            try:
                yield from original_send_message(self, *args, **kwargs)
            finally:
                self.reflected_message = None

        patched_send_message.__wrapped__ = original_send_message
        Coder.send_message = patched_send_message

        original_run_one = Coder.run_one

        def patched_run_one(self, user_message, preproc):
            self.init_before_message()
            if preproc:
                message = self.preproc_user_input(user_message)
            else:
                message = user_message
            self.reflected_message = None
            if message:
                list(self.send_message(message))

        patched_run_one.__wrapped__ = original_run_one
        Coder.run_one = patched_run_one

        print(
            "deepaider: disabled aider reflection loop; "
            f"litellm.request_timeout={litellm.request_timeout}",
            file=sys.stderr,
        )
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
            patch_script = patch_script.replace(
                "LITELLM_REQUEST_TIMEOUT_PLACEHOLDER", str(timeout)
            )
            cmd = [aider_python, "-c", patch_script] + aider_args
            try:
                return subprocess.run(cmd, env=env).returncode
            except OSError as e:
                print(f"deepaider: failed to launch aider: {e}", file=sys.stderr)
        else:
            print("deepaider: could not find aider interpreter, running unpatch",
                  file=sys.stderr)

    try:
        return subprocess.run(["aider"] + aider_args, env=env).returncode
    finally:
        if pid and not keep:
            os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
