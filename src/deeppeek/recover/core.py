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


# ===========================================================================
#  Patch status taxonomy — the single source of truth
# ===========================================================================
#
# Every pass that reasons about "did this patch apply?" MUST classify
# statuses through one of these three sets. Before 2026-11-01, four
# passes each carried their own ad-hoc filter, which disagreed at the
# margins — most visibly `write.preexisting_unmodified` treating
# `already-present` as a no-op (skip the file) while
# `apply_patches.patches_applied` counted it as a success (report the
# file as patched). That mismatch is why comfyui.py's real patch was
# silently dropped in the 2026-10-28 run and the manifest nonetheless
# reported "1 patch applied".

APPLIED_STATUSES = frozenset({
    # The patcher's deterministic or LLM heuristics produced a change.
    "deterministic",
    "llm",
    # Region-replace (2026-10-29): the body replaced an existing
    # region because it was a structurally-modified version of it.
    # Treated as applied: `validate_content` gates the write, and
    # `patch_presence` deliberately skips the per-line check for
    # structural transforms (see the note there).
    "region-replace",
})

NO_CHANGE_STATUSES = frozenset({
    # The patcher declined to change the file. Either the content was
    # already there (`already-present`), or a gate excluded it
    # (misattributed, unplaceable, weak attribution, duplicate
    # insertion, structural damage). The file may still be rewritten
    # from a stored `rec.content`, but no *new* patch bytes are added.
    "already-present",
    "skipped_misattributed",
    "skipped_unplaceable",
    "skipped_weak_attribution",
    "skipped_duplicate_insertion",
    "skipped_structural_damage",
})

FAILED_STATUSES = frozenset({
    # The patcher attempted application and could not place the body.
    # Under fail-closed this forces the record to LOW trust.
    "failed",
})

ALL_TERMINAL_STATUSES = (
    APPLIED_STATUSES | NO_CHANGE_STATUSES | FAILED_STATUSES
)


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


# ===========================================================================
#  Deletion-instruction prose signals  (2026-11-01 narrowing)
# ===========================================================================
#
# A "deletion instruction" is prose that reads as an instruction to
# *remove* the block that follows: "Delete the panelState block:",
# "Remove the following:", "Get rid of …:", etc. In those cases the
# block's body is content-to-delete; the patcher must attempt a
# match-and-remove against the base, not treat the block as a patch to
# be applied.
#
# The pre-2026-11-01 implementation used bare substring matching:
#     any(sig in narrow for sig in ("delete", "remove", ...))
# which fires on any prose that happens to contain the word.
#
# Motivating false positive (comfyui.py, 2026-10-28 run):
#     prose: "Delete-and-recreate the layer instead of updating in place"
#     body:  a full `def comfy_set_layer_pixels(self, params):` body
#
# The prose uses "delete" as a compound-verb ("delete-and-recreate"),
# not as an instruction to delete the block. `_is_deletion_instruction`
# fired, sending the patch through `_delete_exact_block`, which found
# no unique match of the body in base, returned None, and left the
# dispatcher to fall through to the "already-present" branch (via
# `_patch_already_present`'s 85% token-equivalence fallback). The
# manifest claimed success; the file was silently skipped.
#
# Narrowed rule: the deletion verb must be
#   (a) at the start of a line followed by whitespace or a colon, OR
#   (b) followed by a clear deictic ("this", "these", "those"), OR
#   (c) followed by "the" + a deictic-ish noun — "following", "above",
#       "below", "block", "code", "line", "lines", "section",
#       "function", "method", "class", "declaration", "definition", OR
#   (d) part of one of a small set of inherently-deletiony multi-word
#       phrases ("get rid of", "take out", "strip out", "no longer").
#
# A compound verb like "delete-and-recreate" passes none of these:
# `\bdelete\b(?=[\s:])` fails (`-` follows, not whitespace); `delete
# this` etc. needs whitespace between verb and deictic.
#
# The "the + deictic-noun" branch was added in the second round: a
# previous form allowed "the <any noun>", which fires on descriptive
# prose like "Call delete() to remove the item" — `remove the item`
# matched because `item` was accepted as a bare noun. The narrowed
# branch requires the noun to name code-or-deictic material
# specifically, so "the item" no longer matches while "the following
# declaration" still does.

_DELETION_START_RE = re.compile(
    r'^\s*(?:delete|remove|drop|eliminate|wipe)\b(?=[\s:])',
    re.MULTILINE,
)

_DELETION_DETERMINER_RE = re.compile(
    r'\b(?:delete|remove|drop|eliminate|wipe)\s+'
    r'(?:'
    r'this|these|those'
    r'|'
    r'the\s+(?:following|above|below|block|code|line|lines|section|'
    r'function|method|class|declaration|definition)'
    r')\b'
)

_DELETION_PHRASES = (
    "no longer",
    "revert this",
    "revert the",
    "get rid of",
    "take out",
    "strip out",
)


def _has_deletion_signal(prose: str) -> bool:
    """True iff the last three lines of `prose` contain a deletion-
    instruction signal.

    Narrowed 2026-11-01 — see the block comment above for the
    motivating false positive and the reasoning behind the three
    accepted shapes.  The determiner branch additionally requires a
    deictic noun after "the" so descriptive prose like "remove the
    item" doesn't accidentally fire.
    """
    narrow = "\n".join(prose.splitlines()[-3:]).lower()
    if _DELETION_START_RE.search(narrow):
        return True
    if _DELETION_DETERMINER_RE.search(narrow):
        return True
    for phrase in _DELETION_PHRASES:
        if phrase in narrow:
            return True
    return False


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

    Delegates to `_has_deletion_signal` for the prose check; that
    function narrowed its pattern set on 2026-11-01, so this function
    fires on a smaller set of prose than before. See the block
    comment above `_DELETION_START_RE`.
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

# Placement-failure set: statuses that mean "the patch did not land in
# the destination file." Deliberately excludes:
#   · "already-present":          the patch's content was already there
#                                 (or, post-8.7d, was claimed to be and
#                                 has been flipped to "failed" if not)
#   · "skipped_misattributed":    content lives in another file, recorded
#                                 separately in verification.reconciled
#   · "skipped_weak_attribution": tracked separately in reconcile's
#                                 per-patch diagnostics
_PLACEMENT_FAILURES = FAILED_STATUSES | frozenset({
    "skipped_structural_damage",
    "skipped_unplaceable",
})

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
    repair_content: Optional[str] = None
