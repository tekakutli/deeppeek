"""recover.equivalence — Pass 8.10 (structural-equivalence check).

UNSTABLE.  This pass is new (2026-11-05) and its scope is likely to
grow: new divergence kinds, new language families, and new heuristics
for distinguishing "patch intends this change" from "tool introduced
it accidentally" all belong here.

See `pipeline.py`'s module docstring, STRUCTURAL-EQUIVALENCE PASS,
for the rationale.  See `structural_diff.py` for the underlying
signature comparison.

FAST-PATH SKIP (2026-11-05)
===========================
The pass costs one `structural_diff` per eligible record.  Two
fast-paths skip it when it provably cannot find anything:

  1. No patch or pair applied.  Records whose `applied_by` statuses
     are all no-op / failed cannot have diverged from `base` (pass 6
     did not mutate them beyond what already-present detection would
     accept).

  2. No applied body could introduce a declaration or element.
     `_STRUCTURAL_HINT_RE` matches `{`, `}`, `<`-open-tags, top-level
     `def` / `function` / `class` / `const` / `let` / `var`, and
     top-level assignments (`x = …` and `x: T = …`).  A patch body
     with none of those cannot change the structural signature set:
     it contains no declarations, no HTML elements, no dict literals
     (which are what change `return` / `assign` statement signatures),
     and no top-level assignments.  The regex is deliberately
     permissive — if in doubt it runs the check.

Both fast-paths preserve the pass's semantics — they never suppress
a real finding.  They are performance filters only.
"""

import re

from .core import FileRecord


_STRUCTURAL_HINT_RE = re.compile(
    r'\{|\}|<[A-Za-z]'
    r'|(?:^|\n)\s*(?:export\s+)?(?:async\s+)?'
    r'(?:def|function|class|const|let|var)\b'
    r'|(?:^|\n)\s*[A-Za-z_$][\w$]*\s*(?::[^=]*)?\s*=[^=]',
    re.MULTILINE,
)


_APPLIED_STATUSES = frozenset({"deterministic", "llm", "region-replace"})


def check_structural_equivalence(records, blocks_by_idx):
    """Return {path: [SignatureChange, ...]} for every record with
    structural divergences introduced by its patches.

    See module docstring for the fast-path skip logic.
    """
    from .structural_diff import structural_diff

    out: dict = {}
    for path, rec in records.items():
        if rec.content is None or rec.is_deleted:
            continue
        if rec.state not in ("SINGLE_FULL", "FULL_PLUS_PATCH",
                             "MULTI_FULL"):
            continue
        if rec.trust == "LOW":
            continue
        if rec.verification.get("preexisting_unchanged"):
            continue
        if blocks_by_idx is None or rec.last_full_idx is None:
            continue
        base_block = blocks_by_idx.get(rec.last_full_idx)
        if base_block is None:
            continue

        # Fast-path skip 1: no applied patch or pair.
        applied_patches = [
            p for p in rec.patches
            if p.get("applied_by") in _APPLIED_STATUSES
        ]
        applied_pair_bodies = []
        for r in rec.verification.get("pair_records", []):
            if r.get("applied_by") in _APPLIED_STATUSES:
                nb = (r.get("pair") or {}).get("new_body")
                if nb:
                    applied_pair_bodies.append(nb)

        if not applied_patches and not applied_pair_bodies:
            continue

        # Fast-path skip 2: no applied body could introduce a
        # declaration, element, or top-level assignment.
        if (not any(_STRUCTURAL_HINT_RE.search(p.get("body", ""))
                    for p in applied_patches)
                and not any(_STRUCTURAL_HINT_RE.search(b)
                            for b in applied_pair_bodies)):
            continue

        # Compute body_text from EVERY patch body (not just applied)
        # to preserve the pre-fast-path semantics: the "declared by
        # the patch" set is wider than the "applied" set, and a
        # declared-but-not-applied body still covers signatures that
        # appear in `result` from other sources.
        body_parts = [p.get("body", "") for p in rec.patches]
        for r in rec.verification.get("pair_records", []):
            nb = (r.get("pair") or {}).get("new_body")
            if nb:
                body_parts.append(nb)
        body_text = "\n".join(body_parts)

        try:
            changes = structural_diff(
                base_block.body, rec.content, path, body_text)
        except Exception as e:
            rec.warnings.append(
                f"structural-equivalence check raised: {e}")
            continue
        if not changes:
            continue

        rec.trust = "LOW"
        rec.verification["structural_divergence"] = [
            {"kind": c.kind, "lang": c.lang,
             "signature": c.signature, "detail": c.detail}
            for c in changes
        ]
        for c in changes:
            rec.warnings.append(
                f"structural: {c.kind} {c.signature!r} ({c.detail})")
        out[path] = changes
    return out