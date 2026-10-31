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

  · `splice_region(...)` — do the splice, validate, return a
    `SpliceResult` distinguishing "changed", "noop", and "invalid".

SPLICE RESULT (2026-11-06)
==========================
`splice_region` previously returned `str | None`, conflating two
distinct outcomes:

  · "the splice is a no-op — the base already contains this content"
  · "the splice is invalid — the resulting content does not parse"

Both returned `None`. Callers (`_apply_scoped_single`,
`_try_region_replace_lines`, `_try_replace_intent`, ...) treated
`None` as "try the next candidate". When all candidates are no-ops,
they fell through to `_diagnose_scoped_failure` and recorded the
patch `failed`. That is a false failure: the content was already
applied.

The fix is a `SpliceResult` with an explicit `status` field. Callers
now distinguish "already applied" (return `base`) from "genuinely
failed" (return `None`). This is the single largest source of the
2026-10-30 run's false failures.

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

MANUAL HTML SNAP FALLBACK (2026-11-05, revised)
===============================================
Tree-sitter-html is optional.  When it is not installed, the
tree-sitter-based `_embedded_html_top_level_stmt_ranges` returns `[]`,
and `_try_embedded_html_snap` needs a text-only path.  Two failure
modes motivated the revision:

  · First version of `_try_manual_html_snap` required `body` to have
    an HTML opening signature. A body with no signature (e.g. prose,
    or an empty body) returned `None` even when the window was
    clearly inside an HTML block element.

  · When `_try_embedded_html_snap` returned `None` for that case,
    `try_ast_snapped_boundaries` fell through to
    `_python_top_level_stmt_ranges`, which expanded `[start, end)` to
    the *whole* `BODY = r\"\"\"...\"\"\"` assignment.

Revised behaviour, three tiers:

  1. Signature present, matches one or more enclosing elements.
     Return the innermost match.
  2. Signature present, matches no enclosing element. Return `None`
     (fail-closed).
  3. No signature. Return the innermost enclosing range.

REINDENTING SPLICED BODIES (2026-11-05)
=======================================
`splice_region` reindents a dedented body to match the base region's
first line.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional
from ._ast import parse_source


# ===========================================================================
#  SpliceResult — the distinct-outcome type
# ===========================================================================

@dataclass
class SpliceResult:
    """Outcome of a `splice_region` call.

    `content` is the new file content when `status == "changed"` or
    `"noop"`, and `None` when `status == "invalid"`.

    `status` is one of:
      · "changed" — new bytes, validated
      · "noop"    — splice produced the same content as base; the
                    base already contains what the patch declared
      · "invalid" — the candidate splice did not parse / was
                    structurally rejected
    """
    content: Optional[str]
    status: Literal["changed", "noop", "invalid"]

    def is_changed(self) -> bool:
        return self.status == "changed"

    def is_noop(self) -> bool:
        return self.status == "noop"

    def is_invalid(self) -> bool:
        return self.status == "invalid"


# ── structural validation ──────────────────────────────────────────

def structural_valid(content: str, path: str) -> bool:
    """True iff `content` parses structurally (incl. embedded JS/HTML).

    Empty or unrecognised `path` — which happens when callers invoke
    heuristics directly without a record path, as the test suite does
    — falls back to a generic parse-then-balance check.
    """
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        return _py_valid(content, path)
    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        return _js_valid(content, path, ext)
    if ext == ".html":
        return _html_valid(content, path)
    import ast
    import textwrap
    try:
        parse_source(textwrap.dedent(content))
        return True
    except SyntaxError:
        pass
    from .validate_output import _js_balance
    b, p, k, u = _js_balance(content)
    return (b, p, k, u) == (0, 0, 0, 0)


def _py_valid(content: str, path: str) -> bool:
    import ast
    try:
        parse_source(content)
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

    Returns `(new_start, new_end)` or `None`.
    """
    ext = Path(path).suffix.lower() if path else ""

    if ext == ".py":
        html_result = _try_embedded_html_snap(content, start, end, body)
        if html_result is not None:
            return html_result
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


# ── Embedded-HTML range extraction and snap ────────────────────────

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
    ranges = _embedded_html_top_level_stmt_ranges(content)
    if ranges:
        return _try_html_snap(content, start, end, body, ranges)
    return _try_manual_html_snap(content, start, end, body)


def _try_manual_html_snap(content: str, start: int, end: int,
                          body: str) -> Optional[tuple]:
    lines = content.split("\n")
    if start < 0 or end > len(lines) or start >= end:
        return None

    body_sig = _extract_html_opening_signature(body) if body else None

    all_open_re = re.compile(
        r'<(' + '|'.join(
            sorted(re.escape(t) for t in _HTML_BLOCK_TAGS)
        ) + r')\b',
        re.IGNORECASE,
    )

    candidates: list = []
    for i in range(start, -1, -1):
        for m in all_open_re.finditer(lines[i]):
            tag = m.group(1).lower()
            open_only_re = re.compile(
                r'<' + re.escape(tag) + r'\b', re.IGNORECASE)
            close_re = re.compile(
                r'</' + re.escape(tag) + r'\s*>', re.IGNORECASE)
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
        return None

    return min(
        ((s, e) for s, e, _ in candidates),
        key=lambda r: r[1] - r[0],
    )


def _window_inside_html_string(content: str, line: int) -> bool:
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
        tree = parse_source(content)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if getattr(node, "lineno", None) is None:
            continue
        if getattr(node, "end_lineno", None) is None:
            continue
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
        file_end = offset + ep[0] + (1 if ep[1] > 0 else 0)
        if file_end > file_start:
            out.append((file_start, file_end))
    return out


# ── expansion / contraction candidates ─────────────────────────────

def try_boundary_pairs(start, end, total, max_delta=3):
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
                  min_fraction=0.5) -> SpliceResult:
    """Splice base[start:end] with body_lines, validate, and return
    a `SpliceResult` distinguishing the three outcomes.

    The caller decides what to do with each:
      · "changed"  — return content
      · "noop"     — return base (already present)
      · "invalid"  — try next candidate or fail
    """
    base_lines = base.splitlines()
    if start < 0 or end > len(base_lines) or start >= end:
        return SpliceResult(None, "invalid")

    # Reindent the body to match the base region's first line.
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
        return SpliceResult(None, "invalid")
    result = "\n".join(new_lines)
    if base.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    if result == base:
        return SpliceResult(base, "noop")
    if not structural_valid(result, path):
        return SpliceResult(None, "invalid")
    return SpliceResult(result, "changed")
