"""
recover.prose — intent parsing of the prose around code blocks.

The `context_before` on each Block is the text between the previous
code block (or section heading) and this one.  That prose is where
the author tells us *what to do*:

    "Replace the block:"              → replace, old shown next
    "with:"                           → the second half of a pair
    "Replace the entire X for Y:"     → scoped replace, only new shown
    "Delete this:"                    → delete
    "Add this after X:"               → insert
    "Here is the full file:"          → full replacement

Intent vocabulary (each maps to a different patcher):

    REPLACE_PAIR    old + new blocks, side by side
    REPLACE_SCOPED  single block, prose names the target scope
    DELETE          single block, prose signals deletion
    INSERT          single block, prose signals insertion
    FULL            single block, prose signals a full-file write
    UNKNOWN         no clear signal

SCOPE-FROM-BODY EXTRACTION
==========================
When the prose says "replace/update/change the X" (or "add X to Y")
but the block body carries a recognisable scope marker (an HTML
`<details>` with a `<summary>`, a Python `def X`, a JS `function X`
/ `export const X = {`), we can extract the scope name *from the
body* rather than requiring the prose to name it.  This is
`_extract_scope_from_body`, used by `pairing._detect_scoped` as a
fallback.

ADD VERBS
=========
"Add", "insert", "append", and "prepend" are treated as
replace-adjacent verbs for scoped-replace purposes: an instruction
like "add the full-canvas early-out to `drawInpaintContext`:" with
a body that IS the full new function is a scoped-replace in
disguise.  The `_ADD_VERBS` tuple carries these, and
`is_add_instruction` is a per-context check.
"""

import re
from dataclasses import dataclass


REPLACE_PAIR = "replace_pair"
REPLACE_SCOPED = "replace_scoped"
DELETE = "delete"
INSERT = "insert"
FULL = "full"
UNKNOWN = "unknown"


# Words that appear in a "replace" instruction.  Order matters:
# longer, more specific phrases first.
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

_REPLACE_WITH_PROSE = (
    "with:",
    "with the following",
    "with this:",
    "to:",
    "becomes:",
    "becomes",
    "new code:",
)

# Verbs that indicate the block *is* the new content for some scope,
# even without a name in prose.  Used by the pairing fallback.
#
# The trailing space after each verb reduces false positives ("add"
# vs "additional", "insert" vs "insertion").  "modify" and "edit"
# are excluded from the trailing-space form because they commonly
# appear as "modifying" / "edited" in prose.
_REPLACE_VERBS = ("replace", "update", "change", "rewrite", "modify", "edit")

# Add-family verbs.  Treated as replace-adjacent for scoped-apply
# because a body that IS the full new scope is a scoped-replace,
# regardless of whether the prose says "replace" or "add".
_ADD_VERBS = ("add ", "insert ", "append ", "prepend ", "include ")

# Scoped-replace prose — no OLD block follows; only the NEW block,
# and the prose names the target as "<scope> for <name>".
#
# Examples matched:
#   "Replace the entire `<details>` for Inpainting with:"
#   "Replace the whole function parse_config with:"
#   "Replace the block named Foo with:"
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


def is_with_connective(context_before: str) -> bool:
    """True iff the prose preceding the block is a `with:`-style
    follow-on connective.

    Only the last non-empty line is checked — that is the prose
    directly above the code block.  A leading conjunction ("And ",
    "Then ", "And then ") is stripped first, because "And with:"
    reads the same as "with:".
    """
    text = (context_before or "").strip().lower()
    if not text:
        return False
    last_line = ""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            last_line = stripped
            break
    if not last_line:
        return False
    for prefix in ("and then ", "and ", "then "):
        if last_line.startswith(prefix):
            last_line = last_line[len(prefix):]
            break
    return any(last_line.startswith(p) for p in (
        "with:", "with the following", "with this:",
        "to:", "becomes:", "new code:",
    ))


def is_add_instruction(context_before: str) -> bool:
    """True iff the prose contains an add-family verb."""
    text = (context_before or "").lower()
    return any(v in text for v in _ADD_VERBS)


def _extract_scope_from_body(body: str) -> tuple:
    """Return (name, kind) extracted from the block body, or ("", "")."""
    if not body:
        return "", ""
    for pattern, kind in _SCOPE_FROM_BODY_PATTERNS:
        m = pattern.search(body)
        if m:
            return m.group(1).strip(), kind
    return "", ""


# ---------------------------------------------------------------------------
# Scope extraction from block body
# ---------------------------------------------------------------------------

_SCOPE_FROM_BODY_PATTERNS = [
    # HTML <details> with <summary>
    (re.compile(
        r"<details\b[^>]*>\s*<summary>\s*([A-Za-z][\w \-]+?)\s*</summary>",
        re.IGNORECASE | re.DOTALL),
     "details"),
    # Python def/class at top level
    (re.compile(r"^(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE),
     "python"),
    # JS function/class (with or without export)
    (re.compile(
        r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_]\w*)",
        re.MULTILINE),
     "javascript"),
    # JS object literal (with or without export)
    (re.compile(
        r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*\{",
        re.MULTILINE),
     "javascript"),
]
