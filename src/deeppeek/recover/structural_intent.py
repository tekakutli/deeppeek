"""
recover.structural_intent — Pass 8.11: verify that every patch's
declared structural intent (dict keys, decl names, params,
decorator kwargs) is present in the final content.

Warn-only by default (mirrors pass 8.7c). A missing decl is
informational; the file has already been written. This pass exists
to catch the *inverse* of pass 8.7d's bug: a patch marked
`deterministic` whose declared `--output_rotation` key is not
present in the final content. That is a genuine gap; the tool
should say so.
"""
from __future__ import annotations
import sys
from .core import FileRecord
from .ast_intent import extract_intent, describe_missing


def pass8_11_verify_structural_intent(records, blocks_by_idx=None):
    n_warned = 0
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if rec.verification.get("preexisting_unchanged"):
            continue
        for patch in rec.patches:
            status = patch.get("applied_by")
            if status not in ("deterministic", "llm",
                              "already-present"):
                continue
            body = patch.get("body") or ""
            if not body.strip():
                continue
            intent = extract_intent(body, path)
            if intent.is_empty():
                continue
            detail = describe_missing(intent, rec.content, path)
            if detail is None:
                continue
            idx = patch.get("idx")
            rec.warnings.append(
                f"structural-intent: patch idx={idx} was recorded "
                f"as {status!r} but {detail} — the patch's declared "
                f"contribution is absent from the final content"
            )
            rec.verification.setdefault(
                "structural_intent_gaps", []).append({
                    "idx": idx,
                    "status": status,
                    "detail": detail,
                })
            n_warned += 1
            print(f"  [8.11] {path}: patch idx={idx} ({status!r}) "
                  f"missing {detail}", file=sys.stderr)
    return n_warned
