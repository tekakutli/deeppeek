"""
recover.classify — Pass 2 (block classification) plus intent parsing.

Every block gets:
  · kind       (full / partial / not_a_file / recovered_meta)
  · confidence
  · intent     (from prose.parse_intent)
  · scope_hint / scope_name  (only for scoped-replace blocks)

New in 2026-11-01: the intent tracking.  Instead of the patcher
re-deriving "is this a delete / a replace / an insert" from prose on
every pass, we parse it once here and record it on the Block.
`pass5_deduplicate` reads `intent` and `paired_with`; `pass6` reads
the same.  Adding a new prose pattern is one entry in `prose.py`.
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
from .prose_filter import looks_like_prose_ui
from .prose import parse_intent


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
        if b.classified_by == "preexisting":
            b.heuristic_kind = b.kind
            b.intent = "full"
            continue
        _classify_one_heuristic(b)

        # Pre-filter: prose/UI blocks never recover, and their
        # presence wastes LLM escalation budget.  Detecting them
        # here is cheaper than classifying-then-failing-attribution.
        if b.is_orphan and looks_like_prose_ui(b.body):
            b.is_code_file = False
            b.kind = "not_a_file"
            b.confidence = 0.95
            b.disposition = "orphan_prose"
            b.classified_by = "heuristic"
            b.heuristic_kind = "not_a_file"
            b.intent = "full"
            continue
        b.classified_by = "heuristic"
        b.heuristic_kind = b.kind

        # NEW: parse prose intent now, once per block.  Attaches
        # b.intent, b.scope_hint, b.scope_name.  Downstream passes
        # read those instead of re-deriving them.
        intent = parse_intent(b.context_before)
        b.intent = intent.action
        b.scope_hint = intent.scope_hint
        b.scope_name = intent.scope_name

    if mode == "off":
        _apply_hard_overrides(blocks, verbose)
        return

    if mode == "smart":
        ambiguous = []
        for b in blocks:
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
        if b.classified_by == "preexisting":
            continue

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
    path = _basename(b.header_path) or f"unknown_{b.idx}"
    b.canonical_path = path
    lang = b.fence_lang.lower()

    if lang in ("bash", "sh", "text"):
        b.is_code_file = False
        b.kind = "not_a_file"
        b.confidence = 0.90
        return

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

    # Final catch-all.  A `full` print ALWAYS starts at column 0;
    # an indented first line means this is a fragment (a method body,
    # a class-inner fragment, etc.), not a module.  Without this
    # check, an indented 100-line method body lands here as `full`
    # and the sanity gate later rejects it with "SyntaxError at
    # line 1: unexpected indent" — which reads as "bad content"
    # rather than "misrouted fragment".  Classifying it as `partial`
    # routes it through the insert paths (`_try_insert_into_named_
    # scope` when a target class/function exists, `PARTIAL_ONLY`
    # otherwise) so the failure diagnostic names the actual
    # structural problem.
    b.is_code_file = True
    first_line = next((ln for ln in b.body.splitlines() if ln.strip()), "")
    if first_line and first_line[0] in (" ", "\t"):
        b.kind = "partial"
    else:
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
