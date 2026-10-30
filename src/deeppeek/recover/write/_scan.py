"""Cross-file scanning helpers for pass 9.

MEDIUM CHURN.  These functions walk `blocks` or `records` to answer
"what source files does the conversation reference, and which of them
did we fail to recover?".  The MISSING-FILES scope has been narrowed
twice (2026-11-01b, 2026-11-05); further narrowing is plausible as
more data files and generated artefacts turn out to be noise.  When
that happens, this file grows, not `pass9.py`.

PROSE-ONLY MENTION SCAN (2026-11-05)
====================================
`_scan_mentioned_files` reads **prose** (`Block.context_before`) only.
A filename in a code body is an import reference, not a human intent
signal — `_aggregate_missing_imports` already covers imports.  The
2026-11-05 conversation that embedded a `project_dump.md` inside a
recovered_meta block produced 21 spurious "missing" entries before
this filter; after, zero.

MISSING-FILES SCOPE (2026-11-01b)
=================================
`_scan_mentioned_files` matches only source extensions
(`_MENTION_EXTENSIONS`), not the full `SOURCE_EXTENSIONS` set.
`.json`, `.yaml`, `.toml`, `.md`, `.sh` are excluded — they are
config, docs, or invoked scripts, not import targets.
"""

import os
import re
from pathlib import Path

from ..core import FileRecord, _basename
from ..validate import _py_imports, _is_likely_local


# Extensions whose mention in prose is a legitimate "this file was
# supposed to be recovered" signal.  Data files, docs, and shell
# scripts are excluded — see module docstring.
_MENTION_EXTENSIONS = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".rs", ".go", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".java", ".rb", ".php",
})


def _scan_mentioned_files(blocks) -> set:
    """Return basenames of source files named in **prose**, not code.

    Filenames in a code body are imports; `_aggregate_missing_imports`
    already accounts for those.  Filenames in *prose* are the human
    intent: "this conversation is about `foo.py` and `bar.py`".

    The previous version accepted the raw dump text and matched
    filenames inside every recovered source file's body — every
    `from .foo import X` in the recovered content produced a
    spurious "missing" entry.
    """
    mentioned: set = set()
    for b in blocks:
        if b.classified_by == "preexisting":
            continue
        if b.kind == "recovered_meta":
            continue
        prose = b.context_before or ""
        for m in re.finditer(r'[A-Za-z0-9_./\-]+', prose):
            tok = m.group(0)
            if not any(tok.endswith(ext)
                       for ext in _MENTION_EXTENSIONS):
                continue
            base = os.path.basename(tok)
            if base and len(base) > 3:
                mentioned.add(base)
    return mentioned


def _aggregate_missing_imports(records: dict) -> set:
    """Return basenames of local modules that are imported by a
    recovered file but not themselves recovered.

    Dotted paths (``from pg_comfy.image import X``) are resolved
    against the **top-level** module first, because that is the
    level at which deeppeek can decide "recovered / not recovered":

      · ``pg_comfy.py`` is recovered  → ``pg_comfy.image`` resolves
        (whether the sub is a real file, an ``__init__.py`` package,
        or an attribute) — do not report a missing file.
      · ``pg_comfy/__init__.py`` is recovered  → the package exists;
        check for the submodule file specifically.
      · Neither is recovered  → report ``pg_comfy.py`` (once), not
        ``image.py``.
    """
    recovered_files = {
        _basename(p) for p, r in records.items()
        if r.content is not None
    }
    recovered_paths = {
        p.replace("\\", "/").lstrip("./")
        for p, r in records.items()
        if r.content is not None
    }

    expected: set = set()
    for path, rec in records.items():
        if not path.endswith(".py"):
            continue
        if rec.content is None:
            continue
        for module, _name in _py_imports(rec.content):
            if not _is_likely_local(module, records):
                continue
            clean = module.lstrip(".")
            if not clean:
                continue
            parts = clean.split(".")

            top_name = parts[0]
            top_file = top_name + ".py"
            top_pkg = top_name + "/__init__.py"

            if top_file in recovered_paths:
                continue
            if top_pkg in recovered_paths:
                if len(parts) > 1:
                    sub_file = "/".join(parts) + ".py"
                    if sub_file in recovered_paths:
                        continue
                    expected.add(os.path.basename(sub_file))
                continue
            expected.add(top_file)

    return expected - recovered_files


def _recovered_name_prefixes(records: dict) -> set:
    """Prefixes (up to first `_`) of every recovered file's stem.

    Used to filter `_scan_mentioned_files`'s output to files that
    plausibly belong to the same project (`pg_*.py` for the playground,
    `comfy_*.py` for the comfy handler).  A mention of an unrelated
    file with no shared prefix is not evidence of a missed recovery.
    """
    prefixes: set = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        base = os.path.basename(path)
        if not base or "." not in base:
            continue
        stem = base.rsplit(".", 1)[0]
        if "_" not in stem:
            continue
        prefix = stem.split("_", 1)[0] + "_"
        if len(prefix) > 2:
            prefixes.add(prefix)
    return prefixes


def _recovered_extensions(records: dict) -> set:
    """Extensions of every recovered file (for mention filtering)."""
    exts: set = set()
    for path, rec in records.items():
        if rec.content is None:
            continue
        ext = Path(path).suffix.lower()
        if ext:
            exts.add(ext)
    return exts
