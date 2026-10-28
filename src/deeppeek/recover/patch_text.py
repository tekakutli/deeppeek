"""
patch_text — pure text-manipulation primitives.

Leaf module: no project imports.  Everything here is a string-in,
string-or-data-out function.  Splitting this out is what makes the
heuristic modules (patch_scope, patch_heuristics, patch_pairs) able
to depend on each other without cyclic imports — they all agree on
this module's vocabulary.

Not to be confused with `patch_scope` (which finds the boundaries
of a function/class/<details>) or `patch_heuristics` (which applies
inserts).  This module only knows about tokens, braces, comments,
ellipsis markers, and content-presence checks.

Two structural predicates deserve their own docstrings:

  _is_ellipsis_line
      The `...` marker (or its comment-wrapped equivalents) that an
      AI uses to abbreviate "rest of block unchanged".

  _has_placeholder_marker
      The parenthetical form, e.g. `... (docstring unchanged) ...`,
      `// ... (rest unchanged)`, `# ... existing code ...`.  A
      placeholder is an *instruction* to the patcher — "keep the
      base's content here" — not literal code.  Applying it verbatim
      corrupts the target.  See `patch_scope._try_merge_placeholder_in_scope`
      for the merge path that consumes it.

  _content_present_in_base
      Drift-tolerant membership check.  A line "counts as present"
      when it appears in base exactly, or token-equivalently (same
      tokens, different order / whitespace).  This is what makes
      the drift-tolerance fix in patch_pairs and patch_heuristics
      work: a pair whose new_body is already in base in a reordered
      form does not fail as "state drift".

NEW: `_extract_preamble` / `_preserve_preamble`.

When a FULL block replaces a pre-existing file, `pass6_apply_patches`
uses the block's body as the new base.  If the block omits the
pre-existing file's leading docstring / comment preamble (the AI
printed the code but not the wrapper), the resulting content silently
loses that preamble — the pg_state.py failure in the wild
(`STATE_JS = r\"\"\"...\"\"\"` replaced a file whose first 17 lines
were a module docstring).  `_preserve_preamble` restores the
pre-existing preamble when the block doesn't have its own.
"""

import re


# ===========================================================================
#  Token equivalence / drift
# ===========================================================================

_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*|[0-9]+|\S")


def _tokens_key(s):
    """Token set of a line.  Whitespace and token order do not matter."""
    return frozenset(_TOKEN_RE.findall(s))


def _line_token_jaccard(a, b):
    at = _tokens_key(a)
    bt = _tokens_key(b)
    if not at or not bt:
        return 0.0
    return len(at & bt) / len(at | bt)


def _content_present_in_base(base, content, threshold=0.8):
    """Return True iff at least `threshold` of content's non-blank
    lines are present in base, either exactly or token-equivalently.

    This is the drift-tolerance helper used by the "is this patch's
    intent already in base?" checks.  The common drift shapes it
    accepts:

      · Reordered conjuncts:   `a && b` vs `b && a`
      · Reordered object keys: `{a:1, b:2}` vs `{b:2, a:1}`
      · Same tokens, different whitespace: `foo(a,b)` vs `foo(a, b)`

    It does NOT accept genuinely different content.  A patch whose
    identifier set differs from anything in base still fails the
    check and fails closed as today.

    threshold=0.8 is the calibrated default: high enough that a
    single stray line in a five-line block does not accidentally
    pass, low enough that a one-line drift (added trailing semicolon,
    a reordered operand) still counts.
    """
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return True
    base_lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
    base_exact = set(base_lines)
    base_tokens = {_tokens_key(bl): True for bl in base_lines}
    matched = 0
    for nl in lines:
        if nl in base_exact or _tokens_key(nl) in base_tokens:
            matched += 1
    return matched / len(lines) >= threshold


# ===========================================================================
#  Preamble extraction / preservation  (NEW)
# ===========================================================================
#
# A "preamble" is the leading docstring, shebang, or comment block of a
# source file.  When a FULL block replaces a pre-existing file, the
# pre-existing preamble must be preserved if the block omits it.

def _extract_preamble(text):
    """Return the leading preamble of `text`, or "" if none.

    Handles, in order:
      · shebang line (`#!/usr/bin/env python`)
      · contiguous `#` comment lines (Python)
      · `//` comment lines (JS / C-family)
      · `/* ... */` block comments
      · `<!-- ... -->` HTML comments
      · Python triple-quoted docstring at line start

    Returns the preamble as it appeared (indentation preserved,
    including any blank lines between components).  Returns "" if the
    file starts with code rather than a preamble.
    """
    if not text:
        return ""
    lines = text.split("\n")
    n = len(lines)
    i = 0

    if lines[0].startswith("#!"):
        i = 1

    while i < n and lines[i].startswith("#"):
        i += 1

    while i < n and lines[i].startswith("//"):
        i += 1

    if i < n and lines[i].startswith("/*"):
        for j in range(i, n):
            if "*/" in lines[j]:
                i = j + 1
                break

    if i < n and lines[i].lstrip().startswith("<!--"):
        for j in range(i, n):
            if "-->" in lines[j]:
                i = j + 1
                break

    if i < n:
        stripped = lines[i].lstrip()
        for q in ('"""', "'''"):
            if stripped.startswith(q):
                rest = stripped[len(q):]
                if q in rest:
                    i += 1
                else:
                    for j in range(i + 1, n):
                        if q in lines[j]:
                            i = j + 1
                            break
                break

    if i == 0:
        return ""
    return "\n".join(lines[:i])


def _preserve_preamble(preexisting_body, block_body):
    """If `block_body` lacks `preexisting_body`'s preamble (a leading
    docstring or comment block) and doesn't carry one of its own,
    prepend the pre-existing preamble.

    Conservative: only fires when the block has NO preamble.  A block
    that reprints its own preamble is treated as the author's explicit
    choice of a new preamble; the pre-existing one is discarded.

    Motivating case: block 94 in the pg_state.py conversation was a
    `SINGLE_FULL` reprint of the file body — but it started at
    `STATE_JS = r\"\"\"…\"\"\"` and omitted the 17-line module docstring
    that pre-existed.  `pass6` used the block's body as the base and
    `pass9` wrote it verbatim, silently dropping the docstring.  This
    helper restores it.
    """
    if not preexisting_body or not block_body:
        return block_body
    pre_pre = _extract_preamble(preexisting_body)
    if not pre_pre:
        return block_body
    if _extract_preamble(block_body):
        return block_body
    return pre_pre + "\n\n" + block_body


# ===========================================================================
#  Identifier / keyword extraction
# ===========================================================================

_IDENT_KEYWORDS = frozenset({
    "const", "let", "var", "function", "class", "def", "import",
    "export", "return", "if", "for", "while", "async", "await",
    "else", "elif", "try", "catch", "finally", "throw", "raise",
    "new", "delete", "typeof", "instanceof", "in", "of",
})


def _first_identifier(text):
    for m in re.finditer(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", text):
        tok = m.group(0)
        if tok not in _IDENT_KEYWORDS:
            return tok
    return None


def _first_word(s):
    """Return the leading alphabetic word of `s` (lowercased)."""
    out = []
    for ch in s:
        if ch.isalpha() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out).lower()


# ===========================================================================
#  Ellipsis / truncation markers
# ===========================================================================

_TRUNCATION_PATTERNS = [
    re.compile(r'^\s*\.\.\.\s*$', re.MULTILINE),
    re.compile(r'^\s*<\s*\.\.\.\s*>\s*$', re.MULTILINE),
    re.compile(r'//\s*\.\.\.', re.MULTILINE),
    re.compile(r'/\*\s*\.\.\.\s*\*/'),
    re.compile(r'#\s*\.\.\.', re.MULTILINE),
    re.compile(r'<!--\s*\.\.\.\s*-->'),
]

# Parenthetical placeholder: `... (docstring unchanged) ...`,
# `// ... (rest unchanged)`, `# ... existing code ...`,
# `/* ... same as before ... */`.  The parenthetical must contain an
# "unchanged" verb to be recognized — a bare `... (something) ...`
# is not a placeholder, it's content the AI wrote intentionally.
_PLACEHOLDER_MARKER_RE = re.compile(
    r"\.\.\.\s*\(\s*(?:the\s+)?[^)]*?\b"
    r"(?:unchanged|existing|omitted|same|as\s+before|as-is|as\s-is|"
    r"kept|preserved|unchanged\s+except|below|above|rest)"
    r"\b[^)]*\)\s*\.\.\.",
    re.IGNORECASE,
)


def _has_truncation_marker(text):
    if not text:
        return False
    return any(pat.search(text) for pat in _TRUNCATION_PATTERNS)


def _has_placeholder_marker(body):
    """True iff `body` contains an AI placeholder marker.

    Placeholder markers are the parenthetical form an AI uses to
    abbreviate unchanged regions:

        ... (docstring unchanged) ...
        // ... (rest unchanged)
        # ... existing code ...
        /* ... same as before ... */

    They are instructions to the patcher, not literal code.
    Applying them verbatim corrupts the target — the marker text
    replaces whatever it was supposed to abbreviate.

    The recovery pipeline handles them by attempting a merge against
    the base scope's content (see
    `patch_scope._try_merge_placeholder_in_scope`).  If the merge
    cannot find the target scope, `_try_deterministic_patch_inner`
    refuses the block outright (see `patch_dispatch.py`), and the
    record fails closed.
    """
    if not body:
        return False
    return bool(_PLACEHOLDER_MARKER_RE.search(body))


def _is_ellipsis_line(line):
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    for prefix in ("//", "#", "/*", "*", "<!--", "--"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
            break
    for suffix in ("*/", "-->"):
        if stripped.endswith(suffix):
            stripped = stripped[:-len(suffix)].strip()
            break
    if stripped in ("...", "\u2026"):
        return True
    if stripped.startswith("..."):
        after = stripped[3:].lstrip()
        return (not after) or after[0].isalpha()
    if stripped.startswith("\u2026"):
        after = stripped[1:].lstrip()
        return (not after) or after[0].isalpha()
    return False


def _has_ellipsis_marker(body):
    if not body:
        return False
    return any(_is_ellipsis_line(ln) for ln in body.splitlines())


def _split_at_ellipsis(body):
    lines = body.splitlines()
    first = None
    for i, ln in enumerate(lines):
        if _is_ellipsis_line(ln):
            first = i
            break
    if first is None:
        return lines, []
    prefix = lines[:first]
    suffix = [ln for ln in lines[first + 1:] if not _is_ellipsis_line(ln)]
    return prefix, suffix


def _strip_ellipsis_lines(body):
    """Remove `...` marker lines from a body, keeping blank lines."""
    out = []
    for ln in body.split("\n"):
        if _is_ellipsis_line(ln):
            continue
        out.append(ln)
    return "\n".join(out)


# ===========================================================================
#  Empty-block detection (ellipsis fallthrough guard)
# ===========================================================================

_EMPTY_BLOCK_RE = re.compile(r'\{([^{}]*)\}')


def _count_empty_blocks(text):
    if not text:
        return 0
    count = 0
    for m in _EMPTY_BLOCK_RE.finditer(text):
        if not m.group(1).strip():
            count += 1
    return count


def _would_introduce_empty_block(original_body, stripped_body):
    """True iff stripping `...` introduced one or more new empty `{ }`
    blocks."""
    return _count_empty_blocks(stripped_body) > _count_empty_blocks(
        original_body
    )


# ===========================================================================
#  Comment walking
# ===========================================================================

def _body_starts_with_comment(body):
    """True iff the body's first non-blank line is a comment marker."""
    if not body:
        return False
    for line in body.split(chr(10)):
        s = line.strip()
        if not s:
            continue
        return (s.startswith("/*")
                or s.startswith("//")
                or s.startswith("#"))
    return False


def _find_leading_comment_start(base, indent_pos):
    """Walk backward from indent_pos over a leading comment region."""
    if indent_pos <= 0:
        return indent_pos
    pos = indent_pos
    entered_comment = False
    while pos > 0:
        prev_nl = base.rfind(chr(10), 0, pos - 1)
        line_start = prev_nl + 1
        line = base[line_start:pos - 1]
        stripped = line.strip()

        if not stripped:
            if entered_comment:
                return pos
            pos = line_start
            continue

        if stripped.startswith("//") or stripped.startswith("#"):
            entered_comment = True
            pos = line_start
            continue

        if stripped.startswith("@"):
            opens = stripped.count("(")
            closes = stripped.count(")")
            if opens > closes:
                depth = opens - closes
                scan = line_start
                while scan > 0 and depth > 0:
                    prev_nl2 = base.rfind(chr(10), 0, scan - 1)
                    scan2 = prev_nl2 + 1
                    scan_line = base[scan2:scan - 1]
                    depth += scan_line.count(")") - scan_line.count("(")
                    scan = scan2
                if depth == 0:
                    pos = scan
                    entered_comment = True
                    continue
                return pos
            pos = line_start
            entered_comment = True
            continue

        if stripped.endswith("*/"):
            block_start = base.rfind("/*", 0, pos - 1)
            if block_start < 0:
                return pos
            block_line_start = base.rfind(chr(10), 0, block_start) + 1
            between = base[block_start:pos]
            if between.count("/*") != 1:
                return pos
            pos = block_line_start
            entered_comment = True
            continue

        if entered_comment:
            return pos
        return indent_pos

    if entered_comment:
        return pos
    return indent_pos


# ===========================================================================
#  Brace / paren matching
# ===========================================================================

def _find_matching_brace(text, open_idx, is_python=False):
    if open_idx >= len(text) or text[open_idx] != "{":
        return -1
    depth = 0
    i = open_idx
    in_str = None
    in_line_comment = False
    in_block_comment = False
    while i < len(text):
        c = text[i]
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            in_str = c
            i += 1
            continue
        if is_python and c == "#":
            in_line_comment = True
            i += 1
            continue
        if not is_python and c == "/" and i + 1 < len(text):
            if text[i + 1] == "/":
                in_line_comment = True
                i += 2
                continue
            if text[i + 1] == "*":
                in_block_comment = True
                i += 2
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_matching_paren(text, open_idx):
    if open_idx >= len(text) or text[open_idx] != "(":
        return -1
    depth = 0
    i = open_idx
    in_str = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            in_str = c
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ===========================================================================
#  Reindent / line splicing
# ===========================================================================

def _reindent_body(base_indent, body):
    """Shift every non-blank line of `body` so the first non-blank
    line starts at `base_indent`, preserving relative indentation of
    subsequent lines."""
    if not body:
        return body
    lines = body.split("\n")
    first = lines[0]
    orig_indent_len = len(first) - len(first.lstrip(" \t"))
    adjusted = [base_indent + first.lstrip(" \t")]
    for ln in lines[1:]:
        if not ln.strip():
            adjusted.append("")
            continue
        ln_indent = len(ln) - len(ln.lstrip(" \t"))
        relative = max(0, ln_indent - orig_indent_len)
        adjusted.append(base_indent + " " * relative + ln.lstrip(" \t"))
    return "\n".join(adjusted)


def _reindent_body_to_indent(body, target_indent):
    """Shift every non-blank line of `body` so the first non-blank
    line starts at `target_indent`."""
    lines = body.rstrip().split("\n")
    base = ""
    for ln in lines:
        if ln.strip():
            base = ln[:len(ln) - len(ln.lstrip())]
            break
    out = []
    for ln in lines:
        if not ln.strip():
            out.append("")
        else:
            out.append(target_indent + ln[len(base):])
    return "\n".join(out)


def _insert_lines_in_inner(inner, new_lines, before_anchor=None,
                            after_anchor=None, default_position="top"):
    if not new_lines:
        return inner
    lines = inner.split("\n")
    indent = "  "
    for ln in lines:
        if ln.strip():
            stripped = ln.lstrip(" \t")
            indent = ln[:len(ln) - len(stripped)]
            break
    first_indent = None
    for ln in new_lines:
        if ln.strip():
            first_indent = len(ln) - len(ln.lstrip(" \t"))
            break
    if first_indent is None:
        first_indent = 0
    indented = []
    for ln in new_lines:
        if not ln.strip():
            indented.append("")
        else:
            relative = len(ln) - len(ln.lstrip(" \t")) - first_indent
            indented.append(indent + " " * max(0, relative) + ln.lstrip(" \t"))

    if not lines or (len(lines) == 1 and lines[0] == ""):
        return "\n" + "\n".join(indented) + "\n"

    insert_at = None
    if before_anchor is not None:
        for i, ln in enumerate(lines):
            if ln.strip() == before_anchor:
                insert_at = i
                break
    if insert_at is None and after_anchor is not None:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == after_anchor:
                insert_at = i + 1
                break
    if insert_at is None:
        if default_position == "top":
            insert_at = 1 if lines[0] == "" else 0
        else:
            insert_at = len(lines) - 1 if lines[-1] == "" else len(lines)

    return "\n".join(lines[:insert_at] + indented + lines[insert_at:])


# ===========================================================================
#  Body block segmentation (used by the duplicate-filter and dedup path)
# ===========================================================================

def _collect_inserted_lines(old_content, new_content):
    """Return the set of stripped lines whose count increased
    old_content -> new_content."""
    from collections import Counter
    old_count = Counter(
        line.strip() for line in old_content.split("\n") if line.strip()
    )
    new_count = Counter(
        line.strip() for line in new_content.split("\n") if line.strip()
    )
    return {
        line for line, count in new_count.items()
        if count > old_count.get(line, 0)
    }


def _split_body_into_blocks(body):
    """Split body into top-level blocks."""
    blocks = []
    current = []
    depth = 0
    for line in body.split("\n"):
        current.append(line)
        depth += line.count("{") - line.count("}")
        stripped = line.strip()
        if depth == 0 and stripped.endswith((";", "}")):
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


# ===========================================================================
#  Identifier block end (used by insert-after-anchor)
# ===========================================================================

def _find_identifier_block_end_line(base, ident):
    """Return the line index (0-based) of the last line of the
    top-level statement that declares or first references `ident`,
    or None."""
    lines = base.split("\n")
    decl_re = re.compile(
        r'^(?:const|let|var|function|class)\s+' + re.escape(ident) + r'\b')

    decl_idx = None
    for i, line in enumerate(lines):
        if decl_re.match(line.strip()):
            decl_idx = i
            break

    is_const_decl = decl_idx is not None

    if decl_idx is None:
        ident_re = re.compile(r'\b' + re.escape(ident) + r'\b')
        for i, line in enumerate(lines):
            if len(line) == len(line.lstrip()) and ident_re.search(line):
                decl_idx = i
                break

    if decl_idx is None:
        return None

    segment = lines[decl_idx]
    depth = segment.count("{") - segment.count("}")
    end_idx = decl_idx

    if depth > 0:
        i = decl_idx + 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("{") - lines[i].count("}")
            end_idx = i
            i += 1
        if depth > 0:
            return None

    if is_const_decl:
        next_i = end_idx + 1
        while next_i < len(lines) and not lines[next_i].strip():
            next_i += 1
        if next_i < len(lines):
            next_line = lines[next_i]
            ident_re = re.compile(r'\b' + re.escape(ident) + r'\b')
            stripped = next_line.strip()
            if (stripped.startswith("if (")
                    and ident_re.search(next_line)
                    and "{" in next_line):
                depth = next_line.count("{") - next_line.count("}")
                i = next_i + 1
                while i < len(lines) and depth > 0:
                    depth += lines[i].count("{") - lines[i].count("}")
                    i += 1
                if depth == 0:
                    end_idx = i - 1

    return end_idx


# ===========================================================================
#  Source-fragment detection
# ===========================================================================

def _looks_like_python_fragment(text):
    for line in text.splitlines()[:5]:
        s = line.strip()
        if not s:
            continue
        if s.startswith(("import ", "from ", "def ", "class ",
                         "async def ", "@")):
            return True
        if s.startswith(("export ", "const ", "let ", "var ",
                         "function ", "async function ")):
            return False
    return True
