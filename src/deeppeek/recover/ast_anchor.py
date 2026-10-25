"""
recover.ast_anchor — tree-sitter based structural anchoring.

When text heuristics fail because there is no unique string anchor to
insert next to, the AST structure of the target may still unambiguously
determine placement.  This module provides `try_ast_anchor`, one of the
last-resort heuristics.  It is invoked after all text-based inserters
have declined, and it returns a `(line_index, indent, evidence)` tuple
that the caller uses to splice the block into the file.

TWO CONTAINER CASES
===================
Direct
    The block is meant to live at some structural position in the
    file's own AST.  E.g. a `.js` file with a bare `if (x) { ... }`
    statement; find its enclosing function by identifier overlap, or
    insert after the last top-level sibling of the same type.

Embedded
    The block is code *inside a string literal* of the container file.
    E.g. a `.py` file with `PANEL_FIELDS_JS = r""""""` and a JS
    block that belongs inside that string.  This is the common
    "JS/HTML in a Python constant" pattern (playgrounds, template
    systems).  The algorithm:
      1. Regex-scan base for triple-double-quoted and triple-single-
         quoted string literals.
      2. For each, extract its content and try parsing it as the
         block's language.
      3. If the content parses cleanly AND `_ast_insert` finds a
         structural anchor within it, translate the inner line index
         to an outer line index (accounting for the string's offset
         in the file).
      4. If exactly one string literal produces a candidate, return it.
      5. Otherwise return None (fail-closed).

Every insertion is deterministic: if the enclosing-function search
yields multiple candidates with equal overlap, or the block's AST shape
has no matching sibling, we bail.  There is no "best guess".

Depends on the `[ast]` extra: `tree-sitter` + `tree-sitter-javascript`
(+ `tree-sitter-typescript` and `tree-sitter-python` if installed;
each is optional — missing grammars are silently skipped, so the tool
degrades to text-only mode transparently).
"""

from typing import Optional
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LANG_BY_EXT = {
    ".js":  "javascript",
    ".mjs": "javascript",
    ".jsx": "javascript",
    ".ts":  "typescript",
    ".tsx": "tsx",
    ".py":  "python",
}

# Order in which languages are tried when inferring the block's
# language.  More specific (JS) first so a block that could parse as
# both JS and Python is correctly identified as JS.
_LANG_INFER_ORDER = ("javascript", "typescript", "tsx", "python")

# Container languages whose string literals we can scan for embedded code.
# Currently only Python (triple-quoted strings).  JS template literals
# are a future extension.
_CONTAINER_STRING_SCAN = {"python"}

_parser_cache = {}


# ---------------------------------------------------------------------------
# Node type sets (JavaScript)
# ---------------------------------------------------------------------------

_JS_STATEMENT_TYPES = frozenset({
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "try_statement",
    "return_statement",
    "throw_statement",
    "expression_statement",
})

_JS_TOP_LEVEL_DECL_TYPES = frozenset({
    "lexical_declaration",
    "variable_declaration",
    "function_declaration",
    "class_declaration",
    "export_statement",
})

_JS_FUNCTION_TYPES = frozenset({
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
})


# ---------------------------------------------------------------------------
# Embedded-scope regex (Python triple-quoted strings)
# ---------------------------------------------------------------------------
#
# [rbufRBUF]{0,2} matches Python string prefixes (r, b, u, f, rb, br,
# rf, fr, etc.).  Non-greedy .+? stops at the first closing
# triple-quote — the natural string boundary.  If a JS block itself
# contained a triple-quote inside a string literal we would misidentify
# the boundary; that is vanishingly rare and the AST anchoring would
# fail anyway (extracted content wouldn't parse cleanly).

_TRIPLE_DOUBLE_RE = re.compile(  # noqa: F821  (re imported below)
    r'(?:[rbufRBUF]{0,2})"""(.+?)"""',
    re.DOTALL,
)
_TRIPLE_SINGLE_RE = re.compile(
    r"(?:[rbufRBUF]{0,2})'''(.+?)'''",
    re.DOTALL,
)

# Minimum content length to be considered a code-block candidate.
# Below this, the string is almost certainly data, not embedded code.
_MIN_EMBEDDED_LEN = 100


# ---------------------------------------------------------------------------
# Parser loading
# ---------------------------------------------------------------------------

def _get_language(lang_name: str):
    try:
        from tree_sitter import Language
    except ImportError:
        return None
    try:
        if lang_name == "javascript":
            import tree_sitter_javascript as m
            return Language(m.language())
        if lang_name == "typescript":
            import tree_sitter_typescript as m
            return Language(m.language_typescript())
        if lang_name == "tsx":
            import tree_sitter_typescript as m
            return Language(m.language_tsx())
        if lang_name == "python":
            import tree_sitter_python as m
            return Language(m.language())
    except Exception:
        return None
    return None


def _get_parser(lang_name: str):
    if lang_name in _parser_cache:
        return _parser_cache[lang_name]
    lang = _get_language(lang_name)
    if lang is None:
        _parser_cache[lang_name] = None
        return None
    try:
        from tree_sitter import Parser
        try:
            parser = Parser(lang)
        except TypeError:
            parser = Parser()
            parser.set_language(lang)
        _parser_cache[lang_name] = parser
        return parser
    except Exception:
        _parser_cache[lang_name] = None
        return None


def is_available() -> bool:
    """True iff tree-sitter and at least one grammar are importable."""
    return any(_get_parser(n) is not None for n in _LANG_INFER_ORDER)


def _parse(text: str, lang_name: str):
    parser = _get_parser(lang_name)
    if parser is None:
        return None
    try:
        return parser.parse(text.encode("utf-8"))
    except Exception:
        return None


def _parse_any(text: str):
    """Try each supported language.  Return (lang, tree) of the first
    that parses cleanly with no errors, or (None, None)."""
    for lang_name in _LANG_INFER_ORDER:
        tree = _parse(text, lang_name)
        if tree is not None and not tree.root_node.has_error:
            return lang_name, tree
    return None, None


# ---------------------------------------------------------------------------
# AST walking helpers
# ---------------------------------------------------------------------------

def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _node_text(node) -> str:
    try:
        t = node.text
    except Exception:
        return ""
    if isinstance(t, bytes):
        return t.decode("utf-8", errors="replace")
    return t or ""


def _collect_identifiers(root) -> set:
    out = set()
    for n in _walk(root):
        if n.type == "identifier":
            txt = _node_text(n)
            if txt:
                out.add(txt)
    return out


def _collect_declared_identifiers(function_node) -> set:
    declared = set()
    for n in _walk(function_node):
        if n.type == "variable_declarator":
            for c in n.children:
                if c.type == "identifier":
                    declared.add(_node_text(c))
                    break
                if c.type in ("object_pattern", "array_pattern"):
                    for sub in _walk(c):
                        if sub.type == "identifier":
                            declared.add(_node_text(sub))
                    break
        elif n.type in ("formal_parameters", "parameters"):
            for sub in _walk(n):
                if sub.type == "identifier":
                    declared.add(_node_text(sub))
    return declared


def _first_nonerror_child(root):
    for c in root.children:
        if c.type == "ERROR":
            continue
        return c
    return None


def _last_nonerror_child(root):
    for c in reversed(root.children):
        if c.type == "ERROR":
            continue
        return c
    return None


def _is_block_already_present(body: str, container: str) -> bool:
    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not body_lines:
        return True
    container_lines = {ln.strip() for ln in container.splitlines()
                        if ln.strip()}
    return all(ln in container_lines for ln in body_lines)


# ---------------------------------------------------------------------------
# Insertion algorithms
# ---------------------------------------------------------------------------

def _insert_after_last_top_level(content: str, tree, target_type: str):
    """Insert after the last top-level sibling of target_type.

    Returns (line_index, indent, evidence) or None."""
    children = [c for c in tree.root_node.children
                if c.type not in ("comment",)]
    if not children:
        return None

    last = None
    for c in children:
        if c.type == target_type:
            last = c

    if last is None:
        if target_type in _JS_TOP_LEVEL_DECL_TYPES:
            for c in children:
                if c.type in _JS_TOP_LEVEL_DECL_TYPES:
                    last = c
        elif target_type in _JS_STATEMENT_TYPES:
            for c in children:
                if c.type in _JS_STATEMENT_TYPES:
                    last = c

    if last is None:
        return None

    insert_line = last.end_point[0] + 1
    evidence = f"after last top-level {last.type}"
    return (insert_line, "", evidence)


def _insert_in_enclosing_function(content: str, tree, body_first_node):
    """Insert at the end of the function that declares the block's
    local identifiers.  Returns (line_index, indent, evidence) or None."""
    block_idents = _collect_identifiers(body_first_node)
    if not block_idents:
        return None

    candidates = []
    for n in _walk(tree.root_node):
        if n.type in _JS_FUNCTION_TYPES:
            declared = _collect_declared_identifiers(n)
            overlap = block_idents & declared
            if overlap:
                candidates.append((n, len(overlap)))

    if not candidates:
        return None

    candidates.sort(key=lambda x: -x[1])
    if len(candidates) > 1 and candidates[0][1] == candidates[1][1]:
        return None  # ambiguous

    func_node, overlap_count = candidates[0]
    body_node = func_node.child_by_field_name("body")
    if body_node is None:
        return None

    if body_node.start_point[0] == body_node.end_point[0]:
        return None  # single-line body

    first_stmt = None
    last_stmt = None
    close_brace = None
    for c in body_node.children:
        if c.type == "}":
            close_brace = c
            continue
        if c.type == "{":
            continue
        if c.type == "comment":
            continue
        if first_stmt is None:
            first_stmt = c
        last_stmt = c

    if close_brace is None:
        return None

    if first_stmt is not None:
        stmt_start_byte = first_stmt.start_byte
        content_bytes = content.encode("utf-8")
        line_start_byte = content_bytes.rfind(b"\n", 0, stmt_start_byte) + 1
        line_start_char = len(content_bytes[:line_start_byte].decode("utf-8"))
        stmt_start_char = len(content_bytes[:stmt_start_byte].decode("utf-8"))
        indent = content[line_start_char:stmt_start_char]
        if not all(ch in " \t" for ch in indent):
            indent = "  "
    else:
        indent = "  "

    if last_stmt is not None and last_stmt.type == "return_statement":
        insert_line = last_stmt.start_point[0]
    else:
        insert_line = close_brace.start_point[0]

    func_name = ""
    for c in func_node.children:
        if c.type == "identifier":
            func_name = _node_text(c)
            break

    evidence = (f"end of function {func_name or '(anonymous)'} "
                f"(matched {overlap_count} identifier(s))")
    return (insert_line, indent, evidence)


def _ast_insert(content: str, content_tree, body: str, body_tree):
    """Dispatch insertion based on the block's AST shape.

    Returns (line_index, indent, evidence) or None."""
    first = _first_nonerror_child(body_tree.root_node)
    last = _last_nonerror_child(body_tree.root_node)
    if first is None or last is None:
        return None

    if (first.type in _JS_TOP_LEVEL_DECL_TYPES
            or last.type in _JS_TOP_LEVEL_DECL_TYPES):
        return _insert_after_last_top_level(
            content, content_tree, last.type)

    if first.type in _JS_STATEMENT_TYPES:
        result = _insert_in_enclosing_function(content, content_tree, first)
        if result is not None:
            return result
        return _insert_after_last_top_level(
            content, content_tree, first.type)

    return None


# ---------------------------------------------------------------------------
# Direct-container path
# ---------------------------------------------------------------------------

def _try_direct(base: str, body: str, body_lang: str,
                body_tree, container_lang: str):
    if body_lang != container_lang:
        return None
    if _is_block_already_present(body, base):
        return None
    base_tree = _parse(base, container_lang)
    if base_tree is None or base_tree.root_node.has_error:
        return None
    return _ast_insert(base, base_tree, body, body_tree)


# ---------------------------------------------------------------------------
# Embedded-string path
# ---------------------------------------------------------------------------

def _iter_string_literals(base: str):
    for m in _TRIPLE_DOUBLE_RE.finditer(base):
        yield m.start(1), m.group(1)
    for m in _TRIPLE_SINGLE_RE.finditer(base):
        yield m.start(1), m.group(1)


def _try_embedded(base: str, body: str, body_lang: str,
                  body_tree, container_lang: str):
    if container_lang not in _CONTAINER_STRING_SCAN:
        return None

    candidates = []
    for content_start, content in _iter_string_literals(base):
        if not content or len(content) < _MIN_EMBEDDED_LEN:
            continue
        if _is_block_already_present(body, content):
            continue

        content_tree = _parse(content, body_lang)
        if content_tree is None or content_tree.root_node.has_error:
            continue

        inner = _ast_insert(content, content_tree, body, body_tree)
        if inner is None:
            continue
        inner_line_idx, indent, evidence = inner

        inner_lines = content.split("\n")
        if inner_line_idx > len(inner_lines):
            continue
        inner_char = 0
        for i in range(inner_line_idx):
            inner_char += len(inner_lines[i]) + 1
        outer_char = content_start + inner_char
        outer_line_idx = base[:outer_char].count("\n")

        candidates.append((
            outer_line_idx, indent,
            f"embedded in string literal (offset {content_start}): "
            + evidence,
        ))

    if len(candidates) != 1:
        return None
    return candidates[0]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _try_ast_anchor_inprocess(base: str, body: str, path: str,
                             context_before: str = "") -> Optional[tuple]:
    """Find a structural insertion point for body in base.

    Returns (line_index, indent_str, evidence) or None.

    line_index is 0-based into base.split("\n").  indent_str is the
    leading whitespace the block should carry.  evidence is a short
    string describing how the anchor was chosen.

    Handles two cases: Direct (block and container share a language)
    and Embedded (block is code inside a string literal of the
    container).  Returns None when tree-sitter is not installed, the
    file extension has no supported language, the block's language
    cannot be inferred, no structural anchor exists, or the anchor is
    ambiguous."""
    if not base or not body:
        return None
    if "\r\n" in base:
        return None
    body = body.replace("\r\n", "\n")

    body_lang, body_tree = _parse_any(body)
    if body_tree is None:
        return None

    ext = os.path.splitext(path)[1].lower()
    container_lang = _LANG_BY_EXT.get(ext)
    if container_lang is None:
        return None

    direct = _try_direct(base, body, body_lang, body_tree, container_lang)
    if direct is not None:
        return direct

    embedded = _try_embedded(base, body, body_lang, body_tree,
                              container_lang)
    if embedded is not None:
        return embedded

    return None


# ---------------------------------------------------------------------------
# Runtime loading: repo .venv subprocess fallback
# ---------------------------------------------------------------------------
#
# deeppeek runs from a pipx venv by default.  The repo checkout has
# its own .venv, which is where `deeppeek test_recover` expects the
# `[ast]` extras to live (see commands/test_recover.py's setup hint).
# If tree-sitter is not importable in the *current* interpreter, look
# for it in the repo .venv and run the AST work there via subprocess.
#
# The subprocess is spawned only when:
#   · in-process AST is unavailable, AND
#   · a repo .venv exists, AND
#   · that .venv has tree-sitter + at least one grammar
#
# The probe result is cached for the process lifetime so we spawn at
# most one probe per `deeppeek recover` run, plus one subprocess per
# patch that needs AST (typically 0-2).

_VENV_AST_AVAILABLE: Optional[bool] = None


def _find_venv_python() -> Optional[Path]:
    """Locate <repo>/.venv/bin/python (or Scripts/python.exe on Windows).

    Mirrors commands/test_recover.py's lookup.  Returns None if the
    venv does not exist.
    """
    here = Path(__file__).resolve().parent        # .../recover
    repo_root = here.parent.parent.parent         # <repo>
    venv = repo_root / ".venv"
    if sys.platform == "win32":
        candidate = venv / "Scripts" / "python.exe"
    else:
        candidate = venv / "bin" / "python"
    if candidate.is_file():
        return candidate
    return None


def _src_dir() -> Path:
    """Return <repo>/src — the directory containing the deeppeek package.

    Used by the subprocess helper's sys.path.insert so the venv's
    Python can import deeppeek even without an editable install.
    """
    here = Path(__file__).resolve().parent        # .../recover
    return here.parent.parent                     # .../src


def _has_inprocess_ast() -> bool:
    """True iff at least one grammar is importable in this interpreter."""
    return any(_get_parser(n) is not None for n in _LANG_INFER_ORDER)


def _has_venv_ast() -> bool:
    """True iff the repo .venv exists and has tree-sitter + a grammar.

    Cached per-process.  Skips the probe when the current interpreter
    is already the repo .venv (no point spawning a copy of ourselves).
    """
    global _VENV_AST_AVAILABLE
    if _VENV_AST_AVAILABLE is not None:
        return _VENV_AST_AVAILABLE
    py = _find_venv_python()
    if py is None:
        _VENV_AST_AVAILABLE = False
        return False
    try:
        if Path(sys.executable).resolve() == py.resolve():
            _VENV_AST_AVAILABLE = False
            return False
    except (OSError, ValueError):
        pass
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(_src_dir())!r})\n"
        "try:\n"
        "    import tree_sitter\n"
        "except ImportError:\n"
        "    sys.exit(1)\n"
        "for m in ('tree_sitter_javascript', 'tree_sitter_python', "
        "'tree_sitter_typescript'):\n"
        "    try:\n"
        "        __import__(m)\n"
        "        sys.exit(0)\n"
        "    except ImportError:\n"
        "        pass\n"
        "sys.exit(1)\n"
    )
    try:
        proc = subprocess.run(
            [str(py), "-c", probe],
            capture_output=True, timeout=5.0,
        )
        _VENV_AST_AVAILABLE = (proc.returncode == 0)
    except (subprocess.SubprocessError, OSError):
        _VENV_AST_AVAILABLE = False
    return _VENV_AST_AVAILABLE


def _try_ast_anchor_subprocess(base: str, body: str, path: str,
                               context_before: str = "") -> Optional[tuple]:
    """Run _try_ast_anchor_inprocess in the repo .venv's Python.

    Sends {base, body, path, context_before} as JSON on stdin; reads
    back either [line_idx, indent, evidence] or null on stdout.  Any
    failure — timeout, nonzero exit, garbage output — returns None so
    the caller falls back to text heuristics.
    """
    py = _find_venv_python()
    if py is None:
        return None
    payload = json.dumps({
        "base": base,
        "body": body,
        "path": path,
        "context_before": context_before,
    })
    helper = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(_src_dir())!r})\n"
        "from deeppeek.recover.ast_anchor import _try_ast_anchor_inprocess\n"
        "data = json.load(sys.stdin)\n"
        "result = _try_ast_anchor_inprocess(\n"
        "    data['base'], data['body'], data['path'], "
        "data['context_before'])\n"
        "sys.stdout.write(json.dumps(result))\n"
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
    out = proc.stdout.strip()
    if not out or out == "null":
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) != 3:
        return None
    try:
        return (int(data[0]), str(data[1]), str(data[2]))
    except (TypeError, ValueError):
        return None


def is_available() -> bool:
    """True iff AST anchoring can run — in-process or via repo .venv."""
    return _has_inprocess_ast() or _has_venv_ast()


def ast_status() -> dict:
    """Report AST availability and how it is provided.

    Used by `deeppeek diagnose`.  Keys:
      in_process: bool   — tree-sitter importable in current interpreter
      via_venv:   bool   — tree-sitter importable via repo .venv subprocess
      venv_path:  str|None — path to repo .venv python if found
    """
    venv_py = _find_venv_python()
    return {
        "in_process": _has_inprocess_ast(),
        "via_venv": _has_venv_ast(),
        "venv_path": str(venv_py) if venv_py else None,
    }


def try_ast_anchor(base: str, body: str, path: str,
                   context_before: str = "") -> Optional[tuple]:
    """Dispatch to in-process or subprocess AST anchoring.

    Prints explicit ``[ast]`` diagnostics to stderr on every attempt:
    unavailable, tried, succeeded, or returned null.  This is
    deliberate — when AST is silently skipped, the caller has no way
    to distinguish "AST ran and found no anchor" from "AST never
    ran at all".  Both look identical from the returned None.
    """
    if not base or not body:
        return None
    if "\r\n" in base:
        print("  [ast] skipped: CRLF in base", file=sys.stderr)
        return None

    in_proc = _has_inprocess_ast()
    in_venv = (not in_proc) and _has_venv_ast()

    if not in_proc and not in_venv:
        print("  [ast] skipped: tree-sitter unavailable in-process "
              "and in repo .venv", file=sys.stderr)
        print("         install: pipx inject deeppeek tree-sitter "
              "tree-sitter-javascript", file=sys.stderr)
        print("         or:      .venv/bin/pip install -e '.[ast]'",
              file=sys.stderr)
        return None

    if in_proc:
        result = _try_ast_anchor_inprocess(base, body, path,
                                            context_before)
        if result is not None:
            print(f"  [ast] in-process: {result[2]}", file=sys.stderr)
        else:
            print("  [ast] in-process: no structural anchor",
                  file=sys.stderr)
        return result

    py = _find_venv_python()
    result = _try_ast_anchor_subprocess(base, body, path,
                                         context_before)
    if result is not None:
        print(f"  [ast] via {py}: {result[2]}", file=sys.stderr)
    else:
        print(f"  [ast] via {py}: subprocess returned null",
              file=sys.stderr)
    return result
