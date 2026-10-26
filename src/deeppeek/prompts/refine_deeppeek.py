"""`deeppeek prompt refine_deeppeek` — apply, review the aftermath,
AND attach the input conversation and a dump of deeppeek's own source
so the model can propose concrete refinements.

Pipeline (extends `review`):

    verify     hard: git work tree (reset depends on it)
    warn       informational: no deeppeek/ dir yet
    reset      git reset --hard HEAD && git clean -fd
    apply      deeppeek recover <conversation> --apply-here
    require    hard: deeppeek/ must exist post-apply
    status     scripts/status.py in cwd → <ws>/diff.txt
    dump       dump.py -r in the deeppeek source, with DEFAULT_EXCLUDE_DIRS
               extended to also exclude "commands" and "extension"
    pipe       diff.txt + meta files + conversation.md + deeppeek_dump.md
    type       short templated prompt

Differences from `review`:

  · The original conversation markdown is attached as a file chip, so
    the model can see what was actually applied (not just what happened
    when applying it).
  · A full dump of the deeppeek source is attached, so the model can
    quote specific file paths and function names when proposing fixes.

The dump explicitly loads dump.py as a module and overrides
DEFAULT_EXCLUDE_DIRS — the exclusion list is not exposed on dump.py's
CLI, so shelling out would not let us add "commands" and "extension".

`--text STR` behaves exactly as in `review`: it replaces the composer
text verbatim, and the attachments are still piped unless `--no-pipe`
is also given.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STATUS_PY_SCRIPT = _PROJECT_ROOT / "scripts" / "status.py"
_DUMP_PY_SCRIPT = _PROJECT_ROOT / "scripts" / "dumps" / "dump.py"
DEFAULT_CONVERSATION = Path("/tmp/conversation.md")

# Extra directory names to exclude from the deeppeek source dump, on
# top of whatever dump.py ships with at DEFAULT_EXCLUDE_DIRS.
# `commands/` and `extension/` are large subtrees that rarely carry
# the patching logic the refinement prompt cares about; excluding them
# keeps the dump small enough that the model can actually read it end
# to end.  The user can pass `--dump-py` to use a different script
# whose excludes fit better.
_REFINE_DUMP_EXCLUDE_EXTRA = ["commands", "extension"]


DEFAULT_PROMPT_TEMPLATE = """\
This is the outcome of my deeppeek utility trying to apply the patches
you just printed.  Attached: {conversation_name} (what recover was
given), diff.txt (what actually changed on disk), the reports under
deeppeek/, and {deeppeek_dump_name} (a dump of deeppeek's own source).

Help me evaluate the results.  If you detect bugs, propose robust
fixes.  Better still, build infrastructure, further abstractions, or
additional passes so that whatever blocked a successful apply is
resolved.  deeppeek must be able to ingest any script in the wild and
have general solutions to its challenges — nothing custom.

Focus on deeppeek.  Forget about building on the project we were
working on for now.
"""


# ---------------------------------------------------------------------------
# Subprocess helpers — duplicated from review.py, per the self-contained
# convention used by every other prompt module.
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             cwd: Optional[Path] = None,
             timeout: Optional[float] = None,
             allow_failure: bool = False) -> tuple[int, str, str]:
    prefix = f"    $ (cd {cwd} && " if cwd else "    $ "
    suffix = ")" if cwd else ""
    print(f"{prefix}{' '.join(cmd)}{suffix}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        msg = f"`{cmd[0]}` not on PATH"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    if proc.returncode != 0:
        tag = "(allowed)" if allow_failure else ""
        print(f"    exit {proc.returncode} {tag}".rstrip(),
              file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# deeppeek-source inference — duplicated from evolve.py
# ---------------------------------------------------------------------------

def _infer_deeppeek_source() -> Optional[Path]:
    """Return the deeppeek source tree, or None.

    Preference order:
      1. _PROJECT_ROOT — the editable-install case
      2. cwd — the "run from a source checkout" case

    A "source tree" is defined as having pyproject.toml AND
    src/deeppeek/.  A frozen wheel has neither, so inference safely
    fails there.
    """
    for c in (_PROJECT_ROOT, Path.cwd()):
        if not c.is_dir():
            continue
        if (c / "pyproject.toml").is_file() and \
           (c / "src" / "deeppeek").is_dir():
            return c
    return None


# ---------------------------------------------------------------------------
# Pre-reset checks
# ---------------------------------------------------------------------------

def _stage_verify_git(cwd: Path) -> None:
    rc, stdout, _ = _run_cmd(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd, allow_failure=True,
    )
    if rc != 0 or stdout.strip() != "true":
        print(f"error: {cwd} is not a git work tree.", file=sys.stderr)
        print("  this command performs `git reset --hard HEAD` and "
              "`git clean -fd`", file=sys.stderr)
        print("  before reviewing — that requires a git checkout.",
              file=sys.stderr)
        raise SystemExit(2)
    print(f"[git] work tree: {cwd}", file=sys.stderr)


def _stage_warn_if_no_meta(cwd: Path) -> None:
    if (cwd / "deeppeek").is_dir():
        return
    print(f"warning: {cwd}/deeppeek/ does not exist yet.", file=sys.stderr)
    print("  this script will run `git reset --hard HEAD` + "
          "`git clean -fd`,", file=sys.stderr)
    print("  then `deeppeek recover <conversation> --apply-here` — "
          "which creates deeppeek/.", file=sys.stderr)
    print("  if you didn't mean to nuke this tree, Ctrl+C now.",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# Reset + apply
# ---------------------------------------------------------------------------

def _stage_reset(cwd: Path) -> None:
    print(f"[reset] git reset --hard HEAD in {cwd}", file=sys.stderr)
    rc, _, _ = _run_cmd(
        ["git", "reset", "--hard", "HEAD"],
        cwd=cwd, allow_failure=False,
    )
    if rc != 0:
        raise SystemExit(f"error: `git reset --hard HEAD` exited {rc}")
    print(f"[reset] git clean -fd in {cwd}", file=sys.stderr)
    rc, _, _ = _run_cmd(
        ["git", "clean", "-fd"],
        cwd=cwd, allow_failure=False,
    )
    if rc != 0:
        raise SystemExit(f"error: `git clean -fd` exited {rc}")


def _stage_apply(cwd: Path, args: argparse.Namespace) -> int:
    """Run `recover --apply-here` in `cwd`; return its exit code.

    Non-zero exit is not an abort condition — partial application is
    exactly what this command exists to review.  The real gate is
    `_stage_require_meta`.
    """
    print(f"[apply] recover {args.conversation} --apply-here in {cwd}",
          file=sys.stderr)
    prefix = _deeppeek_cmd()
    cmd = prefix + [
        "recover", str(args.conversation), "--apply-here",
    ]
    rc, _, _ = _run_cmd(
        cmd, cwd=cwd,
        timeout=args.recover_timeout, allow_failure=True,
    )
    if rc != 0:
        print(f"  recover exited {rc} — continuing; the reports under "
              f"deeppeek/ record the partial outcome", file=sys.stderr)
    return rc


# ---------------------------------------------------------------------------
# Post-apply checks and captures
# ---------------------------------------------------------------------------

def _stage_require_meta(cwd: Path) -> Path:
    meta = cwd / "deeppeek"
    if not meta.is_dir():
        raise SystemExit(
            f"error: {meta} was not created by the apply step.\n"
            f"  recover --apply-here appears to have run without "
            f"writing reports.\n"
            f"  check the recover output above."
        )
    return meta


def _resolve_workspace(args: argparse.Namespace) -> Path:
    if args.workspace is None:
        return Path(tempfile.mkdtemp(prefix="deeppeek_refine_"))
    ws = args.workspace.resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _stage_status(cwd: Path, ws: Path,
                  args: argparse.Namespace) -> Path:
    script = args.status_py or _STATUS_PY_SCRIPT
    if not script.exists():
        raise SystemExit(
            f"error: status script not found at {script}\n"
            f"  pass --status-py to point at a different script"
        )

    out = ws / "diff.txt"
    cmd = [sys.executable, str(script)]
    print(f"[status] running {script.name} in {cwd}", file=sys.stderr)
    rc, stdout, stderr = _run_cmd(
        cmd, cwd=cwd, timeout=args.status_timeout, allow_failure=True,
    )

    parts = [
        "# git status capture produced by scripts/status.py",
        f"# cwd:       {cwd}",
        f"# exit code: {rc}",
        "",
        stdout.rstrip(),
    ]
    if stderr.strip():
        parts += ["", "# stderr:", stderr.rstrip()]
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"  wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# deeppeek source dump
# ---------------------------------------------------------------------------

def _stage_dump_deeppeek(source: Path, ws: Path,
                          args: argparse.Namespace) -> Path:
    """Dump `source` recursively to <ws>/deeppeek_dump.md.

    Loads dump.py as a module, extends DEFAULT_EXCLUDE_DIRS with
    _REFINE_DUMP_EXCLUDE_EXTRA, then invokes `main()` with `-r -o`
    so the output path is exact.

    Loading-as-module (rather than shelling out) is required because
    dump.py does not expose its exclusion list on the CLI.
    """
    dump_py = args.dump_py or _DUMP_PY_SCRIPT
    if not dump_py.is_file():
        raise SystemExit(
            f"error: dump.py not found at {dump_py}\n"
            f"  pass --dump-py to point at a different script"
        )
    if not source.is_dir():
        raise SystemExit(
            f"error: deeppeek source is not a directory: {source}")

    out = ws / "deeppeek_dump.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "refine_dump_instance", str(dump_py))
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: could not load dump.py from {dump_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    base_excludes = list(getattr(mod, "DEFAULT_EXCLUDE_DIRS", []) or [])
    mod.DEFAULT_EXCLUDE_DIRS = base_excludes + list(_REFINE_DUMP_EXCLUDE_EXTRA)

    print(f"[dump] dump.py -r in {source}", file=sys.stderr)
    print(f"  added excludes (on top of dump.py defaults): "
          f"{_REFINE_DUMP_EXCLUDE_EXTRA}", file=sys.stderr)

    old_cwd = Path.cwd()
    saved_argv = sys.argv
    try:
        os.chdir(str(source))
        sys.argv = [str(dump_py), "-r", "-o", str(out)]
        rc = mod.main()
    finally:
        sys.argv = saved_argv
        os.chdir(str(old_cwd))

    if rc != 0:
        raise SystemExit(f"error: dump.py exited {rc}")
    if not out.is_file() or not out.read_text(encoding="utf-8").strip():
        raise SystemExit(f"error: dump.py did not produce {out}")
    print(f"  wrote {out} ({out.stat().st_size:,} bytes)", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _resolve_composer_text(args: argparse.Namespace,
                           conversation: Path,
                           dump_path: Path) -> tuple[str, str]:
    """Return (text, source_tag) for the composer.

    When `--text STR` is given, STR is returned verbatim — no
    formatting, no wrapping.  Otherwise the short templated default
    is built from the conversation filename and the dump filename.
    """
    if args.text is not None:
        return args.text, "--text override"
    text = DEFAULT_PROMPT_TEMPLATE.format(
        conversation_name=conversation.name,
        deeppeek_dump_name=dump_path.name,
    )
    return text, "default template"


def _stage_emit_parallel(prompt, files, args):
    """Open one composer session, fire attach + type back-to-back,
    collect acks.  Duplicated from review.py per the self-contained
    convention."""
    import base64
    import mimetypes
    from ..commands._composer import broadcast_many, composer_session

    if prompt is not None and not prompt.strip():
        print("error: composer text is empty/whitespace-only",
              file=sys.stderr)
        return 1

    event_specs = []

    if files:
        payload_files = []
        for f in files:
            if not f.is_file():
                print(f"  skipped (not a regular file): {f}",
                      file=sys.stderr)
                continue
            data = f.read_bytes()
            mime = (mimetypes.guess_type(f.name)[0]
                    or "application/octet-stream")
            payload_files.append({
                "name": f.name,
                "mime": mime,
                "base64": base64.b64encode(data).decode("ascii"),
            })
            print(f"  {f.name}  ({len(data):,} bytes)",
                  file=sys.stderr)
        if payload_files:
            event_specs.append({
                "tag": "pipe",
                "event": "attach",
                "payload": {
                    "files": payload_files,
                    "opts": {"delay_ms": 0},
                },
                "ack_name": "attach",
            })

    if prompt:
        event_specs.append({
            "tag": "type",
            "event": "type",
            "payload": {"text": prompt},
            "ack_name": "type",
        })

    if not event_specs:
        return 0

    timeout = max(
        getattr(args, "pipe_timeout", 180.0),
        getattr(args, "type_timeout", 300.0),
    )
    with composer_session(port=getattr(args, "port", 8765),
                         timeout=timeout) as server:
        if server is None:
            return 1
        print(f"[parallel] firing {len(event_specs)} event(s):",
              file=sys.stderr)
        for s in event_specs:
            print(f"  {s['event']} (tag={s['tag']})",
                  file=sys.stderr)
        results = broadcast_many(server, event_specs, timeout=timeout)
        rc = 0
        for s in event_specs:
            r = results.get(s["tag"],
                            {"exit_code": 1, "result": {}})
            if r["exit_code"] == 0:
                print(f"  OK - {s['tag']}", file=sys.stderr)
            else:
                reason = (r["result"].get("reason")
                          or r["result"].get("error")
                          or "(no reason)")
                print(f"  FAIL - {s['tag']}: {reason}",
                      file=sys.stderr)
                if rc == 0:
                    rc = r["exit_code"]
        return rc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    print(f"[refine_deeppeek] cwd: {cwd}", file=sys.stderr)

    # 0. Cheap upfront validation of --text so we don't nuke the
    #    tree and *then* discover the override is empty.
    if args.text is not None and not args.text.strip():
        print("error: --text is empty (or whitespace-only).  "
              "Omit --text to use the default prompt.",
              file=sys.stderr)
        return 2

    # 1. Infer deeppeek source.
    inferred = _infer_deeppeek_source()
    if inferred is None:
        raise SystemExit(
            "error: could not infer the deeppeek source tree "
            "(no pyproject.toml + src/deeppeek/ near the package or "
            "in the current directory).\n"
            "  Run from a deeppeek checkout, or install deeppeek "
            "editable:\n"
            "    pipx install --editable /path/to/deeppeek --force"
        )
    print(f"[inferred] deeppeek-source: {inferred}", file=sys.stderr)

    # 2. Hard: must be a git work tree (reset depends on it).
    _stage_verify_git(cwd)

    # 3. Informational: warn if this tree has never been applied to.
    _stage_warn_if_no_meta(cwd)

    # 4. Reset + apply (deterministic prelude).  Apply's exit code
    #    is captured, NOT treated as fatal.
    _stage_reset(cwd)
    recover_rc = _stage_apply(cwd, args)

    # 5. Hard: deeppeek/ must exist after the apply.
    meta_dir = _stage_require_meta(cwd)
    print(f"  metadata: {meta_dir}", file=sys.stderr)

    # 6. Workspace, status capture, meta list, deeppeek dump.
    ws = _resolve_workspace(args)
    print(f"  workspace: {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)

    status_path = _stage_status(cwd, ws, args)

    meta_files = sorted(p for p in meta_dir.iterdir() if p.is_file())
    print(f"  metadata files: {len(meta_files)}", file=sys.stderr)
    for f in meta_files:
        print(f"    {f.name}", file=sys.stderr)

    dump_path = _stage_dump_deeppeek(inferred, ws, args)

    # Conversation must exist — apply just read it, but check anyway
    # so `--no-pipe` runs can still build the prompt from its path.
    if not args.conversation.is_file():
        raise SystemExit(
            f"error: conversation file not found: {args.conversation}"
        )

    # 7. Build the composer text.
    composer_text, source_tag = _resolve_composer_text(
        args, args.conversation, dump_path,
    )
    print(f"[build] composer text: {len(composer_text):,} chars "
          f"({source_tag})", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] would pipe:", file=sys.stderr)
        print(f"  {status_path}", file=sys.stderr)
        for f in meta_files:
            print(f"  {f}", file=sys.stderr)
        print(f"  {args.conversation}", file=sys.stderr)
        print(f"  {dump_path}", file=sys.stderr)
        print(f"\n[dry-run] recover exited {recover_rc}", file=sys.stderr)
        print(f"[dry-run] composer text ({source_tag}) below; not typed.",
              file=sys.stderr)
        print(composer_text)
        return 0

    if args.no_pipe:
        files = []
    else:
        files = [
            status_path,
            *meta_files,
            args.conversation,
            dump_path,
        ]
    return _stage_emit_parallel(composer_text, files, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "refine_deeppeek",
        help="Reset, re-apply the conversation, capture the aftermath, "
             "and pipe the conversation + a dump of deeppeek itself "
             "for refinement",
    )

    p.add_argument(
        "--conversation", type=Path, default=DEFAULT_CONVERSATION,
        help=f"conversation markdown to re-apply before reviewing "
             f"(default: {DEFAULT_CONVERSATION})",
    )
    p.add_argument(
        "--text", default=None, metavar="STR",
        help="Override the default templated prompt.  STR is typed "
             "into the DeepSeek composer verbatim — exactly that "
             "string, no concatenation with the default template.  "
             "The rest of the pipeline (reset, apply, status, "
             "deeppeek dump, pipe) still runs and the attachments "
             "are still piped unless --no-pipe is also given.  The "
             "string is NOT str.format-ed; run --dry-run first to "
             "see what's available.",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the captured diff.txt and deeppeek dump.  "
             "Default: a fresh temp dir in /tmp.  Point this at a "
             "stable location to keep the artefacts around for "
             "inspection.",
    )
    p.add_argument(
        "--status-py", type=Path, default=None,
        help=f"override the status.py path "
             f"(default: {_STATUS_PY_SCRIPT}).  Must accept no "
             f"arguments and print a diff-style view to stdout.",
    )
    p.add_argument(
        "--dump-py", type=Path, default=None,
        help=f"override the dump.py script path "
             f"(default: {_DUMP_PY_SCRIPT}).  Must support `-r -o "
             f"PATH`; its DEFAULT_EXCLUDE_DIRS will be extended "
             f"with {_REFINE_DUMP_EXCLUDE_EXTRA}.",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="skip the pipe step; only type the composer text.  "
             "Useful when you want only --text in the composer with "
             "no file chips at all.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="run the reset + apply, capture the status, dump "
             "deeppeek, print the file list and the composer text, "
             "then stop — no pipe, no type",
    )
    p.add_argument(
        "--recover-timeout", type=float, default=1800.0,
        help="seconds for `deeppeek recover --apply-here` "
             "(default: 1800)",
    )
    p.add_argument(
        "--status-timeout", type=float, default=60.0,
        help="seconds for scripts/status.py (default: 60)",
    )
    p.add_argument(
        "--pipe-timeout", type=float, default=180.0,
        help="seconds for the pipe event (default: 180)",
    )
    p.add_argument(
        "--type-timeout", type=float, default=300.0,
        help="seconds for the type event (default: 300)",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="port for the local extension server (default: 8765)",
    )

    p.set_defaults(func=run)
