"""
recover.classify — Pass 2 (block classification).

DIFFICULT.  This module owns:

    FIX [A]   RECOVERED_META_RE now matches "FILE N/M: <path>" in any
              form.  The previous regex required "files/" immediately
              after the colon; recover's own header-layout output uses
              "recovered/files/..." which didn't match, so the
              project_dump_recover.md passthrough survived Pass 2,
              its fences got attributed as patches, every patch
              failed, and 6 good files were dropped.
    FIX [4]   narrow-context diff detection via _has_diff_signal /
              _narrow_context from core.
    FIX [1]   structural fallback for .py bodies that fail ast.parse
              because they embed raw JS/CSS.
    FIX [2]   the same fallback inside _looks_like_full_file.
    FIX [3]   the same fallback inside _looks_truncated.
    FIX [9]   escalate-batch detail log gated by `verbose` and capped
              at 10 entries.

    NEW (2026-10-23)  Skip preexisting baselines (blocks with
              classified_by == "preexisting") during classification.

    NEW (2026-10-30)  Diff fences and diff-shaped bodies are
              classified as `partial` + is_code_file=True, so they
              survive pass 3 and reach the patcher.  Previously
              ` ```diff ` shared the "not_a_file" bucket with
              ` ```bash ` and was silently dropped before pass 5
              could attach it to its file as a patch — which is
              why every "here's the diff" reply produced zero
              applied changes.
"""

import json
import sys
from typing import Optional

from .core import (
    Block,
    MAX_TOKENS_CLASSIFY,
    BATCH_SIZE,
    DIFF_CONTEXT_PHRASES,
    REPRINT_PHRASES,
    RECOVERED_META_RE,
    _basename,
    _has_diff_signal,
    _looks_like_diff_body,
    _narrow_context,
)
from .llm import LLMClient


CLASSIFY_PROMPT = """You are analyzing a code-refactor conversation
transcript.  Below are N blocks.  Each has:
  idx:               <integer>
  header_path:       "<path from a File: header, or null>"
  header_modifier:   "<parenthetical modifier, e.g. 'new', 'rewritten'>"
  preceding_context: "<up to 200 chars before the block header>"
  fence_lang:        "<python|js|bash|...>"
  body_line_count:   <int>
  body_first_3:      "<first 3 lines, JSON-string-escaped>"
  body_last_3:       "<last 3 lines, JSON-string-escaped>"

For EACH block, decide:

  is_code_file      true iff the body is actual source code for a FILE,
                    not a bash snippet, not an inline illustration.

  kind              "full"      — a complete module
                    "partial"   — only a fragment
                    "not_a_file" — not source code

  canonical_path    basename of the file (e.g., "pg_state.py").

  confidence        float in [0,1].  Use < 0.7 when unsure.

Return ONLY a JSON array of objects with keys
  idx, is_code_file, kind, canonical_path, confidence
No prose, no fence.

BLOCKS:
"""


def pass2_classify(blocks: list[Block], llm: Optional[LLMClient],
                   mode: str, escalate_below: float,
                   verbose: bool) -> None:
    if not blocks:
        return

    for b in blocks:
        # NEW: preexisting baselines are already known-good full prints
        # — never reclassify them.
        if b.classified_by == "preexisting":
            b.heuristic_kind = b.kind
            continue
        _classify_one_heuristic(b)
        b.classified_by = "heuristic"
        b.heuristic_kind = b.kind

    if mode == "off":
        _apply_hard_overrides(blocks, verbose)
        return

    if mode == "smart":
        ambiguous = []
        for b in blocks:
            # NEW: never escalate a preexisting baseline to the LLM.
            if b.classified_by == "preexisting":
                continue
            reason = _escalation_reason(b, escalate_below)
            if reason is not None:
                b.escalation_reason = reason
                ambiguous.append(b)
    else:  # always
        ambiguous = [b for b in blocks
                     if b.classified_by != "preexisting"]
        for b in ambiguous:
            b.escalation_reason = "always"

    if ambiguous:
        print(f"  [pass 2] escalating {len(ambiguous)}/{len(blocks)} "
              f"block(s) to LLM:", file=sys.stderr)
        if verbose:
            for b in ambiguous[:10]:
                print(f"    idx={b.idx} path={b.canonical_path} "
                      f"lang={b.fence_lang} lines={b.body_line_count} "
                      f"reason={b.escalation_reason}", file=sys.stderr)
            if len(ambiguous) > 10:
                print(f"    … and {len(ambiguous) - 10} more",
                      file=sys.stderr)

    if ambiguous and llm is not None:
        _classify_batches_llm(ambiguous, llm)
    elif ambiguous:
        print("  [pass 2] LLM unavailable — using heuristic labels",
              file=sys.stderr)

    _apply_hard_overrides(blocks, verbose)


def _escalation_reason(b: Block, threshold: float) -> Optional[str]:
    if b.confidence is not None and b.confidence < threshold:
        return f"confidence {b.confidence:.2f} < {threshold}"
    ctx = b.context_before.lower()
    for phrase in DIFF_CONTEXT_PHRASES:
        if phrase in ctx:
            return None
    for phrase in REPRINT_PHRASES:
        if phrase in ctx:
            return None
    return None


def _apply_hard_overrides(blocks: list[Block],
                          verbose: bool = False) -> None:
    for b in blocks:
        # NEW: preexisting baselines are untouched by hard overrides.
        if b.classified_by == "preexisting":
            continue

        # ---- recovered-meta filter ----
        if (RECOVERED_META_RE.search(b.context_before) is not None
                or "SCANNED DIRECTORY" in b.context_before):
            if b.kind != "recovered_meta" and verbose:
                print(f"  [override] idx={b.idx}: recovered-meta "
                      f"listing -> skipped", file=sys.stderr)
            b.is_code_file = False
            b.kind = "recovered_meta"
            b.override_reason = "recovered-meta listing"
            continue

        ctx_narrow = _narrow_context(b.context_before)
        reason: Optional[str] = None
        if _has_diff_signal(ctx_narrow):
            reason = "diff-context anchor"
        else:
            for phrase in REPRINT_PHRASES:
                if phrase in ctx_narrow:
                    reason = f"reprint-context anchor ({phrase.strip()!r})"
                    break

        if reason is None:
            continue
        if b.kind != "full":
            continue

        if verbose:
            print(f"  [override] idx={b.idx} {b.canonical_path}: {reason} "
                  f"-> partial (was full, classified_by={b.classified_by})",
                  file=sys.stderr)
        b.kind = "partial"
        if b.confidence is None or b.confidence > 0.5:
            b.confidence = 0.5
        b.override_reason = reason


def _classify_one_heuristic(b: Block) -> None:
    """Deterministic classifier.  Confidences tuned so that a
    well-structured dump makes zero LLM escalations even under
    `--llm-mode smart`."""
    path = _basename(b.header_path) or f"unknown_{b.idx}"
    b.canonical_path = path
    lang = b.fence_lang.lower()

    # ---- shell / prose / inline snippets: definitely not code ----
    if lang in ("bash", "sh", "text"):
        b.is_code_file = False
        b.kind = "not_a_file"
        b.confidence = 0.90
        return

    # ---- NEW: `diff` fences are patches, not files, but they ARE
    # ---- code: they must survive to the patcher.
    if lang == "diff":
        b.is_code_file = True
        b.kind = "partial"
        b.confidence = 0.95
        return

    if lang in ("json", "yaml", "yml", "toml"):
        if not any(path.endswith(ext) for ext in
                   (".json", ".yaml", ".yml", ".toml")):
            b.is_code_file = False
            b.kind = "not_a_file"
            b.confidence = 0.90
            return

    ctx_narrow = _narrow_context(b.context_before)
    if _has_diff_signal(ctx_narrow):
        b.is_code_file = True
        b.kind = "partial"
        b.confidence = 0.90
        return

    # NEW: a code-fenced body (any of the source languages) whose
    # line prefixes are `+`/`-`/` ` throughout is a patch the LLM
    # emitted without labeling the fence `diff`.  Catch it here so
    # it lands in the patcher instead of being classified `full`
    # (which would make it a candidate base for its own file).
    if lang in ("js", "mjs", "jsx", "ts", "tsx", "py", "python",
                "rs", "go", "c", "cpp", "h", "hpp", "java",
                "rb", "php", "css", "html", ""):
        if _looks_like_diff_body(b.body):
            b.is_code_file = True
            b.kind = "partial"
            b.confidence = 0.85
            return

    if b.body_line_count < 20:
        b.is_code_file = True
        b.kind = "partial"
        b.confidence = 0.90
        return
    if b.body_line_count < 30:
        b.is_code_file = True
        b.kind = "partial"
        b.confidence = 0.80
        return

    if _looks_truncated(b.body, path):
        b.is_code_file = True
        b.kind = "partial"
        b.confidence = 0.70
        return

    if _looks_like_full_file(b.body, path):
        b.is_code_file = True
        b.kind = "full"
        b.confidence = 0.95
        return

    b.is_code_file = True
    b.kind = "full"
    b.confidence = 0.85


_PY_MODULE_TOPLEVELS = (
    '"""', "'''", "import ", "from ", "def ", "class ",
    "async def ", "@", "#!",
)


def _looks_like_python_module_top_level(body: str) -> bool:
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        return any(s.startswith(p) for p in _PY_MODULE_TOPLEVELS)
    return False


def _looks_like_full_file(body: str, path: str) -> bool:
    import ast
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    first = lines[0]
    last = lines[-1].strip()

    if first.startswith((" ", "\t")):
        return False
    if last.endswith((",", "\\", "(", "[", "=>")):
        return False

    if path.endswith(".py"):
        try:
            ast.parse(body)
            return True
        except SyntaxError:
            return _looks_like_python_module_top_level(body)

    return True


def _looks_truncated(body: str, path: str) -> bool:
    if path.endswith(".py"):
        import ast
        try:
            ast.parse(body)
            return False
        except SyntaxError:
            if _looks_like_python_module_top_level(body):
                return False
            return True
    opens = body.count("{") - body.count("}")
    parens = body.count("(") - body.count(")")
    return abs(opens) > 1 or abs(parens) > 1


def _classify_batches_llm(blocks: list[Block],
                          llm: Optional[LLMClient]) -> None:
    if llm is None:
        return
    for batch_start in range(0, len(blocks), BATCH_SIZE):
        batch = blocks[batch_start:batch_start + BATCH_SIZE]
        prompt = CLASSIFY_PROMPT
        for b in batch:
            body_lines = b.body.splitlines()
            first3 = "\n".join(body_lines[:3])
            last3 = "\n".join(body_lines[-3:]) if len(body_lines) > 3 else ""
            ctx = b.context_before.replace("\n", " ")[:200]
            prompt += (
                f"\n idx: {b.idx}"
                f"\n header_path: {json.dumps(b.header_path)}"
                f"\n header_modifier: {json.dumps(b.modifier)}"
                f"\n preceding_context: {json.dumps(ctx)}"
                f"\n fence_lang: {json.dumps(b.fence_lang)}"
                f"\n body_line_count: {b.body_line_count}"
                f"\n body_first_3: {json.dumps(first3)}"
                f"\n body_last_3: {json.dumps(last3)}"
                f"\n ---"
            )

        resp = llm.call(prompt, max_tokens=MAX_TOKENS_CLASSIFY,
                        tag="classify")
        parsed = _parse_json_array(resp or "")
        if not parsed:
            retry = (
                prompt
                + "\n\nIf the previous attempt failed to produce a valid "
                  "JSON array, here's the format again:\n"
                  '[{"idx": 0, "is_code_file": true, "kind": "full", '
                  '"canonical_path": "foo.py", "confidence": 0.9}]'
            )
            resp2 = llm.call(retry, max_tokens=MAX_TOKENS_CLASSIFY,
                             tag="classify")
            parsed = _parse_json_array(resp2 or "")
        if not parsed:
            print("  [LLM] classify batch failed; heuristic labels kept",
                  file=sys.stderr)
            continue

        by_idx = {int(item["idx"]): item for item in parsed
                  if isinstance(item, dict) and "idx" in item}
        for b in batch:
            item = by_idx.get(b.idx)
            if not item:
                continue

            kind = item.get("kind") or "not_a_file"
            is_code = bool(item.get("is_code_file"))
            canon_path = item.get("canonical_path")
            llm_confidence = float(item.get("confidence", 0.5))

            if kind == "full" and b.body_line_count < 30:
                print(f"  [LLM] demoting 'full' block {b.idx} "
                      f"({b.body_line_count} lines)", file=sys.stderr)
                kind = "partial"
                llm_confidence = min(llm_confidence, 0.5)
                b.llm_retries += 1

            b.llm_kind = kind
            b.is_code_file = is_code
            b.kind = kind
            b.canonical_path = canon_path or b.canonical_path
            b.confidence = llm_confidence
            b.classified_by = "llm"


def _parse_json_array(s: str) -> Optional[list]:
    s = s.strip()
    if not s:
        return None
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
        return data if isinstance(data, list) else None
    except Exception:
        return None
