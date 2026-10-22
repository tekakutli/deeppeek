"""`deeppeek prompt evolve` — evolve deeppeek using a failed recovery.

Intention: given a conversation Markdown that represents a failed
`deeppeek recover --apply-to` attempt against a target project, ask
DeepSeek to fix the deeppeek CLI so that the *next* apply-to of the
same conversation actually writes files.  This is deeppeek using its
own tooling — capture, recover, type, enter — against its own source.

Five stages, each a function:

    assemble   validate paths
    generate   run the real commands to produce the four artefacts:
                 deeppeek capture -o <ws>/conversation.md --last N --watch
                 deeppeek recover <conversation> --apply-to <target>
                 copy <target>/deeppeek/       -> <ws>/meta/
                 scan <target>                  -> <ws>/playground_dump.md
                 dump_py -r in <source>         -> <ws>/project_dump_deeppeek.md
               then build the task prompt with those paths interpolated
    compose    fold prompt + four artefacts -> <ws>/bundle.md
    dispatch   deeppeek type <bundle>
               deeppeek enter
    await      poll `deeppeek capture --watch --last 1` until the
               captured file size is stable across two polls
    apply      deeppeek recover <response> --apply-to <source>

Self-invocation: every network-bound step shells out to a
`deeppeek` subprocess.  `_deeppeek_cmd()` prefers the installed
binary and falls back to `sys.executable -m deeppeek` when it's not
on PATH.  Each subprocess opens its own ExportServer on port 8765 —
no state leaks between stages, and every stage can be re-run
standalone for debugging.

The task prompt is NOT a fixed string: it is built by
`_build_prompt` after `generate`, with every path interpolated.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "target", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".deeppeek",
}
_EXCLUDE_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".xpi", ".whl", ".egg",
}
_MAX_ATTACHMENT_BYTES = 2_000_000


# ---------------------------------------------------------------------------
# Task prompt template (filled in by _build_prompt)
# ---------------------------------------------------------------------------

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

I am providing the following inputs:

1. The extracted conversation Markdown containing the patches:
   {conversation}
2. A dump/state of the target code playground after the attempted
   apply: {playground_dump}
3. The deeppeek metadata artifacts:
   {meta}
4. A dump of the deeppeek project itself:
   {project_dump}
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
class _Artefacts:
    conversation: Path
    playground_dump: Path
    meta_dir: Path
    project_dump: Path
    prompt: str
    gen_conversation: str
    gen_playground: str
    gen_meta: str
    gen_project_dump: str


# ---------------------------------------------------------------------------
# Subprocess + filesystem helpers
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    """argv prefix for invoking `deeppeek` as a subprocess."""
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *,
             timeout: Optional[float] = None,
             allow_failure: bool = False,
             cwd: Optional[Path] = None) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=(str(cwd) if cwd else None),
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


def _walk_text_files(root: Path):
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


def _fence_for(content: str) -> str:
    longest = 0
    cur = 0
    for ch in content:
        if ch == "`":
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return "`" * max(3, longest + 1)


def _emit_fenced(lines: list[str], content: str) -> None:
    fence = _fence_for(content)
    lines.append(fence)
    lines.append(content.rstrip("\n"))
    lines.append(fence)
    lines.append("")


def _render_tree_lines(root: Path, excludes: set[str]) -> list[str]:
    def _visible(p: Path) -> bool:
        return (not p.name.startswith(".")) and (p.name not in excludes)

    lines = ["./"]

    def _recurse(d: Path, prefix: str) -> None:
        entries = sorted(
            (e for e in d.iterdir() if _visible(e)),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
        for i, entry in enumerate(entries):
            last = (i == len(entries) - 1)
            conn = "└── " if last else "├── "
            lines.append(
                f"{prefix}{conn}{entry.name}{'/' if entry.is_dir() else ''}"
            )
            if entry.is_dir():
                _recurse(entry, prefix + ("    " if last else "│   "))

    _recurse(root, "")
    return lines


def _generate_directory_dump(root: Path, out: Path) -> None:
    """Emit the SCANNED DIRECTORY format (used for the target playground)."""
    if not root.exists():
        raise SystemExit(f"error: cannot dump nonexistent path: {root}")
    out.parent.mkdir(parents=True, exist_ok=True)

    if root.is_file():
        content = root.read_text(encoding="utf-8", errors="replace")
        lines = [
            "=" * 70,
            f"SCANNED FILE: {root.resolve()}",
            "=" * 70,
            "",
            "=" * 70,
            "FILE CONTENTS",
            "=" * 70,
            "```python",
            content.rstrip("\n"),
            "```",
        ]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    lines: list[str] = []
    lines += ["=" * 70, f"SCANNED DIRECTORY: {root.resolve()}",
              "=" * 70, ""]
    lines += ["=" * 70, "DIRECTORY TREE", "=" * 70, "```"]
    lines += _render_tree_lines(root, _EXCLUDE_DIR_NAMES)
    lines += ["```", ""]

    files = list(_walk_text_files(root))
    total = len(files)
    for i, (fp, rel) in enumerate(files, 1):
        lines += ["=" * 70, f"FILE {i}/{total}: {rel}", "=" * 70,
                  "```python"]
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            content = f"<unreadable: {e}>"
        lines.append(content.rstrip("\n"))
        lines += ["```", ""]

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"    wrote {out} ({out.stat().st_size:,} bytes, "
          f"{total} files)", file=sys.stderr)


def _generate_project_dump_with_dump_py(source: Path, out: Path,
                                         timeout: float) -> None:
    """Run `dump_py -r` in `source` via an interactive bash shell.

    `dump_py` is a bashrc function, so `bash -ic` is required to
    source `.bashrc`.  Because bashrc can print banners, prompts, or
    alias listings to stdout, the call is wrapped between two per-run
    unique markers and only the region between them is kept.
    """
    if not source.exists() or not source.is_dir():
        raise SystemExit(
            f"error: --deeppeek-source must be a directory: {source}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex[:8]
    begin = f"__DPEVOLVE_BEGIN_{run_id}__"
    end = f"__DPEVOLVE_END_{run_id}__"
    script = (
        f"printf '%s\\n' {begin}; "
        f"dump_py -r; "
        f"printf '%s\\n' {end}"
    )

    print(f"    $ (cd {source} && bash -ic 'dump_py -r')",
          file=sys.stderr)
    try:
        proc = subprocess.run(
            ["bash", "-ic", script], cwd=str(source),
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise SystemExit("error: `bash` not on PATH")
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"error: `dump_py -r` timed out after {timeout:.0f}s"
        )

    if proc.returncode != 0:
        print(f"    warning: dump_py exited {proc.returncode}",
              file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)

    stdout = proc.stdout
    if begin in stdout and end in stdout:
        body = stdout.split(begin, 1)[1].split(end, 1)[0].lstrip("\n")
    else:
        print("    warning: markers not found in dump_py stdout; "
              "using raw output", file=sys.stderr)
        body = stdout

    out.write_text(body, encoding="utf-8")
    print(f"    wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)


def _collect_meta(target: Path, dest: Path) -> str:
    """Copy the target's deeppeek/ reports into `dest`.  Returns a
    human-readable provenance string."""
    if not target.is_dir():
        return ""
    sources: list[str] = []
    for sub in ("deeppeek", ".deeppeek"):
        cand = target / sub
        if cand.is_dir():
            shutil.copytree(
                cand, dest, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("cache"),
            )
            sources.append(str(cand))
    for name in ("manifest.json", "warnings.txt", "deleted.txt",
                 "MISSING_FILES.txt"):
        f = target / name
        if f.is_file():
            shutil.copy2(f, dest / name)
            sources.append(str(f))
    return ", ".join(sources) if sources else ""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    *,
    conversation: Path,
    target: Path,
    meta: Path,
    playground_dump: Path,
    project_dump: Path,
    capture_cmd: Optional[str],
) -> str:
    if capture_cmd:
        extraction = (
            "The conversation was extracted with:\n"
            "\n"
            f"    {capture_cmd}\n"
        )
    else:
        extraction = (
            "The conversation is a pre-existing Markdown export "
            "(not captured this run).\n"
        )
    return TASK_PROMPT_TEMPLATE.format(
        conversation=conversation.resolve(),
        target=target.resolve(),
        meta=meta.resolve(),
        playground_dump=playground_dump.resolve(),
        project_dump=project_dump.resolve(),
        extraction=extraction,
    )


# ---------------------------------------------------------------------------
# Stage 1 — assemble
# ---------------------------------------------------------------------------

def _stage_assemble(args: argparse.Namespace) -> None:
    print("[assemble] checking inputs", file=sys.stderr)
    if not args.deeppeek_source.exists() or not args.deeppeek_source.is_dir():
        raise SystemExit(
            f"error: --deeppeek-source must be an existing directory: "
            f"{args.deeppeek_source}"
        )
    if not args.target.exists() or not args.target.is_dir():
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
            raise SystemExit(f"error: --{name.replace('_', '-')} not found: {p}")
    print(f"  deeppeek-source: {args.deeppeek_source}", file=sys.stderr)
    print(f"  target:          {args.target}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Stage 2 — generate
# ---------------------------------------------------------------------------

def _stage_generate(args: argparse.Namespace) -> _Artefacts:
    ws = args.workspace
    ws.mkdir(parents=True, exist_ok=True)
    print(f"[generate] workspace: {ws}", file=sys.stderr)
    prefix = _deeppeek_cmd()

    capture_cmd: Optional[str] = None
    if args.conversation is not None:
        conversation = args.conversation
        gen_conv = f"provided by user via --conversation ({conversation})"
        print(f"  conversation: using {conversation}", file=sys.stderr)
    else:
        conversation = ws / "conversation.md"
        cmd = prefix + [
            "capture", "-o", str(conversation),
            "--last", str(args.last),
            "--watch",
            "--timeout", str(args.capture_timeout),
        ]
        print("  conversation: capturing via deeppeek", file=sys.stderr)
        _run_cmd(cmd, timeout=args.capture_timeout + 10,
                 allow_failure=False)
        gen_conv = f"`{' '.join(cmd)}`"
        capture_cmd = " ".join(cmd)
    if not conversation.exists():
        raise SystemExit(
            f"error: conversation file not produced: {conversation}"
        )

    recover_rc: Optional[int] = None
    recover_cmd_str = (
        f"deeppeek recover {conversation} --apply-to {args.target}"
    )
    if args.skip_recover:
        print("  recover: SKIPPED via --skip-recover", file=sys.stderr)
    else:
        print("  recover: attempting (this is expected to fail)",
              file=sys.stderr)
        cmd = prefix + [
            "recover", str(conversation),
            "--apply-to", str(args.target),
        ]
        recover_rc, _, _ = _run_cmd(
            cmd, timeout=args.recover_timeout, allow_failure=True,
        )

    if args.meta_dir is not None:
        meta_dir = args.meta_dir
        gen_meta = "provided by user via --meta-dir"
        print(f"  meta: using {meta_dir}", file=sys.stderr)
    else:
        meta_dir = ws / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        copied_from = _collect_meta(args.target, meta_dir)
        if recover_rc is not None:
            gen_meta = (
                f"side-effect of `{recover_cmd_str}` "
                f"(exit code {recover_rc})"
            )
            if copied_from:
                gen_meta += f", collected from {copied_from}"
        else:
            gen_meta = f"pre-existing state of {args.target}"
        print(f"  meta: {meta_dir} ({copied_from or 'empty'})",
              file=sys.stderr)

    if args.playground_dump is not None:
        playground_dump = args.playground_dump
        gen_playground = "provided by user via --playground-dump"
        print(f"  playground: using {playground_dump}", file=sys.stderr)
    else:
        playground_dump = ws / "playground_dump.md"
        print(f"  playground: scanning {args.target}", file=sys.stderr)
        _generate_directory_dump(args.target, playground_dump)
        gen_playground = f"directory scan of {args.target}"

    if args.project_dump is not None:
        project_dump = args.project_dump
        gen_project_dump = "provided by user via --project-dump"
        print(f"  project-dump: using {project_dump}", file=sys.stderr)
    else:
        project_dump = ws / "project_dump_deeppeek.md"
        print(f"  project-dump: running `dump_py -r` in "
              f"{args.deeppeek_source}", file=sys.stderr)
        _generate_project_dump_with_dump_py(
            args.deeppeek_source, project_dump, args.dump_py_timeout,
        )
        gen_project_dump = (
            f"`bash -ic 'dump_py -r'` run with cwd={args.deeppeek_source}"
        )

    prompt = _build_prompt(
        conversation=conversation,
        target=args.target,
        meta=meta_dir,
        playground_dump=playground_dump,
        project_dump=project_dump,
        capture_cmd=capture_cmd,
    )
    print(f"  prompt: built ({len(prompt):,} chars)", file=sys.stderr)

    return _Artefacts(
        conversation=conversation,
        playground_dump=playground_dump,
        meta_dir=meta_dir,
        project_dump=project_dump,
        prompt=prompt,
        gen_conversation=gen_conv,
        gen_playground=gen_playground,
        gen_meta=gen_meta,
        gen_project_dump=gen_project_dump,
    )


# ---------------------------------------------------------------------------
# Stage 3 — compose
# ---------------------------------------------------------------------------

def _emit_prompt_section(lines: list[str], title: str,
                         gen_method: str, prompt: str) -> None:
    lines += [
        f"## {title}", "",
        f"**Generation method:** {gen_method}", "",
        prompt.rstrip(), "",
    ]


def _emit_file_section(lines: list[str], title: str,
                       gen_method: str, path: Path,
                       is_dir: bool = False) -> None:
    lines += [
        f"## {title}", "",
        f"**Generation method:** {gen_method}", "",
        f"**Path:** `{path}`", "",
    ]
    if is_dir and path.is_dir():
        lines += ["Directory. Contents below, one subsection per file.", ""]
        count = 0
        for file_path, rel in _walk_text_files(path):
            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as e:
                lines += [f"### `{rel}`", "", f"<unreadable: {e}>", ""]
                continue
            count += 1
            lines += [f"### `{rel}`", ""]
            _emit_fenced(lines, content)
        if count == 0:
            lines += ["<no readable text files>", ""]
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        lines += [f"<unreadable: {e}>", ""]
        return
    _emit_fenced(lines, content)


def _stage_compose(arts: _Artefacts, out: Path) -> Path:
    print(f"[compose] writing bundle -> {out}", file=sys.stderr)

    index = [
        ("[0] Task prompt",
         "dynamically built by `_build_prompt` in `prompts/evolve.py` "
         "from the resolved paths of this run"),
        ("[1] 1. Extracted conversation Markdown",
         arts.gen_conversation),
        ("[2] 2. Target code-playground dump",
         arts.gen_playground),
        ("[3] 3. deeppeek meta artifacts",
         arts.gen_meta),
        ("[4] 4. project_dump_deeppeek.md",
         arts.gen_project_dump),
    ]

    lines: list[str] = [
        "# deeppeek prompt evolve — payload bundle", "",
        (f"_Generated "
         f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}_"),
        "",
        ("Each entry below is prefixed with the command or tool that "
         "produced it, so the recipient can reason about provenance "
         "as well as content.  Entry 0 is the task prompt; entries "
         "1+ are the inputs it references."),
        "",
        "## Payload index", "",
    ]
    for header, method in index:
        lines.append(f"- **{header}**")
        lines.append(f"  — {method}")
    lines += ["", "---", ""]

    _emit_prompt_section(
        lines, "[0] Task prompt",
        index[0][1], arts.prompt,
    )
    _emit_file_section(
        lines, "[1] 1. Extracted conversation Markdown",
        arts.gen_conversation, arts.conversation,
    )
    _emit_file_section(
        lines, "[2] 2. Target code-playground dump",
        arts.gen_playground, arts.playground_dump,
    )
    _emit_file_section(
        lines, "[3] 3. deeppeek meta artifacts",
        arts.gen_meta, arts.meta_dir, is_dir=True,
    )
    _emit_file_section(
        lines, "[4] 4. project_dump_deeppeek.md",
        arts.gen_project_dump, arts.project_dump,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    size = out.stat().st_size
    print(f"  {size:,} bytes, {len(index)} entries", file=sys.stderr)
    if size > 400_000:
        print(
            "  warning: bundle is >400 KiB.  DeepSeek may truncate it. "
            "Consider limiting --target or --deeppeek-source.",
            file=sys.stderr,
        )
    return out


# ---------------------------------------------------------------------------
# Stage 4 — dispatch (via `deeppeek type` + `deeppeek enter`)
# ---------------------------------------------------------------------------

def _stage_dispatch(bundle_path: Path, args: argparse.Namespace) -> int:
    prefix = _deeppeek_cmd()

    print(f"[dispatch] typing {bundle_path.name} "
          f"({bundle_path.stat().st_size:,} bytes)", file=sys.stderr)
    rc, _, _ = _run_cmd(
        prefix + ["type", str(bundle_path)],
        timeout=args.dispatch_timeout,
        allow_failure=False,
    )
    if rc != 0:
        print(f"[dispatch] type failed (exit {rc})", file=sys.stderr)
        return rc

    print("[dispatch] pressing Enter", file=sys.stderr)
    rc, _, _ = _run_cmd(
        prefix + ["enter"],
        timeout=args.dispatch_timeout,
        allow_failure=False,
    )
    return rc


# ---------------------------------------------------------------------------
# Stage 5 — await (poll capture until the reply stops growing)
# ---------------------------------------------------------------------------

def _stage_await(args: argparse.Namespace, ws: Path) -> Optional[Path]:
    if args.response is not None:
        if not args.response.exists():
            print(f"[await] --response not found: {args.response}",
                  file=sys.stderr)
            return None
        print(f"[await] using pre-recorded response {args.response}",
              file=sys.stderr)
        return args.response

    response_path = ws / "response.md"
    if response_path.exists():
        response_path.unlink()

    prefix = _deeppeek_cmd()
    print(f"[await] polling for a stable reply "
          f"(max {args.await_timeout:.0f}s, interval "
          f"{args.await_poll:.0f}s)...", file=sys.stderr)

    deadline = time.monotonic() + args.await_timeout
    last_size = 0
    stable_checks = 0

    while time.monotonic() < deadline:
        time.sleep(args.await_poll)

        cmd = prefix + [
            "capture", "-o", str(response_path),
            "--last", "1",
            "--watch",
            "--no-recover",
            "--timeout", "15",
        ]
        rc, _, _ = _run_cmd(cmd, timeout=20, allow_failure=True)
        if rc != 0 or not response_path.exists():
            print("  capture unavailable; retrying", file=sys.stderr)
            continue

        size = response_path.stat().st_size
        if size == 0:
            continue
        if size == last_size:
            stable_checks += 1
            print(f"  reply stable at {size:,} bytes "
                  f"({stable_checks}/2)", file=sys.stderr)
            if stable_checks >= 2:
                print(f"[await] reply stable at {size:,} bytes",
                      file=sys.stderr)
                return response_path
        else:
            stable_checks = 0
            last_size = size
            print(f"  reply growing ({size:,} bytes)", file=sys.stderr)

    if response_path.exists() and response_path.stat().st_size > 0:
        print(f"[await] timeout; using last capture "
              f"({response_path.stat().st_size:,} bytes)",
              file=sys.stderr)
        return response_path

    print("[await] no reply captured", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Stage 6 — apply (via `deeppeek recover --apply-to`)
# ---------------------------------------------------------------------------

def _stage_apply(response_path: Path, args: argparse.Namespace) -> int:
    apply_target = args.deeppeek_source
    print(f"[apply] applying DeepSeek's fix to {apply_target}",
          file=sys.stderr)

    prefix = _deeppeek_cmd()
    cmd = prefix + [
        "recover", str(response_path),
        "--apply-to", str(apply_target),
    ]
    print(f"[apply] $ {' '.join(cmd)}", file=sys.stderr)
    print(
        "[apply] note: this is the code the prompt is asking DeepSeek to\n"
        "        fix.  A non-zero exit means the current recover still\n"
        "        can't apply the reply's patches — inspect with git diff,\n"
        "        or git checkout -- . to revert.",
        file=sys.stderr,
    )
    try:
        proc = subprocess.run(cmd, timeout=args.recover_timeout)
    except subprocess.TimeoutExpired:
        print(f"[apply] timed out after {args.recover_timeout:.0f}s",
              file=sys.stderr)
        return 3
    return proc.returncode


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    _stage_assemble(args)
    arts = _stage_generate(args)
    bundle = _stage_compose(
        arts, args.bundle_out or (args.workspace / "bundle.md"),
    )

    if args.dry_run:
        print("\n[dry-run] stopping after compose.", file=sys.stderr)
        print(f"  bundle: {bundle}", file=sys.stderr)
        return 0

    rc = _stage_dispatch(bundle, args)
    if rc != 0:
        print(f"[dispatch] failed: exit {rc}", file=sys.stderr)
        return rc

    response = _stage_await(args, args.workspace)
    if response is None:
        return 3
    print(f"[await] response: {response}", file=sys.stderr)

    if args.no_apply:
        print("\n[--no-apply] skipping apply.", file=sys.stderr)
        return 0

    return _stage_apply(response, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "evolve",
        help="Evolve deeppeek using a conversation about a failed apply",
    )

    p.add_argument(
        "--deeppeek-source", type=Path, required=True,
        help="the deeppeek source tree — dumped via dump_py -r, and is "
             "the tree DeepSeek's reply gets applied to at the end",
    )
    p.add_argument(
        "--target", type=Path, required=True,
        help="the target project tree the initial `--apply-to` is "
             "attempted against; its state and meta artefacts go into "
             "the bundle.  Use a scratch copy — the tree is mutated.",
    )

    p.add_argument(
        "--conversation", type=Path, default=None,
        help="pre-captured conversation Markdown.  Omit to run "
             "`deeppeek capture --last N --watch` yourself.",
    )
    p.add_argument(
        "--last", type=int, default=3, metavar="N",
        help="passed to `deeppeek capture --last N` (default: 3)",
    )

    p.add_argument(
        "--workspace", type=Path,
        default=Path("./deeppeek_evolve_workspace"),
        help="where generated artefacts go "
             "(default: ./deeppeek_evolve_workspace)",
    )
    p.add_argument(
        "--bundle-out", type=Path, default=None,
        help="where to write the composed bundle "
             "(default: <workspace>/bundle.md)",
    )
    p.add_argument(
        "--playground-dump", type=Path, default=None,
        help="skip the target scan; use this file",
    )
    p.add_argument(
        "--meta-dir", type=Path, default=None,
        help="skip the meta collection; use this directory",
    )
    p.add_argument(
        "--project-dump", type=Path, default=None,
        help="skip the deeppeek-source scan; use this file",
    )
    p.add_argument(
        "--skip-recover", action="store_true",
        help="don't run the initial `recover --apply-to`; assume "
             "--target is already in post-recover state",
    )

    p.add_argument(
        "--response", type=Path, default=None,
        help="pre-recorded DeepSeek reply; skips dispatch+await",
    )
    p.add_argument(
        "--await-timeout", type=float, default=600.0, metavar="SECONDS",
        help="total time to wait for DeepSeek's reply to stabilise "
             "(default: 600)",
    )
    p.add_argument(
        "--await-poll", type=float, default=30.0, metavar="SECONDS",
        help="seconds between capture polls during await "
             "(default: 30)",
    )

    p.add_argument(
        "--capture-timeout", type=float, default=300.0,
        help="seconds for `deeppeek capture --watch` (default: 300)",
    )
    p.add_argument(
        "--recover-timeout", type=float, default=1800.0,
        help="seconds for the initial `recover --apply-to` and the "
             "final apply (default: 1800)",
    )
    p.add_argument(
        "--dispatch-timeout", type=float, default=600.0,
        help="seconds for `deeppeek type` and `deeppeek enter` "
             "(default: 600)",
    )
    p.add_argument(
        "--dump-py-timeout", type=float, default=300.0,
        help="seconds for `dump_py -r` (default: 300)",
    )

    p.add_argument(
        "--dry-run", action="store_true",
        help="stop after composing the bundle",
    )
    p.add_argument(
        "--no-apply", action="store_true",
        help="await a reply but skip the final apply",
    )

    p.set_defaults(func=run)
