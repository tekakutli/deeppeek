"""
recover.prose — intent parsing of the prose around code blocks.

The `context_before` on each Block is the text between the previous
code block (or section heading) and this one.  That prose is where
the author tells us *what to do*.

PAIR ROLES
==========
For replace-pair detection, each block's prose is classified into
one of three roles:

    OLD      "Find:", "Current:", "Old code:", "Before:", "Existing:",
             "Replace the block:", "Look for:", …

    NEW      "with:", "to:", "becomes:", "Replace with:",
             "And replace with:", "New version:", "Updated:",
             "Use this:", "Changed to:", …

    NEUTRAL  anything else.

Only an OLD (or NEUTRAL) block may be the *old* side of a pair.
A NEW block cannot be the old side of another pair — it is
itself the new side of the pair above it.  This is the invariant
that prevents the "new CSS was paired with next block's new
<details>" bug.

PARTIAL REPLACEMENTS
====================
When prose says "rest unchanged", "otherwise unchanged", "identical
to the original", "only the top", "start of", "prefix of", etc., the
block that follows is understood to be a *prefix* of the new body —
a scoped-replace should splice, not replace.  `is_partial_replacement`
reports this to the patcher.
"""

import re
from dataclasses import dataclass


REPLACE_PAIR = "replace_pair"
REPLACE_SCOPED = "replace_scoped"
DELETE = "delete"
INSERT = "insert"
FULL = "full"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Prose phrase lists
# ---------------------------------------------------------------------------

_REPLACE_PROSE = (
    "replace the block:",
    "replace the block with",
    "replace with",
    "replace the entire",
    "replace the whole",
    "replace the block",
    "replace:",
    "old code:",
    "change to:",
    "swap:",
)

# Last non-empty line ending in (or starting with) one of these marks
# the block as the NEW side of a pair.
_NEW_BLOCK_PROSE_PATTERNS = (
    "with:",
    "with the following:",
    "with the following",
    "with this:",
    "to:",
    "becomes:",
    "becoming:",
    "new code:",
    "replacement:",
    "replace with:",
    "replaced with:",
    "and replace with:",
    "and with:",
    "then with:",
    "and then with:",
    "new version:",
    "updated version:",
    "updated:",
    "use this:",
    "try this:",
    "should be:",
    "should become:",
    "should look like:",
    "changed to:",
    "here is the new version:",
    "here's the new version:",
    "here is the replacement:",
    "here's the replacement:",
)

# Last non-empty line ending in (or starting with) one of these marks
# the block as the OLD side of a pair.
_OLD_BLOCK_PROSE_PATTERNS = (
    "find:",
    "current:",
    "current version:",
    "existing:",
    "existing version:",
    "old:",
    "old code:",
    "old version:",
    "before:",
    "here is the current:",
    "here's the current:",
    "here is the old:",
    "here's the old:",
    "replace the block:",
    "replace this:",
    "look for:",
)

# Prose signalling "rest is unchanged" — a scoped-replace with one of
# these phrases should splice the given prefix into the base, not
# replace it entirely.
_PARTIAL_REPLACE_PROSE_PATTERNS = (
    "rest is unchanged",
    "rest unchanged",
    "rest of the function is unchanged",
    "rest of the function unchanged",
    "rest of the file is unchanged",
    "rest of the file unchanged",
    "rest of the method is unchanged",
    "rest remains unchanged",
    "rest stays",
    "rest of it is unchanged",
    "otherwise unchanged",
    "unchanged except",
    "identical to the version",
    "identical to the original",
    "same as before",
    "no other changes",
    "no other change",
    "only the top",
    "only the beginning",
    "only the start",
    "just the top",
    "just the beginning",
    "just the start",
    "top of the function",
    "top of the method",
    "start of the function",
    "beginning of the function",
    "prefix of the",
)

_REPLACE_WITH_PROSE = (
    "with:",
    "with the following",
    "with this:",
    "to:",
    "becomes:",
    "becomes",
    "new code:",
)

_REPLACE_VERBS = ("replace", "update", "change", "rewrite", "modify", "edit")
_ADD_VERBS = ("add ", "insert ", "append ", "prepend ", "include ")

_SCOPED_REPLACE_RE = re.compile(
    r"replace\s+(?:the\s+)?(?:entire\s+|whole\s+)?"
    r"(?:`?[<]?[\w\-]+[>]?`?\s+)?"
    r"(?:for|named)\s+"
    r"`?(?P<name>[A-Za-z_][\w \-]*?)`?\s*(?:with|:|,)",
    re.IGNORECASE,
)

_DELETE_PROSE = (
    "delete this",
    "delete the",
    "remove this",
    "remove the",
    "get rid of",
    "drop the",
    "strip out",
    "no longer needed",
)

_INSERT_PROSE = (
    "add this",
    "insert this",
    "append this",
    "add the following",
    "insert the following",
    "after this:",
    "before this:",
)

_FULL_PROSE = (
    "here is the full file",
    "here's the full file",
    "full file:",
    "full replacement:",
    "here is the new version",
    "here's the new version",
    "full new version",
    "the complete file",
)


@dataclass
class Intent:
    action: str = UNKNOWN
    scope_hint: str = ""
    scope_name: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_nonempty_line(prose: str) -> str:
    if not prose:
        return ""
    for line in reversed(prose.splitlines()):
        stripped = line.strip().lower()
        if stripped:
            return stripped
    return ""


def _strip_leading_conjunction(line: str) -> str:
    for prefix in ("and then ", "and ", "then "):
        if line.startswith(prefix):
            return line[len(prefix):]
    return line


# ---------------------------------------------------------------------------
# Pair-role classification
# ---------------------------------------------------------------------------

def is_new_block_prose(prose: str) -> bool:
    """True if the last non-empty prose line signals "this block is
    the new version of the one above"."""
    last = _strip_leading_conjunction(_last_nonempty_line(prose))
    if not last:
        return False
    return any(last.endswith(p) or last.startswith(p)
               for p in _NEW_BLOCK_PROSE_PATTERNS)


def is_old_block_prose(prose: str) -> bool:
    """True if the last non-empty prose line signals "this block is
    the old version, to be replaced"."""
    last = _strip_leading_conjunction(_last_nonempty_line(prose))
    if not last:
        return False
    return any(last.endswith(p) or last.startswith(p)
               for p in _OLD_BLOCK_PROSE_PATTERNS)


def is_with_connective(prose: str) -> bool:
    """Backward-compat alias for is_new_block_prose."""
    return is_new_block_prose(prose)


def is_partial_replacement(prose: str) -> bool:
    """True if the prose signals this block is a *prefix* of the new
    body ("rest is unchanged", "start of", …)."""
    if not prose:
        return False
    lower = prose.lower()
    return any(p in lower for p in _PARTIAL_REPLACE_PROSE_PATTERNS)


def is_scoped_replace_by_body(prose: str, body: str) -> bool:
    """True if the prose introduces a scoped replace AND the body
    carries a scope marker (so we know which scope to target).

    This catches "Replace the entire Inpainting `<details>` with:" —
    where the scope name comes from the body's
    `<summary>Inpainting</summary>`, not from "for X" prose."""
    if not prose or not body:
        return False
    prose_lower = prose.lower()
    if "replace" not in prose_lower:
        return False
    last = _last_nonempty_line(prose_lower)
    if not (last.endswith("with:") or last.endswith("with.")
            or "with:" in last[-40:]):
        return False
    name, _kind = _extract_scope_from_body(body)
    return bool(name)


def is_add_instruction(prose: str) -> bool:
    """True iff the prose contains an add-family verb."""
    lower = (prose or "").lower()
    return any(v in lower for v in _ADD_VERBS)


# ---------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------

def parse_intent(context_before: str) -> Intent:
    """Return the structured intent of the prose preceding a block."""
    text = (context_before or "").strip()
    if not text:
        return Intent()

    text_lower = text.lower()
    text_clean = text_lower.replace("`", "")

    for phrase in _REPLACE_WITH_PROSE:
        if text_lower.endswith(phrase) or phrase in text_lower:
            return Intent(action=REPLACE_PAIR, scope_hint="with")

    for phrase in _REPLACE_PROSE:
        if phrase in text_lower:
            return Intent(action=REPLACE_PAIR, scope_hint=phrase)

    m = _SCOPED_REPLACE_RE.search(text_clean)
    if m:
        return Intent(
            action=REPLACE_SCOPED,
            scope_hint="scoped",
            scope_name=m.group("name").strip(),
        )

    for phrase in _FULL_PROSE:
        if phrase in text_lower:
            return Intent(action=FULL)

    for phrase in _INSERT_PROSE:
        if phrase in text_lower:
            return Intent(action=INSERT, scope_hint=phrase)

    for phrase in _DELETE_PROSE:
        if phrase in text_lower:
            return Intent(action=DELETE, scope_hint=phrase)

    return Intent()


# ---------------------------------------------------------------------------
# Scope extraction from body
# ---------------------------------------------------------------------------

def _extract_scope_from_body(body: str) -> tuple:
    """Return (name, kind) extracted from the block body, or ("", "")."""
    if not body:
        return "", ""
    for pattern, kind in _SCOPE_FROM_BODY_PATTERNS:
        m = pattern.search(body)
        if m:
            return m.group(1).strip(), kind
    return "", ""


_SCOPE_FROM_BODY_PATTERNS = [
    (re.compile(
        r"<details\b[^>]*>\s*<summary>\s*([A-Za-z][\w \-]+?)\s*</summary>",
        re.IGNORECASE | re.DOTALL),
     "details"),
    (re.compile(r"^(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE),
     "python"),
    (re.compile(
        r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_]\w*)",
        re.MULTILINE),
     "javascript"),
    (re.compile(
        r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*\{",
        re.MULTILINE),
     "javascript"),
]
