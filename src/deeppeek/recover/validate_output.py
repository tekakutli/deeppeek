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

     IMPORTANT (Fix for pg_pixels.py): the declaration regex MUST
     handle the `export` / `async` prefixes.  The pre-fix version
     matched only `^(?:const|let|var|function|class)\\s+…`, so
     `export function foo() {…}` was never counted — the pg_pixels.py
     failure slipped through with two `export function
     hitTestMaskAtPoint` declarations in the same file.  This module
     now matches `^(?:export\\s+)?(?:async\\s+)?(?:const|let|var|function|class)\\s+…`.

  4. `<details>` element count regression - for HTML content inside
     `.py` strings.

  5. JavaScript parse validation — when tree-sitter + a matching
     grammar are available (the `[ast]` extra).  Runs FIRST: a
     tree-sitter error carries a line/col location, which is what
     the user actually needs; `_js_balance`'s "unclosed string
     literal" is a last-resort fallback when tree-sitter is
     unavailable.

  6. Module-level statement regression - for Python files.

TREE-SITTER-FIRST WITH BALANCE HINT (2026-10-29)
================================================
A pure tree-sitter-first reorder regressed `try_repair`, which gates
on the diagnostic text `unbalanced braces (+N)` to decide whether to
suggest a missing-closer repair.  Tree-sitter reports a parse error
with a location but no brace count, so `_BRACE_FAILURE_RE` inside
`repair.py` stopped matching and the repair suggestion stopped being
produced (fixture 28).

The fix: when tree-sitter reports an error, still compute
`_js_balance` and append the balance diagnosis as a parenthetical
hint.  Both properties hold simultaneously:

  · The primary diagnostic is a tree-sitter error with a location
    ("parse error at line N, col M"), which is what the user needs
    to see for a general structural failure.

  · The balance hint (`unbalanced braces (+1)`) is a substring of
    the message, so downstream consumers that gate on the balance
    shape — `repair.try_repair`, and any future consumer that keys
    on the same signal — continue to work.

DUPLICATE STRING-LITERAL REGISTRATION DEDUP (2026-11-05)
========================================================
`_dedup_duplicate_blocks` catches duplicate `if (cond) { … }`
blocks with identical bodies, and duplicate `const X = …` at column
0.  It does NOT catch duplicate function CALLS that follow the
common registration pattern:

    pgkitRegisterField("view.rotation", { … });
    pgkitRegisterField("view.rotation", { … });  // second copy

    pgkitRegisterAction("action.inpaint", { … });
    pgkitRegisterAction("action.inpaint", { … });

The first registration installs a listener; the second overwrites
the registry entry but does NOT remove the first listener's side
effects.  Both fire.  Result: duplicate events, doubled work,
diagnostic noise.

Motivating failure (2026-10-29 pg_panel_scrubs.py run): a
replace-intent patch that should have REPLACED the rotation section
was instead APPENDED after `pgkitRegisterField("status.running", …)`
(because the anchor lines were filtered).  The result contained two
`pgkitRegisterField("view.rotation", …)` calls, two
`if (rotationEl) { … }` blocks, and an unused `_syncRotationWidget`.

`_find_duplicate_string_calls` catches the registration pattern
generically: any top-level call `name("string", …)` that appears
twice with the same `name` and same `"string"` key, within the same
enclosing Python string (for `.py` destinations — the playground
pattern), or anywhere in the file (for `.js` destinations).
"""
import ast
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional


_LAST_DEDUP_DETAILS = []

_LAST_PARSE_METHOD = "none"


_JS_EXTS = frozenset({".js", ".mjs", ".jsx", ".ts", ".tsx"})
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
    """Return (braces, parens, brackets, strings_unclosed)."""
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
# Module-level statement regression (Python files)
# ---------------------------------------------------------------------------

_MODULE_STMT_KEYWORDS = ("try", "except", "finally", "for", "while", "with")

_TOP_LEVEL_STMT_RE = re.compile(
    r"^(?:" + "|".join(_MODULE_STMT_KEYWORDS) + r")\b",
    re.MULTILINE,
)


def _module_level_stmts_added(content: str, base: Optional[str]) -> list:
    """Return statement keywords at column 0 whose count increased
    from `base` to `content`."""
    def _count(text):
        counter = Counter()
        if not text:
            return counter
        for m in _TOP_LEVEL_STMT_RE.finditer(text):
            if text.rfind("\n", 0, m.start()) + 1 == m.start():
                counter[m.group(0)] += 1
        return counter
    after = _count(content)
    before = _count(base or "")
    added = []
    for kw, n in after.items():
        if n > before.get(kw, 0):
            added.append(kw)
    return sorted(added)


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
    unless it is listed in `allow_missing`."""
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
# tree-sitter helpers (optional)
# ---------------------------------------------------------------------------

def _get_js_parser(lang: str):
    try:
        from .ast_anchor import _get_parser
    except ImportError:
        return None
    try:
        return _get_parser(lang)
    except Exception:
        return None


def _js_parse_check_subprocess(text: str, lang: str) -> Optional[str]:
    try:
        from .ast_anchor import _find_venv_python, _src_dir, _has_venv_ast
    except ImportError:
        return None
    if not _has_venv_ast():
        return None
    py = _find_venv_python()
    if py is None:
        return None
    import json
    import subprocess
    payload = json.dumps({"text": text, "lang": lang})
    helper = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(_src_dir())!r})\n"
        "from deeppeek.recover.ast_anchor import _get_parser\n"
        "data = json.load(sys.stdin)\n"
        "parser = _get_parser(data['lang'])\n"
        "if parser is None:\n"
        "    sys.stdout.write(json.dumps({'available': False}))\n"
        "    sys.exit(0)\n"
        "def _walk(n):\n"
        "    yield n\n"
        "    for c in n.children:\n"
        "        yield from _walk(c)\n"
        "tree = parser.parse(data['text'].encode('utf-8'))\n"
        "if tree.root_node.has_error:\n"
        "    loc = None\n"
        "    for n in _walk(tree.root_node):\n"
        "        if n.type == 'ERROR':\n"
        "            sp = n.start_point\n"
        "            loc = [sp[0] + 1, sp[1] + 1]\n"
        "            break\n"
        "    sys.stdout.write(json.dumps("
        "{'available': True, 'error': True, 'loc': loc}))\n"
        "else:\n"
        "    sys.stdout.write(json.dumps("
        "{'available': True, 'error': False}))\n"
    )
    try:
        proc = subprocess.run(
            [str(py), "-c", helper],
            input=payload, capture_output=True, text=True, timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not result.get("available"):
        return None
    if not result.get("error"):
        return None
    loc = result.get("loc")
    if loc:
        return f"parse error at line {loc[0]}, col {loc[1]}"
    return "parse error"


def _js_lang_for_ext(ext: str) -> Optional[str]:
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

    Runs tree-sitter FIRST when a grammar is available: it returns a
    parse error with a location, which is strictly more useful than
    `_js_balance`'s "unclosed string literal" for a general structural
    failure.  The balance diagnosis is appended as a parenthetical
    hint so that downstream consumers that gate on
    `unbalanced braces (+N)` (`repair.try_repair`) still see the
    signal they depend on.

    Falls back to balance-only when tree-sitter is unavailable, and
    always runs the duplicate-declaration and indented-export checks
    on parseable files.

    Mutates the module-level `_LAST_PARSE_METHOD`.
    """
    global _LAST_PARSE_METHOD

    # 1. tree-sitter first (parse validation with location).
    parse_attempted = False
    if parse_lang is not None:
        parse_attempted = True
        parser = _get_js_parser(parse_lang)
        if parser is None:
            sub_err = _js_parse_check_subprocess(text, parse_lang)
            if sub_err is not None:
                _hint = _balance_hint(text)
                return (f"{label}: JavaScript {sub_err} "
                        f"(via venv tree-sitter){_hint} — likely a "
                        f"patch was inserted where its statement "
                        f"shape does not fit")
            if _LAST_PARSE_METHOD == "none":
                _LAST_PARSE_METHOD = "balance"
        else:
            try:
                tree = parser.parse(text.encode("utf-8"))
                if tree is not None and tree.root_node.has_error:
                    loc = _first_error_location(tree)
                    _hint = _balance_hint(text)
                    if loc is not None:
                        line, col = loc
                        return (f"{label}: JavaScript parse error at "
                                f"line {line}, col {col} (invalid "
                                f"syntax){_hint} — likely a patch was "
                                f"inserted where its statement shape "
                                f"does not fit")
                    return (f"{label}: JavaScript parse error (invalid "
                            f"syntax){_hint} — likely a patch was "
                            f"inserted where its statement shape does "
                            f"not fit")
                _LAST_PARSE_METHOD = "parse"
            except Exception:
                if _LAST_PARSE_METHOD == "none":
                    _LAST_PARSE_METHOD = "balance"
    else:
        if _LAST_PARSE_METHOD == "none":
            _LAST_PARSE_METHOD = "balance"

    # 2. duplicate top-level declarations (valid only on parseable
    #    files; a parse error above already returned).
    #
    # Column-0 declarations only.  JS `const` is block-scoped, so
    # `const res = ...` inside `uploadImageFile` and another
    # `const res = ...` inside `fetchStatus` are distinct bindings,
    # not a duplicate.
    #
    # FIX (pg_pixels.py): `export` / `async` prefixes must be
    # matched.  The pre-fix regex missed both, so `export function
    # foo()` was never counted — two `export function
    # hitTestMaskAtPoint` declarations slipped through undetected.
    decls = re.findall(
        r"^(?:export\s+)?(?:async\s+)?"
        r"(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
        text, re.MULTILINE)
    seen = {}
    for d in decls:
        if d in seen:
            return f"{label}: duplicate top-level declaration '{d}'"
        seen[d] = True

    # 3. indented `export` (invalid ESM).
    _ie = _INDENTED_EXPORT_RE.search(text)
    if _ie:
        _line_no = text.count("\n", 0, _ie.start()) + 1
        return (f"{label}: line {_line_no}: indented `export` "
                f"(invalid ESM — export must be top-level)")

    # 4. balance check (last resort; skipped when tree-sitter
    #    parsed cleanly and no declarations were duplicated).
    braces, parens, brackets, unclosed = _js_balance(text)
    if braces != 0:
        return f"{label}: unbalanced braces ({braces:+d})"
    if parens != 0:
        return f"{label}: unbalanced parens ({parens:+d})"
    if brackets != 0:
        return f"{label}: unbalanced brackets ({brackets:+d})"
    if unclosed:
        return f"{label}: unclosed string literal"

    return None


def _balance_hint(text: str) -> str:
    """Return ` (also: …)` with any balance discrepancies, or `""`.

    Appended to tree-sitter parse errors so that consumers which gate
    on `unbalanced braces (+N)` (notably `repair.try_repair`) keep
    seeing that signal even when the primary diagnosis is a
    tree-sitter error.
    """
    b, p, k, u = _js_balance(text)
    parts = []
    if b != 0:
        parts.append(f"unbalanced braces ({b:+d})")
    if p != 0:
        parts.append(f"unbalanced parens ({p:+d})")
    if k != 0:
        parts.append(f"unbalanced brackets ({k:+d})")
    if u:
        parts.append("unclosed string literal")
    if not parts:
        return ""
    return " (also: " + ", ".join(parts) + ")"


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
    """Return None if content is structurally OK, else an error string."""
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

        if base:
            added_stmts = _module_level_stmts_added(content, base)
            if added_stmts:
                return (f"{path}: module-level statement(s) added at "
                        f"column 0 that weren't in base: {added_stmts}"
                        f" — a patch that should have been inserted "
                        f"into a function was likely appended at "
                        f"module level")

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
                err = _js_structural_check(
                    block, label, parse_lang="javascript")
                if err:
                    return err
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

_IF_OPENER_RE = re.compile(
    r"^([ \t]*)if\s*\((.*)\)\s*(\{)\s*(?://.*)?$", re.MULTILINE)
_MODULE_CONST_RE = re.compile(
    r"^const\s+([A-Za-z_$][\w$]*)\s*=", re.MULTILINE)

_FUNC_DECL_RE = re.compile(
    r"^((?:export\s+)?(?:async\s+)?function\s+"
    r"([A-Za-z_$][\w$]*)\s*\()",
    re.MULTILINE)
_CLASS_DECL_RE = re.compile(
    r"^((?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b)",
    re.MULTILINE)

# NEW (2026-11-05): top-level `name("string-key", { … })` registrations.
# Common patterns: pgkitRegisterField, pgkitRegisterAction, addEventListener
# with a named handler object, custom element registration, etc.
_STRING_LITERAL_CALL_RE = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?P<name>[A-Za-z_$][\w$.]*)\s*\(\s*'
    r'(?P<quote>["\'])(?P<key>[^"\']+)(?P=quote)\s*[,\{]',
    re.MULTILINE,
)


_DEDUP_WINDOW_CHARS = 1500


def _enclosing_scope_depth(content: str, pos: int) -> tuple:
    """Return (depth, last_open_pos) at `pos`."""
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
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def _strip_inline_strings(line: str) -> str:
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


# NEW (2026-11-05): paren matcher for string-literal-call dedup.
def _find_matching_paren(content: str, open_paren_idx: int) -> int:
    if open_paren_idx >= len(content) or content[open_paren_idx] != "(":
        return -1
    depth = 1
    i = open_paren_idx + 1
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
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _has_obvious_duplicates(content: str) -> bool:
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
    # NEW (2026-11-05): string-literal-call registration pattern.
    seen_calls = set()
    for m in _STRING_LITERAL_CALL_RE.finditer(content):
        key = (len(m.group("indent")), m.group("name"), m.group("key"))
        if key in seen_calls:
            return True
        seen_calls.add(key)
    return False


_DEDUP_SAFE_EXTENSIONS = frozenset({
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py",
})


def _python_string_spans(content: str) -> list:
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
    for start, end in spans:
        if start <= pos_a < end and start <= pos_b < end:
            return True
    return False


# NEW (2026-11-05): find duplicate top-level string-literal registrations.
def _find_duplicate_string_calls(
        content: str,
        is_python: bool,
        py_spans: list) -> list[tuple[int, int, str]]:
    """Return [(start_char, end_char, desc)] for duplicate
    `name("key", …)` registrations.

    For `.py` destinations, both occurrences must be inside the SAME
    enclosing triple-quoted string; a duplicate across two different
    embedded-JS strings is an intentional example, not a bug.

    For `.js` destinations, both occurrences must be in the same
    enclosing scope (per `_enclosing_scope_depth`).
    """
    seen: dict[tuple, tuple[int, int]] = {}
    removals: list[tuple[int, int, str]] = []

    for m in _STRING_LITERAL_CALL_RE.finditer(content):
        # Determine enclosing Python string, if relevant.
        enclosing_span = None
        if is_python:
            for span_start, span_end in py_spans or []:
                if span_start <= m.start() < span_end:
                    enclosing_span = (span_start, span_end)
                    break
            if enclosing_span is None:
                # Not inside any Python string literal — the
                # registration pattern is JS-specific, so skip
                # anything outside the embedded JS.
                continue

        open_paren = content.find("(", m.start())
        if open_paren < 0:
            continue
        close_paren = _find_matching_paren(content, open_paren)
        if close_paren < 0:
            continue
        end = close_paren + 1
        if end < len(content) and content[end] == ";":
            end += 1
        if end < len(content) and content[end] == "\n":
            end += 1

        # For non-Python, the scope (enclosing function/block) must
        # match — same as the if-block dedup.
        if is_python:
            scope_key = enclosing_span
        else:
            _d, scope_open = _enclosing_scope_depth(content, m.start())
            scope_key = scope_open

        key = (len(m.group("indent")),
               m.group("name"),
               m.group("key"),
               scope_key)

        if key in seen:
            prev_start, _prev_end = seen[key]
            if m.start() - prev_start > 4000:
                # Far-apart re-registration; treat as intentional.
                seen[key] = (m.start(), end)
                continue
            removals.append((
                m.start(), end,
                f"{m.group('name')}(\"{m.group('key')}\")",
            ))
            continue
        seen[key] = (m.start(), end)
    return removals


def _dedup_duplicate_blocks(content: str, path: str) -> tuple:
    """Remove duplicate-applied blocks.  Returns (new_content, n_removed)."""
    ext = Path(path).suffix.lower()
    if ext not in _DEDUP_SAFE_EXTENSIONS:
        return content, 0
    if not _has_obvious_duplicates(content):
        return content, 0

    is_python = ext == ".py"
    py_spans = _python_string_spans(content) if is_python else None

    removals = []

    seen_cond = {}
    for m in _IF_OPENER_RE.finditer(content):
        cond = re.sub(r"\s+", " ", m.group(2)).strip()
        if not cond:
            continue
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
                if is_python:
                    if not _same_python_string(
                            _prev_start, _m.start(), py_spans):
                        _seen_decl[_key] = (_m.start(), _end)
                        continue
                removals.append((_m.start(), _end,
                                 _kind + " " + _name))
                continue
            _seen_decl[_key] = (_m.start(), _end)

    # NEW (2026-11-05): string-literal-call registration dedup.
    removals.extend(
        _find_duplicate_string_calls(content, is_python, py_spans))

    if not removals:
        return content, 0

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
    """Remove duplicate single-line statements under tight conditions."""
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

def _extract_object_literal_occurrences(content):
    """Return [(var_name, open_pos, close_pos, keys_set)] for every
    top-level object literal in `content`."""
    out = []
    for m in re.finditer(
            r'^(?:[ \t]*)(?:(?:export\s+)?(?:const|let|var)\s+)?'
            r'([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*\{',
            content, re.MULTILINE):
        var_name = m.group(1)
        open_brace = content.find("{", m.start())
        close_brace = _find_matching_close_char(content, open_brace)
        if close_brace == -1:
            continue
        body = content[open_brace + 1:close_brace]
        keys = set()
        for km in re.finditer(
                r'["\']?([A-Za-z_$][\w$]*)["\']?\s*:', body):
            keys.add(km.group(1))
        if keys:
            out.append((var_name, m.start(), close_brace, keys))
    return out


def verify_object_keys_preserved(base, result, path):
    """Return an error string iff a pre-existing top-level object
    literal lost keys in `result`, else None."""
    base_occs   = _extract_object_literal_occurrences(base)
    result_occs = _extract_object_literal_occurrences(result)
    missing = []
    for base_name, _, _, base_keys in base_occs:
        found = False
        for result_name, _, _, result_keys in result_occs:
            if result_name != base_name:
                continue
            if len(base_keys & result_keys) >= 0.8 * len(base_keys):
                found = True
                break
        if not found:
            missing.append(base_name)
    if missing:
        return (f"{path}: pre-existing object literal(s) lost their "
                f"original keys: {sorted(set(missing))[:5]}")
    return None
