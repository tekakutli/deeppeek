"""`deeppeek prompt refine_deeppeek` — apply, review the aftermath,
merge all inputs into one file, and pipe the merged file so the model
can propose concrete refinements.

Pipeline (extends `review`):

    verify     hard: git work tree (reset depends on it)
    warn       informational: no deeppeek/ dir yet
    reset      git reset --hard HEAD && git clean -fd
    apply      deeppeek recover <conversation> --apply-here
    require    hard: deeppeek/ must exist post-apply
    status     scripts/status.py in cwd → <ws>/diff.txt
    dump       `dump.py -r` from <repo>/src/deeppeek/ with
               DEFAULT_INCLUDE_DIRS = ['recover', 'write'] → covers
               every .py under recover/ and recover/write/, plus the
               three top-level deeppeek module files
    cwd-dump   (optional, --with-dump) dump_filtered.py in cwd,
               filtered to files whose basenames appear as `## File:`
               headers in the conversation
    merge      diff.txt + meta files + conversation.md + deeppeek_dump.md
               [+ project_dump_<cwd>_filtered.md with --with-dump]
               → a single refine_deeppeek_merged.md via
               scripts/merge_md.py
    pipe       the single merged.md as one file chip
    type       short templated prompt

The four (or five) inputs are merged into one markdown file before
they hit the pipe stage.  `merge_md.py` uses `+` separators and
`MERGED FILE i/N:` headers, so the merged file remains unambiguous
even when its inputs are themselves dump files.  The AI gets one
file chip containing the conversation, the diff, the reports, and
the deeppeek source dump, all in one.

Differences from `review`:

  · The original conversation markdown is included in the merged
    file, so the model can see what was actually applied (not just
    what happened when applying it).
  · A dump of deeppeek's own source is included, so the model can
    quote specific file paths and function names when proposing fixes.
  · With `--with-dump`, a filtered dump of the current working
    directory is included too, so the model can see the actual current
    state of every file the conversation tries to edit.  The dump is
    produced by `scripts/dumps/dump_filtered.py`, which extracts every
    `## File:` basename from the conversation, scans cwd for matches,
    and passes an explicit file list to `dump.py`.  Basename matching
    is intentionally loose: `utils.py` in several directories all
    match, because the conversation may not encode the project's real
    layout, and a false positive costs one extra file while a false
    negative costs a file the model needs to see.

What the deeppeek-source dump covers
====================================
Concretely: `dump.py -r` is run from `<repo>/src/deeppeek/` with
`DEFAULT_INCLUDE_DIRS = ['recover', 'write']`.  The include-dirs
filter matches basenames at every level of the walk, so:

  · `src/deeppeek/recover/` — every `.py` file: all 9 passes
    (extract, classify, resolve, attribute, dedup, apply_patches,
    validate, verify, reconcile, reverify, write, pipeline, and
    their helper modules), every patch_*.py heuristic, every
    structural helper, and the supporting passes.
  · `src/deeppeek/recover/write/` — every `.py` file: pass 9's
    implementation (pass9.py) and its six helper modules
    (_manifest, _scan, _text, _delete, _disposition, _summary).
  · `src/deeppeek/*.py` — the three top-level module files
    (__init__.py, __main__.py, cli.py).  Included because
    include_dirs only prunes subdirectories, not files at the
    scan root.  They're tiny and contextual.

That's ~80 files.

What it deliberately does NOT cover
===================================
Tests: `deeppeek prompt send_tests` is the workflow for test
regressions.  It attaches the failing test files, their fixtures,
their expected output trees, their pre-existing baselines, and the
test-runner log — precisely scoped to what failed.  That's a
separate command for a separate task.

Non-recover code (`capture/`, `commands/`, `extension/`,
`prompts/`) is not covered because the refine prompt is about
making `recover` general and robust, not about the browser bridge,
CLI dispatch, or AI prompt pipelines.

Docs, README, pyproject.toml, and scripts/ are not covered.  They
contribute signal the model can reason about without (the README's
architecture summary; the `[ast]` extras list), but including them
means the AI spends attention on material that isn't the source of
a `--apply-to` bug.  If a specific fix needs one of those files,
that's a case for attaching it manually to the conversation rather
than shipping it in every refine run.

`--text STR` behaves exactly as in `review`: it replaces the composer
text verbatim, and the merged file is still piped unless `--no-pipe`
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
_DUMP_FILTERED_PY_SCRIPT = _PROJECT_ROOT / "scripts" / "dumps" / "dump_filtered.py"
_MERGE_MD_SCRIPT = _PROJECT_ROOT / "scripts" / "merge_md.py"
_DUMP_FILTERED_TIMEOUT = 300.0  # seconds, for the dump_filtered.py subprocess
DEFAULT_CONVERSATION = Path("/tmp/conversation.md")

# `DEFAULT_INCLUDE_DIRS` matches directory *basenames* at every level
# of the walk.  From `<repo>/src/deeppeek/`, this whitelist keeps
# `recover/` and prunes every other subdirectory.  Within `recover/`,
# it keeps `write/` and prunes `__pycache__/` and any stray hidden
# directory.  Result: recover/*.py + recover/write/*.py, plus the
# three top-level deeppeek module files.
_DUMP_INCLUDE_DIRS = ["recover", "write"]


DEFAULT_PROMPT_TEMPLATE = """\
This is the outcome of my deeppeek utility trying to apply the patches
you just printed through: "deeppeek recover {conversation_name} --apply-here".

I've attached a single merged markdown file ({merged_name}) that
bundles, in order: the original conversation markdown,
diff.txt (what actually changed on disk), the reports under
deeppeek/, and a dump of deeppeek's own source
({deeppeek_dump_name}).  The merge uses per-file headers so you can
locate each piece.

Help me evaluate the results.  If you detect bugs, propose robust
fixes.  Better still, build infrastructure, further abstractions, or
additional passes so that whatever blocked a successful apply is
resolved.  deeppeek must be able to ingest any script in the wild and
have general solutions to its challenges — nothing custom.

Focus on deeppeek.  Forget about building on the project we were
working on for now. Try to use structural techniques, like AST
tree-sitter, over regex.
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
# deeppeek source dump — scan mode with DEFAULT_INCLUDE_DIRS override
# ---------------------------------------------------------------------------

def _stage_dump_deeppeek(source: Path, ws: Path) -> Path:
    """Dump the recovery pipeline from `<source>/src/deeppeek/`.

    Runs `dump.py -r` with its module-level `DEFAULT_INCLUDE_DIRS`
    overridden to `_DUMP_INCLUDE_DIRS` (`['recover', 'write']`),
    chdir'd to the deeppeek package root so `DEFAULT_DIR = "."`
    scans exactly that tree.

    The include-dirs filter matches basenames at every level, so
    'recover' keeps `src/deeppeek/recover/` and prunes every other
    subdirectory at the top; 'write' keeps `recover/write/` and
    prunes `__pycache__/` inside recover/.  Files at the scan root
    (`__init__.py`, `__main__.py`, `cli.py`) are also collected,
    since include_dirs only prunes directories.  ~80 files total.

    Loading dump.py as a module (rather than shelling out) lets us
    override the module-level config without needing a CLI flag
    that doesn't exist.  Same pattern as `dumps_compare.py`'s
    OVERRIDES dict.
    """
    dump_py = _DUMP_PY_SCRIPT
    if not dump_py.is_file():
        raise SystemExit(
            f"error: dump.py not found at {dump_py}\n"
            f"  this file ships with deeppeek at "
            f"`scripts/dumps/dump.py`;\n"
            f"  reinstall from a source checkout "
            f"(`pipx install --editable /path/to/deeppeek --force`)"
        )

    scan_root = source / "src" / "deeppeek"
    if not scan_root.is_dir():
        raise SystemExit(
            f"error: expected deeppeek package at {scan_root}\n"
            f"  (source tree layout: <root>/src/deeppeek/)"
        )

    out = ws / "deeppeek_dump.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "refine_dump_instance", str(dump_py))
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: could not load dump.py from {dump_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The refine prompt's target is the recovery pipeline.  Everything
    # else under src/deeppeek/ (capture, commands, extension, prompts)
    # is out of scope, and the include-dirs whitelist is the mechanism
    # that keeps the dump focused on this.
    mod.DEFAULT_INCLUDE_DIRS = list(_DUMP_INCLUDE_DIRS)

    print(f"[dump] dump.py -r in {scan_root}", file=sys.stderr)
    print(f"  include dirs: {mod.DEFAULT_INCLUDE_DIRS}",
          file=sys.stderr)

    old_cwd = Path.cwd()
    saved_argv = sys.argv
    try:
        os.chdir(str(scan_root))
        sys.argv = [str(dump_py), "-r", "-o", str(out)]
        rc = mod.main()
    finally:
        sys.argv = saved_argv
        os.chdir(str(old_cwd))

    if rc != 0:
        raise SystemExit(f"error: dump.py exited {rc}")
    if not out.is_file() or not out.read_text(encoding="utf-8").strip():
        raise SystemExit(f"error: dump.py did not produce {out}")

    n_files = out.read_text(encoding="utf-8").count("\nFILE ")
    print(f"  wrote {out} ({out.stat().st_size:,} bytes, "
          f"{n_files} file(s))", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# cwd dump (filtered to conversation-referenced files)
# ---------------------------------------------------------------------------
#
# Delegates to `scripts/dumps/dump_filtered.py`, a standalone script
# that extracts every `## File:` basename from the conversation,
# scans cwd for matching files, and runs dump.py with an explicit
# file list.  See that script's module docstring for the design.
#
# Kept as a subprocess (rather than an in-process import) for two
# reasons:
#
#   · dump_filtered.py is deliberately standalone (no deeppeek
#     import) so the user can run it directly from a terminal to
#     preview the dump before wiring it into this pipeline.
#   · The subprocess runs dump.py in a clean argv/cwd context,
#     avoiding any state leak from having just invoked dump.py in
#     `_stage_dump_deeppeek`.

def _stage_dump_cwd_filtered(cwd: Path, ws: Path,
                              conversation: Path) -> Optional[Path]:
    """Run dump_filtered.py; return the dump path, or None if no
    files matched (exit 2).  On hard failure, raise SystemExit.

    Exit codes from dump_filtered.py:
      0 — matches found; real dump written
      2 — no matches; stub written (caller should NOT pipe it)
      anything else — hard error; abort
    """
    script = _DUMP_FILTERED_PY_SCRIPT
    if not script.is_file():
        raise SystemExit(
            f"error: dump_filtered.py not found at {script}\n"
            f"  this file ships with deeppeek at "
            f"`scripts/dumps/dump_filtered.py`;\n"
            f"  reinstall from a source checkout "
            f"(`pipx install --editable /path/to/deeppeek --force`)")

    # Match dump_filtered.py's own default naming (dump.py's
    # `<stem>_<dirname><suffix>` convention + `_filtered`), so the
    # file the user sees in the workspace matches what
    # `scripts/dumps/dump_filtered.py <conv> <cwd>` would have
    # produced on its own.
    dirname = cwd.name or "root"
    dirname = dirname.replace(os.sep, "_").replace("/", "_")
    out = ws / f"project_dump_{dirname}_filtered.md"
    cmd = [
        sys.executable, str(script),
        str(conversation),
        str(cwd),
        "-o", str(out),
    ]

    print(f"[cwd-dump] filtering {cwd} to files mentioned in "
          f"{conversation.name}", file=sys.stderr)
    rc, _, _ = _run_cmd(
        cmd, cwd=cwd,
        timeout=_DUMP_FILTERED_TIMEOUT, allow_failure=True,
    )
    if rc == 0:
        if not out.is_file():
            raise SystemExit(
                f"error: dump_filtered.py reported success but did "
                f"not write {out}")
        return out
    if rc == 2:
        print(f"  [cwd-dump] no files in {cwd} matched any header "
              f"in {conversation.name}; not attaching the cwd dump",
              file=sys.stderr)
        return None
    raise SystemExit(
        f"error: dump_filtered.py exited {rc} "
        f"(see stderr above for details)")


# ---------------------------------------------------------------------------
# Merge — bundle every input into one file via scripts/merge_md.py
# ---------------------------------------------------------------------------

def _stage_merge(ws: Path, files: list[Path],
                 args: argparse.Namespace) -> Path:
    """Merge `files` into a single markdown file using merge_md.py.

    The merge_md.py script uses `+` separators and `MERGED FILE i/N:`
    headers, which stay unambiguous even when the inputs are
    themselves dump files (which use `===` and `FILE i/N:`).  The
    output lands in the workspace as `refine_deeppeek_merged.md`
    and is what the pipe stage attaches to the chat.
    """
    script = args.merge_md or _MERGE_MD_SCRIPT
    if not script.is_file():
        raise SystemExit(
            f"error: merge_md.py not found at {script}\n"
            f"  this file ships with deeppeek at "
            f"`scripts/merge_md.py`;\n"
            f"  reinstall from a source checkout "
            f"(`pipx install --editable /path/to/deeppeek --force`)"
        )

    out = ws / "refine_deeppeek_merged.md"
    existing = [f for f in files if f.is_file()]
    if not existing:
        raise SystemExit(
            "error: no input files to merge — this should not happen; "
            "check that the earlier stages produced their outputs"
        )

    cmd = [
        sys.executable, str(script),
        "-o", str(out),
        "-t", "refine_deeppeek — merged inputs",
    ] + [str(f) for f in existing]

    print(f"[merge] merging {len(existing)} file(s) -> {out}",
          file=sys.stderr)
    for f in existing:
        try:
            print(f"  {f}  ({f.stat().st_size:,} bytes)",
                  file=sys.stderr)
        except OSError:
            print(f"  {f}", file=sys.stderr)

    rc, _, stderr = _run_cmd(
        cmd, timeout=args.merge_timeout, allow_failure=False,
    )
    for line in (stderr or "").splitlines():
        if line.strip():
            print(f"    {line.strip()}", file=sys.stderr)

    if not out.is_file():
        raise SystemExit(
            f"error: merge_md.py reported success but {out} is missing")
    print(f"  wrote {out} ({out.stat().st_size:,} bytes)",
          file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _resolve_composer_text(args: argparse.Namespace,
                           conversation: Path,
                           dump_path: Path,
                           merged_path: Optional[Path]
                           ) -> tuple[str, str]:
    """Return (text, source_tag) for the composer.

    When `--text STR` is given, STR is returned verbatim — no
    formatting, no wrapping.  Otherwise the short templated default
    is built from the conversation filename, the deeppeek dump
    filename, and the merged-file filename.
    """
    if args.text is not None:
        return args.text, "--text override"
    text = DEFAULT_PROMPT_TEMPLATE.format(
        conversation_name=conversation.name,
        deeppeek_dump_name=dump_path.name,
        merged_name=(merged_path.name if merged_path is not None
                     else "(merged inputs)"),
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

    dump_path = _stage_dump_deeppeek(inferred, ws)

    # Conversation must exist — apply just read it, but check anyway
    # so `--no-pipe` runs can still build the prompt from its path.
    if not args.conversation.is_file():
        raise SystemExit(
            f"error: conversation file not found: {args.conversation}"
        )

    # 6b. Optional: cwd dump filtered to files mentioned in the
    #     conversation.  Runs regardless of --no-pipe (like the
    #     status capture and deeppeek dump); the filtered dump is a
    #     useful workspace artifact even when not piped.
    cwd_dump_path: Optional[Path] = None
    if args.with_dump:
        cwd_dump_path = _stage_dump_cwd_filtered(
            cwd, ws, args.conversation)

    # 7. Assemble the inputs that will be merged (and then piped as
    #    one file chip).
    to_merge: list[Path] = [
        status_path,
        *meta_files,
        args.conversation,
        dump_path,
    ]
    if cwd_dump_path is not None:
        to_merge.append(cwd_dump_path)

    # 8. Dry-run: show what would be merged and the composer text.
    if args.dry_run:
        existing = [f for f in to_merge if f.is_file()]
        merged_name = "refine_deeppeek_merged.md"
        composer_text, source_tag = _resolve_composer_text(
            args, args.conversation, dump_path, Path(merged_name),
        )
        print("\n[dry-run] would merge:", file=sys.stderr)
        for f in existing:
            print(f"  {f}", file=sys.stderr)
        print(f"[dry-run] then pipe the single merged file "
              f"({merged_name}) as one chip", file=sys.stderr)
        print(f"\n[dry-run] recover exited {recover_rc}", file=sys.stderr)
        print(f"[dry-run] composer text ({source_tag}) below; not typed.",
              file=sys.stderr)
        print(composer_text)
        return 0

    # 9. Merge inputs into one file chip (skipped with --no-pipe).
    if args.no_pipe:
        merged_path: Optional[Path] = None
        files: list[Path] = []
    else:
        merged_path = _stage_merge(ws, to_merge, args)
        files = [merged_path]

    # 10. Build composer text and pipe.
    composer_text, source_tag = _resolve_composer_text(
        args, args.conversation, dump_path, merged_path,
    )
    print(f"[build] composer text: {len(composer_text):,} chars "
          f"({source_tag})", file=sys.stderr)

    return _stage_emit_parallel(composer_text, files, args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "refine_deeppeek",
        help="Reset, re-apply the conversation, capture the aftermath, "
             "merge the conversation + diff + reports + a dump of "
             "deeppeek itself into one file, and pipe that merged file",
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
             "deeppeek dump, merge, pipe) still runs and the merged "
             "file is still piped unless --no-pipe is also given.  "
             "The string is NOT str.format-ed; run --dry-run first to "
             "see what's available.",
    )
    p.add_argument(
        "--workspace", type=Path, default=None,
        help="directory for the captured diff.txt, deeppeek dump, and "
             "merged output.  Default: a fresh temp dir in /tmp.  "
             "Point this at a stable location to keep the artefacts "
             "around for inspection.",
    )
    p.add_argument(
        "--status-py", type=Path, default=None,
        help=f"override the status.py path "
             f"(default: {_STATUS_PY_SCRIPT}).  Must accept no "
             f"arguments and print a diff-style view to stdout.",
    )
    p.add_argument(
        "--merge-md", type=Path, default=None,
        help=f"override the merge_md.py path "
             f"(default: {_MERGE_MD_SCRIPT}).  Must accept "
             f"`-o PATH FILE [FILE ...]`.",
    )
    p.add_argument(
        "--merge-timeout", type=float, default=120.0,
        help="seconds for `merge_md.py` (default: 120)",
    )
    p.add_argument(
        "--with-dump", action="store_true",
        help="also dump the current working directory (the project "
             "being reviewed) and include it in the merged file.  The "
             "dump is filtered to files whose basenames appear as "
             "`## File:` headers in the conversation — this keeps "
             "it small enough for the model to actually read, "
             "while preserving the current state of every file the "
             "conversation tries to edit.  Basenames with multiple "
             "matches (e.g. `utils.py` in several directories) "
             "include every match; the conversation may not "
             "specify a path, so any potential target is kept.  "
             "Runs regardless of --no-pipe (the filtered dump is "
             "a useful workspace artifact even when not piped).",
    )
    p.add_argument(
        "--no-pipe", action="store_true",
        help="skip the merge and pipe steps; only type the composer "
             "text.  Useful when you want only --text in the "
             "composer with no file chip at all.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="run the reset + apply, capture the status, dump "
             "deeppeek, list what would be merged, print the "
             "composer text, then stop — no merge, no pipe, no type",
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
