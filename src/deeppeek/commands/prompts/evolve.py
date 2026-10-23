"""`deeppeek prompt evolve` — craft the task prompt, type it, pipe inputs.

Pipeline:

    assemble   validate paths, infer deeppeek-source
    generate   run the real commands to produce the four inputs:
                 deeppeek capture -o <ws>/conversation.md --last N --watch \
                     --no-recover
                 deeppeek recover <conversation> --apply-to <target>
                 dump.py -r -o <ws>/project_dump_<target>.md
                 dump.py -r -o <ws>/project_dump_<source>.md
    build      format TASK_PROMPT_TEMPLATE with the real paths
    type       `deeppeek type <tempfile>`      ← the prompt text
    pipe       `deeppeek pipe conv dump meta/* dump`  ← the four inputs

The prompt text and the attachments are separate: the text names the
files, the pipe stage attaches them as file chips.  No inline content
in the prompt.  No Enter.  No reply capture.  No apply.

The deeppeek source tree is inferred automatically — from the package
location for editable installs, or from the current directory if it's
a deeppeek checkout.  No flag, no configuration.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "target", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".deeppeek", "cache", "deeppeek",
}
_EXCLUDE_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".xpi", ".whl", ".egg",
}
_MAX_ATTACHMENT_BYTES = 2_000_000

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DUMP_PY_SCRIPT = _PROJECT_ROOT / "scripts" / "dumps" / "dump.py"


TASK_PROMPT_TEMPLATE = """\
I maintain a CLI utility called deeppeek. Its purpose is to take an
extracted AI conversation in Markdown form — for example
{conversation} — parse the code patches contained in it, and apply
those patches reliably to a target project.

{extraction}

I attempted to apply the extracted patches to a target project with:

    deeppeek recover {conversation} --apply-to {target}

The result was a failure: no project files were created or modified,
apart from metadata artifacts. Those metadata files are under
{meta} and contain errors, warnings, and other side effects from
the attempted apply.

I am providing the following inputs, attached to this message as
files:

1. `{conversation_name}` — the extracted conversation Markdown
   containing the patches.
2. `{playground_dump_name}` — a dump/state of the target code
   playground after the attempted apply.
3. `{meta_name}/` — the deeppeek metadata artifacts (directory).
4. `{project_dump_name}` — a dump of the deeppeek project itself.
   This is the codebase I want you to edit.

Please fix the deeppeek CLI so that it can robustly turn a
conversation Markdown file into changes that are actually applied
through --apply-to. The current approach appears too fragile or too
narrowly tuned. Prefer improving the architecture: add more
abstraction, multiple processing passes, supporting infrastructure,
validation, tests, and sensible fallback/guessing mechanisms rather
than only tuning the existing parser.

The goal is a general-purpose utility that reliably extracts and
applies code patches from AI conversations, not a one-off fix for
this single conversation.
"""


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------

@dataclass
class _Inputs:
    conversation_path: Path
    target_path: Path
    meta_path: Path
    meta_paths: list[Path]
    playground_path: Path
    project_path: Path
    gen_conversation: str
    gen_playground: str
    gen_meta: str
    gen_project_dump: str


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None,
             allow_failure: bool = False) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
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
        print(f"    exit {proc.returncode} {tag}".rstrip(), file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _infer_deeppeek_source() -> Optional[Path]:
    """Infer the deeppeek source tree.

    Preference order:
      1. _PROJECT_ROOT derived from __file__ — correct for editable
         installs (pipx --editable, pip install -e .)
      2. Current working directory, if it's a deeppeek checkout

    A "source tree" is defined as: has a pyproject.toml AND has a
    src/deeppeek/ directory.  A frozen-wheel install points at
    site-packages instead and won't have either, so inference safely
    fails there and the caller is told to run from the source tree.
    """
    for c in (_PROJECT_ROOT, Path.cwd()):
        if not c.is_dir():
            continue
        if (c / "pyproject.toml").is_file() and \
           (c / "src" / "deeppeek").is_dir():
            return c
    return None


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _iter_text_files(root: Path):
    if root.is_file():
        yield root, root.name
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in _EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.suffix.lower() in _EXCLUDE_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_ATTACHMENT_BYTES:
                continue
        except OSError:
            continue
        yield p, rel.as_posix()


def _dump_to_path(directory: Path, out: Path, timeout: float,
                  dump_py: Optional[Path] = None) -> None:
    """Run `dump.py -r -o <out>` in `directory`.

    The output path is chosen by us and passed via `-o`, so we never
    need to guess what filename dump.py picks — it writes exactly
    where we told it.  Uses the running interpreter (sys.executable)
    so `dump.py` sees the same env as deeppeek itself.
    """
    script = dump_py or _DUMP_PY_SCRIPT
    if not directory.is_dir():
        raise SystemExit(f"error: dump target is not a directory: {directory}")
    if not script.exists():
        raise SystemExit(
            f"error: dump script not found at {script}\n"
            f"  (pass --playground-dump / --project-dump to skip the "
            f"dump step, or --dump-py to point at a different script)"
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    python_bin = sys.executable
    cmd = [python_bin, str(script), "-r", "-o", str(out)]
    print(f"    $ (cd {directory} && "
          f"{python_bin} {script} -r -o {out})", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, cwd=str(directory),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"error: dump timed out after {timeout:.0f}s")

    if proc.returncode != 0:
        print(f"    warning: exit {proc.returncode}", file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    for line in (proc.stdout or "").splitlines()[:2]:
        if line.strip():
            print(f"    {line.strip()}", file=sys.stderr)

    if not out.exists() or not out.read_text(encoding="utf-8").strip():
        raise SystemExit(
            f"error: dump.py did not produce {out}\n"
            f"  stdout: {proc.stdout[:200]!r}\n"
            f"  stderr: {proc.stderr[:200]!r}"
        )
    print(f"    wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# Prompt assembly — TEMPLATE ONLY, no attachment content
# ---------------------------------------------------------------------------

def _build_prompt(inputs: _Inputs, capture_cmd: Optional[str]) -> str:
    if capture_cmd:
        extraction = (
            "The conversation was extracted with:\n"
            "\n"
            f"    {capture_cmd}\n"
        )
    else:
        extraction = (
            "The conversation is a pre-existing Markdown export.\n"
        )

    return TASK_PROMPT_TEMPLATE.format(
        conversation=inputs.conversation_path.resolve(),
        conversation_name=inputs.conversation_path.name or "conversation.md",
        target=inputs.target_path.resolve(),
        meta=inputs.meta_path.resolve(),
        meta_name=inputs.meta_path.name or "deeppeek",
        playground_dump_name=inputs.playground_path.name,
        project_dump_name=inputs.project_path.name,
        extraction=extraction,
    )


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _stage_assemble(args: argparse.Namespace) -> None:
    print("[assemble] checking inputs", file=sys.stderr)
    if not args.deeppeek_source.is_dir():
        raise SystemExit(
            f"error: deeppeek-source resolved to a non-directory: "
            f"{args.deeppeek_source}"
        )
    if not args.target.is_dir():
        raise SystemExit(
            f"error: --target must be an existing directory: {args.target}"
        )
    if args.conversation is not None and not args.conversation.exists():
        raise SystemExit(
            f"error: --conversation not found: {args.conversation}"
        )
    for name in ("playground_dump", "meta_dir", "project_dump"):
        p = getattr(args, name)
        if p is not None and not p.exists():
            raise SystemExit(
                f"error: --{name.replace('_', '-')} not found: {p}"
            )
    print(f"  deeppeek-source: {args.deeppeek_source}", file=sys.stderr)
    print(f"  target:          {args.target}", file=sys.stderr)


def _stage_generate(args: argparse.Namespace
                     ) -> tuple[_Inputs, Optional[str]]:
    if args.workspace is None:
        ws = Path(tempfile.mkdtemp(prefix="deeppeek_evolve_"))
    else:
        ws = args.workspace.resolve()
        ws.mkdir(parents=True, exist_ok=True)

    try:
        ws_in_source = ws.is_relative_to(args.deeppeek_source.resolve())
    except (ValueError, OSError):
        ws_in_source = False
    if ws_in_source:
        print(
            f"    warning: workspace {ws} is INSIDE --deeppeek-source.  "
            f"dump.py -r will include the workspace's own files in the "
            f"project dump.  Use a path outside the source tree, or "
            f"omit --workspace to get a /tmp default.",
            file=sys.stderr,
        )

    print(f"[generate] workspace: {ws}", file=sys.stderr)
    print(f"  (kept after run for inspection; rm -rf {ws} when done)",
          file=sys.stderr)
    prefix = _deeppeek_cmd()

    # ---- 1. conversation ----
    capture_cmd: Optional[str] = None
    if args.conversation is not None:
        conversation_path = args.conversation.resolve()
        gen_conv = f"provided via --conversation ({conversation_path})"
        print(f"  conversation: using {conversation_path}", file=sys.stderr)
    else:
        conversation_path = ws / "conversation.md"
        # --no-recover: evolve runs recovery itself in step 2, so the
        # capture stage must only write the markdown.  Under the new
        # "recover-by-default" behaviour of `deeppeek capture`, this
        # flag is required to preserve that separation.
        cmd = prefix + [
            "capture", "-o", str(conversation_path),
            "--last", str(args.last),
            "--watch",
            "--no-recover",
            "--timeout", str(args.capture_timeout),
        ]
        print("  conversation: capturing via deeppeek", file=sys.stderr)
        _run_cmd(cmd, timeout=args.capture_timeout + 10,
                 allow_failure=False)
        gen_conv = f"`{' '.join(cmd)}`"
        capture_cmd = " ".join(cmd)
    if not conversation_path.exists():
        raise SystemExit(
            f"error: conversation file not produced: {conversation_path}"
        )

    # ---- 2. initial recover ----
    recover_rc: Optional[int] = None
    if args.skip_recover:
        print("  recover: SKIPPED via --skip-recover", file=sys.stderr)
    else:
        print("  recover: attempting (this is expected to fail)",
              file=sys.stderr)
        cmd = prefix + [
            "recover", str(conversation_path),
            "--apply-to", str(args.target),
        ]
        recover_rc, _, _ = _run_cmd(
            cmd, timeout=args.recover_timeout, allow_failure=True,
        )

    # ---- 3. meta ----
    if args.meta_dir is not None:
        meta_path = args.meta_dir.resolve()
        gen_meta = f"provided via --meta-dir ({meta_path})"
    else:
        meta_path = args.target / "deeppeek"
        if not meta_path.is_dir():
            meta_path = args.target / ".deeppeek"
        if recover_rc is not None:
            gen_meta = (
                f"side-effect of `deeppeek recover {conversation_path} "
                f"--apply-to {args.target}` (exit code {recover_rc})"
            )
        else:
            gen_meta = f"pre-existing state of {args.target}"
    meta_paths = [p for p, _ in _iter_text_files(meta_path)]
    print(f"  meta: {len(meta_paths)} file(s) from {meta_path}",
          file=sys.stderr)
    if not meta_paths and args.meta_dir is None:
        print(
            f"    warning: no metadata files under {meta_path}.  Did "
            f"recover write its reports elsewhere?  Continuing without "
            f"attachment 3.",
            file=sys.stderr,
        )

    # ---- 4. playground dump (post-recover state of target) ----
    if args.playground_dump is not None:
        playground_path = args.playground_dump.resolve()
        gen_playground = (
            f"provided via --playground-dump ({playground_path})"
        )
        print(f"  playground: using {playground_path}", file=sys.stderr)
    else:
        playground_path = ws / f"project_dump_{args.target.name}.md"
        print(f"  playground: dump.py -r in {args.target}", file=sys.stderr)
        _dump_to_path(
            args.target, playground_path,
            args.dump_py_timeout, dump_py=args.dump_py,
        )
        gen_playground = (
            f"`dump.py -r` in {args.target} "
            f"(output: {playground_path.name})"
        )

    # ---- 5. project dump (deeppeek source) ----
    if args.project_dump is not None:
        project_path = args.project_dump.resolve()
        gen_project_dump = (
            f"provided via --project-dump ({project_path})"
        )
        print(f"  project-dump: using {project_path}", file=sys.stderr)
    else:
        project_path = ws / f"project_dump_{args.deeppeek_source.name}.md"
        print(f"  project-dump: dump.py -r in {args.deeppeek_source}",
              file=sys.stderr)
        _dump_to_path(
            args.deeppeek_source, project_path,
            args.dump_py_timeout, dump_py=args.dump_py,
        )
        gen_project_dump = (
            f"`dump.py -r` in {args.deeppeek_source} "
            f"(output: {project_path.name})"
        )

    inputs = _Inputs(
        conversation_path=conversation_path,
        target_path=args.target,
        meta_path=meta_path,
        meta_paths=meta_paths,
        playground_path=playground_path,
        project_path=project_path,
        gen_conversation=gen_conv,
        gen_playground=gen_playground,
        gen_meta=gen_meta,
        gen_project_dump=gen_project_dump,
    )
    return inputs, capture_cmd


def _stage_type(prompt: str, args: argparse.Namespace) -> int:
    fd, path = tempfile.mkstemp(
        prefix="deeppeek_evolve_", suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        print(f"[type] {len(prompt):,} chars via `deeppeek type`",
              file=sys.stderr)
        prefix = _deeppeek_cmd()
        rc, _, _ = _run_cmd(
            prefix + ["type", path],
            timeout=args.type_timeout,
            allow_failure=False,
        )
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _stage_pipe(inputs: _Inputs, args: argparse.Namespace) -> int:
    files = [
        inputs.conversation_path,
        inputs.playground_path,
        *inputs.meta_paths,
        inputs.project_path,
    ]
    files = [f for f in files if f.exists()]
    if not files:
        print("[pipe] no files to attach", file=sys.stderr)
        return 0

    prefix = _deeppeek_cmd()
    print(f"[pipe] attaching {len(files)} file(s):", file=sys.stderr)
    for f in files:
        print(f"  {f}  ({f.stat().st_size:,} bytes)", file=sys.stderr)
    rc, _, _ = _run_cmd(
        prefix + ["pipe"] + [str(f) for f in files],
        timeout=args.pipe_timeout,
        allow_failure=False,
    )
    return rc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    # Infer deeppeek-source; no flag, no configuration needed.
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
    args.deeppeek_source = inferred
    print(f"[inferred] deeppeek-source: {args.deeppeek_source}",
          file=sys.stderr)

    _stage_assemble(args)
    inputs, capture_cmd = _stage_generate(args)
    prompt = _build_prompt(inputs, capture_cmd)
    print(f"[build] prompt: {len(prompt):,} chars", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] prompt below; not typed.", file=sys.stderr)
        print(prompt)
        if not args.no_pipe:
            pipe_files = [
                inputs.conversation_path,
                inputs.playground_path,
                *inputs.meta_paths,
                inputs.project_path,
            ]
            pipe_files = [f for f in pipe_files if f.exists()]
            print(f"\n[dry-run] would attach {len(pipe_files)} "
                  f"file(s):", file=sys.stderr)
            for f in pipe_files:
                print(f"  {f}  ({f.stat().st_size:,} bytes)",
                      file=sys.stderr)
        return 0

    rc = _stage_type(prompt, args)
    if rc != 0:
        return rc

    if args.no_pipe:
        print("\n[--no-pipe] skipping attachments", file=sys.stderr)
        return 0

    return _stage_pipe(inputs, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "evolve",
        help="Craft a task prompt, type it, pipe the four inputs",
    )

    p.add_argument(
        "--target", type=Path, required=True,
        help="target project tree the initial `--apply-to` is attempted "
             "against; use a scratch copy — the tree is mutated",
    )

    p.add_argument(
        "--conversation", type=Path, default=None,
        help="pre-captured conversation Markdown.  Omit to run "
             "`deeppeek capture --last N --watch`.",
    )
    p.add_argument(
        "--last", type=int, default=3, metavar="N",
        help="passed to `deeppeek capture --last N` (default: 3)",
    )

    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for generated artefacts (conversation capture, "
             "both dumps).  Default: a fresh temp dir in /tmp.  Do NOT "
             "point this inside the deeppeek source tree.",
    )
    p.add_argument(
        "--playground-dump", type=Path, default=None,
        help="skip the target dump; use this file",
    )
    p.add_argument(
        "--meta-dir", type=Path, default=None,
        help="skip reading <target>/deeppeek; use this directory",
    )
    p.add_argument(
        "--project-dump", type=Path, default=None,
        help="skip the deeppeek-source dump; use this file",
    )
    p.add_argument(
        "--skip-recover", action="store_true",
        help="don't run the initial `recover --apply-to`",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="type the prompt but don't attach the input files",
    )

    p.add_argument(
        "--dump-py", type=Path, default=None,
        help=f"override the dump.py script path "
             f"(default: {_DUMP_PY_SCRIPT}).  Must support `-r -o PATH`.",
    )
    p.add_argument(
        "--dump-py-timeout", type=float, default=300.0,
        help="seconds for each `dump.py -r` call (default: 300)",
    )
    p.add_argument(
        "--capture-timeout", type=float, default=300.0,
        help="seconds for `deeppeek capture --watch` (default: 300)",
    )
    p.add_argument(
        "--recover-timeout", type=float, default=1800.0,
        help="seconds for the initial `recover --apply-to` (default: 1800)",
    )
    p.add_argument(
        "--type-timeout", type=float, default=600.0,
        help="seconds for `deeppeek type` (default: 600)",
    )
    p.add_argument(
        "--pipe-timeout", type=float, default=180.0,
        help="seconds for `deeppeek pipe` (default: 180)",
    )

    p.add_argument(
        "--dry-run", action="store_true",
        help="print the prompt and would-attach list to stdout and stop "
             "(no type, no pipe)",
    )

    p.set_defaults(func=run)
