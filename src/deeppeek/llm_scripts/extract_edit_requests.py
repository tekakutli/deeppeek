#!/usr/bin/env python3
"""extract_edit_requests.py — detect which files a conversation is
asking to edit, retrieve those files from dumps, and have the LLM
produce surgical diff patches — ONE LLM call per target file.

Pipeline
--------
  1. Run `extract_code_blocks.py --top-level-only` to get the
     *stripped* conversation (fenced code blocks replaced with
     `{{handle}}` placeholders).  Only the stripped prose is scanned
     for target filenames, so paths inside code examples ("python
     extract_edit_requests.py chat.txt") do not count as targets.

  2. Scan the stripped prose for edit-target phrases:
       "add X to foo.py", "modify bar.py", "edits to baz.py", ...
     → dict of filename -> mention count.

  3. Pick the target(s) to edit.  If one file clearly dominates
     (>= 2x the runner-up), only that file is edited — the common
     case where a chat is about one file and other files are
     mentioned incidentally.  Otherwise every target above a
     min-count threshold is edited.

  4. Load the dump corpus (`--dump-file PATH`, repeatable).

  5. Strip blockquoted (thinking-process) lines from the
     conversation before sending it to the LLM — typically 50–70%
     of a DeepSeek log and irrelevant to the edits.

  6. For each target:
       · resolve against the dump (exact → suffix → basename)
       · if resolved: ONE LLM call with
           (shrunk conversation, current file content)
         asking for a unified diff → apply via POSIX `patch -p0`
         → write the patched file to `edited/<target>`
       · if not resolved: log, skip.

  7. Write `_report.json`.

There is no per-block extraction, no heuristic classification, no
per-block LLM calls.  The LLM sees the conversation and the current
file, and is asked to produce a unified diff.  The diff is applied
by `patch`; on failure the target is logged and skipped.

Output layout
-------------
    /tmp/deeppeek_edits_<stem>_<YYYYmmdd_HHMMSS>/
        edited/                  ← fully-patched files
        _raw/                    ← stripped conversation, dumps,
                                   LLM diffs, failed patches
        _report.json             ← detected targets + per-target results

Flags
-----
    --dump-file PATH          dump.py output (repeatable, required
                              for anything to be edited)
    --implement-max-tokens N  max_tokens per LLM call
                              (default: 16384 — diff output is much
                              smaller than a full file; lower it
                              further if your model is verbose)
    --implement-temperature F temperature for LLM calls (default: 0.1)
    --out-dir PATH, --extractor PATH, --model, -q: as before

Legacy flags (--context-lines, --no-ai, --force-ai,
--no-full-file-filter, --include-non-edits,
--verbose-heuristics, --implement-edits, --temperature, --max-tokens)
are accepted for backward compatibility but ignored.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from deeppeek.llm import (
    LLMClient, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT,
)


# ---- load sibling modules ---------------------------------------

def _load_sibling(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        here = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            name, here / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(
                f"could not import {name}.py — place it next to "
                f"this script or on PYTHONPATH")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


_dc = _load_sibling("dump_corpus")


# ---- defaults ---------------------------------------------------

_DEFAULT_IMPLEMENT_MAX_TOKENS = 16384
_DEFAULT_IMPLEMENT_TEMPERATURE = 0.1


# ---- edit-target detection --------------------------------------

_EDIT_TARGET_RE = re.compile(
    r"\b(?:add|apply|modify|change|edit|update|patch|extend)\b"
    r"[^\n]*?\b(?:to|in)\b[^\n]*?"
    r"`?([\w\-./]+\.(?:py|js|ts|tsx|jsx|md|json|yaml|yml|sh|txt"
    r"|toml|conf|rs|go|c|cpp|h|hpp))`?",
    re.IGNORECASE,
)
_EDITS_TO_RE = re.compile(
    r"\b(?:edits?|changes?|patches?|modifications?|updates?)\b"
    r"[^\n]*?\b(?:to|in|for)\b[^\n]*?"
    r"`?([\w\-./]+\.(?:py|js|ts|tsx|jsx|md|json|yaml|yml|sh|txt"
    r"|toml|conf|rs|go|c|cpp|h|hpp))`?",
    re.IGNORECASE,
)


def _scan_edit_targets(stripped_text: str) -> dict[str, int]:
    """Scan stripped prose for edit-target filenames.

    Matches "add X to foo.py", "edits to bar.py", "modify baz.py",
    etc.  Skips placeholder-only lines — those are code stand-ins,
    not prose.  Returns dict filename -> mention count.
    """
    counts: dict[str, int] = {}
    for line in stripped_text.splitlines():
        if "{{" in line and "}}" in line:
            continue
        for pat in (_EDIT_TARGET_RE, _EDITS_TO_RE):
            for m in pat.finditer(line):
                name = m.group(1)
                counts[name] = counts.get(name, 0) + 1
    return counts


def _pick_targets_to_edit(edit_targets: dict[str, int]) -> list[str]:
    """Return the list of targets to actually edit.

    · Dominant target (>= 2x the runner-up) → edit only that one.
    · Otherwise → edit every target whose mention count is at least
      max(2, top_count // 4).
    """
    if not edit_targets:
        return []
    ordered = sorted(edit_targets.items(), key=lambda x: -x[1])
    if len(ordered) == 1:
        return [ordered[0][0]]
    top_name, top_count = ordered[0]
    second_count = ordered[1][1]
    if top_count >= 2 and top_count >= 2 * second_count:
        return [top_name]
    min_count = max(2, top_count // 4)
    return [name for name, count in ordered if count >= min_count]


# ---- extractor invocation --------------------------------------

def _locate_extractor(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"extractor not found: {p}")
        return p
    candidates = [
        Path(__file__).resolve().parent / "extract_code_blocks.py",
        Path.cwd() / "extract_code_blocks.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "could not locate extract_code_blocks.py — pass --extractor")


def _run_extractor(extractor: Path, input_path: Path,
                   raw_dir: Path, *,
                   top_level_only: bool = True) -> Path:
    """Run the extractor; return the stripped-text path."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable, str(extractor), str(input_path),
        str(raw_dir), "-q", "--no-review-log",
    ]
    if top_level_only:
        args.append("--top-level-only")
    result = subprocess.run(
        args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"extract_code_blocks.py exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}")
    manifest = raw_dir / "manifest.json"
    if not manifest.is_file():
        raise RuntimeError(f"missing manifest: {manifest}")
    for p in raw_dir.iterdir():
        if "_stripped" in p.name:
            return p
    raise RuntimeError(f"no stripped file found in {raw_dir}")


# ---- conversation shrinking ------------------------------------

def _strip_blockquotes(text: str) -> str:
    """Drop blockquoted (thinking-process) lines from a chat log.

    A blockquote line is one whose first non-space char is `>`.
    Lines inside a fenced ``` block are preserved — a diff or
    shell snippet may legitimately contain `>` (`cmd > file`).
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out)


# ---- prompt ----------------------------------------------------

_IMPLEMENT_PROMPT = """\
You are a precise code editor.

Below is a conversation in which edits to files were requested.  The
conversation is shown verbatim, including every fenced code block
(blocks may be patches, fragments, new files, or illustrations — use
your judgement to determine which apply to the target file).

<conversation>
{conversation}
</conversation>

Below is the current, on-disk content of the file `{target}`.

<current-file target="{target}">
{current_content}
</current-file>

Implement ALL the edits that the conversation requests for
`{target}`.  Do not invent edits that were not requested.

Respond with ONE unified diff (as produced by `diff -u` or
`git diff`) that applies cleanly to the current file above.  Put
the diff in a single fenced ```diff block.  Do NOT reprint the
whole file — include only the hunks that change, with three lines
of unchanged context on each side.  The context lines must match
the current file exactly, byte for byte.

Minimal, surgical hunks.  One diff block, no prose before or
after.
"""


def _build_implement_prompt(target: str, conversation: str,
                            current_content: str) -> str:
    return _IMPLEMENT_PROMPT.format(
        target=target,
        conversation=conversation,
        current_content=current_content,
    )


# ---- diff extraction + application ----------------------------

def _fenced_blocks(response: str) -> list[str]:
    """Contents of every fenced ``` block in `response`."""
    lines = response.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            j = i + 1
            body: list[str] = []
            while (j < len(lines)
                   and not lines[j].lstrip().startswith("```")):
                body.append(lines[j])
                j += 1
            if body:
                blocks.append("\n".join(body))
            i = j + 1
        else:
            i += 1
    return blocks


def _looks_like_diff(text: str) -> bool:
    lines = text.splitlines()
    return (any(l.startswith("--- ") for l in lines)
            and any(l.startswith("+++ ") for l in lines)
            and any(l.startswith("@@ ") for l in lines))


def _extract_diff_block(
        response: Optional[str]) -> tuple[Optional[str], bool]:
    """Return (block, is_diff).

    Prefers the largest fenced block that parses as a unified diff.
    Falls back to the largest fenced block of any kind — treated as
    full file content by the caller.
    """
    if not response:
        return None, False
    blocks = _fenced_blocks(response)
    if not blocks:
        return None, False
    diffs = [b for b in blocks if _looks_like_diff(b)]
    if diffs:
        return max(diffs, key=len), True
    return max(blocks, key=len), False


def _sanitize_for_filename(s: str, max_len: int = 80) -> str:
    s = s.replace("/", "__").replace("\\", "__")
    s = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
    return s[:max_len] or "target"


def _normalize_patch(patch_text: str, target: str) -> str:
    """Rewrite ---/+++ headers to point at `target` directly.

    Keeps /dev/null (file add/delete), keeps hunk bodies, drops
    `diff --git`, `index`, `mode` lines — those carry junk paths
    and confuse `patch`.
    """
    out: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            prefix = line[:4]
            rest = line[4:].strip()
            if rest == "/dev/null":
                out.append(line)
                continue
            tab_part = ""
            if "\t" in rest:
                tab_part = "\t" + rest.split("\t", 1)[1]
            out.append(f"{prefix}{target}{tab_part}")
            continue
        if line.startswith((
                "diff --git ", "index ", "new file mode ",
                "deleted file mode ", "old mode ", "new mode ",
                "similarity index ", "rename from ",
                "rename to ")):
            continue
        out.append(line)
    body = "\n".join(out)
    if not body.endswith("\n"):
        body += "\n"
    return body


def _apply_patch(target: str, current_content: str, patch_text: str,
                 raw_dir: Path) -> tuple[bool, str, str]:
    """Apply `patch_text` to `current_content`.

    Returns (ok, new_content_or_empty, detail).  Runs POSIX `patch`
    in a scratch directory seeded with the current file.  On
    failure, the normalized patch and the command's stderr are
    written under `raw_dir/patches/` for inspection.
    """
    normalized = _normalize_patch(patch_text, target)
    safe = _sanitize_for_filename(target)

    try:
        work_dir = Path(tempfile.mkdtemp(
            prefix=f"eer_patch_{safe}_", dir=str(raw_dir)))
    except OSError as e:
        return False, "", f"could not create work dir: {e}"

    target_path = work_dir / target
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(current_content, encoding="utf-8")
    except OSError as e:
        return False, "", f"could not seed target file: {e}"

    patch_file = work_dir / "edit.patch"
    patch_file.write_text(normalized, encoding="utf-8")

    try:
        proc = subprocess.run(
            ["patch", "-p0", "-u", "-i", str(patch_file),
             "--batch", "--no-backup-if-mismatch"],
            cwd=str(work_dir),
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, "", "'patch' command not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "", "patch command timed out after 60s"

    if proc.returncode != 0:
        try:
            diag = raw_dir / "patches"
            diag.mkdir(parents=True, exist_ok=True)
            (diag / f"{safe}.failed.diff").write_text(
                normalized, encoding="utf-8")
            (diag / f"{safe}.patch.stderr").write_text(
                proc.stderr or proc.stdout or "", encoding="utf-8")
        except OSError:
            pass
        snippet = (proc.stderr or proc.stdout or "").strip()
        if len(snippet) > 400:
            snippet = "…" + snippet[-400:]
        return False, "", f"patch exited {proc.returncode}: {snippet}"

    try:
        new_content = target_path.read_text(encoding="utf-8")
    except OSError as e:
        return False, "", f"could not read patched file: {e}"

    try:
        shutil.rmtree(work_dir)
    except OSError:
        pass

    n_hunks = sum(1 for l in normalized.splitlines()
                  if l.startswith("@@ "))
    return True, new_content, f"patch -p0, {n_hunks} hunk(s)"


# ---- file writing ----------------------------------------------

def _write_edited_file(target: str, content: str,
                       out_dir: Path) -> Path:
    """Write `content` to `<out_dir>/edited/<target>`."""
    edir = out_dir / "edited"
    parts = [p for p in Path(target).parts
             if p not in ("", ".", "..", "/")]
    if not parts:
        raise OSError(f"empty target path: {target!r}")
    dst = edir
    for p in parts:
        dst = dst / p
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")
    return dst


# ---- core pipeline ---------------------------------------------

def _run_pipeline(input_path: Path, opts: dict) -> dict:
    if not input_path.is_file():
        return {"rc": 1, "error": f"not a file: {input_path}"}

    try:
        extractor = _locate_extractor(opts.get("extractor"))
    except FileNotFoundError as e:
        return {"rc": 1, "error": str(e)}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if opts.get("out_dir"):
        out_dir = Path(opts["out_dir"]).expanduser().resolve()
    else:
        out_dir = Path("/tmp") / (
            f"deeppeek_edits_{input_path.stem}_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "_raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)

    quiet = opts.get("quiet", False)

    if not quiet:
        print(f"input:     {input_path}", file=sys.stderr)
        print(f"extractor: {extractor}", file=sys.stderr)
        print(f"out_dir:   {out_dir}", file=sys.stderr)

    # 1. Read the original conversation — this is what the LLM sees.
    conversation_text = input_path.read_text(
        encoding="utf-8", errors="replace")

    # Strip blockquoted (thinking-process) sections before sending
    # to the LLM — 50–70% of a typical DeepSeek log, and they
    # describe reasoning, not edits.
    llm_conversation = _strip_blockquotes(conversation_text)
    if not quiet:
        saved = len(conversation_text) - len(llm_conversation)
        if saved:
            print(
                f"conversation: {len(conversation_text):,} → "
                f"{len(llm_conversation):,} chars "
                f"(stripped {saved:,} of blockquoted thinking)",
                file=sys.stderr)

    # 2. Run the extractor only to get the stripped prose (clean scan).
    try:
        stripped_path = _run_extractor(
            extractor, input_path, raw_dir,
            top_level_only=not opts.get("include_blockquotes", False))
    except (RuntimeError, FileNotFoundError) as e:
        return {"rc": 1, "error": str(e)}

    stripped_text = stripped_path.read_text(
        encoding="utf-8", errors="replace")

    # 3. Detect edit targets from prose.
    edit_targets = _scan_edit_targets(stripped_text)
    targets_to_edit = _pick_targets_to_edit(edit_targets)

    if not quiet:
        if edit_targets:
            summary = ", ".join(
                f"{n} ({c}x)" for n, c
                in sorted(edit_targets.items(), key=lambda x: -x[1]))
            print(f"detected:  {summary}", file=sys.stderr)
        else:
            print("detected:  (no edit targets in prose)",
                  file=sys.stderr)
        if targets_to_edit:
            print(f"edit list: {', '.join(targets_to_edit)}",
                  file=sys.stderr)

    report: dict = {
        "input":               str(input_path),
        "conversation_length": len(conversation_text),
        "llm_conversation_length": len(llm_conversation),
        "detected_targets":    edit_targets,
        "targets_to_edit":     targets_to_edit,
        "results":             [],
    }

    def _write_report():
        (out_dir / "_report.json").write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8")

    if not targets_to_edit:
        if not quiet:
            print("\nno edit targets — nothing to do",
                  file=sys.stderr)
        _write_report()
        return {
            "rc": 0, "out_dir": str(out_dir),
            "target_count": 0, "implemented_count": 0,
            "report": str(out_dir / "_report.json"),
        }

    # 4. Load dumps.
    dump_files = opts.get("dump_files") or []
    if not dump_files:
        if not quiet:
            print(
                "\nno --dump-file provided — cannot retrieve "
                "target contents", file=sys.stderr)
        _write_report()
        return {
            "rc": 0, "out_dir": str(out_dir),
            "target_count": len(targets_to_edit),
            "implemented_count": 0,
            "report": str(out_dir / "_report.json"),
            "warning": "no --dump-file; would have edited: "
                       + ", ".join(targets_to_edit),
        }

    corpus, dump_errors = _dc._load_dump_corpus(dump_files)
    for e in dump_errors:
        print(f"  ! {e}", file=sys.stderr)
    if not quiet:
        total = sum(len(d.files) for d in corpus.dumps)
        print(f"dumps:     {len(corpus.dumps)} loaded, "
              f"{total} file(s) indexed", file=sys.stderr)

    # 5. Per target: resolve → one LLM call → diff → apply → write.
    _client_cache: dict = {"client": opts.get("client")}

    def _get_client():
        if _client_cache["client"] is None:
            _client_cache["client"] = LLMClient(
                model=opts.get("model") or DEFAULT_LLM_MODEL,
                timeout=DEFAULT_LLM_TIMEOUT)
        return _client_cache["client"]

    max_tokens = opts.get(
        "implement_max_tokens", _DEFAULT_IMPLEMENT_MAX_TOKENS)
    temperature = opts.get(
        "implement_temperature", _DEFAULT_IMPLEMENT_TEMPERATURE)

    implemented_count = 0

    for target in targets_to_edit:
        res = corpus.resolve(target)
        entry: dict = {
            "target":          target,
            "resolve_status":  res["status"],
            "resolve_method":  res.get("method"),
            "abs_path":        res.get("abs_path"),
            "current_length":  res.get("content_length"),
            "output_path":     None,
            "edited_length":   None,
            "response_format": None,
            "error":           None,
        }

        if res["status"] == "not_found":
            if not quiet:
                print(f"  × not found {target}", file=sys.stderr)
            entry["error"] = "not found in dump"
            report["results"].append(entry)
            continue
        if res["status"] == "ambiguous":
            if not quiet:
                print(f"  ? ambiguous {target} "
                      f"({len(res['candidates'])} candidate(s))",
                      file=sys.stderr)
            entry["error"] = (
                f"ambiguous ({len(res['candidates'])} candidate(s))")
            report["results"].append(entry)
            continue

        if not quiet:
            print(f"  ← retrieved {target}  ← "
                  f"{res['dump_file']}::{res['relpath']}",
                  file=sys.stderr)

        current_content = res["content"]
        prompt = _build_implement_prompt(
            target, llm_conversation, current_content)

        if not quiet:
            print(f"  [LLM:{target}] call "
                  f"(prompt {len(prompt)} chars)",
                  file=sys.stderr)

        try:
            client = _get_client()
            response = client.call(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                tag=f"implement:{Path(target).name}",
            )
        except Exception as e:
            if not quiet:
                print(f"  ! LLM call failed for {target}: {e}",
                      file=sys.stderr)
            entry["error"] = f"LLM call failed: {e}"
            report["results"].append(entry)
            continue

        block, is_diff = _extract_diff_block(response or "")
        if block is None:
            if not quiet:
                print(f"  ! no fenced block in response for {target}",
                      file=sys.stderr)
            entry["error"] = "no fenced block in response"
            report["results"].append(entry)
            continue

        # Persist the raw block for inspection.
        try:
            patches_dir = raw_dir / "patches"
            patches_dir.mkdir(parents=True, exist_ok=True)
            safe = _sanitize_for_filename(target)
            ext = ".diff" if is_diff else ".full.txt"
            (patches_dir / f"{safe}{ext}").write_text(
                block, encoding="utf-8")
        except OSError:
            pass

        if is_diff:
            ok, new_content, detail = _apply_patch(
                target, current_content, block, raw_dir)
            if not ok:
                if not quiet:
                    print(f"  ! patch failed for {target}: {detail}",
                          file=sys.stderr)
                entry["error"] = f"patch failed: {detail}"
                entry["response_format"] = "diff"
                report["results"].append(entry)
                continue
            entry["response_format"] = "diff"
            entry["patch_method"] = detail
            entry["diff_length"] = len(block)
        else:
            # Model ignored the format — treat block as full content.
            if not quiet:
                print(f"  ! {target}: response was not a diff — "
                      f"treating block as full file",
                      file=sys.stderr)
            new_content = block
            entry["response_format"] = "full_file_fallback"

        try:
            out_path = _write_edited_file(
                target, new_content, out_dir)
        except OSError as e:
            if not quiet:
                print(f"  ! failed to write {target}: {e}",
                      file=sys.stderr)
            entry["error"] = f"write failed: {e}"
            report["results"].append(entry)
            continue

        implemented_count += 1
        entry["output_path"] = str(out_path)
        entry["edited_length"] = len(new_content)
        if not quiet:
            fmt = entry["response_format"]
            extra = (f", {entry['patch_method']}"
                     if fmt == "diff" else "")
            print(f"  ✓ edited {target}  "
                  f"({len(current_content)} → {len(new_content)} chars, "
                  f"{fmt}{extra})", file=sys.stderr)
        report["results"].append(entry)

    _write_report()

    if not quiet:
        print(f"\nimplemented: {implemented_count} file(s) in "
              f"{out_dir / 'edited'}/", file=sys.stderr)
        print(f"report:      {out_dir / '_report.json'}",
              file=sys.stderr)
        print(f"raw:         {raw_dir}/", file=sys.stderr)

    return {
        "rc":               0,
        "out_dir":          str(out_dir),
        "raw_dir":          str(raw_dir),
        "target_count":     len(targets_to_edit),
        "implemented_count": implemented_count,
        "report":           str(out_dir / "_report.json"),
        "detected_targets": edit_targets,
        "targets_to_edit":  targets_to_edit,
        "results":          report["results"],
    }


# ---- CLI -------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_edit_requests.py",
        description=(
            "Detect which files a conversation asks to edit, "
            "retrieve them from dumps, and have the LLM produce "
            "surgical diff patches — one LLM call per file."),
    )
    p.add_argument("input", metavar="INPUT", help="input file")
    p.add_argument("--out-dir", metavar="PATH", default=None)
    p.add_argument("--extractor", metavar="PATH", default=None)
    p.add_argument("--model", default=DEFAULT_LLM_MODEL)
    p.add_argument("--dump-file", action="append", default=[],
                   dest="dump_files", metavar="PATH",
                   help="dump.py output (repeatable)")
    p.add_argument("--implement-max-tokens", type=int,
                   default=_DEFAULT_IMPLEMENT_MAX_TOKENS,
                   metavar="N",
                   help=f"max_tokens per LLM call (default: "
                        f"{_DEFAULT_IMPLEMENT_MAX_TOKENS})")
    p.add_argument("--implement-temperature", type=float,
                   default=_DEFAULT_IMPLEMENT_TEMPERATURE,
                   metavar="F",
                   help=f"temperature for LLM calls (default: "
                        f"{_DEFAULT_IMPLEMENT_TEMPERATURE})")
    p.add_argument("--include-blockquotes", action="store_true",
                   help="keep blockquoted (thinking-process) blocks "
                        "in the stripped prose (only affects target "
                        "detection — the LLM conversation is always "
                        "stripped)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress per-target progress")

    # Legacy no-op flags — accepted so old wrappers don't break.
    p.add_argument("--context-lines", type=int, default=30,
                   help=argparse.SUPPRESS)
    p.add_argument("--include-non-edits", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--no-full-file-filter", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--no-ai", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--force-ai", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--verbose-heuristics", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--temperature", type=float, default=0.1,
                   help=argparse.SUPPRESS)
    p.add_argument("--max-tokens", type=int, default=600,
                   help=argparse.SUPPRESS)
    p.add_argument("--implement-edits", action="store_true",
                   help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    opts = {
        "out_dir":                args.out_dir,
        "extractor":              args.extractor,
        "model":                  args.model,
        "dump_files":             args.dump_files,
        "implement_max_tokens":   args.implement_max_tokens,
        "implement_temperature":  args.implement_temperature,
        "include_blockquotes":    args.include_blockquotes,
        "quiet":                  args.quiet,
    }
    result = _run_pipeline(Path(args.input), opts)
    if result["rc"] != 0:
        print(f"error: {result.get('error', 'unknown error')}",
              file=sys.stderr)
    return result["rc"]


# ---- llm_scripts hooks -----------------------------------------

NAME = "extract_edit_requests"
DESCRIPTION = (
    "Detect which files a conversation asks to edit, retrieve them "
    "from dumps, and have the LLM produce surgical diff patches — "
    "one LLM call per file.")
requires_llm = False


def register_flags(parser):
    parser.add_argument(
        "--eer-out-dir", type=Path, default=None,
        dest="eer_out_dir", metavar="PATH",
        help="(extract_edit_requests) override the output directory")
    parser.add_argument(
        "--eer-extractor", type=str, default=None,
        dest="eer_extractor", metavar="PATH",
        help="(extract_edit_requests) path to extract_code_blocks.py")
    parser.add_argument(
        "--eer-include-blockquotes", action="store_true",
        dest="eer_include_blockquotes",
        help="(extract_edit_requests) keep blockquoted blocks in "
             "the stripped prose (target detection only)")
    parser.add_argument(
        "--eer-quiet", action="store_true",
        dest="eer_quiet",
        help="(extract_edit_requests) suppress per-target progress")
    parser.add_argument(
        "--eer-dump-file", action="append", default=[],
        dest="eer_dump_files", metavar="PATH",
        help="(extract_edit_requests) dump.py output for "
             "target-file retrieval. Repeatable.")
    parser.add_argument(
        "--eer-implement-max-tokens", type=int,
        default=_DEFAULT_IMPLEMENT_MAX_TOKENS,
        dest="eer_implement_max_tokens", metavar="N",
        help=f"(extract_edit_requests) max_tokens per LLM call "
             f"(default: {_DEFAULT_IMPLEMENT_MAX_TOKENS})")
    parser.add_argument(
        "--eer-implement-temperature", type=float,
        default=_DEFAULT_IMPLEMENT_TEMPERATURE,
        dest="eer_implement_temperature", metavar="F",
        help=f"(extract_edit_requests) temperature for LLM calls "
             f"(default: {_DEFAULT_IMPLEMENT_TEMPERATURE})")

    # Legacy no-op flags.
    parser.add_argument("--eer-include-non-edits", action="store_true",
                        dest="eer_include_non_edits",
                        help=argparse.SUPPRESS)
    parser.add_argument("--eer-no-full-file-filter",
                        action="store_true",
                        dest="eer_no_full_file_filter",
                        help=argparse.SUPPRESS)
    parser.add_argument("--eer-no-ai", action="store_true",
                        dest="eer_no_ai", help=argparse.SUPPRESS)
    parser.add_argument("--eer-force-ai", action="store_true",
                        dest="eer_force_ai", help=argparse.SUPPRESS)
    parser.add_argument("--eer-verbose-heuristics",
                        action="store_true",
                        dest="eer_verbose_heuristics",
                        help=argparse.SUPPRESS)
    parser.add_argument("--eer-context-lines", type=int, default=30,
                        dest="eer_context_lines",
                        help=argparse.SUPPRESS)
    parser.add_argument("--eer-implement-edits",
                        action="store_true",
                        dest="eer_implement_edits",
                        help=argparse.SUPPRESS)


def ctx_from_args(args):
    return {
        "_eer_input_file": (str(args.file.resolve())
                            if args.file is not None else None),
        "_eer_out_dir": (str(args.eer_out_dir.resolve())
                         if getattr(args, "eer_out_dir", None)
                         else None),
        "_eer_extractor": getattr(args, "eer_extractor", None),
        "_eer_include_blockquotes": getattr(
            args, "eer_include_blockquotes", False),
        "_eer_quiet": getattr(args, "eer_quiet", False),
        "_eer_temperature": getattr(args, "temperature",
                                    _DEFAULT_IMPLEMENT_TEMPERATURE),
        "_eer_max_tokens": getattr(args, "max_tokens",
                                   _DEFAULT_IMPLEMENT_MAX_TOKENS),
        "_eer_dump_files": list(getattr(
            args, "eer_dump_files", []) or []),
        "_eer_implement_max_tokens": getattr(
            args, "eer_implement_max_tokens",
            _DEFAULT_IMPLEMENT_MAX_TOKENS),
        "_eer_implement_temperature": getattr(
            args, "eer_implement_temperature",
            _DEFAULT_IMPLEMENT_TEMPERATURE),
    }


def build_prompt(input_value, ctx):
    ctx["_eer_input_text"] = input_value
    n = len(input_value) if hasattr(input_value, "__len__") else 0
    return (f"(extract_edit_requests: {n} chars stashed "
            f"for processing)")


def on_response(response, ctx):
    input_text = ctx.pop("_eer_input_text", "")
    input_file = ctx.pop("_eer_input_file", None)
    session_dir = ctx.get("session_dir") or Path("/tmp")

    if input_file and Path(input_file).is_file():
        input_path = Path(input_file)
    else:
        input_path = Path(session_dir) / "input.md"
        input_path.write_text(input_text, encoding="utf-8")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = ctx.pop("_eer_out_dir", None) or str(
        Path(session_dir) / "edits" / f"run_{ts}")

    opts = {
        "out_dir":               out_dir,
        "extractor":             ctx.pop("_eer_extractor", None),
        "model":                 ctx.get("model") or DEFAULT_LLM_MODEL,
        "include_blockquotes":   ctx.get(
            "_eer_include_blockquotes", False),
        "quiet":                 ctx.get("_eer_quiet", False),
        "client":                ctx.get("_llm_client"),
        "dump_files":            list(ctx.get("_eer_dump_files", [])),
        "implement_max_tokens":  ctx.get(
            "_eer_implement_max_tokens",
            _DEFAULT_IMPLEMENT_MAX_TOKENS),
        "implement_temperature": ctx.get(
            "_eer_implement_temperature",
            _DEFAULT_IMPLEMENT_TEMPERATURE),
    }

    result = _run_pipeline(input_path, opts)
    if result["rc"] != 0:
        return (f"extract_edit_requests failed: "
                f"{result.get('error', 'unknown error')}")

    detected = result.get("detected_targets") or {}
    detected_str = ", ".join(
        f"{n}({c}x)"
        for n, c in sorted(detected.items(), key=lambda x: -x[1])
    ) or "(none)"

    lines = [
        f"detected: {len(detected)} file(s) — {detected_str}",
        f"edit list: "
        f"{', '.join(result.get('targets_to_edit') or []) or '(none)'}",
        f"edited:   {result['implemented_count']}"
        f"/{result['target_count']} target(s)",
    ]
    if result.get("warning"):
        lines.append(f"warning:  {result['warning']}")
    lines.append(f"files:    {result['out_dir']}/edited/")
    lines.append(f"report:   {result['report']}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
