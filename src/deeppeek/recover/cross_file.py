"""Cross-file duplicate detection.

Finds newly-added top-level functions or classes that appear
verbatim in multiple records.  Informational only -- the write pass
records findings in the manifest and warnings.txt, but does not
fail-closed.

Motivation: the most common authoring error when patching multiple
files is "add helper X" that mistakenly lands in file A AND file B
(or the same helper is added twice because two blocks both looked
like the right place).  The result compiles, but bloats the
codebase, drifts out of sync, and defeats refactoring.

Not fail-closed: legitimate cross-file duplication exists (small
utility shims, intentional forks for micro-service separation,
per-platform variants).  The MIN_LINES threshold is set high enough
that only substantive bodies are compared, and the report names
every affected file so a human can decide quickly.

DEPENDENCY HYGIENE (2026-10-30)
===============================
The pre-fix version imported `_norm_lines` and `_find_matching_close_char`
from `validate_output` at module level.  That made `cross_file` (a
small, leaf-ish duplicate detector) depend on the whole
structural-validation stack, and — critically — turned any
**syntax error** in `validate_output.py` into a `pipeline.run()`
crash.  The import lived inside `run()`'s `from .cross_file import
find_cross_file_duplicates` line, which was not wrapped in any
try/except, so one bad docstring in a downstream module cascaded
into "every fixture fails" for 40 tests.

Both helpers have safer homes:

  • `_norm_lines` is three lines of `str.splitlines` +
    `strip` + `join`.  It belongs locally.

  • `_find_matching_close_char` (the char-based brace matcher) is
    semantically identical to `patch_text._find_matching_brace`,
    which `cross_file` can import without dragging in
    `validate_output` at all.  `patch_text` is a genuinely
    dependency-free leaf module; `validate_output` is not.

The result: `cross_file` imports only `hashlib`, `re`, and
`patch_text`.  `pipeline.run()`'s late import of `cross_file`
becomes safe, and a future structural-validation bug can no longer
crash a run before `pass9_write`.
"""
from __future__ import annotations

import hashlib
import re

from .patch_text import _find_matching_brace


MIN_LINES_FOR_CROSS_FILE = 10


_FUNC_DECL_RE = re.compile(
    r"^((?:export\s+)?(?:async\s+)?function\s+"
    r"([A-Za-z_$][\w$]*)\s*\()",
    re.MULTILINE)
_CLASS_DECL_RE = re.compile(
    r"^((?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b)",
    re.MULTILINE)


def _norm_lines(s: str) -> str:
    """Strip each non-empty line and rejoin, dropping blank lines.

    Local definition (rather than importing from `validate_output`) so
    that `cross_file` does not depend on the structural-validation
    stack.  See module docstring, DEPENDENCY HYGIENE.
    """
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def _extract_top_level_signatures(content):
    if not content:
        return
    for pat, kind in (
            (_FUNC_DECL_RE, "function"),
            (_CLASS_DECL_RE, "class")):
        for m in pat.finditer(content):
            if (content.rfind(chr(10), 0, m.start()) + 1
                    != m.start()):
                continue
            name = m.group(2)
            header_end = content.find(chr(10), m.end())
            if header_end == -1:
                header_end = len(content)
            open_brace = content.find("{", m.end(), header_end)
            if open_brace == -1:
                continue
            close_brace = _find_matching_brace(content, open_brace)
            if close_brace == -1:
                continue
            body = _norm_lines(content[m.start():close_brace + 1])
            if not body:
                continue
            line_count = body.count(chr(10)) + 1
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            yield kind, name, body, sha, line_count


def find_cross_file_duplicates(records, blocks_by_idx):
    """Return a list of cross-file duplicate descriptors.  Empty on
    clean runs.  Never raises."""
    if not records:
        return []

    by_sha = {}
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.state in ("PARTIAL_ONLY", "MISSING"):
            continue
        if rec.is_deleted:
            continue

        base_sigs = set()
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block and base_block.body:
                for (_k, _n, _b, sha, _c) in \
                        _extract_top_level_signatures(base_block.body):
                    base_sigs.add(sha)

        for kind, name, body, sha, line_count in \
                _extract_top_level_signatures(rec.content):
            if sha in base_sigs:
                continue
            if line_count < MIN_LINES_FOR_CROSS_FILE:
                continue
            by_sha.setdefault(sha, []).append({
                "path": path,
                "name": name,
                "kind": kind,
                "line_count": line_count,
            })

    duplicates = []
    for sha, entries in by_sha.items():
        files = {e["path"] for e in entries}
        if len(files) < 2:
            continue
        rep = entries[0]
        duplicates.append({
            "name": rep["name"],
            "kind": rep["kind"],
            "files": sorted(files),
            "line_count": rep["line_count"],
            "body_sha256": sha[:16],
        })

    duplicates.sort(key=lambda d: (
        -d["line_count"], d["files"], d["name"]))
    return duplicates
