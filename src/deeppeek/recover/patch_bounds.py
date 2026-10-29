"""Boundary adjustment + structural validation for patch splices.

The three heuristics that do a region *replace* (`_try_replace_intent`,
`_try_region_replace`, `_apply_scoped_single`) all pick a `[start, end)`
line window by heuristics, splice the body in, and return whatever the
splice produced. Two failure modes are common:

  1. Off-by-one boundaries.  `end` lands mid-statement (before the
     closing `}` or `);`), so the old statement's tail survives as an
     orphan after the new body.

  2. Line-based boundaries without structural awareness.  The correct
     replacement region is the span between two AST statements; a
     line-based similarity search has no way to know that.

Both are addressable at the splice site without case-specific rules:

  · `structural_valid(content, path)` — parse-check `content`, including
    embedded JS/HTML inside Python raw-string literals. This is the
    missing gate that let pass-8 catch broken embedded JS one pass late.

  · `try_ast_snapped_boundaries(...)` — use `ast` (`.py`) or tree-sitter
    (`.js`/`.ts`/etc., and the dominant embedded JS/HTML string in a
    `.py`) to snap `[start, end)` outward to complete enclosing
    statements.

  · `try_boundary_pairs(...)` — expand/contract by ±1..N lines; cheap
    fallback when the AST snap can't help.

  · `splice_region(...)` — do the splice, validate, return `None` on
    parse error or extreme size loss.

EMBEDDED-HTML SNAPPING (2026-10-29)
===================================
A `.py` file that embeds an HTML string — the playground pattern
(`PG_PANEL_BODY = r\"\"\"...\"\"\"`) — needs its region-replace
boundaries snapped to the enclosing HTML *element*, not the enclosing
Python statement.  Without this, a patch that replaces a 3-line
`<input>` window with a 16-line `<div class="field">...` block gets
spliced into the inner 3-line window, producing a `<div>` nested
inside the outer `<div class="field">` that was already there.  Valid
Python (the whole thing is inside a triple-quoted string), valid
Python (ast.parse succeeds), and semantically broken (two Rotation
labels, two range inputs, one orphaned value span).

The fix: `try_ast_snapped_boundaries` first tries
`_try_embedded_html_snap`, which:

  · locates every HTML block element inside every triple-quoted string
    in the file;
  · filters to ranges that fully enclose `[start, end)`;
  · if `body` has an opening tag (and class/style signature), prefers
    the innermost enclosing element whose opening tag matches that
    signature;
  · otherwise falls back to the innermost enclosing element.

When the body's opening signature doesn't match any enclosing element
(the fallback path), the innermost element is used.  That is still
wrong for the classic nested-`<div>` case, but it is *bounded* wrong —
the outer element survives — rather than catastrophically wrong.  The
`min_fraction` gate in `splice_region` catches the "over-replace"
direction when a stray small fragment is spliced into a large
container.

MANUAL HTML SNAP FALLBACK (2026-11-05, revised)
===============================================
Tree-sitter-html is optional.  When it is not installed, the
tree-sitter-based `_embedded_html_top_level_stmt_ranges` returns `[]`,
and `_try_embedded_html_snap` needs a text-only path.  Two failure
modes motivated the revision:

  · First version of `_try_manual_html_snap` required `body` to have
    an HTML opening signature (`<div class="..."`).  A body with no
    signature (e.g. prose, or an empty body) returned `None` even
    when the window was clearly inside an HTML block element.
    Motivating case: `test_patch_bounds_html.py::embedded html snap
    falls back to innermost`.

  · When `_try_embedded_html_snap` returned `None` for that case,
    `try_ast_snapped_boundaries` fell through to
    `_python_top_level_stmt_ranges`, which expanded `[start, end)` to
    the *whole* `BODY = r\"\"\"...\"\"\"` assignment.  That is the worst
    possible snap — larger than either the outer or inner element —
    and produces a silent over-replacement.  Motivating case: the
    same test's `expected (2, 6) — inner div range; got (0, 9)`.

Revised behaviour, three tiers:

  1. **Signature present, matches one or more enclosing elements.**
     Return the innermost match.  Preserves `pg_base.py` semantics
     where `<div class="field" style="...">` matches only the outer
     wrapper, and identifies it unambiguously.

  2. **Signature present, matches no enclosing element.**  Return
     `None` (fail-closed).  Preserves
     `test_manual_html_snap_none_when_class_mismatch`.

  3. **No signature.**  Scan for *every* block-tag open (not just one
     tag), compute each open's matching close by depth, and return the
     innermost enclosing range.  This matches what `_try_html_snap`
     does via tree-sitter (min over enclosing), so behaviour is
     identical whether tree-sitter-html is installed or not.

Additionally, `try_ast_snapped_boundaries` now consults
`_window_inside_html_string(content, start)` — a cheap string scan —
and if the window is inside a triple-quoted string containing an HTML
block tag, it returns `None` instead of falling through to the Python
statement ranges.  This closes the `(0, 9)` case at its source rather
than relying on the manual fallback to be perfectly correct.

REINDENTING SPLICED BODIES (2026-11-05)
=======================================
`splice_region` reindents a dedented body to match the base region's
first line.  Without this, a body written at column 0 spliced into an
8-space-indented context either fails `ast.parse` (Python) or lands
at column 0 inside an HTML string.  The `inpaint_server.py` run showed
the first case; the pg_base.py run showed the second.  A body that
reopens a wrapper at the wrong column is structurally valid Python
(string contents are opaque) but semantically wrong HTML.

The reindent is guarded: only fires when the body's first line has
zero indentation and the base's first line does not.  A body that
already carries the base's indentation is spliced verbatim.

Each heuristic wires them in identically: build a candidate list from
the AST snap + the pair sweep, try each, return the first valid one.
"""
import re
from pathlib import Path
from typing import Optional


# ── structural validation ──────────────────────────────────────────

def structural_valid(content: str, path: str) -> bool:
    """True iff `content` parses structurally (incl. embedded JS/HTML).

    Empty or unrecognised `path` — which happens when callers invoke
    heuristics directly without a record path, as the test suite does
    — falls back to a generic parse-then-balance check.  Without this,
    the structural gate silently accepts broken content for empty-path
    calls, defeating the AST-snap that's supposed to reject off-by-one
    splices.
    """
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        return _py_valid(content, path)
    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        return _js_valid(content, path, ext)
    if ext == ".html":
        return _html_valid(content, path)
    # Unknown / empty extension: try Python parse first (most content
    # is Python or Python-containing), then JS balance.  A `.py` snippet
    # with apostrophes in `#` comments would pass `ast.parse` but fail
    # `_js_balance`; the reverse is true for a JS snippet containing
    # `#`, but that's rare.
    import ast
    import textwrap
    try:
        ast.parse(textwrap.dedent(content))
        return True
    except SyntaxError:
        pass
    from .validate_output import _js_balance
    b, p, k, u = _js_balance(content)
    return (b, p, k, u) == (0, 0, 0, 0)


def _py_valid(content: str, path: str) -> bool:
    import ast
    try:
        ast.parse(content)
    except SyntaxError:
        return False
    from .validate_output import (
        _iter_triple_quoted, _js_structural_check,
        _html_structural_check, _looks_like_js, _looks_like_html,
    )
    for offset, block in _iter_triple_quoted(content):
        lineno = content.count("\n", 0, offset) + 1
        label = f"{path} (embedded at line {lineno})"
        if _looks_like_html(block):
            if _html_structural_check(block, label) is not None:
                return False
        elif _looks_like_js(block):
            if _js_structural_check(
                    block, label, parse_lang="javascript") is not None:
                return False
    return True


def _js_valid(content: str, path: str, ext: str) -> bool:
    from .validate_output import _js_structural_check, _js_lang_for_ext
    return _js_structural_check(
        content, path, parse_lang=_js_lang_for_ext(ext)) is None


def _html_valid(content: str, path: str) -> bool:
    from .validate_output import _html_structural_check
    return _html_structural_check(content, path) is None


# ── AST-snapped boundaries ─────────────────────────────────────────

def try_ast_snapped_boundaries(content, path, start, end, body=""):
    """Snap `[start, end)` outward to enclosing statement boundaries.

    Returns `(new_start, new_end)` or `None`. For `.py` also considers
    the top-level statements of any dominant embedded JS string and
    any embedded HTML element, so a `.py`-with-playground-JS case snaps
    to the correct JS nodes rather than the (irrelevant) Python wrapper,
    and a `.py`-with-playground-HTML case snaps to the enclosing HTML
    element that matches the patch body's opening tag.

    `body` is optional. When omitted (as in older call sites or tests
    that just exercise the range extraction), only the "min/max of
    every overlapping statement" expansion happens. When provided, the
    embedded-HTML path additionally prefers the innermost matching
    signature.

    THE HTML-FALLTHROUGH GUARD (2026-11-05)
    =======================================
    When `_try_embedded_html_snap` returns `None` but the window is
    still inside a triple-quoted HTML string, we DO NOT fall through
    to `_python_top_level_stmt_ranges`.  Those ranges expand to the
    *whole* `BODY = r\"\"\"...\"\"\"` assignment — the wrong
    granularity — and silently over-replacing it produces a nested
    wrapper.  Fail closed instead; the caller then falls through to
    the insert heuristics rather than over-replacing.

    Motivating case: `test_patch_bounds_html.py::embedded html snap
    falls back to innermost` reported `expected (2, 6) — inner div
    range; got (0, 9)` — the (0, 9) came from
    `_python_top_level_stmt_ranges` on the `BODY = r\"\"\"...\"\"\"`
    statement after `_try_embedded_html_snap` failed to produce a
    snap.
    """
    ext = Path(path).suffix.lower() if path else ""

    # HTML-aware snap: embedded HTML in `.py`, or standalone `.html`.
    # Both use the innermost enclosing element (preferring a signature
    # match when the body opens with an HTML block element).
    if ext == ".py":
        html_result = _try_embedded_html_snap(content, start, end, body)
        if html_result is not None:
            return html_result
        # Guard: if the window is inside an embedded HTML string but
        # no enclosing element was found, return None.  Falling
        # through to Python ranges would over-expand to the whole
        # `BODY = r\"\"\"...\"\"\"` assignment.  See module docstring,
        # THE HTML-FALLTHROUGH GUARD.
        if _window_inside_html_string(content, start):
            return None
    elif ext == ".html":
        html_result = _try_html_snap(
            content, start, end, body,
            _html_top_level_stmt_ranges(content, include_nested=True))
        if html_result is not None:
            return html_result

    stmt_ranges = []
    if ext == ".py":
        stmt_ranges.extend(_python_top_level_stmt_ranges(content))
        stmt_ranges.extend(_embedded_js_top_level_stmt_ranges(content))
    elif ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        stmt_ranges = _js_top_level_stmt_ranges(content, ext)
    if not stmt_ranges:
        return None
    overlapping = [(s, e) for s, e in stmt_ranges
                   if s < end and e > start]
    if not overlapping:
        return None
    new_start = min(s for s, _ in overlapping)
    new_end = max(e for _, e in overlapping)
    if new_start == start and new_end == end:
        return None
    return new_start, new_end


# ── Embedded-HTML range extraction and snap (2026-10-29) ──────────

_HTML_TRIPLE_RE = re.compile(
    r'(?:[rbufRBUF]{0,2})("""|\'\'\')(.*?)\1',
    re.DOTALL,
)

_HTML_BLOCK_TAGS = frozenset({
    "div", "details", "section", "form", "table", "ul", "ol",
    "li", "p", "button", "article", "header", "footer", "nav",
    "main", "aside", "figure", "fieldset", "dl", "blockquote",
})


def _try_html_snap(content, start, end, body, ranges):
    """Snap `[start, end)` to the innermost enclosing HTML element in
    `ranges`.

    Selection:
      1. Restrict to ranges that fully enclose `[start, end)`.
      2. If `body` has an HTML opening tag signature, prefer the
         innermost range whose opening line matches that signature.
      3. Otherwise, pick the innermost enclosing range.

    Returns `(new_start, new_end)` or `None`.
    """
    if not ranges:
        return None

    enclosing = [(s, e) for s, e in ranges if s <= start and e >= end]
    if not enclosing:
        return None

    body_sig = _extract_html_opening_signature(body)
    if body_sig is not None:
        lines = content.splitlines()
        matching = []
        for s, e in enclosing:
            if 0 <= s < len(lines) and _html_signature_matches(
                    lines[s], body_sig):
                matching.append((s, e))
        if matching:
            return min(matching, key=lambda r: r[1] - r[0])

    return min(enclosing, key=lambda r: r[1] - r[0])


def _try_embedded_html_snap(content, start, end, body):
    """Snap `[start, end)` to the enclosing HTML element inside one of
    the file's triple-quoted strings.  Only fires when the window is
    entirely inside one of those strings and that string parses as
    HTML.  See `_try_html_snap` for the selection logic.

    Falls back to `_try_manual_html_snap` when tree-sitter-html is
    not installed (`_embedded_html_top_level_stmt_ranges` returns
    `[]`).  See the module docstring, MANUAL HTML SNAP FALLBACK.
    """
    ranges = _embedded_html_top_level_stmt_ranges(content)
    if ranges:
        return _try_html_snap(content, start, end, body, ranges)
    # tree-sitter-html missing or all strings failed to parse.
    return _try_manual_html_snap(content, start, end, body)


def _try_manual_html_snap(content: str, start: int, end: int,
                          body: str) -> Optional[tuple]:
    """Fallback for when tree-sitter-html is not installed.

    Scans content line-by-line for HTML block-element openings and,
    for each opening, computes its matching close by depth counting.
    Filters candidates to those whose close is on or after `end`.

    Selection (see module docstring, MANUAL HTML SNAP FALLBACK):

      · body_sig present and matches one or more enclosing elements →
        innermost match.
      · body_sig present and matches none → None (fail-closed).
      · body_sig absent → innermost enclosing element.

    Depth starts at 1 (the caller's window is presumed to be inside
    the element being opened) so single-line `<div></div>` resolves
    and nested opens of the same tag close at the correct level.

    Motivating cases:

      · `test_manual_html_snap_none_when_class_mismatch`: signature
        mismatches must NOT snap (fail-closed).
      · `test_patch_bounds_html.py::embedded html snap falls back to
        innermost`: an empty body must still snap to the innermost
        enclosing block element.
      · pg_base.py: `<div class="field" style="...">` matches only
        the outer wrapper, unambiguously.
    """
    lines = content.split("\n")
    if start < 0 or end > len(lines) or start >= end:
        return None

    body_sig = _extract_html_opening_signature(body) if body else None

    # Find every block-element open on or before `start`, compute its
    # matching close by depth, keep those whose close is on or after
    # `end`.  One open per line is enough: HTML block tags do not
    # legitimately repeat on one line in the playground pattern, and
    # if they do (a compact one-liner), the innermost one on that
    # line is the correct target anyway.
    all_open_re = re.compile(
        r'<(' + '|'.join(
            sorted(re.escape(t) for t in _HTML_BLOCK_TAGS)
        ) + r')\b',
        re.IGNORECASE,
    )

    candidates: list = []  # (open_idx, close_idx_exclusive, opening_line)
    for i in range(start, -1, -1):
        for m in all_open_re.finditer(lines[i]):
            tag = m.group(1).lower()
            open_only_re = re.compile(
                r'<' + re.escape(tag) + r'\b', re.IGNORECASE)
            close_re = re.compile(
                r'</' + re.escape(tag) + r'\s*>', re.IGNORECASE)
            # Depth 1 = we are inside the element that just opened.
            # Count opens and closes to the right of the open.
            depth = 1
            close_idx = -1
            for j in range(i, len(lines)):
                line_j = lines[j] if j > i else lines[i][m.end():]
                depth += len(open_only_re.findall(line_j))
                depth -= len(close_re.findall(line_j))
                if depth <= 0:
                    close_idx = j
                    break
            if close_idx < 0:
                continue
            if close_idx < end:
                continue
            candidates.append((i, close_idx + 1, lines[i]))
            # One candidate per line: the innermost match on this line
            # is the correct target.
            break

    if not candidates:
        return None

    if body_sig is not None:
        matching = [
            (s, e) for s, e, line in candidates
            if _html_signature_matches(line, body_sig)
        ]
        if matching:
            return min(matching, key=lambda r: r[1] - r[0])
        # Signature mismatch: fail closed.  Do NOT fall back to
        # innermost — a body whose sig doesn't match any enclosing
        # element is not a safe replacement, and the caller must
        # decide to fail-closed or try another heuristic.  See
        # `test_manual_html_snap_none_when_class_mismatch`.
        return None

    # No signature: fall back to innermost enclosing element.  This
    # is the "falls back to innermost" case from the module
    # docstring.
    return min(
        ((s, e) for s, e, _ in candidates),
        key=lambda r: r[1] - r[0],
    )


def _window_inside_html_string(content: str, line: int) -> bool:
    """True iff `line` falls inside a triple-quoted string whose
    content contains at least one HTML block tag.

    Used by `try_ast_snapped_boundaries` to refuse the Python-statement
    fallback when the window is inside embedded HTML but no enclosing
    HTML element was found.  The Python statement that wraps the
    string (`BODY = r\"\"\"...\"\"\"`) spans the whole string —
    expanding to it is a silent over-replacement that produces a
    nested wrapper.

    Cheap: one regex scan per triple-quoted string, no tree-sitter.
    """
    if not content or line < 0:
        return False
    lines = content.split("\n")
    if line >= len(lines):
        return False

    _str_re = re.compile(
        r'(?:[rbufRBUF]{0,2})(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')',
        re.DOTALL,
    )
    tag_markers = tuple("<" + t for t in _HTML_BLOCK_TAGS)
    for m in _str_re.finditer(content):
        start_line = content.count("\n", 0, m.start())
        end_line = content.count("\n", 0, m.end())
        if not (start_line <= line <= end_line):
            continue
        body = m.group(1) or m.group(2) or ""
        for marker in tag_markers:
            if marker in body:
                return True
    return False


def _embedded_html_top_level_stmt_ranges(content):
    """Return [(start_line, end_line)] for every HTML block element
    (nested or not) inside every triple-quoted string literal.

    Skips very short strings (<60 chars) — those are almost always
    docstrings that happen to mention a tag, not real embedded markup.
    A five-element HTML fragment is ~100 chars; the threshold sits
    just below that so genuine small playground bodies (a `<div>` with
    one `<input>` and one `<span>`, for instance) still parse while a
    one-line `# example: <div>` in a comment does not.
    """
    ranges = []
    for m in _HTML_TRIPLE_RE.finditer(content):
        body = m.group(2)
        if len(body) < 60:
            continue
        inner = _html_top_level_stmt_ranges(body, include_nested=True)
        if not inner:
            continue
        offset = content.count("\n", 0, m.start(2))
        for s, e in inner:
            ranges.append((s + offset, e + offset))
    return ranges


def _html_top_level_stmt_ranges(content, include_nested=False):
    """Return [(start_line, end_line)] for HTML block elements.

    `include_nested=True` returns every block element at any depth
    (outermost and inner alike); `include_nested=False` returns only
    the outermost block elements. The embedded-HTML snap uses nested
    so it can pick a signature match at the correct depth.

    "Block element" is a fixed tag whitelist (`_HTML_BLOCK_TAGS`).
    Inline-only tags (`<span>`, `<a>`, `<input>`, ...) are not ranges;
    the enclosing block is the boundary for a patch that references
    them. Tree-sitter-html missing → empty list.
    """
    try:
        import tree_sitter_html  # type: ignore
        from tree_sitter import Language, Parser
    except ImportError:
        return []
    try:
        lang = Language(tree_sitter_html.language())
        parser = Parser(lang)
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return []
    if tree.root_node.has_error:
        return []

    out = []

    def _walk(node):
        if node.type == "element":
            tag = _html_tag_name(node)
            if tag in _HTML_BLOCK_TAGS:
                out.append(
                    (node.start_point[0], node.end_point[0] + 1))
                if not include_nested:
                    return
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return out


def _html_tag_name(node):
    for child in node.children:
        if child.type in ("start_tag", "self_closing_tag"):
            for c in child.children:
                if c.type == "tag_name":
                    t = c.text
                    if isinstance(t, bytes):
                        t = t.decode("utf-8", "replace")
                    return (t or "").lower()
    return None


def _extract_html_opening_signature(body):
    """Extract `{tag, class?, style?}` from the first non-blank line of
    `body`, or None if it isn't an HTML block-element opening tag.

    The signature captures the tag name plus the exact `class="..."`
    and `style="..."` attribute values, so a body whose opening is
    `<div class="field" style="align-items:center;">` only matches an
    enclosing `<div>` with those same attributes. That's how the
    `pg_base.py` outer-field replacement is distinguished from the
    inner flex `<div>`.
    """
    if not body:
        return None
    first_line = next(
        (ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if not first_line.startswith("<"):
        return None
    m = re.match(r'^<([a-zA-Z][\w-]*)\b', first_line)
    if not m:
        return None
    tag = m.group(1).lower()
    if tag not in _HTML_BLOCK_TAGS:
        return None

    class_m = re.search(
        r'\bclass\s*=\s*["\']([^"\']*)["\']', first_line)
    classes = class_m.group(1) if class_m else None

    style_m = re.search(
        r'\bstyle\s*=\s*["\']([^"\']*)["\']', first_line)
    style = style_m.group(1) if style_m else None

    sig = {"tag": tag}
    if classes is not None:
        sig["class"] = classes
    if style is not None:
        sig["style"] = style
    return sig


def _html_signature_matches(opening_line, sig):
    """True iff `opening_line`'s tag (and captured attributes) match
    `sig`. Extra attributes on `opening_line` are permitted; only the
    ones captured in `sig` are compared."""
    if not opening_line or not sig:
        return False
    m = re.match(r'^\s*<([a-zA-Z][\w-]*)\b', opening_line)
    if not m:
        return False
    tag = m.group(1).lower()
    if tag != sig["tag"]:
        return False
    if "class" in sig:
        class_m = re.search(
            r'\bclass\s*=\s*["\']([^"\']*)["\']', opening_line)
        if not class_m or class_m.group(1) != sig["class"]:
            return False
    if "style" in sig:
        style_m = re.search(
            r'\bstyle\s*=\s*["\']([^"\']*)["\']', opening_line)
        if not style_m or style_m.group(1) != sig["style"]:
            return False
    return True


def _python_top_level_stmt_ranges(content):
    import ast
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if getattr(node, "lineno", None) is None:
            continue
        if getattr(node, "end_lineno", None) is None:
            continue
        # ast lineno/end_lineno are 1-based inclusive; convert to
        # 0-based [start, end_exclusive).
        out.append((node.lineno - 1, node.end_lineno))
    return out


def _js_top_level_stmt_ranges(content, ext):
    from .ast_anchor import _get_parser, _LANG_BY_EXT
    lang = _LANG_BY_EXT.get(ext)
    if lang is None:
        return []
    parser = _get_parser(lang)
    if parser is None:
        return []
    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return []
    if tree.root_node.has_error:
        return []
    return _tree_top_level_ranges(tree.root_node, offset=0)


def _embedded_js_top_level_stmt_ranges(content):
    from .ast_anchor import (
        _get_parser, _TRIPLE_DOUBLE_RE, _TRIPLE_SINGLE_RE,
    )
    parser = _get_parser("javascript")
    if parser is None:
        return []
    out = []
    matches = []
    for regex in (_TRIPLE_DOUBLE_RE, _TRIPLE_SINGLE_RE):
        for m in regex.finditer(content):
            content_start = m.start(1)
            string_content = m.group(1)
            if len(string_content) < 100:
                continue
            matches.append((content_start, string_content))
    for content_start, string_content in matches:
        try:
            tree = parser.parse(string_content.encode("utf-8"))
        except Exception:
            continue
        if tree.root_node.has_error:
            continue
        # The JS content begins on the same file line as the opening
        # `r\"\"\"`; `content_start_line` is the 0-based file line index
        # of that opening quote. JS line 0 is the empty line right
        # after it, which is still file line `content_start_line`.
        content_start_line = content.count("\n", 0, content_start)
        out.extend(_tree_top_level_ranges(
            tree.root_node, offset=content_start_line))
    return out


def _tree_top_level_ranges(root_node, offset):
    out = []
    for child in root_node.children:
        sp = child.start_point
        ep = child.end_point
        file_start = offset + sp[0]
        # tree-sitter end_point is (line, col) exclusive.  The node's
        # last touched line is ep[0] if ep[1] > 0, else ep[0] - 1.
        file_end = offset + ep[0] + (1 if ep[1] > 0 else 0)
        if file_end > file_start:
            out.append((file_start, file_end))
    return out


# ── expansion / contraction candidates ─────────────────────────────

def try_boundary_pairs(start, end, total, max_delta=3):
    """Yield `(start, end)` candidates: original, then ±delta.

    Expansions yielded before contractions — cutting a statement
    short is more likely to leave an orphan than including a
    neighbouring one, so the pipeline prefers expansion when both
    would produce valid content.
    """
    seen = set()

    def _emit(s, e):
        if 0 <= s < e <= total and (s, e) not in seen:
            seen.add((s, e))
            return True
        return False

    if _emit(start, end):
        yield start, end
    for d in range(1, max_delta + 1):
        for (s, e) in ((start - d, end + d),
                       (start - d, end),
                       (start, end + d),
                       (start + d, end),
                       (start, end - d)):
            if _emit(s, e):
                yield s, e


# ── splice + validate ──────────────────────────────────────────────

def splice_region(base, start, end, body_lines, path,
                  min_fraction=0.5) -> Optional[str]:
    """Splice base[start:end] with body_lines, validate, return or None.

    Reindents a dedented body to match the base region's first line —
    see the module docstring, REINDENTING SPLICED BODIES.  Without
    this, a body written at column 0 spliced into an 8-space-indented
    context either fails `ast.parse` (Python) or lands at column 0
    inside an HTML string (both observed in the 2026-11-05 run).
    """
    base_lines = base.splitlines()
    if start < 0 or end > len(base_lines) or start >= end:
        return None

    # Reindent the body to match the base region's first line.  Fires
    # only when the body's first line has zero indent and the base's
    # does not — a body that already carries an indent is spliced
    # verbatim.
    if body_lines and start < len(base_lines):
        first_body = body_lines[0]
        first_base = base_lines[start]
        body_indent = first_body[
            :len(first_body) - len(first_body.lstrip())
        ]
        base_indent = first_base[
            :len(first_base) - len(first_base.lstrip())
        ]
        if body_indent == "" and base_indent != "":
            body_lines = [
                (base_indent + ln) if ln.strip() else ln
                for ln in body_lines
            ]

    new_lines = base_lines[:start] + list(body_lines) + base_lines[end:]
    if len(new_lines) < len(base_lines) * min_fraction:
        return None
    result = "\n".join(new_lines)
    if base.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    if result == base:
        return None
    if not structural_valid(result, path):
        return None
    return result
