"""
recover.ast_anchor — tree-sitter based structural anchoring.

When the text-based heuristics in `apply_patches` fail because there is
no unique string anchor to insert next to, the AST structure of the
target file may still unambiguously determine placement.

Two block shapes are handled:

  1. **Statement** (bare `if`, `for`, `while`, `expression_statement`,
     `return_statement`, ...).  These need a function container.
     `try_ast_anchor` finds the enclosing function by matching the
     block's referenced identifiers against functions in base that
     declare those identifiers.  Insert at the end of that function,
     before its closing brace (or before a trailing `return`).

  2. **Top-level declaration** (`lexical_declaration`, `function_
     declaration`, `class_declaration`, `export_statement`).  Insert
     after the last top-level sibling of the same (or same class) type.

Every insertion is deterministic: if the enclosing-function search
yields more than one candidate with equal overlap, or the block's AST
shape has no matching sibling, `try_ast_anchor` returns None and the
caller fail-closes with a specific reason.  There is no "best guess".

`try_ast_anchor` is one of the last-resort heuristics.  If tree-sitter
is not installed (no `[ast]` extra), the import fails and the caller
catches it — the tool degrades to text-only mode transparently.

Supported languages (ext → language):
    .js / .mjs / .jsx  → javascript
    .ts                 → typescript
    .tsx                → tsx
    .py                 → python  (falls through to None for lack of
                                    a Python-specific type map)

Dependency
==========
Install the extra:

    pipx inject deeppeek 'deeppeek[ast]'

Without it, deeppeek works exactly as before.
"""

from typing import Optional
import os
import sys


# ---------------------------------------------------------------------------
# Language maps and caches
# ---------------------------------------------------------------------------

_LANG_BY_EXT = {
    ".js":   "javascript",
    ".mjs":  "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".py":   "python",
}

_parser_cache = {}


# ---------------------------------------------------------------------------
# Node type sets
# ---------------------------------------------------------------------------
#
# Statements that need a function container.  Excludes top-level
# declarations (those are handled separately).
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

# Top-level declarations.  A block whose first OR last top-level node
# is one of these is treated as a top-level block.
_JS_TOP_LEVEL_DECL_TYPES = frozenset({
    "lexical_declaration",
    "variable_declaration",
    "function_declaration",
    "class_declaration",
    "export_statement",
})

# Function-like nodes whose body can enclose statements.
_JS_FUNCTION_TYPES = frozenset({
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
})


# ---------------------------------------------------------------------------
# Language / parser loading
# ---------------------------------------------------------------------------

def _get_language(lang_name: str):
    """Return a tree-sitter Language, or None if unavailable."""
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
    """Return a cached tree-sitter Parser or None."""
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
    """True iff tree-sitter and the javascript grammar are importable."""
    return _get_parser("javascript") is not None


# ---------------------------------------------------------------------------
# Node helpers
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
    """Names of every `identifier` node in the tree (not property names)."""
    out = set()
    for n in _walk(root):
        if n.type == "identifier":
            txt = _node_text(n)
            if txt:
                out.add(txt)
    return out


def _collect_declared_identifiers(function_node) -> set:
    """Names declared in a function via const/let/var or parameters."""
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


def _is_block_already_present(body: str, base: str) -> bool:
    """True iff every non-blank line of body appears in base.

    Guards against inserting a duplicate when the block is already in
    the file.  The caller's own `_patch_already_present` check fires
    after our heuristics run, so if we don't guard here we'd insert a
    duplicate before it gets a chance to see it.
    """
    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not body_lines:
        return True
    base_lines = {ln.strip() for ln in base.splitlines() if ln.strip()}
    return all(ln in base_lines for ln in body_lines)


# ---------------------------------------------------------------------------
# Insertion logic
# ---------------------------------------------------------------------------

def _insert_after_last_top_level(base: str, base_tree,
                                 target_type: str):
    """Insert after the last top-level sibling of `target_type`.

    Returns (line_index, indent_str, evidence) or None.
    """
    children = [c for c in base_tree.root_node.children
                if c.type not in ("comment",)]
    if not children:
        return None

    last = None
    for c in children:
        if c.type == target_type:
            last = c

    # Fallback: last sibling in the same node class
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

    insert_line = base[:last.end_byte].count("\n") + 1
    lines = base.split("\n")
    if insert_line > len(lines):
        insert_line = len(lines)

    evidence = f"after last top-level {last.type}"
    return (insert_line, "", evidence)


def _insert_in_enclosing_function(base: str, base_tree,
                                  body_first_node):
    """Insert at the end of the function that declares the block's
    local identifiers.

    Returns (line_index, indent_str, evidence) or None.
    """
    block_idents = _collect_identifiers(body_first_node)
    if not block_idents:
        return None

    candidates = []
    for n in _walk(base_tree.root_node):
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

    # Bail on single-line bodies (open and close on same line).
    if body_node.start_point[0] == body_node.end_point[0]:
        return None

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

    # Determine indent from first statement's line.
    if first_stmt is not None:
        stmt_start = first_stmt.start_byte
        line_start = base.rfind("\n", 0, stmt_start) + 1
        indent = base[line_start:stmt_start]
        if not all(ch in " \t" for ch in indent):
            indent = "  "
    else:
        indent = "  "

    # Insert before last stmt if it's a return, else at close-brace line.
    if last_stmt is not None and last_stmt.type == "return_statement":
        insert_line = base[:last_stmt.start_byte].count("\n")
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def try_ast_anchor(base: str, body: str, path: str,
                   context_before: str = "") -> Optional[tuple]:
    """Find a structural insertion point for `body` in `base`.

    Returns (line_index, indent_str, evidence) or None.

    `line_index` is a 0-based index into `base.split("\\n")` where the
    reindented block should be inserted.  `indent_str` is the leading
    whitespace the block should carry.  `evidence` is a short string
    describing how the anchor was chosen — surfaced in stderr and the
    manifest for debugging.

    Returns None when:
      * tree-sitter is not installed,
      * the file extension has no supported language,
      * base or body fails to parse,
      * the block's identifiers match multiple functions equally,
      * no top-level sibling of the block's type exists,
      * the block is already present verbatim in base,
      * any other reason that would make placement ambiguous.
    """
    if not base or not body:
        return None
    if "\r\n" in base:
        # CRLF handling would complicate line-index arithmetic; the
        # tool's primary use case is LF-only conversation output.
        # Bailing is safer than half-correct insertion.
        return None
    body = body.replace("\r\n", "\n")

    ext = os.path.splitext(path)[1].lower()
    lang_name = _LANG_BY_EXT.get(ext)
    if lang_name is None:
        return None

    parser = _get_parser(lang_name)
    if parser is None:
        return None

    if _is_block_already_present(body, base):
        return None

    try:
        base_bytes = base.encode("utf-8")
        base_tree = parser.parse(base_bytes)
        if base_tree.root_node.has_error:
            return None
        body_tree = parser.parse(body.encode("utf-8"))
        if body_tree.root_node.has_error:
            return None
    except Exception:
        return None

    first_node = _first_nonerror_child(body_tree.root_node)
    last_node = _last_nonerror_child(body_tree.root_node)
    if first_node is None or last_node is None:
        return None

    first_type = first_node.type
    last_type = last_node.type

    # Case 1: top-level declaration block.
    if (first_type in _JS_TOP_LEVEL_DECL_TYPES
            or last_type in _JS_TOP_LEVEL_DECL_TYPES):
        return _insert_after_last_top_level(base, base_tree, last_type)

    # Case 2: statement block — try enclosing function first, then
    # fall back to top-level placement (for bare `expression_statement`
    # or other top-level-appropriate shapes).
    if first_type in _JS_STATEMENT_TYPES:
        result = _insert_in_enclosing_function(
            base, base_tree, first_node)
        if result is not None:
            return result
        return _insert_after_last_top_level(base, base_tree, first_type)

    return None
