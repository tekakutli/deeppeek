"""
recover.core — constants, phrase lists, and the two dataclasses.

STABLE.  Nothing here changes when pass logic changes.  Every pass
imports from this module.
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


_DELETE_SIGNAL_PHRASES = (
    "delete",
    "remove",
    "get rid of",
    "no longer",
    "revert",
    "drop",
    "strip out",
    "strip ",
    "eliminate",
    "take out",
    "wipe",
)


def _has_deletion_signal(prose: str) -> bool:
    """True iff the last three lines of `prose` contain a deletion-
    signal phrase."""
    narrow = "\n".join(prose.splitlines()[-3:]).lower()
    return any(sig in narrow for sig in _DELETE_SIGNAL_PHRASES)


def _looks_like_diff_body(body: str) -> bool:
    """True iff `body` is a raw diff block."""
    lines = body.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        return False

    checked = 0
    for ln in lines:
        if not ln.strip():
            continue
        if ln.startswith(("---", "+++", "@@")):
            return True
        checked += 1
        if checked >= 5:
            break

    n_add = 0
    n_rem = 0
    n_ctx = 0
    for ln in lines:
        if not ln:
            continue
        c = ln[0]
        if c == "+" and not ln.startswith("+++"):
            n_add += 1
        elif c == "-" and not ln.startswith("---"):
            n_rem += 1
        elif c == " ":
            n_ctx += 1
        elif c == "\\":
            continue
        else:
            return False

    if n_add == 0 and n_rem == 0:
        return False
    if n_add > 0 and n_rem > 0:
        return True
    if n_ctx > 0:
        return True
    return (n_rem >= 3) or (n_add >= 3)


def _is_pure_deletion_diff_strict(body: str) -> bool:
    """True iff `body` is diff-shaped AND has NO `+` lines AND no
    space-prefixed context lines.
    """
    for ln in body.splitlines():
        if not ln:
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            return False
        if ln.startswith(" "):
            return False
    return any(
        ln.startswith("-") and not ln.startswith("---")
        for ln in body.splitlines()
    )


def _strip_diff_prefixes(body: str) -> str:
    """Return `body` with diff-prefix characters stripped from each line."""
    out = []
    for ln in body.splitlines():
        if not ln:
            out.append(ln)
            continue
        if ln.startswith(("+++", "---", "@@")):
            continue
        if ln[0] in ("+", "-", " "):
            out.append(ln[1:])
        else:
            out.append(ln)
    return "\n".join(out)


def _is_deletion_instruction(body: str, context_before: str) -> bool:
    """True iff `body` should be handled as a "delete this block"
    instruction rather than a diff patch or object-literal
    redeclaration.
    """
    if _is_pure_deletion_diff_strict(body):
        return True
    if _looks_like_diff_body(body):
        return False
    return _has_deletion_signal(context_before)


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
    output_path: Optional[str] = None
    confidence: Optional[float] = None
    classified_by: str = "?"
    escalation_reason: Optional[str] = None
    heuristic_kind: Optional[str] = None
    llm_kind: Optional[str] = None
    override_reason: Optional[str] = None
    llm_retries: int = 0
    # NEW (2026-11-01): intent tracking for replace pairs.
    intent: str = "unknown"
    scope_hint: str = ""
    scope_name: str = ""
    paired_with: Optional[int] = None
    # Set by classify/attribute stages as an early disposition
    # hint ("orphan_prose", "orphan_not_code", ...).  Read by
    # write.py's _compute_block_disposition; falls back to a
    # derivation from the block's final state when unset.
    disposition: Optional[str] = None
    # True iff this fence appeared inside a markdown blockquote.
    # deeppeek's own formatter (formatter.py) wraps AI thought
    # content in blockquotes, so a blockquoted fence is (per that
    # contract) part of the AI's reasoning, not its answer.  Such
    # blocks are still recoverable when prose attribution names
    # the target file -- the AI frequently commits code it
    # discussed in thinking -- but unattributed blockquoted blocks
    # are skipped silently instead of reported as orphan noise.
    in_blockquote: bool = False


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
    # NEW (2026-11-01): pair tracking.
    pairs: list = field(default_factory=list)
    sanity_rejected: bool = False
    # NEW (2026-11-02): structural-brace-repair suggestion, if any.
    # Populated by `validate.pass8_verify` when a sanity-rejected
    # record's failure is exactly `unbalanced braces (+N)` and the
    # block shape meets `repair.try_repair`'s high-confidence
    # criteria.  Contains the full file content (base + repaired
    # block inserted) ready to `cp` over the target.  Never written
    # to the target by `pass9_write` — a `.repaired` suggestion
    # lands in `low_trust/` and the manifest records
    # `verification.sanity_repair_available: true`.
    repair_content: Optional[str] = None
