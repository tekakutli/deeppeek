#!/usr/bin/env python3
"""extract_named_files.py — extract, name, and write code blocks to /tmp/.

Pipeline
--------
  1. Run `extract_code_blocks.py` on the input file.  Produces one
     blob per fenced code block, a manifest, and a stripped copy of
     the original with `{{handle}}` placeholders where each kept
     block was.  (Thinking-process blocks are already filtered by
     the extractor.)

  2. For each blob, run `code_blob_analysis.analyze`.  It produces:
       · a signed score in [-1, +1] for full-file vs fragment
       · a confidence band (high / ambiguous)
       · ranked filename candidates from context and content
     All heuristic, no LLM.

  3. Decide per block:
       · high_full_file  → keep, use the top filename candidate if
                            one exists (no LLM call)
       · high_fragment   → skip (no LLM call)
       · ambiguous       → call the LLM with the heuristic signals
                            included as hints in the prompt, so the
                            model has the same evidence the heuristic
                            saw
     `--force-ai` overrides: always call the LLM and log any
     disagreement with the heuristic.
     `--no-ai` disables the LLM entirely: heuristic-only
     classification, fall back to `block_filename` when no better
     name is found.

  4. Flatten every surviving file's suggested path to a bare
     basename (dedup on collisions), write all files into one flat
     `files/` subdirectory, and record the original suggested path
     for each in `_path_map.json`.  Fragments are skipped by
     default; `--include-fragments` writes them as
     `fragment_NNN.<ext>` (already flat).

Output layout
-------------
    /tmp/deeppeek_extract_<stem>_<YYYYmmdd_HHMMSS>/
        files/                    ← flat subdir, everything one level
            <flat names>          (basenames only; dedup suffixes
                                   _2, _3, … on collision)
        _raw/                     intermediate extraction
            block_NNN_lang.ext
            manifest.json
            <stem>_stripped.txt
        _path_map.json            flat name → suggested path
                                    {
                                      "flat_dir": ".../files",
                                      "entries": [
                                        {"flat_filename": "pg_state.py",
                                         "suggested_path":
                                           "playgrounds/interceptor/pg_state.py",
                                         "block_filename":
                                           "block_002_python.py",
                                         "is_full_file": true},
                                        ...
                                      ]
                                    }
        _classification_log.json  one record per block:
                                    index, is_full_file,
                                    heuristic{score, confidence, signals},
                                    ai{...} (if called),
                                    suggested_path, final_filename,
                                    reasoning

The `_path_map.json` entries are the "potential map": each flat file
corresponds to a path the pipeline originally inferred from the
chat context (`suggested_path`).  Consumers that want to reconstruct
the intended directory tree can use the map directly; consumers that
just want a flat bag of files can ignore it.

Flags
-----
    --out-dir PATH           Override default /tmp/... dir.
    --extractor PATH         Path to extract_code_blocks.py
                             (default: same directory as this script).
    --model NAME             LLM model (default: DEFAULT_LLM_MODEL).
    --temperature FLOAT      LLM temperature (default: 0.1).
    --max-tokens INT         LLM max_tokens per classification call
                             (default: 600).
    --context-lines INT      Lines of context per side (default: 30).
    --include-fragments      Also write fragments.
    --no-ai                  Skip the LLM; heuristic only.
    --force-ai               Always call the LLM, even for
                             high-confidence heuristic cases; log
                             disagreements.
    --verbose-heuristics     Print per-block heuristic breakdown to
                             stderr (score, signals, candidates).
    -q, --quiet              Suppress per-blob progress on stderr.

Exit codes
----------
    0  ran to completion (even if some blobs were classified as
       fragments and skipped)
    1  hard error (extractor failed, no manifest, LLM unreachable
       when the LLM was required)

## Framework hooks (llm_scripts)

This file is BOTH a standalone CLI (see "Usage" above) AND an
llm_scripts module.  When loaded via `--script extract_named_files`:

    deeppeek editor --script extract_named_files --file other.txt
    deeppeek editor --script extract_named_files "some markdown..."

the `build_prompt` / `on_response` hooks below route through
`_run_pipeline`, reusing the editor-injected client
(`ctx["_llm_client"]`) so `--url`, `--llm-timeout`, `--model`,
`--temperature`, `--max-tokens` flow through unchanged.
`requires_llm = False` because the framework's single-shot call is
not used — the pipeline makes its own per-ambiguous-block calls.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from deeppeek.llm import (
    LLMClient, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT,
)

try:
    from code_blob_analysis import analyze as analyze_blob
except ImportError:
    # Fall back to loading from the script's own directory.
    import importlib.util
    _here = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "code_blob_analysis", _here / "code_blob_analysis.py")
    if _spec is None or _spec.loader is None:
        raise ImportError(
            "could not import code_blob_analysis.py — place it "
            "next to this script or on PYTHONPATH")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["code_blob_analysis"] = _mod
    _spec.loader.exec_module(_mod)
    analyze_blob = _mod.analyze


# ---- defaults ----------------------------------------------------

_DEFAULT_CONTEXT_LINES = 30
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_MAX_TOKENS = 600

_EXT = {
    "python": "py", "py": "py", "py3": "py", "python3": "py",
    "javascript": "js", "js": "js", "jsx": "jsx",
    "typescript": "ts", "ts": "ts", "tsx": "tsx",
    "json": "json", "yaml": "yaml", "yml": "yml",
    "css": "css", "scss": "scss",
    "html": "html", "htm": "html",
    "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "rust": "rs", "go": "go",
    "c": "c", "cpp": "cpp", "c++": "cpp",
    "java": "java", "kotlin": "kt", "ruby": "rb",
    "markdown": "md", "md": "md", "text": "txt",
    "": "txt",
}


def _ext_for(lang: str) -> str:
    lang = (lang or "").lower()
    if lang in _EXT:
        return _EXT[lang]
    s = "".join(c for c in lang if c.isalnum())
    return s[:4] if s else "txt"


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
                   raw_dir: Path) -> list[dict]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(extractor), str(input_path),
         str(raw_dir), "-q", "--no-review-log"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"extract_code_blocks.py exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}")
    manifest_path = raw_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"missing manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _find_stripped(raw_dir: Path, input_path: Path) -> Optional[Path]:
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


# ---- heuristic adapter -------------------------------------------

def _run_heuristic(block: dict, context: str) -> dict:
    """Adapter around `code_blob_analysis.analyze`.

    Assumes signature `analyze(block_dict, context_str)` where
    `block_dict` is the manifest entry (has `content`, `lang`,
    `filename`, `handle`, `index`) and `context_str` is the
    surrounding text window.  If `code_blob_analysis.py` uses a
    different signature, change THIS function rather than every
    call site.

    Expected return shape (consumed by `_build_heuristic_hints`,
    `_record_from_heuristic`, and `_classify_via_ai`):

        {
          "score":              float in [-1, +1]
          "confidence":         "high" | "ambiguous"
          "is_full_file":       bool   (meaningful when high)
          "signals":            [obj with .name, .weight, .detail]
          "filename_candidates": [{"name", "source", "confidence"}]
        }
    """
    return analyze_blob(block, context)


# ---- LLM classification (ambiguous path only) -------------------

_PROMPT_TEMPLATE = """\
You are a code-archaeologist.  Given a block of code extracted from a
chat log, decide whether it represents a COMPLETE FILE or a FRAGMENT,
and if complete, suggest a filename.

A COMPLETE FILE typically has:
  · A module-level docstring or header comment at the very top
  · Imports / module-level setup at the start
  · Complete top-level constructs (no "..." elision markers, no
    "# existing code" placeholders)
  · Could be saved and run / imported as-is
  · The surrounding context presents it as a complete file
    ("here's foo.py", "## 1/3 — `path/to/file.py`", "Save as
    `foo.py`", etc.)

A FRAGMENT typically has:
  · Starts mid-definition (indentation implies we're inside something)
  · Elision markers: "...", "# ...", "# existing code",
    "# rest of function", "# similarly for X"
  · Is a single isolated function or class with no imports or module
    context
  · The surrounding context presents it as a change or snippet
    ("add this to X.py", "modify the foo function", "here's the
    relevant bit")

The heuristic analysis below has already been run against this block
and its context.  Treat those signals as STRONG evidence — they are
derived from `ast` parsing and header detection, not guesses.  If
they say "full file" with high confidence, you should agree unless
you see a clear reason not to.  If they say "fragment" with high
confidence, you should agree unless you see a clear reason not to.

Language: {lang}
Original filename: {orig_filename}

Heuristic pre-analysis:
{heuristic_hints}

Block content:
<block>
{content}
</block>

Surrounding context (block appears where you see {handle}):
<context>
{context}
</context>

Respond with STRICT JSON only (no prose, no markdown fences):
{{
  "is_full_file": <true|false>,
  "suggested_filename": <"path/to/file.ext" or null>,
  "reasoning": "<one sentence>",
  "agrees_with_heuristic": <true|false>
}}

Filename rules:
  · Prefer a name explicitly given in the surrounding context
  · Otherwise infer from the docstring, the primary class/function,
    or the file's apparent purpose
  · Include the proper extension: .py for python, .js / .jsx for
    javascript, .ts / .tsx for typescript, .rs for rust, .go for go,
    .c for c, .cpp for c/c++, .java for java, .rb for ruby, .sh for
    bash/shell, .json, .yaml/.yml, .html, .css, .md, etc.
  · Include directory prefixes only when context implies them
  · No leading "/" and no absolute paths
"""


def _build_heuristic_hints(analysis: dict) -> str:
    lines = [f"score: {analysis['score']:+.2f} "
             f"(confidence: {analysis['confidence']})"]
    if analysis["signals"]:
        lines.append("signals:")
        for s in analysis["signals"]:
            sign = "+" if s.weight > 0 else ""
            detail = f"  ({s.detail})" if s.detail else ""
            lines.append(f"  {sign}{s.weight:.2f}  {s.name}{detail}")
    if analysis["filename_candidates"]:
        lines.append("filename candidates:")
        for c in analysis["filename_candidates"][:5]:
            lines.append(
                f"  {c['name']}  "
                f"(source: {c['source']}, confidence: {c['confidence']})")
    return "\n".join(lines)


def _build_prompt(block: dict, context: str, analysis: dict) -> str:
    return _PROMPT_TEMPLATE.format(
        lang=block.get("lang") or "(unknown)",
        orig_filename=block.get("filename") or "?",
        heuristic_hints=_build_heuristic_hints(analysis),
        content=block.get("content") or "",
        handle=block.get("handle") or "?",
        context=context,
    )


def _parse_json_response(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            inner = s[nl + 1:]
            if inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3].rstrip()
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
    start = s.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def _classify_via_ai(block: dict, context: str, analysis: dict,
                     client: LLMClient, temperature: float,
                     max_tokens: int) -> dict:
    prompt = _build_prompt(block, context, analysis)
    record = {
        "index":              block["index"],
        "block_filename":     block.get("filename"),
        "lang":               block.get("lang"),
        "is_full_file":       False,
        "suggested_filename": None,
        "suggested_path":     None,   # filled in by _flatten_and_map
        "final_filename":     None,
        "reasoning":          "",
        "raw_response":       None,
        "error":              None,
        "heuristic": {
            "score":      analysis["score"],
            "confidence": analysis["confidence"],
            "signals":    [{"name": s.name, "weight": s.weight}
                            for s in analysis["signals"]],
        },
        "ai": None,
    }
    try:
        response = client.call(
            prompt, max_tokens=max_tokens, temperature=temperature,
            tag=f"name:{block['index']:03d}",
        )
    except Exception as e:
        record["error"] = f"LLM call failed: {e}"
        # Fall back to heuristic when the LLM is unreachable.
        record["is_full_file"] = bool(analysis["is_full_file"])
        if analysis["is_full_file"] and analysis["filename_candidates"]:
            record["final_filename"] = _safe_relative_path(
                analysis["filename_candidates"][0]["name"], block)
        return record

    if response is None:
        record["error"] = "LLM returned None"
        record["is_full_file"] = bool(analysis["is_full_file"])
        return record

    record["raw_response"] = response
    parsed = _parse_json_response(response)
    if parsed is None:
        record["error"] = "could not parse JSON from response"
        record["is_full_file"] = bool(analysis["is_full_file"])
        return record

    record["ai"] = parsed
    record["is_full_file"] = bool(parsed.get("is_full_file"))
    sug = parsed.get("suggested_filename")
    if isinstance(sug, str) and sug.strip():
        record["suggested_filename"] = sug.strip()
    record["reasoning"] = str(parsed.get("reasoning") or "")

    if record["is_full_file"]:
        # Prefer AI's suggestion; fall back to heuristic's top candidate.
        name = record["suggested_filename"]
        if not name and analysis["filename_candidates"]:
            name = analysis["filename_candidates"][0]["name"]
        if name:
            record["final_filename"] = _safe_relative_path(name, block)
    return record


def _record_from_heuristic(block: dict, analysis: dict) -> dict:
    """Record for a high-confidence heuristic decision (no LLM call).

    Matches `_classify_via_ai`'s behavior: a full-file block with no
    filename candidate is left without a `final_filename` and will be
    skipped by `_write_named`.  (`--no-ai` mode has a looser fallback
    handled separately in `_run_pipeline`.)
    """
    is_full = bool(analysis.get("is_full_file"))
    candidates = analysis.get("filename_candidates") or []
    final = None
    if is_full and candidates:
        final = _safe_relative_path(candidates[0]["name"], block)
    return {
        "index":              block["index"],
        "block_filename":     block.get("filename"),
        "lang":               block.get("lang"),
        "is_full_file":       is_full,
        "suggested_filename": None,
        "suggested_path":     None,   # filled in by _flatten_and_map
        "final_filename":     final,
        "reasoning": (f"heuristic (score {analysis['score']:+.2f}, "
                      f"{analysis['confidence']})"),
        "raw_response":       None,
        "error":              None,
        "heuristic": {
            "score":      analysis["score"],
            "confidence": analysis["confidence"],
            "signals":    [{"name": s.name, "weight": s.weight}
                            for s in analysis["signals"]],
        },
        "ai": None,
    }


def _fallback_record(block: dict, error: Exception) -> dict:
    return {
        "index":              block["index"],
        "block_filename":     block.get("filename"),
        "lang":               block.get("lang"),
        "is_full_file":       False,
        "suggested_filename": None,
        "suggested_path":     None,
        "final_filename":     None,
        "reasoning":          "",
        "raw_response":       None,
        "error":              f"heuristic analysis failed: {error}",
        "heuristic":          None,
        "ai":                 None,
    }


# ---- final-name resolution ---------------------------------------

def _safe_relative_path(suggestion: Optional[str],
                        block: dict) -> str:
    fallback = (f"fragment_{block['index']:03d}."
                f"{_ext_for(block.get('lang'))}")
    if not suggestion:
        return fallback
    p = Path(suggestion.replace("\\", "/"))
    if p.is_absolute():
        p = Path(*p.parts[1:])
    parts = [x for x in p.parts if x and x not in (".", "..")]
    if not parts:
        return fallback
    if not parts[-1] or parts[-1] in (".", ".."):
        return fallback
    return str(Path(*parts))


def _dedup_names(records: list[dict]) -> None:
    used: set[str] = set()
    for rec in records:
        name = rec.get("final_filename")
        if not name:
            continue
        if name not in used:
            used.add(name)
            continue
        base = Path(name)
        parent, stem, suffix = base.parent, base.stem, base.suffix
        n = 2
        candidate = name
        while candidate in used:
            candidate = str(parent / f"{stem}_{n}{suffix}")
            n += 1
        rec["final_filename"] = candidate
        used.add(candidate)


def _flatten_and_map(records: list[dict], files_dir: Path) -> dict:
    """Flatten every `final_filename` to its basename, dedup on the
    basenames, and return the potential-path map for
    `_path_map.json`.

    Before mutation, each surviving record's relative path (with any
    directory components) is preserved in `suggested_path`.  After
    mutation, `final_filename` is a bare basename — every file lands
    one level deep in `files_dir`, and any name collision is resolved
    by `_dedup_names` (`foo.py` → `foo_2.py`, etc.).

    Records without a `final_filename` (fragments with
    `include_fragments=False`) are skipped by the map, matching what
    `_write_named` does.
    """
    files_dir.mkdir(parents=True, exist_ok=True)

    for rec in records:
        fn = rec.get("final_filename")
        if fn:
            rec["suggested_path"] = fn
            rec["final_filename"] = Path(fn).name
        else:
            rec["suggested_path"] = None

    # Re-dedup now that every name is flat — `foo/a.py` and
    # `bar/a.py` collide at the basename layer even if they didn't
    # collide at the relative-path layer.
    _dedup_names(records)

    return {
        "flat_dir": str(files_dir),
        "description": (
            "Potential-path map.  Every extracted file lands flat in "
            "`files/`; each entry records the path the classifier "
            "originally inferred from the chat context "
            "(`suggested_path`).  Use this to reconstruct the "
            "intended directory layout, or to look up where a given "
            "flat file 'should' have gone."
        ),
        "entries": [
            {
                "flat_filename":  rec["final_filename"],
                "suggested_path": rec.get("suggested_path"),
                "block_filename": rec.get("block_filename"),
                "is_full_file":   rec["is_full_file"],
            }
            for rec in records if rec.get("final_filename")
        ],
    }


# ---- write results -----------------------------------------------

def _write_named(raw_dir: Path, out_dir: Path, records: list[dict],
                 *, include_fragments: bool,
                 quiet: bool) -> tuple[int, int]:
    written = skipped = 0
    for rec in records:
        fn = rec.get("final_filename")
        if not fn:
            skipped += 1
            if not quiet and not rec["is_full_file"]:
                print(f"  · skip  {rec['block_filename']}  (fragment)",
                      file=sys.stderr)
            continue
        if not rec["is_full_file"] and not include_fragments:
            skipped += 1
            if not quiet:
                print(f"  · skip  {rec['block_filename']} → {fn}  "
                      f"(fragment)", file=sys.stderr)
            continue
        src = raw_dir / rec["block_filename"]
        if not src.is_file():
            print(f"  ! missing blob: {src}", file=sys.stderr)
            skipped += 1
            continue
        dst = out_dir / fn
        # `fn` is already a bare basename after _flatten_and_map, so
        # this mkdir is a no-op — kept for safety if _write_named is
        # ever called with non-flat names.
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        written += 1
        if not quiet:
            kind = "full" if rec["is_full_file"] else "frag"
            print(f"  → write [{kind}] {rec['block_filename']} "
                  f"→ files/{fn}", file=sys.stderr)
    return written, skipped


# ---- core pipeline (shared by CLI and hooks) --------------------

def _run_pipeline(input_path: Path, opts: dict) -> dict:
    """Run the full extraction pipeline on `input_path`.

    `opts` keys (all optional; defaults applied here):

        out_dir              — output dir; when falsy, timestamped
                               /tmp/deeppeek_extract_<stem>_<ts>/
        extractor            — path to extract_code_blocks.py
        model                — LLM model
        temperature          — LLM temperature
        max_tokens           — LLM max_tokens per call
        context_lines        — context lines per side
        include_fragments    — also write fragments
        no_ai                — heuristic-only classification
        force_ai             — always call LLM, log disagreements
        verbose_heuristics   — print per-block heuristic breakdown
        quiet                — suppress progress on stderr
        client               — pre-built LLMClient (bypasses init)

    Returns a summary dict:
        rc            int — 0 on success, 1 on hard error
        error         str — present on rc != 0
        out_dir       str — output dir (rc == 0)
        raw_dir       str — intermediate dir (rc == 0)
        files_dir     str — flat files dir (rc == 0)
        path_map      str — _path_map.json path (rc == 0)
        written       int
        skipped       int
        record_count  int
    """
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
            f"deeppeek_extract_{input_path.stem}_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "_raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)

    quiet = opts.get("quiet", False)
    verbose_h = opts.get("verbose_heuristics", False)
    no_ai = opts.get("no_ai", False)
    force_ai = opts.get("force_ai", False)
    context_lines = opts.get("context_lines", _DEFAULT_CONTEXT_LINES)
    include_fragments = opts.get("include_fragments", False)

    if not quiet:
        print(f"input:     {input_path}", file=sys.stderr)
        print(f"extractor: {extractor}", file=sys.stderr)
        print(f"out_dir:   {out_dir}", file=sys.stderr)

    try:
        manifest = _run_extractor(extractor, input_path, raw_dir)
    except (RuntimeError, FileNotFoundError) as e:
        return {"rc": 1, "error": str(e)}

    if not manifest and not quiet:
        print("no code blocks found — writing empty outputs.",
              file=sys.stderr)
    elif not quiet:
        print(f"{len(manifest)} block(s) extracted → {raw_dir}/",
              file=sys.stderr)

    stripped_path = _find_stripped(raw_dir, input_path)
    stripped_text = (stripped_path.read_text(encoding="utf-8")
                     if stripped_path else "")

    # Lazy client: only initialise if an ambiguous block actually
    # needs the LLM (and --no-ai isn't set).
    _client_cache = {"client": opts.get("client")}

    def _get_client():
        if _client_cache["client"] is None:
            _client_cache["client"] = LLMClient(
                model=opts.get("model") or DEFAULT_LLM_MODEL,
                timeout=DEFAULT_LLM_TIMEOUT)
        return _client_cache["client"]

    records: list[dict] = []
    for b in manifest:
        context = _extract_context(
            stripped_text, b.get("handle", ""), context_lines)
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
                  f"is_full_file={analysis['is_full_file']}",
                  file=sys.stderr)
            for s in analysis["signals"]:
                sign = "+" if s.weight > 0 else ""
                detail = f"  ({s.detail})" if s.detail else ""
                print(f"      {sign}{s.weight:.2f}  {s.name}{detail}",
                      file=sys.stderr)
            for c in analysis["filename_candidates"][:5]:
                print(f"      candidate: {c['name']}  "
                      f"(source={c['source']}, "
                      f"confidence={c['confidence']})",
                      file=sys.stderr)

        use_ai = (not no_ai) and (force_ai
                                   or analysis["confidence"] == "ambiguous")

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
            # Under --force-ai, log disagreements with the heuristic.
            if force_ai and rec.get("ai") is not None:
                ai_full = bool(rec["is_full_file"])
                h_full = bool(analysis["is_full_file"])
                if ai_full != h_full:
                    rec["disagreement"] = True
                    if not quiet:
                        print(f"  ! disagreement {b['filename']}: "
                              f"heuristic={h_full}, ai={ai_full}",
                              file=sys.stderr)
        else:
            rec = _record_from_heuristic(b, analysis)
            # --no-ai: fall back to block_filename for full-file
            # records that the heuristic couldn't name.  This is
            # deliberately looser than the heuristic-auto path —
            # the docstring promises block_filename fallback here.
            if no_ai and rec["is_full_file"] and not rec["final_filename"]:
                rec["final_filename"] = b.get("filename")

        if not quiet:
            kind = "full" if rec["is_full_file"] else "frag"
            src = "ai" if use_ai else "heuristic"
            target = rec.get("final_filename") or "(skip)"
            print(f"  · {b['filename']}  [{kind}/{src}] → {target}",
                  file=sys.stderr)
        records.append(rec)

    # Fill fragment names for non-full records when including them.
    # These are already flat — `_flatten_and_map` just records them
    # as their own suggested_path.
    for rec in records:
        if rec.get("final_filename") is None and include_fragments:
            rec["final_filename"] = (
                f"fragment_{rec['index']:03d}."
                f"{_ext_for(rec.get('lang'))}")

    # Flatten suggested paths to basenames, dedup, and build the
    # potential-path map.  Writes nothing else on its own.
    files_dir = out_dir / "files"
    path_map = _flatten_and_map(records, files_dir)
    (out_dir / "_path_map.json").write_text(
        json.dumps(path_map, indent=2, default=str),
        encoding="utf-8",
    )

    written, skipped = _write_named(
        raw_dir, files_dir, records,
        include_fragments=include_fragments, quiet=quiet)

    (out_dir / "_classification_log.json").write_text(
        json.dumps(records, indent=2, default=str),
        encoding="utf-8",
    )

    if not quiet:
        print(f"\nwrote {written} file(s) to {files_dir}/ "
              f"({skipped} skipped)", file=sys.stderr)
        print(f"map:   {out_dir / '_path_map.json'}",
              file=sys.stderr)
        print(f"log:   {out_dir / '_classification_log.json'}",
              file=sys.stderr)
        print(f"raw:   {raw_dir}/", file=sys.stderr)

    return {
        "rc": 0,
        "out_dir": str(out_dir),
        "raw_dir": str(raw_dir),
        "files_dir": str(files_dir),
        "path_map": str(out_dir / "_path_map.json"),
        "written": written,
        "skipped": skipped,
        "record_count": len(records),
    }


# ---- standalone CLI ----------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_named_files.py",
        description=(
            "Extract code blocks, classify (file vs fragment) using "
            "structural heuristics first and an LLM only when "
            "ambiguous, then write flat-named files into `files/` "
            "with a `_path_map.json` recording each file's "
            "originally-suggested path."
        ),
    )
    p.add_argument("input", metavar="INPUT", help="input file")
    p.add_argument("--out-dir", metavar="PATH", default=None,
                   help="output dir (default: /tmp/"
                        "deeppeek_extract_<stem>_<ts>)")
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
                   help=f"LLM max_tokens per call (default: "
                        f"{_DEFAULT_MAX_TOKENS})")
    p.add_argument("--context-lines", type=int,
                   default=_DEFAULT_CONTEXT_LINES, metavar="N",
                   help=f"context lines per side (default: "
                        f"{_DEFAULT_CONTEXT_LINES})")
    p.add_argument("--include-fragments", action="store_true",
                   help="also write fragment blobs as "
                        "fragment_NNN.<ext>")
    p.add_argument("--no-ai", action="store_true",
                   help="no LLM; heuristic-only classification")
    p.add_argument("--force-ai", action="store_true",
                   help="always call LLM, log disagreements with "
                        "heuristic")
    p.add_argument("--verbose-heuristics", action="store_true",
                   help="print per-block heuristic breakdown to "
                        "stderr")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress per-blob progress on stderr")
    return p


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    opts = {
        "out_dir": args.out_dir,
        "extractor": args.extractor,
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "context_lines": args.context_lines,
        "include_fragments": args.include_fragments,
        "no_ai": args.no_ai,
        "force_ai": args.force_ai,
        "verbose_heuristics": args.verbose_heuristics,
        "quiet": args.quiet,
    }
    result = _run_pipeline(Path(args.input), opts)
    if result["rc"] != 0:
        print(f"error: {result.get('error', 'unknown error')}",
              file=sys.stderr)
    return result["rc"]


# ---- llm_scripts hooks -------------------------------------------

NAME = "extract_named_files"
DESCRIPTION = (
    "Extract fenced code blocks, classify each with structural "
    "heuristics + an LLM only when ambiguous, write them all flat "
    "into files/, and record the originally-suggested paths in "
    "_path_map.json."
)
# The pipeline drives its own per-ambiguous-block LLM calls via
# ctx["_llm_client"]; the framework's single-shot call is skipped.
requires_llm = False


def register_flags(parser):
    parser.add_argument(
        "--enf-out-dir", type=Path, default=None,
        dest="enf_out_dir", metavar="PATH",
        help="(extract_named_files) override the output directory "
             "(default: <session_dir>/extracted/run_<ts>/)",
    )
    parser.add_argument(
        "--enf-extractor", type=str, default=None,
        dest="enf_extractor", metavar="PATH",
        help="(extract_named_files) path to extract_code_blocks.py",
    )
    parser.add_argument(
        "--enf-include-fragments", action="store_true",
        dest="enf_include_fragments",
        help="(extract_named_files) also write fragments as "
             "fragment_NNN.<ext>",
    )
    parser.add_argument(
        "--enf-no-ai", action="store_true",
        dest="enf_no_ai",
        help="(extract_named_files) heuristic-only; no LLM calls",
    )
    parser.add_argument(
        "--enf-force-ai", action="store_true",
        dest="enf_force_ai",
        help="(extract_named_files) always call the LLM and log "
             "disagreements with the heuristic",
    )
    parser.add_argument(
        "--enf-verbose-heuristics", action="store_true",
        dest="enf_verbose_heuristics",
        help="(extract_named_files) print per-block heuristic "
             "breakdown to stderr",
    )
    parser.add_argument(
        "--enf-context-lines", type=int,
        default=_DEFAULT_CONTEXT_LINES, dest="enf_context_lines",
        metavar="N",
        help="(extract_named_files) lines of surrounding context "
             f"per side (default: {_DEFAULT_CONTEXT_LINES})",
    )
    parser.add_argument(
        "--enf-quiet", action="store_true",
        dest="enf_quiet",
        help="(extract_named_files) suppress per-blob progress",
    )


def ctx_from_args(args):
    return {
        "_enf_input_file": (str(args.file.resolve())
                            if args.file is not None else None),
        "_enf_out_dir": (str(args.enf_out_dir.resolve())
                         if getattr(args, "enf_out_dir", None) else None),
        "_enf_extractor": getattr(args, "enf_extractor", None),
        "_enf_include_fragments": getattr(
            args, "enf_include_fragments", False),
        "_enf_no_ai": getattr(args, "enf_no_ai", False),
        "_enf_force_ai": getattr(args, "enf_force_ai", False),
        "_enf_verbose_heuristics": getattr(
            args, "enf_verbose_heuristics", False),
        "_enf_context_lines": getattr(
            args, "enf_context_lines", _DEFAULT_CONTEXT_LINES),
        "_enf_quiet": getattr(args, "enf_quiet", False),
        # Reuse the editor subparser's --temperature / --max-tokens
        # so both entry points honour the same flags.
        "_enf_temperature": getattr(args, "temperature",
                                    _DEFAULT_TEMPERATURE),
        "_enf_max_tokens": getattr(args, "max_tokens",
                                   _DEFAULT_MAX_TOKENS),
    }


def build_prompt(input_value, ctx):
    # `requires_llm = False`: the return value is never sent to the
    # LLM.  Stash the input for on_response and return a short note.
    ctx["_enf_input_text"] = input_value
    n = len(input_value) if hasattr(input_value, "__len__") else 0
    return f"(extract_named_files: {n} chars stashed for processing)"


def on_response(response, ctx):
    input_text = ctx.pop("_enf_input_text", "")
    input_file = ctx.pop("_enf_input_file", None)
    session_dir = ctx.get("session_dir") or Path("/tmp")

    # Prefer the original --file path when it still exists on disk —
    # that's what the user named on the command line and what they
    # expect to be processed.  Otherwise write the in-memory text to
    # session_dir (positional-text invocation).
    if input_file and Path(input_file).is_file():
        input_path = Path(input_file)
    else:
        input_path = Path(session_dir) / "input.md"
        input_path.write_text(input_text, encoding="utf-8")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = ctx.pop("_enf_out_dir", None) or str(
        Path(session_dir) / "extracted" / f"run_{ts}")

    opts = {
        "out_dir": out_dir,
        "extractor": ctx.pop("_enf_extractor", None),
        "model": ctx.get("model") or DEFAULT_LLM_MODEL,
        "temperature": ctx.get("_enf_temperature", _DEFAULT_TEMPERATURE),
        "max_tokens": ctx.get("_enf_max_tokens", _DEFAULT_MAX_TOKENS),
        "context_lines": ctx.get("_enf_context_lines",
                                 _DEFAULT_CONTEXT_LINES),
        "include_fragments": ctx.get("_enf_include_fragments", False),
        "no_ai": ctx.get("_enf_no_ai", False),
        "force_ai": ctx.get("_enf_force_ai", False),
        "verbose_heuristics": ctx.get("_enf_verbose_heuristics", False),
        "quiet": ctx.get("_enf_quiet", False),
        "client": ctx.get("_llm_client"),
    }

    result = _run_pipeline(input_path, opts)
    if result["rc"] != 0:
        return (f"extract_named_files failed: "
                f"{result.get('error', 'unknown error')}")

    return (
        f"extracted {result['record_count']} block(s): "
        f"{result['written']} written, {result['skipped']} skipped\n"
        f"files: {result['files_dir']}/\n"
        f"path map: {result['path_map']}\n"
        f"raw: {result['raw_dir']}/"
    )


if __name__ == "__main__":
    sys.exit(main())
