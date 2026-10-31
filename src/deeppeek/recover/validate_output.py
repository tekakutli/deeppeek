"""Post-apply structural validation.

(Unchanged module docstring — see prior revision for the full text.
The "REFERENCE-AWARE DECLARATION-PRESENCE CHECK (2026-11-05)" section
is implemented via AST rather than regex; see
`_function_presence_check`'s updated docstring below.)

STRUCTURAL TECHNIQUES (2026-11-05)
==================================
Three regex-based checks have been replaced with AST/tree-sitter
equivalents:

  `_module_level_stmts_added`
      Previously counted `try`/`for`/`while`/`with` lines via a
      column-0 regex.  Now uses `ast.parse` and walks `tree.body`,
      counting `Try`/`For`/`AsyncFor`/`While`/`With`/`AsyncWith`
      nodes.

  `_function_presence_check`
      Previously extracted declared names via `_DECL_PATTERNS`, a
      column-0 regex that missed `export`/`async` prefixes.  Now
      dispatches on path extension:
        * `.py` → `ast.parse` + `tree.body`
        * `.js`/`.ts` → tree-sitter-javascript + `root_node.children`
      Falls back to the regex when tree-sitter is unavailable.

  `_duplicate_top_level_js_decl`
      New helper extracted from `_js_structural_check`.  Prefers
      tree-sitter-javascript; falls back to the column-0 regex when
      the grammar isn't installed.

TREE-REUSE VALIDATION (2026-11-05)
==================================
`validate_content` parses `content` and `base` exactly once each,
then hands the trees to both the module-level-statement check and
the function-presence check.  Prior revisions parsed the same body
twice per check, doubling the per-file cost of every post-patch
validation.  On an `--apply-to` run against a large pre-existing
tree, that doubled parse cost dominated the runtime.

The string-interface wrappers (`_module_level_stmts_added(content,
base)` and `_function_presence_check(base, result, label)`) are
preserved for tests and for the `.js`/`.ts` dispatch path where the
language-appropriate parser lives in `_declared_names`.

DUPLICATE-NESTING FAIL-CLOSED (2026-11-06)
==========================================
`_has_duplicate_class_nesting` is called from two gate sites:

  · `pass6_sanity._check_structural_sanity` — the exact check the
    2026-10-29 pg_base.py run was designed to catch (`<div
    class="field">` nested inside itself).

  · `structural_diff` — reports duplicate nesting as a structural
    divergence.

The pre-fix implementation returned `False` whenever
tree-sitter-html was unavailable, which silently defeated both
gates whenever the running interpreter couldn't import the
grammar.  The 2026-10-30 run's manifest recorded
`in_process: false` for the whole run; every HTML patch in that
run went through the gate with it returning `False` unconditionally.

The check is now three-tiered:

  1. In-process tree-sitter-html.  Precise; returns `None` when
     the grammar is unavailable or the tree doesn't parse.
  2. Repo .venv subprocess.  Same precision, ~200 ms overhead.
     Reached only when tier 1 declines AND the venv has the
     grammar.  Cached on `(html_text)` so repeat calls with the
     same input hit a dict lookup.
  3. Line-based fail-closed fallback.  Less precise; catches the
     exact "element with class=X nested inside another element
     with class=X" shape via a regex + tag-stack sweep.  Chosen
     when both tree-sitter tiers decline, so the gate fails
     closed rather than silently passing the exact corruption it
     was built to reject.

The line-based fallback's regexes (`_DUP_TAG_TOKEN_RE`,
`_DUP_CLASS_ATTR_RE`, `_DUP_VOID_TAGS`) are the only regexes in
this module that participate in a *safety* gate; all other
structural checks are AST/tree-sitter based.  The fallback exists
so a machine without tree-sitter-html (in-process AND in the
repo .venv) still gets a fail-closed gate rather than a silent
bypass.
"""
import ast
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Optional
from ._ast import parse_source


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


def _node_text(node) -> str:
    """Extract a tree-sitter node's text as a string."""
    try:
        t = node.text
    except Exception:
        return ""
    if isinstance(t, bytes):
        return t.decode("utf-8", errors="replace")
    return t or ""


# ---------------------------------------------------------------------------
# Module-level statement regression (Python files) — AST-based
# ---------------------------------------------------------------------------

_TOP_LEVEL_KIND = {
    ast.Try:          "try",
    ast.For:          "for",
    ast.AsyncFor:     "for",
    ast.While:        "while",
    ast.With:         "with",
    ast.AsyncWith:    "with",
}


def _module_level_stmts_added_trees(content_tree, base_tree) -> list:
    """`_module_level_stmts_added`, over pre-parsed trees."""
    def _count(tree):
        counter: Counter = Counter()
        if tree is None:
            return counter
        for node in tree.body:
            for cls, kw in _TOP_LEVEL_KIND.items():
                if isinstance(node, cls):
                    counter[kw] += 1
                    break
        return counter
    after = _count(content_tree)
    before = _count(base_tree)
    return sorted(kw for kw, n in after.items() if n > before.get(kw, 0))


def _module_level_stmts_added(content: str, base: Optional[str]) -> list:
    """Return statement keywords at column 0 whose count increased
    from `base` to `content`.

    AST-based as of 2026-11-05.  The set of statement kinds that
    signal "a patch was appended at module level instead of inside a
    function" is `{try, for, while, with}`.

    String-interface wrapper; delegates to `_module_level_stmts_added_trees`.
    Callers that already have a `content_tree` (from `validate_content`)
    should call the `_trees` variant directly to avoid double parsing.
    """
    try:
        content_tree = parse_source(content)
    except SyntaxError:
        return []
    base_tree = None
    if base:
        try:
            base_tree = parse_source(base)
        except SyntaxError:
            pass
    return _module_level_stmts_added_trees(content_tree, base_tree)


# ---------------------------------------------------------------------------
# Function / declaration presence — AST/tree-sitter-based
# ---------------------------------------------------------------------------

def _declared_names_py(body: str) -> set:
    """Top-level declarations via `ast.parse`.
    """
    try:
        tree = parse_source(body)
    except SyntaxError:
        return set()
    out: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            out.add(node.target.id)
    return out


def _declared_names_js(body: str) -> set:
    """Top-level declarations via tree-sitter-javascript.
    """
    from .ast_anchor import _get_parser
    parser = _get_parser("javascript")
    if parser is None:
        return set()
    try:
        tree = parser.parse(body.encode("utf-8"))
    except Exception:
        return set()
    if tree is None or tree.root_node.has_error:
        return set()

    def _name_of_decl(node) -> Optional[str]:
        if node.type in ("function_declaration",
                         "generator_function_declaration",
                         "class_declaration"):
            for c in node.children:
                if c.type == "identifier":
                    t = _node_text(c)
                    return t or None
        elif node.type in ("lexical_declaration",
                           "variable_declaration"):
            for c in node.children:
                if c.type == "variable_declarator":
                    for cc in c.children:
                        if cc.type == "identifier":
                            t = _node_text(cc)
                            return t or None
        return None

    out: set = set()

    def _walk(node):
        for child in node.children:
            if child.type == "export_statement":
                _walk(child)
                continue
            name = _name_of_decl(child)
            if name:
                out.add(name)

    _walk(tree.root_node)
    return out


def _declared_names(body: str, path: str) -> set:
    """Dispatch on path extension for language-appropriate parsing.
    """
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        return _declared_names_py(body)
    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
        return _declared_names_js(body)
    return _declared_names_py(body) | _declared_names_js(body)


def _declared_names_from_tree(tree) -> set:
    """Top-level Python declarations in `tree`."""
    out: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            out.add(node.target.id)
    return out


def _function_presence_check_trees(base_tree, content_tree,
                                    content_text: str, label: str,
                                    allow_missing: Optional[set] = None,
                                    ) -> Optional[str]:
    """Python tree-reuse variant.  `content_text` is needed for the
    dangling-reference check; passing it avoids re-encoding the tree.
    """
    base_names = _declared_names_from_tree(base_tree)
    if not base_names:
        return None
    result_names = _declared_names_from_tree(content_tree)
    missing = base_names - result_names
    if allow_missing:
        missing = missing - set(allow_missing)
    if not missing:
        return None

    dangling = set()
    for name in missing:
        if re.search(r"\b" + re.escape(name) + r"\b", content_text):
            dangling.add(name)
    if not dangling:
        return None

    listed = sorted(dangling)[:6]
    return (f"{label}: declaration(s) present in base but missing "
            f"from result, and still referenced: {listed}"
            + (" ..." if len(dangling) > 6 else ""))


def _function_presence_check(base: str, result: str,
                              label: str,
                              allow_missing: Optional[set] = None,
                              ) -> Optional[str]:
    """Flag declarations that were removed from `base` AND are still
    referenced in `result`.

    A declaration present in base and absent in result is a violation
    only when the removal created a *dangling reference*.  A rename
    (old name gone, new name present) leaves no dangling reference
    and passes; a clean deletion (removal without remaining references)
    also passes.

    String-interface wrapper.  For `.py` destinations the tree-reuse
    path is preferred; this wrapper exists for tests and for
    `.js`/`.ts` dispatches where the language-appropriate parser
    lives in `_declared_names`.

    `allow_missing` opts specific names out of the check entirely —
    used by callers that have already attributed a declaration's
    disappearance to an explicit deletion patch.
    """
    if not base:
        return None
    ext = Path(label).suffix.lower() if label else ""
    if ext == ".py":
        try:
            base_tree = parse_source(base)
        except SyntaxError:
            return None
        try:
            content_tree = parse_source(result)
        except SyntaxError:
            return None
        return _function_presence_check_trees(
            base_tree, content_tree, result, label,
            allow_missing=allow_missing)
    # JS / TS — dispatch to `_declared_names` (tree-sitter).
    base_names = _declared_names(base, label)
    if not base_names:
        return None
    result_names = _declared_names(result, label)
    missing = base_names - result_names
    if allow_missing:
        missing = missing - set(allow_missing)
    if not missing:
        return None
    dangling = set()
    for name in missing:
        if re.search(r"\b" + re.escape(name) + r"\b", result):
            dangling.add(name)
    if not dangling:
        return None
    listed = sorted(dangling)[:6]
    return (f"{label}: declaration(s) present in base but missing "
            f"from result, and still referenced: {listed}"
            + (" ..." if len(dangling) > 6 else ""))


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

_DECL_KEYWORD_LINE_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?"
    r"(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _duplicate_top_level_js_decl_regex(text: str) -> Optional[str]:
    """Fallback: regex-based duplicate detection at column 0.

    Matches the shape the current regex matched.  Retained for when
    tree-sitter-javascript is unavailable (either in-process or via
    the repo .venv), so the check silently degrades rather than
    mis-flagging.
    """
    seen: set = set()
    for name in _DECL_KEYWORD_LINE_RE.findall(text):
        if name in seen:
            return name
        seen.add(name)
    return None


def _duplicate_top_level_js_decl(text: str) -> Optional[str]:
    """Return the first duplicated top-level JS declaration name,
    or None.

    Prefers tree-sitter-javascript.  Falls back to the column-0
    regex when the grammar is unavailable in-process and via the
    repo .venv.  Column-0 anchoring in the regex path is deliberate:
    an indented `export` is a distinct ESM violation caught by
    `_INDENTED_EXPORT_RE`, not a "duplicate top-level declaration".
    """
    parser = _get_js_parser("javascript")
    if parser is None:
        return _duplicate_top_level_js_decl_regex(text)
    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return _duplicate_top_level_js_decl_regex(text)
    if tree is None or tree.root_node.has_error:
        return _duplicate_top_level_js_decl_regex(text)

    def _name_of_decl(node) -> Optional[str]:
        if node.type in ("function_declaration",
                         "generator_function_declaration",
                         "class_declaration"):
            for c in node.children:
                if c.type == "identifier":
                    t = _node_text(c)
                    return t or None
        elif node.type in ("lexical_declaration",
                           "variable_declaration"):
            for c in node.children:
                if c.type == "variable_declarator":
                    for cc in c.children:
                        if cc.type == "identifier":
                            t = _node_text(cc)
                            return t or None
        return None

    seen: set = set()

    def _walk(node) -> Optional[str]:
        for child in node.children:
            if child.type == "export_statement":
                dup = _walk(child)
                if dup:
                    return dup
                continue
            name = _name_of_decl(child)
            if name:
                if name in seen:
                    return name
                seen.add(name)
        return None

    return _walk(tree.root_node)


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

    # 2. duplicate top-level declarations (AST-first).
    dup = _duplicate_top_level_js_decl(text)
    if dup:
        return f"{label}: duplicate top-level declaration '{dup}'"

    # 3. indented `export` (invalid ESM).
    _ie = _INDENTED_EXPORT_RE.search(text)
    if _ie:
        _line_no = text.count("\n", 0, _ie.start()) + 1
        return (f"{label}: line {_line_no}: indented `export` "
                f"(invalid ESM — export must be top-level)")

    # 4. balance check (last resort).
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
# Duplicate-nesting detection (2026-11-05, fail-closed 2026-11-06)
# ---------------------------------------------------------------------------

def _has_duplicate_class_nesting_from_tree(root_node) -> bool:
    """Tree-walking core: True iff any element with `class="X"` has a
    descendant element with the same class.

    Extracted from `_has_duplicate_class_nesting` (2026-11-06) so the
    same logic is reusable from the subprocess helper and stays
    testable without spinning up tree-sitter.
    """
    def _class_of(node):
        for child in node.children:
            if child.type == "start_tag":
                for c in child.children:
                    if c.type == "attribute":
                        name = value = None
                        for ac in c.children:
                            if ac.type == "attribute_name":
                                name = _node_text(ac).lower()
                            elif ac.type == "quoted_attribute_value":
                                for qc in ac.children:
                                    if qc.type == "attribute_value":
                                        value = _node_text(qc)
                        if name == "class" and value:
                            return value
        return None

    def _walk(node, seen):
        if node.type == "element":
            cls = _class_of(node)
            next_seen = seen
            if cls is not None:
                if cls in seen:
                    return True
                next_seen = seen | {cls}
            for child in node.children:
                if _walk(child, next_seen):
                    return True
            return False
        for child in node.children:
            if _walk(child, seen):
                return True
        return False

    return _walk(root_node, frozenset())


def _has_duplicate_class_nesting_ast_only(
        html_text: str) -> Optional[bool]:
    """Return True/False via in-process tree-sitter-html, or None
    when the grammar is unavailable or the tree doesn't parse.

    The `None` return distinguishes "grammar declined" from
    "grammar ran and found no nesting" — the caller uses that
    distinction to decide whether to try the subprocess or the
    line-based fallback.
    """
    if not html_text or not html_text.strip():
        return False
    try:
        import tree_sitter_html  # type: ignore
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        parser = Parser(Language(tree_sitter_html.language()))
        tree = parser.parse(html_text.encode("utf-8"))
    except Exception:
        return None
    if tree is None or tree.root_node.has_error:
        return None
    return _has_duplicate_class_nesting_from_tree(tree.root_node)


@lru_cache(maxsize=128)
def _has_duplicate_class_nesting_subprocess(
        html_text: str) -> Optional[bool]:
    """Run `_has_duplicate_class_nesting_ast_only` in the repo .venv.

    Returns None when the venv has no tree-sitter-html either; the
    caller falls through to the line-based fallback.
    """
    try:
        from .ast_anchor import (
            _find_venv_python, _src_dir, _has_venv_ast,
        )
    except ImportError:
        return None
    if not _has_venv_ast():
        return None
    py = _find_venv_python()
    if py is None:
        return None
    import json as _json
    import subprocess as _subprocess
    payload = _json.dumps({"html": html_text})
    helper = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(_src_dir())!r})\n"
        "from deeppeek.recover.validate_output import (\n"
        "    _has_duplicate_class_nesting_ast_only,\n"
        ")\n"
        "d = json.load(sys.stdin)\n"
        "r = _has_duplicate_class_nesting_ast_only(d['html'])\n"
        "sys.stdout.write(\n"
        "    'null' if r is None else ('true' if r else 'false')\n"
        ")\n"
    )
    try:
        proc = _subprocess.run(
            [str(py), "-c", helper],
            input=payload, capture_output=True, text=True, timeout=15.0,
        )
    except (_subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


_TAG_TOKEN_RE = re.compile(
    r"<(/?)([A-Za-z][\w-]*)"
    r"((?:\s+[^\s>/=]+"
    r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*)"
    r"\s*(/?)>"
)
_CLASS_ATTR_RE = re.compile(r"\bclass\s*=\s*[\"']([^\"']*)[\"']")
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


def _has_duplicate_class_nesting_linebased(html_text: str) -> bool:
    """Line-based fallback: detect the specific failure shape.

    Detects the pattern the motivating failure produced: an element
    with `class="X"` nested inside another element that also has
    `class="X"`.

    Errs on the side of detection — false positives trigger a
    structural rejection; false negatives would let corruption
    through.  The fallback exists so a machine without
    tree-sitter-html (in-process AND in the repo .venv) still gets a
    fail-closed gate rather than a silent bypass.

    Not a full HTML parser.  Mismatched nesting is not detected,
    CDATA sections are not handled, script/style content is treated
    as text.  That is the contract: fail-closed on the exact shape
    the gate exists for, permissive about everything else.
    """
    if not html_text:
        return False
    stack: list = []
    for m in _TAG_TOKEN_RE.finditer(html_text):
        is_close = m.group(1) == "/"
        tag = m.group(2).lower()
        attrs = m.group(3) or ""
        self_close = bool(m.group(4))
        if is_close:
            if stack:
                stack.pop()
            continue
        if tag in _VOID_TAGS or self_close:
            continue
        cm = _CLASS_ATTR_RE.search(attrs)
        cls = cm.group(1) if cm else None
        if cls is not None and cls in stack:
            return True
        stack.append(cls)
    return False


def _has_duplicate_class_nesting(html_text: str) -> bool:
    """True iff any element with class="X" contains a descendant
    element with the same class="X".

    Three tiers, tried in order (2026-11-06):

      1. In-process tree-sitter-html.  Precise; returns None when
         the grammar is unavailable or the tree doesn't parse.
      2. Repo .venv subprocess.  Same precision, ~200 ms overhead.
         Reached only when (1) declines AND the venv has the
         grammar.  Cached on `(html_text)`.
      3. Line-based fail-closed fallback.  Less precise, catches
         the exact shape the gate exists for.

    Pre-fix, tier 1 returned False when unavailable, so the gate
    silently passed the exact corruption it was built to reject.
    The 2026-10-30 run's manifest recorded `in_process: false` for
    every HTML patch — the gate was a no-op for the entire run.
    """
    if not html_text or not html_text.strip():
        return False
    tier1 = _has_duplicate_class_nesting_ast_only(html_text)
    if tier1 is not None:
        return tier1
    tier2 = _has_duplicate_class_nesting_subprocess(html_text)
    if tier2 is not None:
        return tier2
    return _has_duplicate_class_nesting_linebased(html_text)


def _extract_embedded_html(py_content: str) -> str:
    """Concatenate every triple-quoted string in a .py file that
    looks like HTML.
    """
    if not py_content:
        return ""
    chunks = []
    for _offset, block in _iter_triple_quoted(py_content):
        if _looks_like_html(block):
            chunks.append(block)
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Extract embedded triple-quoted strings
# ---------------------------------------------------------------------------


_TRIPLE_DQ_RE = re.compile(
    r'(?:[rbufRBUF]{0,2})"""(.*?)"""', re.DOTALL)
_TRIPLE_SQ_RE = re.compile(
    r"(?:[rbufRBUF]{0,2})'''(.*?)'''", re.DOTALL)


def _iter_triple_quoted(content: str):
    """Yield `(content_start, content)` for every triple-quoted
    string literal in `content`.

    Fast-path: a 3-byte `in` check against triple quotes that
    short-circuits the majority of `.py` files (which have no
    triple-quoted string at all) before the regex scan runs.
    Precompiled regexes avoid the per-call `re.finditer` pattern
    cache lookup that the previous version paid on every call.
    """
    if '"""' not in content and "'''" not in content:
        return
    for m in _TRIPLE_DQ_RE.finditer(content):
        yield m.start(1), m.group(1)
    for m in _TRIPLE_SQ_RE.finditer(content):
        yield m.start(1), m.group(1)


def _looks_like_js(text: str) -> bool:
    """True iff `text` contains enough JS-specific code signals to
    justify running the JS parser on it.
    """
    if not text:
        return False

    if re.search(r'\bfunction\s+[A-Za-z_$]\w*\s*\(', text):
        return True
    if re.search(r'\b(?:const|let|var)\s+[A-Za-z_$]\w*\s*=\s*'
                 r'(?:function|\{|\[|document\.|new\s|async\b)',
                 text):
        return True

    signals = 0
    if re.search(r"\bexport\s+(?:function|const|class|default)\b", text):
        signals += 2
    if re.search(r"\bconst\s+[A-Za-z_$]\w*\s*=", text):
        signals += 1
    if re.search(r"\b(?:let|var)\s+[A-Za-z_$]\w*\s*=", text):
        signals += 1
    if "=>" in text:
        signals += 1
    if re.search(r"\bdocument\.[A-Za-z_$]\w*\s*\(", text):
        signals += 1
    if re.search(r"\bwindow\.[A-Za-z_$]\w*\s*\(", text):
        signals += 1
    return signals >= 3


def _looks_like_html(text: str) -> bool:
    """True iff `text` contains at least one HTML block or inline tag
    from a conservative common-tag whitelist.

    Used to gate structural-HTML checks (tag balance, duplicate class
    nesting, signature extraction) so they run on embedded HTML rather
    than on arbitrary JS/string content.

    Why the list includes `<div>`: the pg_base.py motivating case
    (2026-11-05) is a `<div class="field">` nested inside another
    `<div class="field">` inside a Python raw string.  The previous
    narrow check (`<details>`/`<button>` only) silently skipped it —
    `_check_structural_sanity` never saw the duplicate, the malformed
    result shipped, and every downstream gate reported success.  The
    whole class of `<div>`-based nesting corruption was invisible.

    The list stays whitelist-shaped (specific strings, not a `<[a-z]`
    regex) to keep the false-positive surface bounded: an arbitrary
    string with a bare `<` is not HTML; a string with `<div` almost
    certainly is.
    """
    return (
        "<details" in text or "</details>" in text
        or "<button" in text or "</button>" in text
        or "<div" in text or "</div>" in text
        or "<span" in text or "</span>" in text
        or "<input" in text
        or "<section" in text or "<form" in text
        or "<table" in text or "<ul" in text or "<ol" in text
        or "<details " in text   # catches `<details class=...` too
    )


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
# Public entry point — parses each tree exactly once
# ---------------------------------------------------------------------------

def validate_content(content: str, path: str,
                     base: Optional[str] = None,
                     allow_missing: Optional[set] = None,
                     ) -> Optional[str]:
    """Return None if content is structurally OK, else an error string.

    As of 2026-11-05, `.py` validation parses `content` and `base`
    exactly once each, then hands the trees to both the
    module-level-statement check and the function-presence check.
    Prior revisions parsed the same body twice per check, doubling
    the per-file cost of every post-patch validation.
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
            content_tree = parse_source(content)
        except SyntaxError as e:
            return (f"{path}: python SyntaxError at line {e.lineno}: "
                    f"{e.msg}")

        base_tree = None
        if base:
            try:
                base_tree = parse_source(base)
            except SyntaxError:
                base_tree = None

        if base_tree is not None:
            added_stmts = _module_level_stmts_added_trees(
                content_tree, base_tree)
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

        if base_tree is not None:
            return _function_presence_check_trees(
                base_tree, content_tree, content, path,
                allow_missing=allow_missing)
        return None

    return None


# ============================================================================
#  Duplicate-block deduplication
# ============================================================================
#
# (Everything below is unchanged from the prior revision; the module
# compiles without modification.)

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


def _js_scope_for_pos(content: str, path: str, pos: int) -> tuple:
    """Return `(region_start, region_end, scope_open)` for `pos`."""
    ext = Path(path).suffix.lower() if path else ""
    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        _, scope_open = _enclosing_scope_depth(content, pos)
        return (0, len(content), scope_open)
    if ext == ".py":
        spans = _python_string_spans(content)
        for s, e in spans:
            if s <= pos < e:
                inner = content[s:e]
                inner_pos = pos - s
                _, scope_open = _enclosing_scope_depth(inner, inner_pos)
                return (s, e, scope_open)
        return (pos, pos, -1)
    _, scope_open = _enclosing_scope_depth(content, pos)
    return (0, len(content), scope_open)


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
            spans.append((m.start(1), m.end(1)))
    spans.sort()
    return spans


def _same_python_string(pos_a: int, pos_b: int, spans: list) -> bool:
    for start, end in spans:
        if start <= pos_a < end and start <= pos_b < end:
            return True
    return False


def _find_duplicate_string_calls(
        content: str,
        is_python: bool,
        py_spans: list,
        path: str = "") -> list:
    """Return [(start_char, end_char, desc)] for duplicate
    `name("key", …)` registrations.
    """
    seen: dict = {}
    removals: list = []

    for m in _STRING_LITERAL_CALL_RE.finditer(content):
        if is_python:
            scope_start, scope_end, js_scope = _js_scope_for_pos(
                content, path, m.start())
            if scope_start == scope_end:
                continue
            scope_key = (scope_start, scope_end, js_scope)
        else:
            _d, scope_open = _enclosing_scope_depth(content, m.start())
            scope_key = scope_open

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

        key = (len(m.group("indent")),
               m.group("name"),
               m.group("key"),
               scope_key)

        if key in seen:
            prev_start, _prev_end = seen[key]
            if m.start() - prev_start > 4000:
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
    """Remove duplicate-applied blocks.  Returns (new_content, n_removed).
    """
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
                if (_js_scope_for_pos(content, path, prev_start)
                        != _js_scope_for_pos(content, path, m.start())):
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
                if (_js_scope_for_pos(content, path, prev_start)
                        != _js_scope_for_pos(content, path, m.start())):
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
                    if (_js_scope_for_pos(content, path, _prev_start)
                            != _js_scope_for_pos(
                                content, path, _m.start())):
                        _seen_decl[_key] = (_m.start(), _end)
                        continue
                removals.append((_m.start(), _end,
                                 _kind + " " + _name))
                continue
            _seen_decl[_key] = (_m.start(), _end)

    removals.extend(
        _find_duplicate_string_calls(content, is_python, py_spans, path))

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
            region_start, region_end, scope_open = _js_scope_for_pos(
                content, path, line_starts[i])
        except Exception:
            continue
        key = (indent, s)
        candidates.setdefault(key, []).append(
            (i, region_start, region_end, scope_open))

    removals = []
    for key, occs in candidates.items():
        if len(occs) != 2:
            continue
        (i1, r1_start, r1_end, scope1) = occs[0]
        (i2, r2_start, r2_end, scope2) = occs[1]
        if r1_start != r2_start or r1_end != r2_end:
            continue
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
