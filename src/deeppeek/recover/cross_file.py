"""Cross-file duplicate detection — structural.

Finds newly-added top-level functions or classes that appear verbatim
in multiple records.  Informational only -- the write pass records
findings in the manifest and warnings.txt, but does not fail-closed.

Motivation: the most common authoring error when patching multiple
files is "add helper X" that mistakenly lands in file A AND file B
(or the same helper is added twice because two blocks both looked
like the right place).  The result compiles, but bloats the
codebase, drifts out of sync, and defeats refactoring.

Not fail-closed: legitimate cross-file duplication exists (small
utility shims, intentional forks for micro-service separation,
per-platform variants).  The MIN_LINES threshold is set high enough
that only substantive bodies are compared, and the report names
every affected file so a human can decide quickly.

STRUCTURAL COMPARISON (2026-11-05)
=================================
The matcher is structural, not textual.  The pre-2026-11-05 version
serialised each function/class body's *lines* (stripped, joined)
and hashed the result; a reformatting (tabs → spaces, trailing-
comma removal, an interleaved comment) changed the hash and the
duplicate was missed.  That is wrong for the motivating use case:
the point is "the same helper was added twice", not "the same
helper was added twice byte-for-byte".

The canonical form used for the hash is language-aware:

  .py       `ast.unparse` of the top-level FunctionDef /
            AsyncFunctionDef / ClassDef node.  Reformatting-
            invariant (comments stripped, whitespace normalised,
            indentation regenerated, quote style canonicalised) while
            preserving structure and identifier names.  Two
            functions with different bodies or different names hash
            differently.

  .py+JS    JS/TS content embedded in triple-quoted strings; same
            canonicalisation as standalone `.js`.  This is what makes
            "the same JS helper added to two `.py` files" detectable
            — the `.py` container's language is irrelevant, only the
            embedded JS payload's shape.

  .js/.ts   tree-sitter-javascript canonical tree serialisation.
            Node type + child recursion with comments skipped; leaf
            identifiers included verbatim.  Reformatting-invariant.

  .html     Not currently scanned.  A `details`-keyed variant is a
            plausible extension but out of scope for the "helper
            added twice" motivating case.

  unknown   Both parsers attempted; regex fallback if neither
            succeeds.

Fallback when tree-sitter or a grammar is unavailable: the previous
regex + brace-matcher path.  Same graceful-degradation contract as
`ast_anchor.try_ast_anchor` and `structural_diff._js_signatures`:
the tool must run on machines without the `[ast]` extra, and the
fallback correctly identifies *verbatim* duplicates (just not
reformatted ones).

Cross-language boundary: a language tag is prefixed into the hash
input, so a Python function named `foo` and a JS function named
`foo` never collide even if their canonical bodies happen to be
similar.

DEPENDENCY HYGIENE (2026-10-30)
===============================
The pre-fix version imported `_norm_lines` and `_find_matching_close_char`
from `validate_output` at module level.  That made `cross_file` (a
small, leaf-ish duplicate detector) depend on the whole
structural-validation stack, and — critically — turned any
**syntax error** in `validate_output.py` into a `pipeline.run()`
crash.  The import lived inside `run()`'s `from .cross_file import
find_cross_file_duplicates` line, which was not wrapped in any
try/except, so one bad docstring in a downstream module cascaded
into "every fixture fails" for 40 tests.

Both helpers have safer homes:

  • `_norm_lines` is three lines of `str.splitlines` +
    `strip` + `join`.  It belongs locally.

  • `_find_matching_close_char` is only used by the *fallback*
    regex matcher; the structural path uses parsers.  The fallback
    imports it lazily so a broken `patch_text` does not break the
    structural path.

`_ast` is a leaf (imports only `ast`, `warnings`), so importing it
at module level is safe — same pattern as `patch_dispatch`.  The
heavier dependencies (`validate_output`, `ast_anchor`, `patch_text`)
are lazy, imported only by the code path that actually needs them.

PYTHON SIG CACHE (2026-11-05)
=============================
`_python_top_level_signatures` calls `ast.unparse` for every
top-level FunctionDef / AsyncFunctionDef / ClassDef in `content`,
then SHA-256s the result.  `find_cross_file_duplicates` calls it
twice per record — once against `base_block.body` (to compute the
"preexisting signatures" set), once against `rec.content` (the
candidate "new signatures").  When a function exists unchanged in
both, its `ast.unparse` output (and therefore its SHA) is
identical; the unparse and hash are done twice for nothing.

`@lru_cache(maxsize=256)` on `_python_sig_cache(content, path)` —
the tuple-returning worker — makes the second call a dict lookup.
256 entries covers a 500-file `--apply-to` run's working set
comfortably; anything beyond that evicts LRU, which is fine (the
second call comes milliseconds after the first).
"""
from __future__ import annotations

import ast
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from ._ast import parse_source


MIN_LINES_FOR_CROSS_FILE = 10


# ===========================================================================
#  Structural signature extraction — dispatch on path extension
# ===========================================================================

def _extract_top_level_signatures(
        content: str, path: str = "") -> Iterator[tuple]:
    """Yield `(kind, name, canonical_body, sha, line_count)` for every
    top-level function/class in `content`, using the language-appropriate
    parser.

    `path`'s extension selects the parser:
      .py/.pyi     `ast` (plus recursive scan of embedded JS)
      .js/.ts/...  tree-sitter-javascript
      anything else (or parser unavailable) → regex + brace fallback

    Yields nothing when the extension's parser fails AND the fallback
    finds nothing parseable.  A file whose Python is malformed still
    yields any well-formed JS inside its triple-quoted strings — the
    regex that finds those blocks is line-based and does not require
    the outer Python to parse.
    """
    ext = Path(path).suffix.lower() if path else ""

    if ext in (".py", ".pyi"):
        for entry in _python_top_level_signatures(content, path):
            yield entry
        for entry in _embedded_js_signatures(content, path):
            yield entry
        # Fall back to the regex matcher for embedded JS when tree-
        # sitter-javascript is unavailable.  The regex is line-based,
        # so it finds `function foo() {` patterns inside triple-quoted
        # strings regardless of the surrounding Python's parseability.
        if not _js_parser_available():
            yield from _regex_fallback_signatures(content, path)
        return

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        found = False
        for entry in _js_top_level_signatures(content, path):
            found = True
            yield entry
        if not found and not _js_parser_available():
            yield from _regex_fallback_signatures(content, path)
        return

    # Unknown extension (or empty path): attempt both parsers, fall
    # back to regex if neither produced anything.
    found = False
    for entry in _python_top_level_signatures(content, path):
        found = True
        yield entry
    for entry in _js_top_level_signatures(content, path):
        found = True
        yield entry
    if not found:
        yield from _regex_fallback_signatures(content, path)


def _js_parser_available() -> bool:
    """True iff tree-sitter-javascript is importable and parseable.

    Used to decide whether the regex fallback needs to run.  The
    probe is per-call rather than cached, because `ast_anchor` caches
    the parser internally and the check is cheap once the grammar is
    loaded.
    """
    try:
        from .ast_anchor import _get_parser
        return _get_parser("javascript") is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
#  Python
# ---------------------------------------------------------------------------

def _python_top_level_signatures(
        content: str, path: str) -> Iterator[tuple]:
    """Yield signatures for every top-level def/async def/class.

    Delegates to `_python_sig_cache`, which caches the tuple of
    `(kind, name, canonical, sha, line_count)` entries on
    `(content, path)`.  See module docstring, PYTHON SIG CACHE.

    Skips when `ast.parse` fails (unparseable file), when the node is
    not one of the three accepted types, or when `ast.unparse` raises
    on a pathological AST.
    """
    yield from _python_sig_cache(content, path)


@lru_cache(maxsize=256)
def _python_sig_cache(content: str, path: str) -> tuple:
    """Cached worker behind `_python_top_level_signatures`.

    `ast.unparse` on every top-level def/class, then SHA-256 of the
    canonical form.  Cached on `(content, path)` — the same
    `(base_block.body, path)` and `(rec.content, path)` pair that
    `find_cross_file_duplicates` computes back-to-back for every
    record hits the cache on the second call.
    """
    try:
        tree = parse_source(content, filename=path or "<cross_file>")
    except SyntaxError:
        return ()

    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "function"
        elif isinstance(node, ast.ClassDef):
            kind = "class"
        else:
            continue

        try:
            canonical = ast.unparse(node)
        except Exception:
            continue

        line_count = node.end_lineno - node.lineno + 1
        sha = _structural_hash("python", canonical)
        out.append((kind, node.name, canonical, sha, line_count))
    return tuple(out)


# ---------------------------------------------------------------------------
#  JavaScript / TypeScript
# ---------------------------------------------------------------------------

_JS_LEAF_TYPES = frozenset({
    "identifier",
    "property_identifier",
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
    "private_property_identifier",
    "type_identifier",
    "statement_identifier",
    "number",
    "string",
    "template_string",
    "regex",
    "true",
    "false",
    "null",
    "undefined",
})


def _js_top_level_signatures(
        content: str, path: str) -> Iterator[tuple]:
    """Yield signatures for every top-level JS/TS function, class, or
    arrow-function/function-expression assignment.

    Yields nothing when tree-sitter-javascript is unavailable; the
    caller falls through to `_regex_fallback_signatures`.
    """
    from .ast_anchor import _get_parser, _LANG_BY_EXT

    ext = Path(path).suffix.lower() if path else ""
    lang = _LANG_BY_EXT.get(ext) or "javascript"
    parser = _get_parser(lang)
    if parser is None:
        return

    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return
    if tree is None or tree.root_node.has_error:
        return

    for child in tree.root_node.children:
        yield from _js_signature_from_node(child)


def _js_signature_from_node(node) -> Iterator[tuple]:
    """Dispatch one tree-sitter node to the appropriate extractor.

    `export_statement` wrappers are recursed into so `export function
    foo` yields the same entry as `function foo` — the intent of the
    author was "this function exists", not "this function has an
    export keyword".
    """
    if node.type == "export_statement":
        for child in node.children:
            yield from _js_signature_from_node(child)
        return

    if node.type in (
            "function_declaration",
            "generator_function_declaration",
            "class_declaration"):
        name = _js_decl_name(node)
        if name is None:
            return
        kind = ("class" if node.type == "class_declaration"
                else "function")
        canonical = _js_canonical(node)
        line_count = node.end_point[0] - node.start_point[0] + 1
        yield (kind, name, canonical,
               _structural_hash("js", canonical), line_count)
        return

    if node.type in ("lexical_declaration", "variable_declaration"):
        # `const foo = () => {...}`, `const foo = function () {...}`,
        # `const foo = class {...}`.  Multi-declarators (`const a = 1,
        # b = () => {}`) yield entries only for the ones whose value is
        # a function or class expression.
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            name = None
            value = None
            for c in child.children:
                if c.type == "identifier":
                    name = _node_text(c)
                elif c.type in ("arrow_function", "function_expression",
                                "function", "class", "class_expression",
                                "generator_function"):
                    value = c
            if name is None or value is None:
                continue
            kind = "class" if "class" in value.type else "function"
            canonical = _js_canonical(value)
            line_count = value.end_point[0] - value.start_point[0] + 1
            yield (kind, name, canonical,
                   _structural_hash("js", canonical), line_count)


def _js_decl_name(node) -> Optional[str]:
    for child in node.children:
        if child.type == "identifier":
            return _node_text(child) or None
    return None


def _node_text(node) -> str:
    t = getattr(node, "text", None)
    if t is None:
        return ""
    if isinstance(t, bytes):
        return t.decode("utf-8", "replace")
    return t


def _js_canonical(node) -> str:
    """Recursive canonical serialisation of a tree-sitter node.

    Format: `(node_type leaf1 leaf2 ...)` where `leaf` is either a
    nested `(node_type ...)` for child nodes or the literal text for
    leaf-token types.

    `comment` nodes and their descendants are skipped, so an
    interleaved comment does not change the hash.  Whitespace is
    implicitly invisible — the sexp is built from node types and leaf
    texts, not from source bytes.
    """
    if node.type == "comment":
        return ""

    parts: list = [f"({node.type}"]
    for child in node.children:
        if child.type == "comment":
            continue
        if child.type in _JS_LEAF_TYPES:
            parts.append(" ")
            parts.append(_node_text(child))
        else:
            sub = _js_canonical(child)
            if sub:
                parts.append(" ")
                parts.append(sub)
    parts.append(")")
    return "".join(parts)


# ---------------------------------------------------------------------------
#  Embedded JS in `.py` files
# ---------------------------------------------------------------------------

def _embedded_js_signatures(
        content: str, path: str) -> Iterator[tuple]:
    """Yield signatures for JS/TS functions/classes inside triple-quoted
    strings of `content`.

    Reuses `_js_top_level_signatures` on each triple-quoted block that
    `validate_output._looks_like_js` accepts.  The language tag remains
    `"js"`, so an embedded JS function and a standalone JS function
    with the same canonical form hash identically — this is what makes
    "the same helper added to two `.py` files" detected.
    """
    from .validate_output import _iter_triple_quoted, _looks_like_js

    for _offset, block in _iter_triple_quoted(content):
        if not _looks_like_js(block):
            continue
        # Synthesise a filename so `_js_top_level_signatures` sees a
        # `.js` extension when it picks the grammar.  The path is only
        # used for grammar selection, not for hashing.
        yield from _js_top_level_signatures(block, "embedded.js")


# ---------------------------------------------------------------------------
#  Regex + brace-matcher fallback (verbatim-only)
# ---------------------------------------------------------------------------

_FUNC_DECL_RE = re.compile(
    r"^((?:export\s+)?(?:async\s+)?function\s+"
    r"([A-Za-z_$][\w$]*)\s*\()",
    re.MULTILINE)
_CLASS_DECL_RE = re.compile(
    r"^((?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b)",
    re.MULTILINE)


def _norm_lines(s: str) -> str:
    """Strip each non-empty line and rejoin, dropping blank lines.

    Local definition (rather than importing from `validate_output`) so
    that the fallback path does not depend on the structural-validation
    stack.  See module docstring, DEPENDENCY HYGIENE.
    """
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def _regex_fallback_signatures(
        content: str, path: str) -> Iterator[tuple]:
    """Verbatim-only matcher using column-0 declaration regexes and the
    brace matcher.

    Used when tree-sitter-javascript is unavailable.  Finds the same
    duplicates the pre-2026-11-05 matcher found: byte-identical bodies
    (after line-strip-and-join).  Misses reformatted duplicates; that
    is the graceful-degradation contract.
    """
    if not content:
        return
    # Lazy import so a broken `patch_text` does not break the
    # structural path.
    try:
        from .patch_text import _find_matching_brace
    except ImportError:
        return

    for pat, kind in (
            (_FUNC_DECL_RE, "function"),
            (_CLASS_DECL_RE, "class")):
        for m in pat.finditer(content):
            if (content.rfind(chr(10), 0, m.start()) + 1
                    != m.start()):
                continue
            name = m.group(2)
            header_end = content.find(chr(10), m.end())
            if header_end == -1:
                header_end = len(content)
            open_brace = content.find("{", m.end(), header_end)
            if open_brace == -1:
                continue
            close_brace = _find_matching_brace(content, open_brace)
            if close_brace == -1:
                continue
            body = _norm_lines(content[m.start():close_brace + 1])
            if not body:
                continue
            line_count = body.count(chr(10)) + 1
            sha = _structural_hash("js", body)
            yield kind, name, body, sha, line_count


# ---------------------------------------------------------------------------
#  Structural hash
# ---------------------------------------------------------------------------

def _structural_hash(lang: str, canonical: str) -> str:
    """SHA-256 of the language-tagged canonical body.

    The language tag prevents a Python function and a JS function that
    happen to canonicalise to the same string from colliding.  Both
    are unlikely in practice, but the tag makes the invariant explicit
    rather than accidental.
    """
    payload = f"lang:{lang}\n{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ===========================================================================
#  The pass
# ===========================================================================

def find_cross_file_duplicates(records, blocks_by_idx):
    """Return a list of cross-file duplicate descriptors.  Empty on
    clean runs.  Never raises."""
    if not records:
        return []

    by_sha: dict = {}
    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.state in ("PARTIAL_ONLY", "MISSING"):
            continue
        if rec.is_deleted:
            continue

        # Signatures already present in the record's base are not
        # "newly-added" — they were on disk before this run.  Skip
        # them so a pre-existing function that happens to have a twin
        # in another pre-existing file is not flagged.
        base_sigs: set = set()
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block and base_block.body:
                for (_k, _n, _b, sha, _c) in \
                        _extract_top_level_signatures(
                            base_block.body, path):
                    base_sigs.add(sha)

        for kind, name, body, sha, line_count in \
                _extract_top_level_signatures(rec.content, path):
            if sha in base_sigs:
                continue
            if line_count < MIN_LINES_FOR_CROSS_FILE:
                continue
            by_sha.setdefault(sha, []).append({
                "path": path,
                "name": name,
                "kind": kind,
                "line_count": line_count,
            })

    duplicates = []
    for sha, entries in by_sha.items():
        files = {e["path"] for e in entries}
        if len(files) < 2:
            continue
        rep = entries[0]
        duplicates.append({
            "name": rep["name"],
            "kind": rep["kind"],
            "files": sorted(files),
            "line_count": rep["line_count"],
            "body_sha256": sha[:16],
        })

    duplicates.sort(key=lambda d: (
        -d["line_count"], d["files"], d["name"]))
    return duplicates
