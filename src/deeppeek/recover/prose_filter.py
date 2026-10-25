"""Early prose/UI detection and body-header filename extraction.

Two targeted heuristics that reduce noise in the recovery pipeline
without risking legitimate content:

  looks_like_prose_ui(body)
      True iff the body reads as prose or a UI mockup rather than
      code.  Used at pass 2 to pre-filter orphans that were never
      going to be recoverable, keeping them out of the escalation
      queue (in --llm-mode smart) and out of the actionable orphan
      list (in warnings.txt).

  filename_in_orphan_header(body, existing_paths)
      Returns filenames mentioned in the body's first 3 non-blank
      lines.  Used at pass 4 as an attribution signal: an orphan
      whose first comment names its target file is attributable
      even if the surrounding prose does not.

Both are deliberately conservative.  A false positive at
`looks_like_prose_ui` costs a lost orphan recovery; a false
negative keeps one noisy warning.  Every check that could
plausibly be code errs toward "keep".
"""
from __future__ import annotations

import re

from .core import _basename


# Filenames with common source extensions.  Presence of a filename
# anywhere in the body means the block might be attributable, so
# never filter it as prose.
_FILENAME_RE = re.compile(
    r'\b[A-Za-z_][\w]*\.'
    r'(?:py|js|mjs|cjs|ts|tsx|jsx|md|json|yaml|yml|toml|sh|bash|'
    r'html|css|scss|rs|go|rb|php|java|kt|c|cc|cpp|h|hpp)\b')

_UI_BRACKET_RE = re.compile(r'\[[A-Z][\w \-]+\]')
_UI_RADIO_RE = re.compile(r'\(\s*[•\u2713xX]\s*\)')
_UI_CHECKBOX_RE = re.compile(r'\[\s*[xX\u2713]\s*\]')

_CODE_KEYWORDS = (
    "function ", "def ", "class ", "const ", "let ", "var ",
    "import ", "export ", "return ", "if ", "for ", "while ",
    "try ", "catch ", "except ", "else ", "elif ",
)

_MIN_LEN = 15


def looks_like_prose_ui(body):
    """True iff body is prose or a UI mockup, not code.

    Conservative across multiple checks: returns False the moment any
    plausible code signal appears.  Only fires on blocks that have
    none of: braces, semicolons, assignment, code keywords,
    filename references, or docstring quoting.
    """
    if not body:
        return False
    stripped = body.strip()
    if len(stripped) < _MIN_LEN:
        return False

    # Docstrings: Python triple-quoted text is code, even if the
    # content reads as prose.
    if stripped.startswith(('"""', "'''")):
        return False

    if any(c in stripped for c in "{};"):
        return False
    if " = " in stripped:
        return False
    if any(kw in stripped for kw in _CODE_KEYWORDS):
        return False
    if _FILENAME_RE.search(stripped):
        return False

    # UI mockup patterns
    if (_UI_BRACKET_RE.search(stripped)
            or _UI_RADIO_RE.search(stripped)
            or _UI_CHECKBOX_RE.search(stripped)):
        return True

    # Sentence-like: 4+ words AND at least one ends with `.` or `,`
    words = stripped.split()
    if len(words) >= 4:
        for w in words:
            if w.endswith(".") or w.endswith(","):
                return True
    return False


def filename_in_orphan_header(body, existing_paths):
    """Return filenames from `existing_paths` mentioned in the body's
    first 3 non-blank lines.

    The header comment above a fragment often names its target file:
    `// pg_base.py Inpainting section`.  Pass 4's normal prose scan
    only looks at `context_before` (text above the fence); this scan
    looks inside the fence's own opening lines.  Returns at most one
    match — ambiguity (two filenames mentioned) is caller-handled.
    """
    if not body:
        return []
    lines = [ln for ln in body.split("\n") if ln.strip()][:3]
    text = "\n".join(lines)
    matches = []
    for path in existing_paths:
        base = _basename(path)
        if not base or len(base) < 4:
            continue
        if base in text or path in text:
            matches.append(path)
    return matches
