"""recover.write — Pass 9 (write files, manifest, warnings).

The 2026-11-05 split collapsed the original ~1400-line `write.py` into
a small package organised by churn profile.  Each sub-module owns one
concern; the `__init__.py` re-exports the full pre-split surface so
every import that used to say `from deeppeek.recover.write import X`
keeps resolving.

The historical documentation that used to sit in this module's
docstring — every "New in 2026-XX" annotation for the write pass — is
preserved in `pass9.py`'s module docstring, which is where the main
loop lives.

STABILITY MAP
=============

  _text.py             very stable   string truncation + warning agg
  _scan.py             medium        mention scan, import aggregation
  _delete.py           stable        filesystem deletion
  _disposition.py      medium        block-outcome classification
  _manifest.py         churny        manifest dict shape
  _summary.py          churny        console output
  pass9.py             very churny   main loop + file writing

Adding a new "New in 2026-XX" feature to pass 9 usually touches
`pass9.py` and one of `_manifest.py` / `_summary.py`.  `_text.py`,
`_delete.py`, and `_disposition.py` should stay small and unchanged.
"""

from .pass9 import pass9_write
from ._text import (
    _orphan_first_line,
    _patch_first_line,
    _patch_context,
    _trust_reason,
    _warn_class_key,
    _aggregate_warnings,
)
from ._scan import (
    _scan_mentioned_files,
    _aggregate_missing_imports,
    _recovered_name_prefixes,
    _recovered_extensions,
)
from ._delete import _apply_deletions
from ._disposition import _compute_block_disposition
from ._manifest import build_manifest
from ._summary import print_summary


__all__ = [
    "pass9_write",
    "_orphan_first_line",
    "_patch_first_line",
    "_patch_context",
    "_trust_reason",
    "_warn_class_key",
    "_aggregate_warnings",
    "_scan_mentioned_files",
    "_aggregate_missing_imports",
    "_recovered_name_prefixes",
    "_recovered_extensions",
    "_apply_deletions",
    "_compute_block_disposition",
    "build_manifest",
    "print_summary",
]
