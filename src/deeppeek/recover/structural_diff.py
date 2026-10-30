# src/deeppeek/recover/structural_diff.py
"""
Structural divergence detection: compares base, result, and patch body
at the *AST / tree-sitter element* level, not the line level.

Returns a list of `SignatureChange` records describing what the tool did
beyond what the patch's own body declares. Every reference here resolves
at import time; `pipeline._find_unresolved_module_names` walks every
module's AST at startup to enforce that invariant.

Signature families per language:

  .py       top-level `def` / `async def` / `class` / `assign`, plus a
            coarse statement-signature set (see `statement_sig`).
  .py+html  HTML element signatures inside triple-quoted strings.
  .html     HTML element signatures, nesting-aware.
  .js/.ts   top-level function/class/const/let/var declarations.

Each signature is a stable one-liner. Two structurally identical files
produce identical signature sets; a tool that accidentally adds a
declaration produces an "added" change, and one that accidentally
deletes a declaration produces a "removed" change.

FRAGMENT-BODY HANDLING (2026-11-05)
===================================
`_python_signatures` and `_python_statement_sigs` operate on
`patch_body`, which for scoped-replace patches is often an *indented
fragment* — the function body without its enclosing `def` — rather
than a well-formed module.  `ast.parse` on `try: ...` at 4-space
indent raises `IndentationError` (a `SyntaxError` subclass), so the
pre-fix versions silently returned `set()` and every statement in
`result` looked like a tool-introduced addition.

Two changes fix this:

  · `_parse_python_or_dedent` tries the raw string first, then
    `textwrap.dedent(text)`.  Motivating case (fixture 40): the
    patch body is an indented `try: ... except: ...` block;
    dedenting makes it parseable and its `extra = ...` assignment
    shows up in the signature set as expected.

  · `_body_parses` gates the `python` and `python_stmt` families in
    `structural_diff`.  A body that still doesn't parse after dedent
    declares nothing we can observe; reporting every signature in
    `result` as an addition would be noise, not signal.  The
    structural gate remains active for well-formed bodies, where the
    feature is load-bearing (the motivating case was a well-formed
    scoped-replace that silently dropped a sibling function).

The gate applies only to `python` / `python_stmt`.  The `html` and
`js` families use tree-sitter parsers that already return `set()`
for unparseable inputs, and their motivating failures are always
well-formed bodies — no fragment-body case exists for them.

SOURCE-LEVEL SYNTAX WARNINGS (2026-11-05)
=========================================
`_python_signatures` and `_python_statement_sigs` route through
`_ast.parse_source`, not `ast.parse`, so a source-level
`SyntaxWarning` in a fragment or in a file under analysis (invalid
escape sequences, deprecated syntax) does not flood the operator
log once per parse per pass.  `parse_source` retains `ast.parse`'s
exception contract, so `SyntaxError` handling at every call site is
unchanged.
"""
from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._ast import parse_source


@dataclass
class SignatureChange:
    kind:      str   # "added" | "removed" | "duplicated-nesting"
    lang:      str   # "python" | "python_stmt" | "html" | "js"
    signature: str
    detail:    str


# ---------------------------------------------------------------------------
#  Python parsing with dedent fallback
# ---------------------------------------------------------------------------

def _parse_python_or_dedent(content: str, path: str = "") -> Optional[ast.AST]:
    """Parse `content` as a module, trying direct parse first, then a
    `textwrap.dedent`-based fallback.

    Returns the `ast.Module`, or None if neither parses.

    Motivating case (fixture 40): the patch body is an indented
    `try: ... except: ...` fragment. `ast.parse(content)` raises
    `IndentationError`; `ast.parse(textwrap.dedent(content))` succeeds.
    Without this fallback, `_python_signatures` and
    `_python_statement_sigs` both returned `set()`, and every statement
    in the result looked like a tool-introduced addition.
    """
    for candidate in (content, textwrap.dedent(content)):
        try:
            return parse_source(candidate,
                                filename=path or "<patch_body>")
        except SyntaxError:
            pass
    return None


# ---------------------------------------------------------------------------
#  Python
# ---------------------------------------------------------------------------

def _python_signatures(content: str, path: str = "") -> set[str]:
    """Top-level declaration signatures in a Python source string.

    AST-based. Covers `def`, `async def`, `class`, and simple
    `NAME = ...` assignments. A statement inside a function body is
    *not* a top-level declaration — the coarse statement-signature
    check (`_python_statement_sigs`) handles those separately.

    Tries direct parse first, then `textwrap.dedent` — see
    `_parse_python_or_dedent`'s docstring for the fragment-body
    motivation.
    """
    tree = _parse_python_or_dedent(content, path)
    if tree is None:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out.add(f"def {node.name}")
        elif isinstance(node, ast.AsyncFunctionDef):
            out.add(f"async def {node.name}")
        elif isinstance(node, ast.ClassDef):
            out.add(f"class {node.name}")
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.add(f"assign {tgt.id}")
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            out.add(f"assign {node.target.id}")
    return out


def _python_statement_sigs(content: str, path: str = "") -> set[str]:
    """Statement-level signatures for Python (in addition to top-level).

    Uses `statement_sig._python_sig` for the concrete shape. The point
    is to catch *modified* statements (like a `return {...}` whose keys
    changed) that aren't top-level declarations.

    Tries direct parse first, then `textwrap.dedent` — see
    `_parse_python_or_dedent`'s docstring for the fragment-body
    motivation.
    """
    from .statement_sig import _python_sig

    tree = _parse_python_or_dedent(content, path)
    if tree is None:
        return set()
    out: set[str] = set()

    def _walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Return, ast.Assign, ast.AnnAssign,
                                  ast.Expr)):
                sig = _python_sig(child)
                if sig is not None:
                    out.add(repr(sig))
            if hasattr(child, "body"):
                _walk(child)

    _walk(tree)
    return out


def _body_parses(body: str, path: str) -> bool:
    """True iff `body` can be parsed as a fragment in `path`'s
    destination language.

    Used by `structural_diff` to decide whether to trust the body's
    signature set as authoritative.  When it returns False, the caller
    suppresses the `python` and `python_stmt` families — a body that
    can't be parsed declares nothing we can observe, so reporting every
    statement in `result` as an addition would be pure noise.

    Empty body is treated as parseable (declares nothing).
    """
    if not body or not body.strip():
        return True
    ext = Path(path).suffix.lower() if path else ""
    if ext == ".py":
        return _parse_python_or_dedent(body, path) is not None
    # For non-Python destinations, signature extraction falls back to
    # other parsers.  We don't gate those here; tree-sitter-based
    # extraction returns empty sets naturally when the body doesn't
    # parse, and the JS family is intentionally permissive.
    return True


# ---------------------------------------------------------------------------
#  JavaScript / TypeScript
# ---------------------------------------------------------------------------

def _js_signatures(content: str) -> set[str]:
    """Top-level JS/TS declaration signatures.

    Uses tree-sitter-javascript when available (via `ast_anchor`'s
    grammar dispatch); falls back to a column-0 regex when the grammar
    is not installed. The regex is *not* the primary path — tree-sitter
    gives us `.type` / `.start_point` structural facts; the fallback
    exists only because deeppeek must run on machines without the
    `[ast]` extra.
    """
    from .validate_output import _declared_names_js
    names = _declared_names_js(content)
    if names:
        return {f"js {n}" for n in names}

    # Fallback when tree-sitter-javascript is unavailable.
    out: set[str] = set()
    for m in re.finditer(
            r'(?m)^(?:export\s+)?(?:async\s+)?'
            r'(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)',
            content):
        out.add(f"js {m.group(1)}")
    return out


# ---------------------------------------------------------------------------
#  HTML
# ---------------------------------------------------------------------------

def _node_text(node) -> str:
    t = getattr(node, "text", None)
    if t is None:
        return ""
    if isinstance(t, bytes):
        return t.decode("utf-8", "replace")
    return t


def _html_element_signature(node) -> Optional[str]:
    """One-line signature for a tree-sitter-html `element` node:
    `<tag>` or `<tag>.<class>` or `<tag>[<style>]` or
    `<tag>.<class>[<style>]`."""
    tag = None
    cls = None
    style = None
    for child in node.children:
        if child.type == "start_tag":
            for c in child.children:
                if c.type == "tag_name":
                    tag = _node_text(c).lower()
                elif c.type == "attribute":
                    name = None
                    value = None
                    for ac in c.children:
                        if ac.type == "attribute_name":
                            name = _node_text(ac).lower()
                        elif ac.type == "quoted_attribute_value":
                            for qc in ac.children:
                                if qc.type == "attribute_value":
                                    value = _node_text(qc)
                    if name == "class" and value:
                        cls = value
                    elif name == "style" and value:
                        style = value
    if not tag:
        return None
    sig = tag
    if cls:
        sig += f".{cls}"
    if style:
        sig += f"[{style}]"
    return sig


def _html_signatures(content: str) -> set[str]:
    """HTML element signatures at every depth (nested or not)."""
    try:
        import tree_sitter_html  # type: ignore
        from tree_sitter import Language, Parser
    except ImportError:
        return set()
    try:
        parser = Parser(Language(tree_sitter_html.language()))
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return set()
    if tree is None or tree.root_node.has_error:
        return set()

    out: set[str] = set()

    def _walk(node):
        if node.type == "element":
            sig = _html_element_signature(node)
            if sig:
                out.add(sig)
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return out


_TRIPLE_DQ_RE = re.compile(
    r'(?:[rbufRBUF]{0,2})"""(.*?)"""', re.DOTALL)
_TRIPLE_SQ_RE = re.compile(
    r"(?:[rbufRBUF]{0,2})'''(.*?)'''", re.DOTALL)


def _iter_triple_quoted(text: str):
    """Yield `(content_start, content)` for every triple-quoted string.

    Fast-path + precompiled regexes.  See the matching function in
    `validate_output.py` for the rationale.
    """
    if '"""' not in text and "'''" not in text:
        return
    for m in _TRIPLE_DQ_RE.finditer(text):
        yield m.start(1), m.group(1)
    for m in _TRIPLE_SQ_RE.finditer(text):
        yield m.start(1), m.group(1)


def extract_signatures(content: str, path: str) -> dict[str, set[str]]:
    """...existing docstring (which is currently in the module
    docstring; move or duplicate as appropriate)...

    The `.py` branch now gates `_html_signatures(content)` behind a
    first-non-blank-line-starts-with-`<` check.  That call is a
    whole-file tree-sitter-html parse that can only ever return an
    empty set for a well-formed Python source — the previous
    unconditional call cost one wasted parse per `(base, result,
    patch_body)` triple per record on pass 8.10.
    """
    out: dict[str, set[str]] = {}
    ext = Path(path).suffix.lower() if path else ""

    if ext == ".py":
        out["python"] = _python_signatures(content, path)
        out["python_stmt"] = _python_statement_sigs(content, path)
        out["html"] = _embedded_html_signatures(content)
        # Raw HTML body (patch_body case): a bare `<div>...</div>`
        # fragment with no triple-quoted wrapper.
        # `_embedded_html_signatures` finds nothing there; the
        # direct parse is the only way to see the elements.  Gate
        # it on the first non-blank line starting with `<` so a
        # pure Python source never triggers the tree-sitter-html
        # parse.
        first = next(
            (ln.strip() for ln in content.splitlines() if ln.strip()),
            "")
        if first.startswith("<"):
            out["html"] = out["html"] | _html_signatures(content)
    elif ext == ".html":
        out["html"] = _html_signatures(content)
    elif ext in (".js", ".mjs", ".ts", ".tsx", ".jsx", ".cjs"):
        out["js"] = _js_signatures(content)
        out["html"] = _embedded_html_signatures(content)
    return out


def _embedded_html_signatures(content: str) -> set[str]:
    """HTML element signatures inside triple-quoted Python strings."""
    from .validate_output import _looks_like_html
    out: set[str] = set()
    for _offset, block in _iter_triple_quoted(content):
        if not _looks_like_html(block):
            continue
        out |= _html_signatures(block)
    return out


# ---------------------------------------------------------------------------
#  Signature extraction dispatch
# ---------------------------------------------------------------------------

def extract_signatures(content: str, path: str) -> dict[str, set[str]]:
    """Return `{lang: {signature, ...}}` for `content`.

    Signatures are canonical one-liners, stable across identical
    structural states.

    For `.py` destinations, the content may be either a Python module
    OR a raw HTML fragment that a patch will splice into an embedded
    string.  `structural_diff` receives both forms: `base`/`result`
    are always full `.py` sources, while `patch_body` may be a raw
    HTML fragment with no triple-quoted wrapper.  The extra
    `_html_signatures(content)` call handles the fragment case —
    without it, every HTML element added by a raw-HTML body would be
    flagged as tool-introduced (fixture 42: `<div class="divider">`
    spliced into `PG_PANEL_BODY`, flagged as `added div.divider`).

    Tree-sitter-html returns empty for non-HTML content (Python text
    has no element nodes), so the union is safe for pure-code bodies
    too.
    """
    out: dict[str, set[str]] = {}
    ext = Path(path).suffix.lower() if path else ""

    if ext == ".py":
        out["python"] = _python_signatures(content, path)
        out["python_stmt"] = _python_statement_sigs(content, path)
        out["html"] = (
            _embedded_html_signatures(content)
            | _html_signatures(content)
        )
    elif ext == ".html":
        out["html"] = _html_signatures(content)
    elif ext in (".js", ".mjs", ".ts", ".tsx", ".jsx", ".cjs"):
        out["js"] = _js_signatures(content)
        out["html"] = _embedded_html_signatures(content)
    return out


# ---------------------------------------------------------------------------
#  Structural diff
# ---------------------------------------------------------------------------

def structural_diff(base: str, result: str, path: str,
                    patch_body: str) -> list[SignatureChange]:
    """Return structural changes the *patch* introduced beyond what its
    body declares.

    A change is reported iff a signature appears in the result but not
    the base and not the patch body ("added"), or vice-versa ("removed").
    Signatures the patch *itself* declares are not reported — those are
    the intended work.

    `base == result` short-circuits: pre-existing unchanged records have
    no diff to compute, and 200+ of them were paying full `ast.parse` +
    tree-sitter costs in the 2026-10-29 run.

    Fragment-body handling (2026-11-05): if `patch_body` cannot be
    parsed as a Python fragment (even after dedent), the `python` and
    `python_stmt` signature families are suppressed.  A body that
    can't be parsed declares nothing we can observe; without this
    suppression, every statement in `result` looks like a
    tool-introduced addition.  Motivating case: fixture 40's indented
    `try: ... except: ...` body produced a false "added `extra = ...`".
    """
    if not base or not result:
        return []
    if base == result:
        return []

    base_sigs = extract_signatures(base, path)
    result_sigs = extract_signatures(result, path)
    body_sigs = extract_signatures(patch_body, path)
    changes: list[SignatureChange] = []

    # --- Fragment-body suppression --------------------------------
    #
    # When the patch body is a fragment (indented `try:`, indented
    # def, unbalanced braces) that doesn't parse, `body_sigs` is
    # empty and every signature in `result` looks like an addition.
    # Suppress the Python families for that case; other families
    # (HTML, JS) still run.
    body_parseable = _body_parses(patch_body, path)

    for lang, base_set in base_sigs.items():
        if lang in ("python", "python_stmt") and not body_parseable:
            continue
        result_set = result_sigs.get(lang, set())
        body_set = body_sigs.get(lang, set())

        for sig in sorted(base_set - result_set):
            if sig in body_set:
                continue
            changes.append(SignatureChange(
                kind="removed", lang=lang, signature=sig,
                detail=f"{sig!r} was in the base but is missing from "
                       f"the result — the patch removed it without "
                       f"declaring it",
            ))

        for sig in sorted(result_set - base_set):
            if sig in body_set:
                continue
            changes.append(SignatureChange(
                kind="added", lang=lang, signature=sig,
                detail=f"{sig!r} is present in the result but not the "
                       f"base and not declared by the patch body — "
                       f"the tool introduced it",
            ))

    # HTML-specific duplicate-nesting check. Reuse the implementations
    # already in validate_output; do NOT reimplement them.
    from .validate_output import (
        _extract_embedded_html, _has_duplicate_class_nesting,
    )
    if path.endswith(".py"):
        base_html = _extract_embedded_html(base)
        result_html = _extract_embedded_html(result)
        if base_html and result_html:
            if (_has_duplicate_class_nesting(result_html)
                    and not _has_duplicate_class_nesting(base_html)):
                changes.append(SignatureChange(
                    kind="duplicated-nesting", lang="html",
                    signature="(nested class)",
                    detail="the patch nested an element inside a "
                           "duplicate of itself — a wrapper that should "
                           "have been replaced was retained alongside "
                           "the new copy",
                ))
    elif path.endswith(".html"):
        if (_has_duplicate_class_nesting(result)
                and not _has_duplicate_class_nesting(base)):
            changes.append(SignatureChange(
                kind="duplicated-nesting", lang="html",
                signature="(nested class)",
                detail="the patch nested an element inside a duplicate "
                       "of itself",
            ))

    return changes
