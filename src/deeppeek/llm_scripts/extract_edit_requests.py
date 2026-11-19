#!/usr/bin/env python3
"""extract_edit_requests.py — extract code blocks, classify each as
"edit request" or not, collect the target files, retrieve their
current contents from one or more `dump.py` outputs, and optionally
have the LLM produce the fully-edited files.

Pipeline
--------
  1. Run `extract_code_blocks.py --top-level-only` — blockquoted
     ("thinking process") blocks stay verbatim in the stripped file.

  2. Pre-filter FULL-FILE blocks via `code_blob_analysis.analyze`.
     Confidently-full files with no edit header in context are
     skipped (they're new-file creations).  Disable with
     `--no-full-file-filter`.

  3. For each remaining blob, run `edit_blob_analysis.analyze_edit`:
     a signed edit-vs-not score, a confidence band, an `edit_kind`,
     and ranked target-file candidates.  Heuristic, no LLM.

  4. Decide per block:
       · full file (pre-filter)  → skip
       · high_edit               → record (no LLM)
       · high_not_edit           → skip (no LLM)
       · ambiguous               → LLM call, heuristic hints as
                                   evidence
     `--force-ai`, `--no-ai` override.

  5. Write each edit-request blob flat into `files/` as
     `<stem>__blkNNN.<ext>` (or `unknown__blkNNN.<ext>`).

  6. OPTIONAL — retrieval: if `--dump-file PATH` is passed (one or
     more times), every target path is looked up in an in-memory
     `DumpCorpus` built from those dumps.  Matches are recorded on
     each record (`retrieved = {...}`), and uniquely-resolved file
     contents are written under `retrieved/<dump-stem>/<relpath>`.

  7. OPTIONAL — implement-edits: if `--implement-edits` is set
     (requires `--dump-file`), one LLM call is made per resolved
     target.  Context: the current file content from the dump + a
     *purged conversation* where full-file blocks are stripped out
     and the edit fragments for this target are inlined.  The LLM
     is asked to output the complete edited file, which is written
     under `edited/<target-path>`.

Output layout
-------------
    /tmp/deeppeek_edits_<stem>_<YYYYmmdd_HHMMSS>/
        files/                     ← flat edit-request blobs
        retrieved/                 ← dump-sourced target file contents
        edited/                    ← LLM-produced full edited files
        _raw/                      ← intermediate extraction
        _edit_map.json             flat name → target + metadata
        _edit_targets.json         target file → edits (reverse index)
        _retrieved_files.json      per-target retrieval report
        _implement_log.json        per-target implement-edits log
        _classification_log.json   one record per block

Flags
-----
    --dump-file PATH          dump.py output to index (repeatable).
    --implement-edits         run the LLM per resolved target; writes
                              to edited/.
    --implement-max-tokens N  max_tokens per implement call
                              (default: 4096)
    --implement-temperature F temperature for implement (default: 0.1)
    --no-full-file-filter, --no-ai, --force-ai, --include-non-edits,
    --include-blockquotes, --verbose-heuristics, -q: unchanged.

Framework hooks (llm_scripts)
-----------------------------
Standalone CLI AND module.  Hook flags are prefixed `--eer-`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from deeppeek.llm import (
    LLMClient, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT,
)


# ---- load sibling heuristic modules -----------------------------

try:
    import edit_blob_analysis as _eba
except ImportError:
    _here = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "edit_blob_analysis", _here / "edit_blob_analysis.py")
    if _spec is None or _spec.loader is None:
        raise ImportError(
            "could not import edit_blob_analysis.py — place it "
            "next to this script or on PYTHONPATH")
    _eba = importlib.util.module_from_spec(_spec)
    sys.modules["edit_blob_analysis"] = _eba
    _spec.loader.exec_module(_eba)

analyze_edit_blob = _eba.analyze_edit
_near_handle_lines = _eba.near_handle_lines
_check_edit_header = _eba._check_edit_header

# `code_blob_analysis` is optional.
_cba = None
try:
    import code_blob_analysis as _cba
except ImportError:
    _here = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "code_blob_analysis", _here / "code_blob_analysis.py")
    if _spec is not None and _spec.loader is not None:
        _cba = importlib.util.module_from_spec(_spec)
        sys.modules["code_blob_analysis"] = _cba
        _spec.loader.exec_module(_cba)

analyze_code_blob = _cba.analyze if _cba is not None else None
_HAS_CODE_BLOB_ANALYSIS = _cba is not None


# ---- load sibling pipeline modules ------------------------------

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


_dc = _load_sibling("dump_corpus")     # dump parsing + retrieval
_ep = _load_sibling("edit_prompts")    # prompt build / parse
_eo = _load_sibling("edit_outputs")    # file naming + writing


# ---- defaults ----------------------------------------------------

_DEFAULT_CONTEXT_LINES = 30
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_MAX_TOKENS = 600
_DEFAULT_IMPLEMENT_MAX_TOKENS = 4096
_DEFAULT_IMPLEMENT_TEMPERATURE = 0.1


# ---- extractor invocation ----------------------------------------

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
                   top_level_only: bool = True) -> list[dict]:
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
    manifest_path = raw_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"missing manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _find_stripped(raw_dir: Path) -> Optional[Path]:
    for p in raw_dir.iterdir():
        if "_stripped" in p.name:
            return p
    return None


# ---- context extraction ------------------------------------------

def _extract_context(stripped_text: str, handle: str,
                     n_lines: int) -> str:
    if not handle:
        return ""
    lines = stripped_text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if handle in line:
            idx = i
            break
    if idx is None:
        return ""
    start = max(0, idx - n_lines)
    end = min(len(lines), idx + n_lines + 1)
    return "\n".join(lines[start:end])

# ---- conversation-level edit-target detection --------------------

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
    """Scan prose for phrases that establish an edit target file.

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


def _build_edit_target_hint(targets: dict[str, int]) -> str:
    if not targets:
        return ""
    top = sorted(targets.items(), key=lambda x: -x[1])[:3]
    return ("[Files being edited in this conversation: "
            + ", ".join(f"{n} ({c}x)" for n, c in top) + "]")


def _pick_dominant_target(targets: dict[str, int]) -> Optional[str]:
    """Top edit target if it clearly dominates, else None.

    Dominant = mentioned at least twice AND either the only target
    or at least 2x the runner-up.  Guards the fallback against
    multi-file conversations where nothing stands out.
    """
    if not targets:
        return None
    ordered = sorted(targets.items(), key=lambda x: -x[1])
    top_name, top_count = ordered[0]
    if top_count < 2:
        return None
    if len(ordered) > 1 and top_count < 2 * ordered[1][1]:
        return None
    return top_name


# ---- heuristic adapters ------------------------------------------

def _run_heuristic(block: dict, context: str) -> dict:
    return analyze_edit_blob(block, context)


def _run_code_blob_analysis(block: dict, context: str):
    if analyze_code_blob is None:
        return None
    try:
        return analyze_code_blob(block, context)
    except Exception as e:
        print(f"  ! code_blob_analysis failed for "
              f"{block.get('filename')}: {e}", file=sys.stderr)
        return None


def _is_replacement_edit_context(context: str, handle: str) -> bool:
    if not context or not handle:
        return False
    near = _near_handle_lines(context, handle, before_lines=10)
    is_edit_hdr, _is_full_hdr, _is_create_hdr, _ = \
        _check_edit_header(near)
    return is_edit_hdr


# ---- record construction ----------------------------------------

def _classify_via_ai(block: dict, context: str, analysis: dict,
                     client: LLMClient, temperature: float,
                     max_tokens: int) -> dict:
    prompt = _ep._build_prompt(block, context, analysis)
    record = {
        "index":            block["index"],
        "block_filename":   block.get("filename"),
        "lang":             block.get("lang"),
        "is_edit_request":  False,
        "edit_kind":        None,
        "target_file":      None,
        "flat_filename":    None,
        "target_path":      None,
        "confidence":       analysis["confidence"],
        "classification":   "ai_error",
        "reasoning":        "",
        "raw_response":     None,
        "error":            None,
        "heuristic": {
            "score":      analysis["score"],
            "confidence": analysis["confidence"],
            "signals":    [{"name": s.name, "weight": s.weight}
                            for s in analysis["signals"]],
            "edit_kind":  analysis.get("edit_kind"),
        },
        "ai": None,
    }
    try:
        response = client.call(
            prompt, max_tokens=max_tokens, temperature=temperature,
            tag=f"edit:{block['index']:03d}")
    except Exception as e:
        record["error"] = f"LLM call failed: {e}"
        record["is_edit_request"] = bool(analysis["is_edit_request"])
        record["edit_kind"] = analysis.get("edit_kind")
        record["classification"] = (
            "ai_edit" if record["is_edit_request"]
            else "ai_not_edit")
        if analysis["is_edit_request"] and analysis["target_candidates"]:
            record["target_file"] = _eo._clean_target_path(
                analysis["target_candidates"][0]["name"])
        return record

    if response is None:
        record["error"] = "LLM returned None"
        record["is_edit_request"] = bool(analysis["is_edit_request"])
        record["edit_kind"] = analysis.get("edit_kind")
        record["classification"] = (
            "ai_edit" if record["is_edit_request"]
            else "ai_not_edit")
        return record

    record["raw_response"] = response
    parsed = _ep._parse_json_response(response)
    if parsed is None:
        record["error"] = "could not parse JSON from response"
        record["is_edit_request"] = bool(analysis["is_edit_request"])
        record["edit_kind"] = analysis.get("edit_kind")
        record["classification"] = (
            "ai_edit" if record["is_edit_request"]
            else "ai_not_edit")
        return record

    record["ai"] = parsed
    record["is_edit_request"] = bool(parsed.get("is_edit_request"))
    record["classification"] = (
        "ai_edit" if record["is_edit_request"]
        else "ai_not_edit")
    kind = parsed.get("edit_kind")
    if isinstance(kind, str) and kind.strip():
        record["edit_kind"] = kind.strip()
    elif record["is_edit_request"]:
        record["edit_kind"] = analysis.get("edit_kind") or "fragment"

    tgt = parsed.get("target_file")
    if isinstance(tgt, str) and tgt.strip():
        record["target_file"] = _eo._clean_target_path(tgt.strip())
    if record["is_edit_request"] and not record["target_file"]:
        if analysis["target_candidates"]:
            record["target_file"] = _eo._clean_target_path(
                analysis["target_candidates"][0]["name"])
    record["reasoning"] = str(parsed.get("reasoning") or "")
    return record


def _record_from_heuristic(block: dict, analysis: dict) -> dict:
    is_edit = bool(analysis.get("is_edit_request"))
    target = None
    if is_edit and analysis["target_candidates"]:
        target = _eo._clean_target_path(
            analysis["target_candidates"][0]["name"])
    return {
        "index":            block["index"],
        "block_filename":   block.get("filename"),
        "lang":             block.get("lang"),
        "is_edit_request":  is_edit,
        "edit_kind":        analysis.get("edit_kind") if is_edit else None,
        "target_file":      target,
        "flat_filename":    None,
        "target_path":      None,
        "confidence":       analysis["confidence"],
        "classification":   ("heuristic_edit" if is_edit
                              else "heuristic_not_edit"),
        "reasoning": (
            f"heuristic (score {analysis['score']:+.2f}, "
            f"{analysis['confidence']})"),
        "raw_response":     None,
        "error":            None,
        "heuristic": {
            "score":      analysis["score"],
            "confidence": analysis["confidence"],
            "signals":    [{"name": s.name, "weight": s.weight}
                            for s in analysis["signals"]],
            "edit_kind":  analysis.get("edit_kind"),
        },
        "ai": None,
    }


def _record_from_full_file_skip(block: dict,
                                code_analysis: dict) -> dict:
    return {
        "index":            block["index"],
        "block_filename":   block.get("filename"),
        "lang":             block.get("lang"),
        "is_edit_request":  False,
        "edit_kind":        None,
        "target_file":      None,
        "flat_filename":    None,
        "target_path":      None,
        "confidence":       "full_file",
        "classification":   "full_file_skip",
        "reasoning": (
            f"pre-filter: full file per code_blob_analysis "
            f"(score {code_analysis['score']:+.2f}, "
            f"{code_analysis['confidence']}); no edit header "
            f"nearby"),
        "raw_response":     None,
        "error":            None,
        "code_blob_analysis": {
            "score":      code_analysis["score"],
            "confidence": code_analysis["confidence"],
            "is_full_file": code_analysis["is_full_file"],
            "signals":    [{"name": s.name, "weight": s.weight}
                            for s in code_analysis["signals"]],
            "filename_candidates":
                code_analysis["filename_candidates"],
        },
        "heuristic":        None,
        "ai":               None,
    }


def _fallback_record(block: dict, error: Exception) -> dict:
    return {
        "index":            block["index"],
        "block_filename":   block.get("filename"),
        "lang":             block.get("lang"),
        "is_edit_request":  False,
        "edit_kind":        None,
        "target_file":      None,
        "flat_filename":    None,
        "target_path":      None,
        "confidence":       "ambiguous",
        "classification":   "error",
        "reasoning":        "",
        "raw_response":     None,
        "error":            f"heuristic analysis failed: {error}",
        "heuristic":        None,
        "ai":               None,
    }


# ---- core pipeline (shared by CLI and hooks) --------------------

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
    verbose_h = opts.get("verbose_heuristics", False)
    no_ai = opts.get("no_ai", False)
    force_ai = opts.get("force_ai", False)
    context_lines = opts.get("context_lines", _DEFAULT_CONTEXT_LINES)
    include_non_edits = opts.get("include_non_edits", False)
    include_blockquotes = opts.get("include_blockquotes", False)
    no_full_file_filter = opts.get("no_full_file_filter", False)

    if no_full_file_filter:
        use_prefilter = False
        prefilter_reason = "disabled via --no-full-file-filter"
    elif _HAS_CODE_BLOB_ANALYSIS:
        use_prefilter = True
        prefilter_reason = "using code_blob_analysis"
    else:
        use_prefilter = False
        prefilter_reason = ("warning: code_blob_analysis not "
                            "available; running edit analysis on "
                            "all blocks")

    if not quiet:
        print(f"input:     {input_path}", file=sys.stderr)
        print(f"extractor: {extractor}", file=sys.stderr)
        print(f"out_dir:   {out_dir}", file=sys.stderr)
        if not include_blockquotes:
            print("filter:    blockquoted (thinking-process) blocks "
                  "skipped via --top-level-only", file=sys.stderr)
        print(f"prefilter: {prefilter_reason}", file=sys.stderr)

    try:
        manifest = _run_extractor(
            extractor, input_path, raw_dir,
            top_level_only=not include_blockquotes,
        )
    except (RuntimeError, FileNotFoundError) as e:
        return {"rc": 1, "error": str(e)}

    if not quiet:
        print(f"{len(manifest)} block(s) extracted → {raw_dir}/",
              file=sys.stderr)

    stripped_path = _find_stripped(raw_dir)
    stripped_text = (stripped_path.read_text(encoding="utf-8")
                     if stripped_path else "")
    edit_targets = _scan_edit_targets(stripped_text)
    edit_target_hint = _build_edit_target_hint(edit_targets)
    dominant_target = _pick_dominant_target(edit_targets)
    if not quiet and edit_targets:
        print(f"edit-targets: {edit_target_hint}", file=sys.stderr)

    _client_cache = {"client": opts.get("client")}

    def _get_client():
        if _client_cache["client"] is None:
            _client_cache["client"] = LLMClient(
                model=opts.get("model") or DEFAULT_LLM_MODEL,
                timeout=DEFAULT_LLM_TIMEOUT)
        return _client_cache["client"]

    # ---- per-block classification --------------------------------
    records: list[dict] = []
    for b in manifest:
        context = _extract_context(
            stripped_text, b.get("handle", ""), context_lines)

        if edit_target_hint:
            context = edit_target_hint + "\n\n" + context

        if use_prefilter:
            code_analysis = _run_code_blob_analysis(b, context)
            if (code_analysis is not None
                    and code_analysis.get("is_full_file") is True):
                if not _is_replacement_edit_context(
                        context, b.get("handle", "")):
                    if verbose_h and not quiet:
                        print(f"  [prefilter] {b['filename']}: "
                              f"FULL FILE "
                              f"(score={code_analysis['score']:+.2f},"
                              f" confidence="
                              f"{code_analysis['confidence']}) → "
                              f"skipped", file=sys.stderr)
                    rec = _record_from_full_file_skip(b, code_analysis)
                    if not quiet:
                        print(f"  · {b['filename']}  "
                              f"[skip/full-file]", file=sys.stderr)
                    records.append(rec)
                    continue
                elif verbose_h and not quiet:
                    print(f"  [prefilter] {b['filename']}: "
                          f"full-file shape but edit header in "
                          f"context — running edit analysis",
                          file=sys.stderr)

        try:
            analysis = _run_heuristic(b, context)
        except Exception as e:
            print(f"  ! heuristic analysis failed for "
                  f"{b.get('filename')}: {e}", file=sys.stderr)
            records.append(_fallback_record(b, e))
            continue

        if verbose_h and not quiet:
            print(f"  [heuristic] {b['filename']}: "
                  f"score={analysis['score']:+.2f} "
                  f"confidence={analysis['confidence']} "
                  f"is_edit={analysis['is_edit_request']} "
                  f"kind={analysis.get('edit_kind')}",
                  file=sys.stderr)
            for s in analysis["signals"]:
                sign = "+" if s.weight > 0 else ""
                print(f"      {sign}{s.weight:.2f}  {s.name}",
                      file=sys.stderr)
            for c in analysis["target_candidates"][:5]:
                print(f"      target: {c['name']}  "
                      f"(source={c['source']}, "
                      f"confidence={c['confidence']})",
                      file=sys.stderr)

        use_ai = (not no_ai) and (
            force_ai or analysis["confidence"] == "ambiguous")

        if use_ai:
            try:
                client = _get_client()
            except Exception as e:
                return {"rc": 1,
                        "error": f"could not init LLM client: {e}"}
            rec = _classify_via_ai(
                b, context, analysis, client,
                temperature=opts.get("temperature",
                                     _DEFAULT_TEMPERATURE),
                max_tokens=opts.get("max_tokens",
                                    _DEFAULT_MAX_TOKENS))
            if force_ai and rec.get("ai") is not None:
                ai_edit = bool(rec["is_edit_request"])
                h_edit = bool(analysis["is_edit_request"])
                if ai_edit != h_edit:
                    rec["disagreement"] = True
                    if not quiet:
                        print(f"  ! disagreement {b['filename']}: "
                              f"heuristic={h_edit}, ai={ai_edit}",
                              file=sys.stderr)
        else:
            rec = _record_from_heuristic(b, analysis)

        if not quiet:
            kind = "edit" if rec["is_edit_request"] else "skip"
            src = "ai" if use_ai else "heuristic"
            target = rec.get("target_file") or "(unresolved)"
            print(f"  · {b['filename']}  [{kind}/{src}] → {target}",
                  file=sys.stderr)
        records.append(rec)

    # ---- fallback: assign dominant edit target to unresolved edits --
    if dominant_target:
        patched = 0
        for rec in records:
            if (rec.get("is_edit_request")
                    and not rec.get("target_file")
                    and rec.get("classification") != "full_file_skip"):
                rec["target_file"] = dominant_target
                rec["target_source"] = "conversation_fallback"
                patched += 1
        if not quiet and patched:
            print(f"fallback:   assigned {patched} unresolved "
                  f"edit(s) to {dominant_target}", file=sys.stderr)

    # ---- retrieval from dumps ------------------------------------
    dump_files = opts.get("dump_files") or []
    corpus: Optional[_dc.DumpCorpus] = None
    retrieved_by_target: dict[str, dict] = {}
    retrieved_written = 0
    retrieval_report = None

    if dump_files:
        corpus, dump_errors = _dc._load_dump_corpus(dump_files)
        for e in dump_errors:
            print(f"  ! {e}", file=sys.stderr)
        if not quiet:
            total_in_dumps = sum(len(d.files) for d in corpus.dumps)
            print(f"dumps:     {len(corpus.dumps)} loaded, "
                  f"{total_in_dumps} file(s) indexed", file=sys.stderr)

        for rec in records:
            target = rec.get("target_file")
            if not target:
                continue
            res = retrieved_by_target.get(target)
            if res is None:
                res = corpus.resolve(target)
                retrieved_by_target[target] = res
            rec["retrieved"] = {
                "status":          res["status"],
                "method":          res.get("method"),
                "dump_file":       res.get("dump_file"),
                "relpath":         res.get("relpath"),
                "abs_path":        res.get("abs_path"),
                "content_length":  res.get("content_length"),
                "candidate_count": (
                    len(res.get("candidates", []))
                    if res["status"] == "ambiguous" else 0),
            }
            if not quiet:
                if res["status"] == "resolved":
                    print(f"  ← retrieved  {target}  "
                          f"← {res['dump_file']}::{res['relpath']}",
                          file=sys.stderr)
                elif res["status"] == "ambiguous":
                    print(f"  ? ambiguous   {target}  "
                          f"({len(res['candidates'])} candidate(s))",
                          file=sys.stderr)
                else:
                    print(f"  × not found  {target}", file=sys.stderr)

        retrieved_written = _dc._write_retrieved(
            retrieved_by_target, out_dir, quiet=quiet)

        retrieval_report = {
            "description": (
                "Target-file retrieval report.  `dumps` lists each "
                "dump parsed (with the absolute SCANNED DIRECTORY "
                "it recorded).  `by_target` maps each target-path "
                "suggestion to the resolved file — including every "
                "dump that provided it.  Resolved contents are "
                "written under `retrieved/`."),
            "dumps": [
                {
                    "dump_file":   str(d.path),
                    "scanned_dir": d.scanned_dir,
                    "file_count":  len(d.files),
                }
                for d in corpus.dumps
            ],
            "by_target": {
                t: {k: v for k, v in r.items() if k != "content"}
                for t, r in retrieved_by_target.items()
            },
        }
        (out_dir / "_retrieved_files.json").write_text(
            json.dumps(retrieval_report, indent=2, default=str),
            encoding="utf-8",
        )

    # ---- implement edits -----------------------------------------
    implement_edits = opts.get("implement_edits", False)
    implemented_count = 0
    implement_log: list[dict] = []
    implement_log_path: Optional[Path] = None

    if implement_edits:
        if no_ai:
            print("  ! --implement-edits requires LLM; skipped "
                  "because --no-ai is set", file=sys.stderr)
        elif not dump_files:
            print("  ! --implement-edits requires --dump-file; "
                  "skipped", file=sys.stderr)
        elif corpus is None:
            print("  ! no dump corpus loaded; implement-edits "
                  "skipped", file=sys.stderr)
        else:
            handle_by_index = {
                b["index"]: b.get("handle", "") for b in manifest}
            resolved_targets = [
                (t, r) for t, r in retrieved_by_target.items()
                if r["status"] == "resolved" and r.get("content")
            ]
            if not resolved_targets:
                print("  ! no resolved targets to implement; "
                      "skipping", file=sys.stderr)
            else:
                # Build the SHARED context ONCE.  Every implement
                # call gets the exact same context — only the
                # prompt's "<this-file>" slot varies.
                shared_context = _ep._build_implement_context(
                    stripped_text, handle_by_index, records,
                    raw_dir, retrieved_by_target)
                if not quiet:
                    print(f"\nimplementing edits for "
                          f"{len(resolved_targets)} resolved "
                          f"target(s) "
                          f"(shared context: "
                          f"{len(shared_context)} chars)",
                          file=sys.stderr)

                edited_by_target: dict[str, str] = {}
                for target, _res in resolved_targets:
                    prompt = _ep._build_implement_prompt(
                        target, shared_context)
                    try:
                        client = _get_client()
                        response = client.call(
                            prompt,
                            max_tokens=opts.get(
                                "implement_max_tokens",
                                _DEFAULT_IMPLEMENT_MAX_TOKENS),
                            temperature=opts.get(
                                "implement_temperature",
                                _DEFAULT_IMPLEMENT_TEMPERATURE),
                            tag=f"implement:{Path(target).name}",
                        )
                    except Exception as e:
                        print(f"  ! implement call failed for "
                              f"{target}: {e}", file=sys.stderr)
                        implement_log.append({
                            "target": target,
                            "error":  f"LLM call failed: {e}",
                        })
                        continue
                    edited = _ep._extract_edited_file_from_response(
                        response or "")
                    if edited is None:
                        implement_log.append({
                            "target":        target,
                            "error":         "could not extract "
                                             "fenced block from "
                                             "response",
                            "raw_response":  response,
                        })
                        continue
                    edited_by_target[target] = edited
                    if not quiet:
                        print(f"  ✓ edited  {target}  "
                              f"({len(edited)} chars)",
                              file=sys.stderr)

                implemented_count, write_log = \
                    _eo._write_edited_files(
                        edited_by_target, out_dir)
                implement_log.extend(write_log)
                implement_log_path = out_dir / "_implement_log.json"
                implement_log_path.write_text(
                    json.dumps(implement_log, indent=2, default=str),
                    encoding="utf-8",
                )
                if not quiet:
                    print(f"implement: {implemented_count} file(s) "
                          f"written to {out_dir / 'edited'}/",
                          file=sys.stderr)

    # ---- build edit map + write blobs ----------------------------
    files_dir = out_dir / "files"
    edit_map, target_index = _eo._build_edit_map(
        records, files_dir, include_non_edits=include_non_edits)

    (out_dir / "_edit_map.json").write_text(
        json.dumps(edit_map, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "_edit_targets.json").write_text(
        json.dumps(target_index, indent=2, default=str),
        encoding="utf-8",
    )

    written, skipped = _eo._write_blobs(
        raw_dir, files_dir, records, quiet=quiet)

    (out_dir / "_classification_log.json").write_text(
        json.dumps(records, indent=2, default=str),
        encoding="utf-8",
    )

    edit_count = sum(1 for r in records if r.get("is_edit_request"))
    full_file_count = sum(
        1 for r in records
        if r.get("classification") == "full_file_skip")
    target_count = len(target_index["targets"])
    unresolved_count = len(target_index["unresolved_edits"])

    retrieved_resolved = sum(
        1 for r in retrieved_by_target.values()
        if r["status"] == "resolved")
    retrieved_ambiguous = sum(
        1 for r in retrieved_by_target.values()
        if r["status"] == "ambiguous")
    retrieved_not_found = sum(
        1 for r in retrieved_by_target.values()
        if r["status"] == "not_found")

    if not quiet:
        if full_file_count:
            print(f"\npre-filter: {full_file_count} full-file "
                  f"block(s) skipped via code_blob_analysis",
                  file=sys.stderr)
        print(f"\nwrote {written} file(s) to {files_dir}/ "
              f"({skipped} skipped)", file=sys.stderr)
        print(f"edit requests: {edit_count} across {target_count} "
              f"target file(s); {unresolved_count} unresolved",
              file=sys.stderr)
        if corpus is not None:
            print(f"retrieved: {retrieved_resolved} resolved / "
                  f"{retrieved_ambiguous} ambiguous / "
                  f"{retrieved_not_found} not-found across "
                  f"{len(corpus.dumps)} dump(s)",
                  file=sys.stderr)
            print(f"retrieved: {out_dir / 'retrieved'}/",
                  file=sys.stderr)
            print(f"report:    "
                  f"{out_dir / '_retrieved_files.json'}",
                  file=sys.stderr)
        if implement_edits:
            print(f"implemented: {implemented_count} file(s) in "
                  f"{out_dir / 'edited'}/", file=sys.stderr)
            if implement_log_path:
                print(f"implement:   {implement_log_path}",
                      file=sys.stderr)
        print(f"map:     {out_dir / '_edit_map.json'}",
              file=sys.stderr)
        print(f"targets: {out_dir / '_edit_targets.json'}",
              file=sys.stderr)
        print(f"log:     {out_dir / '_classification_log.json'}",
              file=sys.stderr)
        print(f"raw:     {raw_dir}/", file=sys.stderr)

    return {
        "rc":                   0,
        "out_dir":              str(out_dir),
        "raw_dir":              str(raw_dir),
        "files_dir":            str(files_dir),
        "edit_map":             str(out_dir / "_edit_map.json"),
        "targets":              str(out_dir / "_edit_targets.json"),
        "written":              written,
        "skipped":              skipped,
        "record_count":         len(records),
        "edit_count":           edit_count,
        "target_count":         target_count,
        "unresolved_count":     unresolved_count,
        "full_file_count":      full_file_count,
        "dump_count":           len(corpus.dumps) if corpus else 0,
        "retrieved_resolved":   retrieved_resolved,
        "retrieved_ambiguous":  retrieved_ambiguous,
        "retrieved_not_found":  retrieved_not_found,
        "retrieved_written":    retrieved_written,
        "retrieved":            retrieved_by_target,
        "retrieved_json":       (str(out_dir / "_retrieved_files.json")
                                 if retrieval_report is not None
                                 else None),
        "implemented_count":    implemented_count,
        "implement_log":        (str(implement_log_path)
                                 if implement_log_path else None),
    }


# ---- standalone CLI ----------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_edit_requests.py",
        description=(
            "Extract code blocks, pre-filter confidently-full files, "
            "classify the rest as edit requests, retrieve target-file "
            "contents from one or more dump.py outputs, and "
            "(optionally) have the LLM produce the fully-edited "
            "files."),
    )
    p.add_argument("input", metavar="INPUT", help="input file")
    p.add_argument("--out-dir", metavar="PATH", default=None,
                   help="output dir (default: /tmp/"
                        "deeppeek_edits_<stem>_<ts>)")
    p.add_argument("--extractor", metavar="PATH", default=None,
                   help="path to extract_code_blocks.py")
    p.add_argument("--model", default=DEFAULT_LLM_MODEL,
                   help=f"LLM model (default: {DEFAULT_LLM_MODEL})")
    p.add_argument("--temperature", type=float,
                   default=_DEFAULT_TEMPERATURE, metavar="F",
                   help=f"LLM temperature (default: "
                        f"{_DEFAULT_TEMPERATURE})")
    p.add_argument("--max-tokens", type=int,
                   default=_DEFAULT_MAX_TOKENS, metavar="N",
                   help=f"LLM max_tokens per classification call "
                        f"(default: {_DEFAULT_MAX_TOKENS})")
    p.add_argument("--context-lines", type=int,
                   default=_DEFAULT_CONTEXT_LINES, metavar="N",
                   help=f"context lines per side (default: "
                        f"{_DEFAULT_CONTEXT_LINES})")
    p.add_argument("--include-blockquotes", action="store_true",
                   help="also classify blocks detected inside "
                        "markdown blockquotes")
    p.add_argument("--include-non-edits", action="store_true",
                   help="also write non-edit blobs into files/")
    p.add_argument("--no-full-file-filter", action="store_true",
                   help="skip the code_blob_analysis pre-filter")
    p.add_argument("--no-ai", action="store_true",
                   help="no LLM; heuristic-only classification")
    p.add_argument("--force-ai", action="store_true",
                   help="always call LLM, log disagreements")
    p.add_argument("--verbose-heuristics", action="store_true",
                   help="print per-block heuristic breakdown")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress per-blob progress")

    # ---- new: dump retrieval + implement-edits -------------------
    p.add_argument("--dump-file", action="append", default=[],
                   dest="dump_files", metavar="PATH",
                   help="dump.py output to index for target-file "
                        "retrieval. Repeatable; files are indexed "
                        "into one in-memory corpus.")
    p.add_argument("--implement-edits", action="store_true",
                   help="after retrieval, call the LLM per resolved "
                        "target to produce a fully-edited version "
                        "of the file, written under edited/.  "
                        "Requires --dump-file and an LLM.")
    p.add_argument("--implement-max-tokens", type=int,
                   default=_DEFAULT_IMPLEMENT_MAX_TOKENS,
                   metavar="N",
                   help=f"max_tokens per implement-edits call "
                        f"(default: {_DEFAULT_IMPLEMENT_MAX_TOKENS})")
    p.add_argument("--implement-temperature", type=float,
                   default=_DEFAULT_IMPLEMENT_TEMPERATURE,
                   metavar="F",
                   help=f"temperature for implement-edits "
                        f"(default: {_DEFAULT_IMPLEMENT_TEMPERATURE})")
    return p


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    opts = {
        "out_dir":              args.out_dir,
        "extractor":            args.extractor,
        "model":                args.model,
        "temperature":          args.temperature,
        "max_tokens":           args.max_tokens,
        "context_lines":        args.context_lines,
        "include_blockquotes":  args.include_blockquotes,
        "include_non_edits":    args.include_non_edits,
        "no_full_file_filter":  args.no_full_file_filter,
        "no_ai":                args.no_ai,
        "force_ai":             args.force_ai,
        "verbose_heuristics":   args.verbose_heuristics,
        "quiet":                args.quiet,
        "dump_files":           args.dump_files,
        "implement_edits":      args.implement_edits,
        "implement_max_tokens": args.implement_max_tokens,
        "implement_temperature": args.implement_temperature,
    }
    result = _run_pipeline(Path(args.input), opts)
    if result["rc"] != 0:
        print(f"error: {result.get('error', 'unknown error')}",
              file=sys.stderr)
    return result["rc"]


# ---- llm_scripts hooks -------------------------------------------

NAME = "extract_edit_requests"
DESCRIPTION = (
    "Extract fenced code blocks, pre-filter confidently-full files, "
    "classify the rest as edit requests, retrieve target-file contents "
    "from one or more dump.py outputs, and (optionally) have the LLM "
    "produce the fully-edited files.")
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
        help="(extract_edit_requests) also classify blocks inside "
             "markdown blockquotes")
    parser.add_argument(
        "--eer-include-non-edits", action="store_true",
        dest="eer_include_non_edits",
        help="(extract_edit_requests) also write non-edit blobs")
    parser.add_argument(
        "--eer-no-full-file-filter", action="store_true",
        dest="eer_no_full_file_filter",
        help="(extract_edit_requests) skip the code_blob_analysis "
             "pre-filter")
    parser.add_argument(
        "--eer-no-ai", action="store_true",
        dest="eer_no_ai",
        help="(extract_edit_requests) heuristic-only; no LLM calls")
    parser.add_argument(
        "--eer-force-ai", action="store_true",
        dest="eer_force_ai",
        help="(extract_edit_requests) always call the LLM and log "
             "disagreements with the heuristic")
    parser.add_argument(
        "--eer-verbose-heuristics", action="store_true",
        dest="eer_verbose_heuristics",
        help="(extract_edit_requests) print per-block heuristic "
             "breakdown to stderr")
    parser.add_argument(
        "--eer-context-lines", type=int,
        default=_DEFAULT_CONTEXT_LINES, dest="eer_context_lines",
        metavar="N",
        help="(extract_edit_requests) lines of surrounding context "
             f"per side (default: {_DEFAULT_CONTEXT_LINES})")
    parser.add_argument(
        "--eer-quiet", action="store_true",
        dest="eer_quiet",
        help="(extract_edit_requests) suppress per-blob progress")
    parser.add_argument(
        "--eer-dump-file", action="append", default=[],
        dest="eer_dump_files", metavar="PATH",
        help="(extract_edit_requests) dump.py output for target-file "
             "retrieval. Repeatable.")
    parser.add_argument(
        "--eer-implement-edits", action="store_true",
        dest="eer_implement_edits",
        help="(extract_edit_requests) after retrieval, call the LLM "
             "per resolved target and write edited/.")
    parser.add_argument(
        "--eer-implement-max-tokens", type=int,
        default=_DEFAULT_IMPLEMENT_MAX_TOKENS,
        dest="eer_implement_max_tokens", metavar="N",
        help="(extract_edit_requests) max_tokens per implement call "
             f"(default: {_DEFAULT_IMPLEMENT_MAX_TOKENS})")
    parser.add_argument(
        "--eer-implement-temperature", type=float,
        default=_DEFAULT_IMPLEMENT_TEMPERATURE,
        dest="eer_implement_temperature", metavar="F",
        help="(extract_edit_requests) temperature for implement "
             f"(default: {_DEFAULT_IMPLEMENT_TEMPERATURE})")


def ctx_from_args(args):
    return {
        "_eer_input_file": (str(args.file.resolve())
                            if args.file is not None else None),
        "_eer_out_dir": (str(args.eer_out_dir.resolve())
                         if getattr(args, "eer_out_dir", None) else None),
        "_eer_extractor": getattr(args, "eer_extractor", None),
        "_eer_include_blockquotes": getattr(
            args, "eer_include_blockquotes", False),
        "_eer_include_non_edits": getattr(
            args, "eer_include_non_edits", False),
        "_eer_no_full_file_filter": getattr(
            args, "eer_no_full_file_filter", False),
        "_eer_no_ai": getattr(args, "eer_no_ai", False),
        "_eer_force_ai": getattr(args, "eer_force_ai", False),
        "_eer_verbose_heuristics": getattr(
            args, "eer_verbose_heuristics", False),
        "_eer_context_lines": getattr(
            args, "eer_context_lines", _DEFAULT_CONTEXT_LINES),
        "_eer_quiet": getattr(args, "eer_quiet", False),
        "_eer_temperature": getattr(args, "temperature",
                                    _DEFAULT_TEMPERATURE),
        "_eer_max_tokens": getattr(args, "max_tokens",
                                   _DEFAULT_MAX_TOKENS),
        "_eer_dump_files": list(getattr(
            args, "eer_dump_files", []) or []),
        "_eer_implement_edits": getattr(
            args, "eer_implement_edits", False),
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
    return f"(extract_edit_requests: {n} chars stashed for processing)"


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
        "temperature":           ctx.get("_eer_temperature",
                                         _DEFAULT_TEMPERATURE),
        "max_tokens":            ctx.get("_eer_max_tokens",
                                         _DEFAULT_MAX_TOKENS),
        "context_lines":         ctx.get("_eer_context_lines",
                                         _DEFAULT_CONTEXT_LINES),
        "include_blockquotes":   ctx.get("_eer_include_blockquotes",
                                         False),
        "include_non_edits":     ctx.get("_eer_include_non_edits",
                                         False),
        "no_full_file_filter":   ctx.get("_eer_no_full_file_filter",
                                         False),
        "no_ai":                 ctx.get("_eer_no_ai", False),
        "force_ai":              ctx.get("_eer_force_ai", False),
        "verbose_heuristics":    ctx.get("_eer_verbose_heuristics",
                                         False),
        "quiet":                 ctx.get("_eer_quiet", False),
        "client":                ctx.get("_llm_client"),
        "dump_files":            list(ctx.get("_eer_dump_files", [])),
        "implement_edits":       ctx.get("_eer_implement_edits", False),
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

    return (
        f"extracted {result['record_count']} block(s): "
        f"{result['edit_count']} edit request(s) across "
        f"{result['target_count']} target file(s), "
        f"{result['unresolved_count']} unresolved, "
        f"{result['full_file_count']} full-file pre-filtered; "
        f"{result['written']} file(s) written\n"
        f"retrieval:  {result.get('retrieved_resolved', 0)} "
        f"resolved / {result.get('retrieved_ambiguous', 0)} "
        f"ambiguous / {result.get('retrieved_not_found', 0)} "
        f"not-found across {result.get('dump_count', 0)} dump(s)\n"
        f"implement:  {result.get('implemented_count', 0)} file(s)"
        + (f"  ({result['implement_log']})"
           if result.get('implement_log') else "") + "\n"
        f"files:      {result['files_dir']}/\n"
        f"targets:    {result['targets']}\n"
        f"map:        {result['edit_map']}\n"
        f"raw:        {result['raw_dir']}/"
    )


if __name__ == "__main__":
    sys.exit(main())
