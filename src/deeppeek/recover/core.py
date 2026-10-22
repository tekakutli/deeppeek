"""
recover.core — constants, phrase lists, and the two dataclasses.

STABLE.  Nothing here changes when pass logic changes.  Every pass
imports from this module.

Sections:

    FENCE, DEL_MARKER_RE, DEFAULT_*        configuration constants
    phrase lists                           diff/reprint/fix heuristics
    _has_diff_signal, _narrow_context      narrow-window diff test
    RECOVERED_META_RE                      signature of recover's own
                                           output in a later message
    Block, FileRecord                      the two dataclasses
    _del_marker_names, _basename           small shared helpers
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional


FENCE = chr(96) * 3


DEL_MARKER_RE = re.compile(
    r'(?P<bracket>[\w./\-]+\.\w+)\s+\[(?:DEL|DELETED|REMOVED)\]'
    r'|'
    r'(?:^|\s)(?:sudo\s+)?rm\s+(?:-\w+\s+)*(?P<rm>[\w./\-]+\.\w+)'
)


DEFAULT_LLM_URL   = "http://127.0.0.1:6006/v1/chat/completions"
DEFAULT_LLM_MODEL = "qwen3.5-35b-heretic-mtp"

MAX_TOKENS_CLASSIFY   = 700
MAX_TOKENS_DIFF       = 512
BATCH_SIZE            = 8
DEFAULT_ESCALATE      = 0.85
DEFAULT_LLM_TIMEOUT   = 60
DEFAULT_PROBE_TIMEOUT = 10


DIFF_CONTEXT_PHRASES = (
    "change inside",
    "add this",
    "insert ",
    "replace the",
    "diff instruction",
    "add the following",
    "replace with",
    "update the",
    "the fix",
    "only show the changed",
    "only the changed",
)

_CONTEXT_WINDOW = 2

_STRONG_DIFF_PHRASES = (
    "change inside",
    "diff instruction",
    "only the changed",
    "only show the changed",
    "add the following",
    "replace with",
)

_WEAK_DIFF_PHRASES = ("the fix", "insert ", "replace the", "update the")


def _has_diff_signal(ctx: str) -> bool:
    for phrase in _STRONG_DIFF_PHRASES:
        if phrase in ctx:
            return True
    if any(w in ctx for w in ("inside", "below", "in the", "at the")):
        for phrase in _WEAK_DIFF_PHRASES:
            if phrase in ctx:
                return True
    return False


def _narrow_context(context_before: str) -> str:
    return "\n".join(
        context_before.splitlines()[-_CONTEXT_WINDOW:]
    ).lower()


FIX_PROSE_PHRASES = (
    "fix", "correct", "bug", "instead", "was wrong",
    "didn't work", "no longer", "had a problem", "should be",
    "the problem", "the issue",
)

REPRINT_PHRASES = (
    "reprint in place",
    "in-place reprint",
    "in place reprint",
    "unchanged except",
    "otherwise unchanged",
    "rest of the file is unchanged",
    "rest of the file unchanged",
    "every other line is unchanged",
    "identical to the version",
)


RECOVERED_META_RE = re.compile(r"FILE\s+\d+/\d+:\s*\S+")


MODIFIER_RANK = {
    "corrected": 100, "fixed": 100, "final": 100,
    "with fix": 100, "with fix applied": 100,
    "bug fix": 100, "bugfix": 100,
    "revised": 50, "updated": 50, "refactored": 30,
    "reprinted": 10, "recovered": 10, "new": 10,
    "rewritten": 10,
}

SKIP_MISSING_FILES = {
    "setup.py", "requirements.txt", "package.json", "package-lock.json",
    "__init__.py", "conftest.py", "tox.ini", "pyproject.toml",
    "Makefile", "README.md", "LICENSE", ".gitignore",
}

SOURCE_EXTENSIONS = (
    ".py", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".md", ".json",
    ".yaml", ".yml", ".css", ".toml", ".rs", ".go", ".c", ".cpp",
    ".h", ".hpp", ".java", ".rb", ".php", ".html", ".sh",
)


def _del_marker_names(text: str) -> set[str]:
    names: set[str] = set()
    for m in DEL_MARKER_RE.finditer(text):
        name = m.group("bracket") or m.group("rm")
        if name:
            names.add(name)
    return names


def _basename(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    return os.path.basename(p.strip().strip(chr(96)))


@dataclass
class Block:
    idx: int
    header_path: Optional[str]
    modifier: Optional[str]
    fence_lang: str
    body: str
    body_line_count: int
    context_before: str
    line_start: int
    line_end: int
    is_orphan: bool = False
    attributed_to: Optional[str] = None
    attributed_by: Optional[str] = None
    is_code_file: Optional[bool] = None
    kind: Optional[str] = None
    canonical_path: Optional[str] = None
    # NEW: the resolved path this block's content belongs to.  For
    # pre-existing baselines this equals canonical_path (already a
    # full relative path).  For conversation blocks it is set by
    # `resolve_paths()` — the real path in the target tree, resolved
    # from header_path + content + context.  For orphans it stays
    # None and is populated later by pass 4's attribution.  For
    # standard (non --apply-to) recovery it equals canonical_path
    # (basename).
    output_path: Optional[str] = None
    confidence: Optional[float] = None
    classified_by: str = "?"
    escalation_reason: Optional[str] = None
    heuristic_kind: Optional[str] = None
    llm_kind: Optional[str] = None
    override_reason: Optional[str] = None
    llm_retries: int = 0


@dataclass
class FileRecord:
    path: str
    state: str
    source_blocks: list = field(default_factory=list)
    last_full_idx: Optional[int] = None
    patches: list = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    trust: str = "SKIP"
    content: Optional[str] = None
    verification: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    all_full_idxs: list = field(default_factory=list)
    chosen_full_idx: Optional[int] = None
    dedup_reason: Optional[str] = None
    import_problems: list = field(default_factory=list)
    preexisting_base: bool = False
