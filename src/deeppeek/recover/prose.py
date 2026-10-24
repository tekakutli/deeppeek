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
    # Strip backticks only for the scoped-replace regex, which
    # tolerates them imperfectly; leave the rest of `text` alone so
    # multi-word phrases match cleanly.
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

    This is the single strongest signal for pair formation.  The
    connective names the *second* half of a replace: the AI is
    saying "the block above this line is the old version; the block
    below is the new version, with: [this]".  Two adjacent code
    blocks separated by "with:" *is* the replace-pair shape,
    regardless of what verb the first block's prose used.  Requiring
    the first block to say "Replace the block:" was why
    "Replace the doc comment above X:" silently failed to form a
    pair and its old block was dropped as a stray snippet.

    Only the last non-empty line is checked — that is the prose
    directly above the code block.  A leading conjunction
    ("And ", "Then ", "And then ") is stripped first, because
    "And with:" reads the same as "with:".
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
