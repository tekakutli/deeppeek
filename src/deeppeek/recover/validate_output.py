"""Post-apply structural validation.

`pass8_verify` runs `ast.parse` for `.py` files, but that only
validates the OUTER Python.  Playground and template projects embed
JS/HTML inside raw triple-quoted string literals; the outer Python
parses fine, but the embedded JS can be broken.

This module inspects the actual content the patcher produced and
detects the failure classes that matter for those projects:

  1. Function-presence regression - a function/class/const declared
     in the base is missing from the result.  Catches a scoped-replace
     that deleted its target instead of replacing it.

  2. Brace/paren/bracket balance - for JS content (standalone files
     and embedded inside `.py` strings).  Catches structural appends
     that inserted an unbalanced block.

  3. Duplicate top-level declaration - for JS content.  Catches the
     same block being applied twice.

  4. `<details>` element count regression - for HTML content inside
     `.py` strings.  Catches a scoped replace that silently dropped
     an entire section.

  5. JavaScript parse validation — when tree-sitter + a matching
     grammar is available (the `[ast]` extra), the JS content is
     parsed with a real grammar.  Balance checks cannot detect scope
     errors: an `if (...) { ... }` statement inserted between two
     `key: value,` object members is brace-balanced but
     syntactically invalid.  A parse check catches it.

All checks return None on success, or a one-line string describing
the failure.  Any non-None return means the file must be fail-closed
(LOW trust -> low_trust/) so the target tree is preserved.

PARSE-METHOD TRACKING
=====================
`validate_content` sets the module-level `_LAST_PARSE_METHOD` to one
of:

    "none"    no JS content was checked (nothing to verify)
    "balance" JS was checked with brace-balance only (no parser
              available for this language, or the parse check was
              skipped by design, e.g. .jsx where JSX support varies
              across tree-sitter-javascript versions)
    "parse"   JS was checked with a real grammar

Callers (notably `validate.pass8_verify`) can read this after a
successful call to record the verification method in the manifest.
A manifest that reports `content_valid: "ok"` from a balance-only
check is honest about its limits; one reported from a parse check is
not.  Callers that don't read the flag see no behaviour change.
"""

import ast
import re
import sys
from pathlib import Path
from typing import Optional


# Accumulator that _dedup_repeated_statements
# and _dedup_duplicate_blocks populate so callers
# can record audit trail in the manifest.  Reset by
# the caller before each invocation.
_LAST_DEDUP_DETAILS = []

# Set by validate_content to record how the file was verified.
# "none" / "balance" / "parse".  Callers that want to distinguish
# "verified by a real grammar" from "verified by balance only" read
# this after validate_content returns.  See module docstring.
_LAST_PARSE_METHOD = "none"


_JS_EXTS = frozenset({".js", ".mjs", ".jsx", ".ts", ".tsx"})
# `export` is only legal at module top-level in ESM.  An indented
# `export function X()` or `export const X = ...` is invalid syntax
# and — in the recovery pipeline — is a smoking gun for a nested
# declaration inserted by a mis-firing anchor heuristic.  The
# specific corruption: an ellipsis or context-insert heuristic
# placed `export function buildContextMaskB64() {}` inside the
# existing `buildContextMaskB64()` body.
_INDENTED_EXPORT_RE = re.compile(
    r"^[ \t]+export\s+(?:function|class|const|let|var|default)\b",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _js_balance(text: str) -> tuple:
    """Return (braces, parens, brackets, strings_unclosed).

    Skips content inside strings and comments.  Nonzero means unbalanced.
    """
    braces = parens = brackets = 0
    i, n = 0, len(text)
    in_str = None
    while i < n:
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
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i + 2)
                i = n if j == -1 else j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1
        i += 1
    return braces, parens, brackets, (1 if in_str else 0)


# ---------------------------------------------------------------------------
# Function / declaration presence
# ---------------------------------------------------------------------------

_DECL_PATTERNS = (
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
        re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?class\s+([A-Za-z_$][\w$]*)",
        re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
        re.MULTILINE),
)


def _declared_names(text: str) -> set:
    """Return every declaration name found in text (any language)."""
    out = set()
    for pat in _DECL_PATTERNS:
        for m in pat.finditer(text):
            out.add(m.group(1))
    return out


def _function_presence_check(base: str, result: str,
                              label: str,
                              allow_missing: Optional[set] = None,
                              ) -> Optional[str]:
    """Every top-level name declared in base must appear in result,
    unless it is listed in `allow_missing` (declarations that a
    deletion patch was explicitly instructed to remove)."""
    if not base:
        return None
    base_names = _declared_names(base)
    if not base_names:
        return None
    result_names = _declared_names(result)
    missing = base_names - result_names
    if allow_missing:
        missing = missing - set(allow_missing)
    if missing:
        listed = sorted(missing)[:6]
        return (f"{label}: declaration(s) present in base but missing "
                f"from result: {listed}"
                + (" ..." if len(missing) > 6 else ""))
    return None


# ---------------------------------------------------------------------------
# tree-sitter helpers (optional — no hard dependency)
# ---------------------------------------------------------------------------
#
# The parse-validation layer is a pure improvement: when tree-sitter
# and a matching grammar are importable, the JS content is parsed and
# syntax errors are caught.  When they are not, the existing balance
# checks are the whole story — no behaviour change from pre-parse
# validate_output.
#
# Parser lookup goes through `ast_anchor._get_parser`, which already
# caches per-language parsers in `_parser_cache`.  That cache is the
# reason the probe is cheap even when validate_content calls this once
# per embedded JS string.

def _get_js_parser(lang: str):
    """Return a tree-sitter parser for `lang`, or None.

    `lang` is a tree-sitter language id ("javascript",
    "typescript", "tsx", "python").  Only the JS-family ids are
    passed from this module; python uses ast.parse, not tree-sitter.
    """
    try:
        from .ast_anchor import _get_parser
    except ImportError:
        return None
    try:
        return _get_parser(lang)
    except Exception:
        return None


def _js_lang_for_ext(ext: str) -> Optional[str]:
    """Return the tree-sitter language id to parse `ext` files with,
    or None to skip the parse check.

    `.jsx` is intentionally mapped to None: JSX support varies
    across tree-sitter-javascript versions, and a false parse error
    on a legitimate JSX file is worse than a balance-only check.
    The pg_state.py failure class (a brace-balanced but
    syntactically-invalid fragment in plain JS) is caught by the
    balance checks for `.jsx` too — the only thing skipped is the
    additional parse confirmation.
    """
    if ext == ".ts":
        return "typescript"
    if ext == ".tsx":
        return "tsx"
    if ext in (".js", ".mjs"):
        return "javascript"
    return None


def _iter_ts_nodes(node):
    yield node
    for child in node.children:
        yield from _iter_ts_nodes(child)


def _first_error_location(tree) -> Optional[tuple]:
    """Return 1-based (line, col) of the first tree-sitter ERROR
    node, or None if no ERROR node is found."""
    if tree is None or getattr(tree, "root_node", None) is None:
        return None
    for node in _iter_ts_nodes(tree.root_node):
        if node.type == "ERROR" or getattr(node, "is_error", False):
            try:
                sp = node.start_point
                return (sp[0] + 1, sp[1] + 1)
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# JS structural checks
# ---------------------------------------------------------------------------

def _js_structural_check(text: str, label: str,
                         parse_lang: Optional[str] = None,
                         ) -> Optional[str]:
    """Structural validation for JS content.

    `parse_lang` is the tree-sitter language id to use for a final
    parse-validation pass.  When None, that pass is skipped (only
    balance/indent/duplicate checks run — the pre-parse behaviour).
    When set but the parser is unavailable, the pass is silently
    skipped (no new hard dependency; `_LAST_PARSE_METHOD` records the
    downgrade as "balance").

    Mutates the module-level `_LAST_PARSE_METHOD`.  Callers are
    expected to reset it before calling (validate_content does).
    """
    global _LAST_PARSE_METHOD

    braces, parens, brackets, unclosed = _js_balance(text)
    if braces != 0:
        return f"{label}: unbalanced braces ({braces:+d})"
    if parens != 0:
        return f"{label}: unbalanced parens ({parens:+d})"
    if brackets != 0:
        return f"{label}: unbalanced brackets ({brackets:+d})"
    if unclosed:
        return f"{label}: unclosed string literal"

    # NEW: indented `export` is invalid ESM — `export` must be at
    # module top-level.  This catches the nested-declaration
    # corruption where a scope decl was inserted inside another
    # function body.
    _ie = _INDENTED_EXPORT_RE.search(text)
    if _ie:
        _line_no = text.count("\n", 0, _ie.start()) + 1
        return (f"{label}: line {_line_no}: indented `export` "
                f"(invalid ESM — export must be top-level)")

    # Column-0 declarations only.  JS `const` is block-scoped, so
    # `const res = ...` inside `uploadImageFile` and another
    # `const res = ...` inside `fetchStatus` are distinct bindings,
    # not a duplicate.  Only unindented declarations at the top level
    # of the JS file are true duplicates.  An earlier version of
    # this check used `^\s*`, which flagged every local variable
    # declared twice — the false-positive class that rejected
    # pg_state.py, pg_pixels.py, pg_dispatch.py, pg_panel_bindings.py,
    # pg_view_image.py, pg_comfy_client.py, and pg_krita_client.py
    # on the conversation that revealed the bug.
    decls = re.findall(
        r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
        text, re.MULTILINE)
    seen = {}
    for d in decls:
        if d in seen:
            return f"{label}: duplicate top-level declaration '{d}'"
        seen[d] = True

    # Parse validation — the last line of defence.  Balance checks
    # can verify punctuation; only a grammar can verify that the
    # punctuation is arranged in a valid scope.  The pg_state.py
    # corruption shape (an `if` between two `key: value,` object
    # members) passes every balance check above and fails here.
    if parse_lang is not None:
        parser = _get_js_parser(parse_lang)
        if parser is None:
            # No parser for this language.  Fall back to balance-only;
            # record the downgrade so the manifest can honestly report
            # "ok (balance-only)" rather than "ok".
            if _LAST_PARSE_METHOD == "none":
                _LAST_PARSE_METHOD = "balance"
        else:
            try:
                tree = parser.parse(text.encode("utf-8"))
                if tree is not None and tree.root_node.has_error:
                    loc = _first_error_location(tree)
                    if loc is not None:
                        line, col = loc
                        return (f"{label}: JavaScript parse error at "
                                f"line {line}, col {col} (invalid "
                                f"syntax) — likely a patch was "
                                f"inserted where its statement shape "
                                f"does not fit")
                    return (f"{label}: JavaScript parse error (invalid "
                            f"syntax) — likely a patch was inserted "
                            f"where its statement shape does not fit")
                _LAST_PARSE_METHOD = "parse"
            except Exception:
                # Parser crashed — treat as balance-only so a broken
                # parser doesn't cause false rejections.  Downgrade
                # `_LAST_PARSE_METHOD` only if we haven't already
                # upgraded to "parse" (which can happen when a .py
                # file has multiple embedded JS strings and an
                # earlier one parsed cleanly).
                if _LAST_PARSE_METHOD == "none":
                    _LAST_PARSE_METHOD = "balance"
    else:
        # Caller opted out of parse validation (e.g. .jsx).  Still
        # run the balance checks (already done above); record that
        # the verification was balance-only.
        if _LAST_PARSE_METHOD == "none":
            _LAST_PARSE_METHOD = "balance"

    return None


# ---------------------------------------------------------------------------
# HTML structural checks
# ---------------------------------------------------------------------------

_HTML_TAG_PAIRS = (
    ("details", "</details>", "<details"),
    ("div", "</div>", "<div"),
    ("button", "</button>", "<button"),
)


def _html_structural_check(text: str, label: str) -> Optional[str]:
    for name, close_tag, open_re in _HTML_TAG_PAIRS:
        opens = len(re.findall(re.escape(open_re) + r"\b", text))
        closes = text.count(close_tag)
        if opens != closes:
            return (f"{label}: {opens} <{name}> vs {closes} </{name}>")
    braces, parens, brackets, _ = _js_balance(text)
    if braces != 0:
        return f"{label}: unbalanced braces in embedded HTML ({braces:+d})"
    return None


# ---------------------------------------------------------------------------
# Extract embedded triple-quoted strings
# ---------------------------------------------------------------------------

def _iter_triple_quoted(content: str):
    for m in re.finditer(
            r"(?:[rbufRBUF]{0,2})" + chr(34) * 3
            + r"(.*?)" + chr(34) * 3,
            content, re.DOTALL):
        yield m.start(1), m.group(1)
    for m in re.finditer(
            r"(?:[rbufRBUF]{0,2})" + chr(39) * 3
            + r"(.*?)" + chr(39) * 3,
            content, re.DOTALL):
        yield m.start(1), m.group(1)


def _looks_like_js(text: str) -> bool:
    signals = 0
    if re.search(r"\bexport\s+(?:function|const|class)\b", text):
        signals += 2
    if re.search(r"\bfunction\s+\w+\s*\(", text):
        signals += 1
    if re.search(r"\bconst\s+\w+\s*=", text):
        signals += 1
    if "=>" in text:
        signals += 1
    if "document." in text:
        signals += 1
    return signals >= 2


def _looks_like_html(text: str) -> bool:
    return ("<details" in text or "</details>" in text
            or "<button" in text)


def _looks_like_css(text: str) -> bool:
    """True iff text contains CSS-specific structure that would
    be meaningless or misleading inside a JS body.

    Two patterns cover the observed failure shape and are
    deliberately narrow enough to miss no valid JS:

      1. A CSS ID selector at line start followed by `{`.
         In JS, `#word {` can only appear inside a class body
         as part of `#word() {` (a private method) or as
         `#word = ...` (a private field).  CSS `#maskPromptRow
         textarea {` -- hash-word followed by space and another
         word and `{` -- has no JS interpretation.

      2. Two or more `prop: value;` declarations on one line.
         JS object literals use `,` between entries; `;`
         after every value is CSS-only.
    """
    if not text:
        return False
    if re.search(
        r"^[ \t]*#[A-Za-z][\w-]*(?:\s+[\w-]+)+\s*\{",
        text, re.MULTILINE,
    ):
        return True
    if re.search(
        r"^[ \t]*[\w-]+\s*:\s*[^;{}\n]+;\s*"
        r"[\w-]+\s*:\s*[^;{}\n]+;",
        text, re.MULTILINE,
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_content(content: str, path: str,
                     base: Optional[str] = None,
                     allow_missing: Optional[set] = None,
                     ) -> Optional[str]:
    """Return None if content is structurally OK, else an error string.

    SIDE EFFECT: sets the module-level `_LAST_PARSE_METHOD` to one of
    `"none"`, `"balance"`, or `"parse"`, describing how the JS parts
    of `content` were verified.  Callers that want to record the
    verification method in the manifest read this after the call
    returns.  See module docstring for the semantics of each value.
    """
    global _LAST_PARSE_METHOD
    _LAST_PARSE_METHOD = "none"

    if content is None:
        return f"{path}: content is None"
    if not content.strip():
        return f"{path}: content is empty"

    ext = Path(path).suffix.lower()

    if ext in _JS_EXTS:
        parse_lang = _js_lang_for_ext(ext)
        err = _js_structural_check(content, path, parse_lang=parse_lang)
        if err:
            return err
        if base:
            return _function_presence_check(
                base, content, path, allow_missing=allow_missing)
        return None

    if ext == ".py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            return (f"{path}: python SyntaxError at line {e.lineno}: "
                    f"{e.msg}")

        base_js_texts = []
        if base:
            for _off, _b in _iter_triple_quoted(base):
                if _looks_like_js(_b):
                    base_js_texts.append(_b)
        base_js_css = any(_looks_like_css(b) for b in base_js_texts)

        for offset, block in _iter_triple_quoted(content):
            lineno = _line_of(content, offset)
            label = f"{path} (embedded at line {lineno})"
            if _looks_like_html(block):
                err = _html_structural_check(block, label)
                if err:
                    return err
            elif _looks_like_js(block):
                # Embedded JS is always plain JavaScript (playgrounds
                # don't ship JSX or TS inside Python strings).  Parse
                # with the JS grammar specifically; never fall through
                # to the .jsx opt-out.
                err = _js_structural_check(
                    block, label, parse_lang="javascript")
                if err:
                    return err
                # Cross-language guard: CSS appearing in JS that
                # did not have CSS in base is a strong signal that
                # a block from a different language was applied
                # here.  Reject rather than write corrupted JS.
                if (base_js_texts
                        and not base_js_css
                        and _looks_like_css(block)):
                    return (f"{label}: CSS structure appeared "
                            f"inside embedded JS that had none in "
                            f"base -- cross-language contamination, "
                            f"rejecting")

        if base:
            return _function_presence_check(
                base, content, path, allow_missing=allow_missing)
        return None

    return None


# ---------------------------------------------------------------------------
# Duplicate-block deduplication
# ---------------------------------------------------------------------------
#
# Duplicate application is one of the two ways a conversation with
# overlapping code blocks produces corrupt output (the other is
# truncation, handled by `repair.py`).  The shape:
#
#   · two adjacent source blocks with similar/identical content
#   · both applied — once via `_try_insert_after_anchor`, once via
#     `_try_structural_append`, once via `_try_context_insert`
#   · result contains the same `if (COND) { ... }` guard twice, or
#     the same `const NAME = ...; if (NAME) { ... }` wiring twice
#
# Detected and removed here, before `validate_content` runs.  If the
# deduped content validates, it's written.  If not, the record
# fail-closes as before — dedup never makes the result worse.
#
# Two classes:
#
#   Class A — duplicate `if (COND) { ... }` blocks
#     Same normalized COND, same indent, within 100 lines of each
#     other, bodies overlap after stripping whitespace.  This is the
#     pg_panel_fields.py shape (guard block inserted twice).
#
#   Class B — duplicate `const NAME = ...;` at module scope
#     Same NAME, indent 0.  Removes the second const and any
#     immediately-following `if (NAME) { ... }`.  This is the
#     pg_panel_scrubs.py shape (wiring pair inserted twice).
#
# Both classes are *always* bugs when the bodies overlap — either a
# real user error (JS would reject the duplicate const; duplicate
# if-conditions are redundant) or, in the recovery context, a
# duplicate-insertion bug.  Removing the second is never worse than
# leaving it.

_IF_OPENER_RE = re.compile(
    r"^([ \t]*)if\s*\((.*)\)\s*(\{)\s*(?://.*)?$", re.MULTILINE)
_MODULE_CONST_RE = re.compile(
    r"^const\s+([A-Za-z_$][\w$]*)\s*=", re.MULTILINE)

# Class C: function/class declarations at column 0 (no indent).
# The name capture group is for fast duplicate-detection; the
# full declaration text (header + body) is what actually gets
# compared during dedup.
_FUNC_DECL_RE = re.compile(
    r"^((?:export\s+)?(?:async\s+)?function\s+"
    r"([A-Za-z_$][\w$]*)\s*\()",
    re.MULTILINE)
_CLASS_DECL_RE = re.compile(
    r"^((?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b)",
    re.MULTILINE)

_DEDUP_WINDOW_CHARS = 1500


def _enclosing_scope_depth(content: str, pos: int) -> tuple:
    """Return (depth, last_open_pos) at `pos`.  Strings/comments skipped."""
    depth = 0
    stack = []
    i = 0
    in_str = None
    while i < pos:
        c = content[i]
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
        if c == "/" and i + 1 < pos:
            if content[i + 1] == "/":
                j = content.find("\n", i + 2)
                i = pos if j == -1 or j >= pos else j
                continue
            if content[i + 1] == "*":
                j = content.find("*/", i + 2)
                i = pos if j == -1 or j >= pos else j + 2
                continue
        if c == "{":
            depth += 1
            stack.append(i)
        elif c == "}":
            depth -= 1
            if stack:
                stack.pop()
        i += 1
    return depth, (stack[-1] if stack else -1)



def _norm_lines(s: str) -> str:
    """Whitespace-normalized form: all non-blank lines stripped,
    joined by newlines.  Used to compare two blocks for identity
    regardless of indentation."""
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def _strip_inline_strings(line: str) -> str:
    """Remove single-line quoted strings, JS line comments, and
    single-line JS block comments from a line.  Used for line-based
    brace counting without miscounting braces inside strings or
    comments."""
    out, in_str, i, n = [], None, 0, len(line)
    while i < n:
        c = line[i]
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
        if c == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if c == "/" and i + 1 < n and line[i + 1] == "*":
            j = line.find("*/", i + 2)
            if j != -1:
                i = j + 2
                continue
            break
        out.append(c)
        i += 1
    return "".join(out)
def _find_matching_close_char_linebased(content: str,
                                        open_brace_idx: int) -> int:
    """Line-based brace matcher for Python files with embedded JS.

    Robust against triple-quoted string delimiters by
    processing each line independently — no cross-line string state.
    """
    line_idx_start = content.count("\n", 0, open_brace_idx)
    lines = content.split("\n")
    if line_idx_start >= len(lines):
        return -1
    line_starts = [0]
    for j in range(len(lines) - 1):
        line_starts.append(line_starts[-1] + len(lines[j]) + 1)
    line_start_pos = line_starts[line_idx_start]
    depth = 0
    for i in range(line_idx_start, len(lines)):
        line = lines[i]
        if i == line_idx_start:
            line = line[open_brace_idx - line_start_pos:]
        stripped = _strip_inline_strings(line)
        depth += stripped.count("{") - stripped.count("}")
        if depth == 0 and i > line_idx_start:
            return line_starts[i] + len(lines[i]) - 1
    return -1


def _find_matching_close_char(content: str, open_brace_idx: int,
                              is_python: bool = False) -> int:
    """Return the char index of the `}` matching the `{` at
    `open_brace_idx`, or -1 if unbalanced.  Strings and comments are
    skipped (a `}` inside a string or comment does not close the
    block)."""
    if open_brace_idx >= len(content) or content[open_brace_idx] != "{":
        return -1
    if is_python:
        return _find_matching_close_char_linebased(content, open_brace_idx)
    depth = 1
    i = open_brace_idx + 1
    n = len(content)
    in_str = None
    while i < n:
        c = content[i]
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
        if c == "/" and i + 1 < n:
            if content[i + 1] == "/":
                j = content.find("\n", i + 2)
                i = n if j == -1 else j
                continue
            if content[i + 1] == "*":
                j = content.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _has_obvious_duplicates(content: str) -> bool:
    """Cheap pre-check: any duplicate `const NAME` or `if (COND)`?"""
    seen_consts = set()
    for m in _MODULE_CONST_RE.finditer(content):
        name = m.group(1)
        if name in seen_consts:
            return True
        seen_consts.add(name)
    seen_ifs = set()
    for m in _IF_OPENER_RE.finditer(content):
        key = (len(m.group(1)),
               re.sub(r"\s+", " ", m.group(2)).strip())
        if key in seen_ifs:
            return True
        seen_ifs.add(key)
    # Class C: function/class name duplicates at column 0
    seen_funcs = set()
    for m in _FUNC_DECL_RE.finditer(content):
        if (content.rfind(chr(10), 0, m.start()) + 1
                != m.start()):
            continue
        name = m.group(2)
        if name in seen_funcs:
            return True
        seen_funcs.add(name)
    seen_classes = set()
    for m in _CLASS_DECL_RE.finditer(content):
        if (content.rfind(chr(10), 0, m.start()) + 1
                != m.start()):
            continue
        name = m.group(2)
        if name in seen_classes:
            return True
        seen_classes.add(name)
    return False


# DEDUP-v2: file-type guard + same-string check for .py

_DEDUP_SAFE_EXTENSIONS = frozenset({
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py",
})


def _python_string_spans(content: str) -> list:
    """Return (start, end) spans of every triple-quoted string literal.

    Used to verify that duplicate blocks in embedded JS are inside the
    SAME Python string (a pipeline artifact) rather than in different
    strings (likely documentation examples)."""
    spans = []
    TRIPLE_D = chr(34) * 3
    TRIPLE_S = chr(39) * 3
    for delim in (TRIPLE_D, TRIPLE_S):
        escaped = re.escape(delim)
        pat = re.compile(
            r"(?:[rbufRBUF]{0,2})" + escaped + r"(.*?)" + escaped,
            re.DOTALL,
        )
        for m in pat.finditer(content):
            spans.append((m.start(), m.end()))
    spans.sort()
    return spans


def _same_python_string(pos_a: int, pos_b: int, spans: list) -> bool:
    """True iff both positions are inside the same triple-quoted string."""
    for start, end in spans:
        if start <= pos_a < end and start <= pos_b < end:
            return True
    return False


def _dedup_duplicate_blocks(content: str, path: str) -> tuple:
    """Remove duplicate-applied blocks.  Returns (new_content, n_removed).

    File-type guard: dedup runs only on code files (.js/.mjs/.cjs/
    .jsx/.ts/.tsx/.py).  For documentation-style files (.md, .html,
    .css, .yaml, .json, .toml, .sh, ...) duplicate const/if lines are
    almost always author-written examples, not pipeline artifacts;
    removing them would corrupt the file.  Skipping those extensions
    trades coverage for safety, which is the correct trade-off.

    Within code files:

      A. Duplicate if-blocks (if (COND) { ... }) with identical
         normalized bodies in the same scope, within
         _DEDUP_WINDOW_CHARS.

      B. Duplicate module-scope const declarations (const NAME = ...),
         plus their trailing if (NAME) { ... } blocks, within
         _DEDUP_WINDOW_CHARS.

    For Python files with embedded JS, "same scope" is approximated
    by "same triple-quoted string literal": a duplicate in a different
    string is treated as an author-written example, not a pipeline
    artifact.  This is a heuristic; a same-string before/after example
    would still be collapsed, but cross-string examples are protected.
    """
    ext = Path(path).suffix.lower()
    if ext not in _DEDUP_SAFE_EXTENSIONS:
        return content, 0
    if not _has_obvious_duplicates(content):
        return content, 0

    is_python = ext == ".py"
    py_spans = _python_string_spans(content) if is_python else None

    removals = []

    # ---- Class A: duplicate if-blocks with same condition ----
    seen_cond = {}
    for m in _IF_OPENER_RE.finditer(content):
        cond = re.sub(r"\s+", " ", m.group(2)).strip()
        if not cond:
            continue
        # m.start(3) is the `{` the regex captured directly — robust
        # against trailing `// comment` on the opener line, where
        # m.end() - 1 would point into the comment.
        open_char = m.start(3)
        close_char = _find_matching_close_char(
            content, open_char, is_python=is_python)
        if close_char == -1:
            continue
        end_char = close_char + 1
        if end_char < len(content) and content[end_char] == "\n":
            end_char += 1

        key = (len(m.group(1)), cond)
        if key in seen_cond:
            prev_start, prev_end = seen_cond[key]
            if m.start() - prev_start > _DEDUP_WINDOW_CHARS:
                seen_cond[key] = (m.start(), end_char)
                continue
            body_a = _norm_lines(content[prev_start:prev_end])
            body_b = _norm_lines(content[m.start():end_char])
            if not body_a or body_a != body_b:
                continue
            if is_python:
                if not _same_python_string(prev_start, m.start(), py_spans):
                    continue
            else:
                if (_enclosing_scope_depth(content, prev_start)
                        != _enclosing_scope_depth(content, m.start())):
                    continue
            removals.append((m.start(), end_char,
                             "if-block (cond: " + cond[:50] + ")"))
            continue
        seen_cond[key] = (m.start(), end_char)

    # ---- Class B: duplicate module-scope const NAME = ----
    seen_const = {}
    for m in _MODULE_CONST_RE.finditer(content):
        if m.start() != content.rfind("\n", 0, m.start()) + 1:
            continue
        name = m.group(1)
        line_end = content.find("\n", m.start())
        if line_end == -1:
            line_end = len(content)
        else:
            line_end += 1
        end_char = line_end
        tail = content[line_end:]
        skip = 0
        while skip < len(tail) and tail[skip] in " \t\n":
            skip += 1
        if tail.startswith("if (" + name + ")", skip):
            brace_rel = tail.find("{", skip)
            if brace_rel != -1:
                close_abs = _find_matching_close_char(
                    content, line_end + brace_rel, is_python=is_python)
                if close_abs != -1:
                    end_char = close_abs + 1
                    if (end_char < len(content)
                            and content[end_char] == "\n"):
                        end_char += 1

        if name in seen_const:
            prev_start, _ = seen_const[name]
            if m.start() - prev_start > _DEDUP_WINDOW_CHARS:
                seen_const[name] = (m.start(), end_char)
                continue
            if is_python:
                if not _same_python_string(prev_start, m.start(), py_spans):
                    continue
            removals.append((m.start(), end_char, "const " + name))
            continue
        seen_const[name] = (m.start(), end_char)

    # ---- Class C: duplicate function/class declarations ----
    # Two identical `function NAME(...) { body }` (or `class
    # NAME { body }`) in the same enclosing scope within the
    # window: remove the later one.  Body comparison includes
    # the header line, so different parameter lists are not
    # considered identical.  Column-0 only -- indented
    # declarations are anonymous or nested, and their scope is
    # meaningful even if the header text matches.
    for _pat, _kind in (
            (_FUNC_DECL_RE, "function"),
            (_CLASS_DECL_RE, "class")):
        _seen_decl = {}
        for _m in _pat.finditer(content):
            if (content.rfind(chr(10), 0, _m.start()) + 1
                    != _m.start()):
                continue
            _name = _m.group(2)
            _header_end = content.find(chr(10), _m.end())
            if _header_end == -1:
                _header_end = len(content)
            _open = content.find("{", _m.end(), _header_end)
            if _open == -1:
                continue
            _close = _find_matching_close_char(content, _open)
            if _close == -1:
                continue
            _end = _close + 1
            if _end < len(content) and content[_end] == chr(10):
                _end += 1
            _body = _norm_lines(content[_m.start():_close + 1])
            if not _body:
                continue
            _key = (_kind, _name, _body)
            if _key in _seen_decl:
                _prev_start, _prev_end = _seen_decl[_key]
                if (_m.start() - _prev_start
                        > _DEDUP_WINDOW_CHARS):
                    _seen_decl[_key] = (_m.start(), _end)
                    continue
                if (_enclosing_scope_depth(content, _prev_start)
                        != _enclosing_scope_depth(
                            content, _m.start())):
                    _seen_decl[_key] = (_m.start(), _end)
                    continue
                # Same-string guard: identical functions inside two
                # DIFFERENT embedded string literals of a .py file are
                # author-written duplication (per-string), not a
                # pipeline artifact.  Preserve both.
                if is_python:
                    if not _same_python_string(
                            _prev_start, _m.start(), py_spans):
                        _seen_decl[_key] = (_m.start(), _end)
                        continue
                removals.append((_m.start(), _end,
                                 _kind + " " + _name))
                continue
            _seen_decl[_key] = (_m.start(), _end)

    if not removals:
        return content, 0

    # Merge overlapping removals (Class A and B can both flag the same
    # block; applying overlapping removals in reverse order corrupts
    # the content because positions shift after each removal).
    removals.sort(key=lambda r: r[0])
    merged = [removals[0]]
    for r in removals[1:]:
        prev_start, prev_end, prev_desc = merged[-1]
        if r[0] <= prev_end:
            merged[-1] = (prev_start, max(prev_end, r[1]),
                          prev_desc + " + " + r[2])
        else:
            merged.append(r)
    removals = merged

    global _LAST_DEDUP_DETAILS
    _LAST_DEDUP_DETAILS = [
        {"path": path, "kind": "block", "desc": desc}
        for _rstart, _rend, desc in removals
    ]
    removals.sort(key=lambda r: r[0], reverse=True)
    new_content = content
    for rstart, rend, desc in removals:
        print("  [dedup] " + path + ": removing duplicate " + desc,
              file=sys.stderr)
        new_content = new_content[:rstart] + new_content[rend:]
    return new_content, len(removals)


def _dedup_repeated_statements(content, path, base=None,
                                patch_bodies=None, window=15):
    """Remove duplicate single-line statements under tight conditions:

      1. Code file (not documentation, not CSS-in-py).
      2. Line does NOT appear in `base` -- i.e. it is genuinely new,
         added by the patches, not inherited from the pre-existing
         file.  A legitimate duplicate (e.g. `resetView();` appearing
         in both `setImage` and `clearImage`) is in `base` and is
         therefore skipped.
      3. Line appears in at least one `patch_bodies` entry.  A
         user-authored duplication with no patch provenance is left
         alone.
      4. Exactly two occurrences in the result.
      5. Both in the same enclosing scope (by innermost `{` position).
      6. Within `window` lines of each other.

    Removing one copy of a line that is byte-identical, at the same
    indent, in the same scope, and that the patches added twice yields
    the single-application result the patch text intended.
    """
    from pathlib import Path as _P
    ext = _P(path).suffix.lower()
    if ext not in frozenset({
        ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py",
    }):
        return content, 0

    if not patch_bodies:
        return content, 0

    patch_line_set = set()
    for pb in patch_bodies:
        for line in pb.split(chr(10)):
            s = line.strip()
            if s and s.endswith(";"):
                patch_line_set.add(s)

    if not patch_line_set:
        return content, 0

    base_lines_set = set()
    if base:
        for line in base.split(chr(10)):
            s = line.strip()
            if s:
                base_lines_set.add(s)

    lines = content.split(chr(10))
    line_starts = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line) + 1

    candidates = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or not s.endswith(";"):
            continue
        if "{" in s or "}" in s:
            continue
        if s not in patch_line_set:
            continue
        if s in base_lines_set:
            continue
        indent = len(line) - len(line.lstrip())
        try:
            _, scope_open = _enclosing_scope_depth(content, line_starts[i])
        except Exception:
            continue
        key = (indent, s)
        candidates.setdefault(key, []).append((i, scope_open))

    removals = []
    for key, occs in candidates.items():
        if len(occs) != 2:
            continue
        (i1, scope1), (i2, scope2) = occs
        if scope1 != scope2:
            continue
        if i2 - i1 > window:
            continue
        removals.append((i2, lines[i2].strip()))

    if not removals:
        return content, 0

    remove_set = {i for i, _ in removals}
    out = [line for i, line in enumerate(lines) if i not in remove_set]
    global _LAST_DEDUP_DETAILS
    _LAST_DEDUP_DETAILS = [
        {"path": path, "kind": "stmt", "line": text}
        for _i, text in removals
    ]
    for _i, text in removals:
        print(f"  [dedup-stmt] {path}: removed duplicate line "
              f"{text!r}", file=sys.stderr)
    return chr(10).join(out), len(removals)
