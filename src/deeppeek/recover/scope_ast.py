# src/deeppeek/recover/scope_ast.py
"""
scope_ast — find a named scope by AST, not by regex.

The single implementation of "where is the function/class/element
named X". All scope-based heuristics route through this.

The pre-AST codebase maintained four hand-written regex pattern
lists (in `patch_scope.py`, `patch_scope_insert.py`,
`patch_anchor_insert.py`). Each was fragile against shapes the
author didn't think of — decorated functions, `async def`, arrow
functions, class methods, generator functions, HTML elements with
attributes, etc.

The AST-based finder handles every shape the language defines, in
one place, with exact boundaries.

CACHE FIX (2026-11-05)
======================
The pre-fix caches used `key = (hash(base), len(base), name, path)`.
Two different bases with the same 64-bit hash *and* the same length
would return the wrong cached result.  Vanishingly unlikely per pair,
permanent if it happens, and it wasn't even faster — CPython hashes
`str` once per object and dict lookup is O(1) either way.

The caches are now `@functools.lru_cache(maxsize=1024)` keyed on the
actual `base` string.  `lru_cache` evicts LRU instead of `.clear()`
on overflow, so busy runs don't thrash.

`_python_decl_index(content)` caches the per-content declaration
index: one `ast.walk(tree)` per unique content, reused by every scope
lookup on that file.  A file with 3 scope lookups pays 1 walk
instead of 3; a file with 6 pays 1 instead of 6.

SUBPROCESS FALLBACK (2026-11-06)
================================
When in-process tree-sitter is unavailable but the repo .venv has
it (`ast_status() == {"in_process": False, "via_venv": True}`), the
three public finders fall back to a subprocess running the venv's
Python.  The subprocess imports `deeppeek.recover.scope_ast` from
`<repo>/src/` via `sys.path.insert`, calls the corresponding
`_find_*_cached` function, and returns its result via JSON.

The motivating failure: the 2026-10-31 inpaint conversation.  Every
patch that targeted a JS or HTML scope inside a Python raw string
failed with "scope X could not be located structurally (AST scope
finder returned None)" — but every one of those scopes *existed* in
the base.  The running interpreter simply could not import
`tree_sitter_html` or `tree_sitter_javascript`, so
`_find_embedded_js_scope` and `_find_html_scope` both returned None
and `find_scope` returned None and `_apply_scoped_single` refused
the splice and the patch was recorded as `failed`.

Per-query cost: ~200 ms.  Cache: `@lru_cache(maxsize=128)` on the
subprocess helper, keyed on `(kind, base, name, path, anchor_pos)`.
For the 8 lookups the motivating run needed, ~1.6 s added.  During
reconciliation replay, the same `(kind, base, name, path)` hits the
cache, so it's a dict lookup.

The subprocess only fires when `_find_scope_cached` returned None.
The common case — running from the venv, `in_process: True` — never
spawns a subprocess.  When the subprocess *does* run and also
returns None, the lookup genuinely has no scope; the diagnostic
that `_diagnose_scoped_failure` emits then names the true cause
("scope X could not be located structurally") rather than lying
("scope matched but replacement produced no change") — see
`patch_diag.py`'s HONEST SCOPED-FAILURE DIAGNOSTIC section.

BOUNDARIES
==========
`find_scope` returns `(start_char, end_char, kind)` where:
  · `start_char` is the first char of the declaration — the `@` of
    the first decorator, or the `def` / `class` / `function` /
    `export` / `const` / `let` / `var` / `<details>` if no decorator.
  · `end_char` is one past the last char of the scope's body. When
    the source continues after the scope, `end_char` points just
    past the `\\n` that ends the scope's last line.
  · `kind` is `"python"`, `"js"`, or `"html"`.

`find_scope_body_span` returns `(body_open_char, body_close_char)` —
the *interior* of the scope, excluding the declaration header and
the closer.  This is what the ellipsis-splice path needs: a place
to insert new prefix / suffix lines *inside* the existing body,
without touching the declaration or the closing brace.

`find_enclosing_scope_name` returns the name of the innermost
**function or class** whose span contains a given char position.
Assignments are NOT enclosing scopes: `x = 1` is a name the module
(or a function) binds, not a scope anything lives inside.

`find_scope` returns the FIRST match (by source order). If multiple
scopes share the name, callers that need disambiguation pass
`scope_hint` — currently unused but reserved for future use.

UNKNOWN EXTENSION FALLBACK
==========================
When `path` is empty or unrecognised, `find_scope` and
`find_scope_body_span` try the extensions in order: Python, JS,
HTML, then embedded JS. The HTML fallback exists because callers
that omit `path` (unit tests for the scoped-replace helpers,
ad-hoc scripts) routinely pass HTML sources — the pre-AST
`_SCOPE_PATTERNS[0]` handled those without needing a path.

PYTHON BODY-OPEN / BODY-CLOSE (2026-11-05)
==========================================
`find_scope_body_span` exists because the pre-AST
`patch_scope._find_scope_in_base` returned `None` for Python
`def foo():` scopes — Python bodies use `:` not `{`, so the
`base.find("{")` lookup failed.  That was silently masked by
`_try_deterministic_patch_inner`'s `_strip_ellipsis_lines`
fallback, which did a full scope replace instead of a mid-body
prefix-modification splice.  The pg_pixels.py case (fixture 41)
exercised the correct path for `.js`; the equivalent `.py` case
fell through to the worse fallback.  Every Python file with an
ellipsis-marked edit was affected.  No current fixture caught it
because all three ellipsis fixtures are `.js`.

EMBEDDED JS IN PYTHON STRINGS (2026-11-05)
==========================================
The playground pattern holds JS modules inside Python raw strings:

    PG_PANEL_JS = r\"\"\"...\"\"\"
    function foo() { ... }
    const board = { ... };
    \"\"\"

Before this tier existed, `find_scope(base, "board", "pg_base.py")`
went Python-first (no Python `board` def/class), then — if HTML
was tried — no `<details>` match, then stopped.  A `.py` file with
an embedded JS `const board = {...}` returned `None` for every
`board` lookup, silently declining every heuristic that would have
placed a patch next to that object.  `_apply_scoped_single`,
`_try_insert_after_def`, `_find_scope_body_range` — all four
mutation call sites — threaded `path` and hit the same wall.

`_find_embedded_js_scope` closes this: for `.py` (and for the
empty-path fallback), after Python and HTML tiers decline, the
function iterates every triple-quoted string in `base`, skips
strings below the 100-char threshold used by
`patch_bounds._embedded_js_top_level_stmt_ranges`, filters to
strings that `validate_output._looks_like_js` accepts, parses each
as JS, and translates the match back to outer-file coordinates.

The `.py` path tries tiers in order **Python → HTML → embedded
JS** because:
  · Python is the fastest check and the most common shape.
  · `_find_html_scope` parses the whole `.py` file with tree-sitter-
    html, which is lenient enough to find `<details>` inside a
    Python triple-quoted string directly — no translation needed.
    That tier already handled the fixture 42 case.
  · Embedded JS is the most expensive check (parse Python, iterate
    strings, parse each as JS) and is the last resort.

For the empty-path fallback, the tier order is **Python → JS →
HTML → embedded JS**.  Direct-JS-on-a-Python-file always fails
(tree-sitter-javascript rejects `def` / `r\"\"\"` syntax), so the
JS tier is skipped cleanly and HTML runs first; embedded JS is
last.

`find_scope_body_span` gets the same tier expansion so the
ellipsis-splice path handles `PG_PANEL_JS = r\"\"\"function foo() { ... }\"\"\"`.
`find_enclosing_scope_name` gets it too, so "insert after `foo`"
prose inside embedded JS resolves to the JS function rather than
to nothing.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ._ast import parse_source

# Cache size for the three finder caches.  A mid-sized --apply-to
# run touches ~500 files; 1024 entries hold every (base, name, path)
# triple plus headroom for reconciliation replay.
_SCOPE_CACHE_MAX = 1024

# Minimum length of a triple-quoted string for it to count as a
# candidate for embedded-JS scanning.  Matches the threshold used
# by `patch_bounds._embedded_js_top_level_stmt_ranges` — small
# strings are almost always docstrings or code examples, not
# whole JS modules.
_MIN_EMBEDDED_JS_LEN = 100


# ---------------------------------------------------------------------------
#  Python declaration index  (NEW 2026-11-05)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _python_decl_index(content: str) -> dict:
    """`{name: [nodes]}` for every named declaration in `content`.

    One `ast.walk(tree)` per unique content, cached.  Every scope
    lookup for this file reuses the index instead of re-walking.

    "Named declaration" here means FunctionDef, AsyncFunctionDef,
    ClassDef, `X = ...` (Assign), and `X: T = ...` (AnnAssign).  The
    node type distinguishes scope-with-body (functions/classes) from
    scope-with-value (assignments); callers that need the former
    filter with `isinstance`.

    Returned dict is cached — callers MUST NOT mutate it or the
    lists it contains.  Every existing caller iterates or uses
    `.get(...)` only.
    """
    try:
        tree = parse_source(content)
    except SyntaxError:
        return {}

    idx: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            idx.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    idx.setdefault(tgt.id, []).append(node)
                    break
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            idx.setdefault(node.target.id, []).append(node)
    return idx


def _iter_python_nodes(tree):
    """Yield (name, node) for every named declaration in `tree`.

    Backward-compat shim.  New code should use `_python_decl_index`
    instead — this walks the tree fresh on every call.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            yield node.name, node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    yield tgt.id, node
                    break
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            yield node.target.id, node


# ===========================================================================
#  Runtime AST availability + subprocess fallback  (NEW 2026-11-06)
# ===========================================================================
#
# See the module docstring, SUBPROCESS FALLBACK.

def _has_inprocess_ast() -> bool:
    """True iff at least one tree-sitter grammar is importable in the
    current interpreter."""
    try:
        from .ast_anchor import _has_inprocess_ast as _h
        return _h()
    except Exception:
        return False


def _has_venv_ast() -> bool:
    """True iff the repo .venv has tree-sitter + at least one grammar.

    The underlying probe in `ast_anchor._has_venv_ast` is itself
    `@lru_cache`d via `_VENV_AST_AVAILABLE`, so this wrapper pays the
    import-of-ast_anchor cost once per process.
    """
    try:
        from .ast_anchor import _has_venv_ast as _h
        return _h()
    except Exception:
        return False


@lru_cache(maxsize=128)
def _scope_query_subprocess(
        kind: str, base: str, name: str, path: str,
        anchor_pos: int = 0):
    """Run a scope query in the repo .venv's Python interpreter.

    `kind` is one of:
      · "scope"           — `_find_scope_cached(base, name, path, '')`
      · "body_span"       — `_find_scope_body_span_cached(base, name, path)`
      · "enclosing"       — `_find_enclosing_scope_name_cached(base, anchor_pos, path)`

    Returns the same value the corresponding in-process function
    would return, or None on any failure (subprocess crash,
    unparseable JSON, etc.).  Memoized on the full argument tuple so
    reconciliation replay hits the cache.  See module docstring,
    SUBPROCESS FALLBACK.
    """
    try:
        from .ast_anchor import _find_venv_python, _src_dir
    except ImportError:
        return None
    py = _find_venv_python()
    if py is None:
        return None

    payload = json.dumps({
        "kind": kind,
        "base": base,
        "name": name,
        "path": path,
        "anchor_pos": anchor_pos,
    })
    helper = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(_src_dir())!r})\n"
        "from deeppeek.recover.scope_ast import (\n"
        "    _find_scope_cached, _find_scope_body_span_cached,\n"
        "    _find_enclosing_scope_name_cached,\n"
        ")\n"
        "d = json.load(sys.stdin)\n"
        "kind = d['kind']\n"
        "if kind == 'scope':\n"
        "    r = _find_scope_cached(d['base'], d['name'],\n"
        "                           d['path'], '')\n"
        "    out = None if r is None else list(r)\n"
        "elif kind == 'body_span':\n"
        "    r = _find_scope_body_span_cached(\n"
        "        d['base'], d['name'], d['path'])\n"
        "    out = None if r is None else list(r)\n"
        "elif kind == 'enclosing':\n"
        "    r = _find_enclosing_scope_name_cached(\n"
        "        d['base'], d['anchor_pos'], d['path'])\n"
        "    out = r\n"
        "else:\n"
        "    out = None\n"
        "sys.stdout.write(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [str(py), "-c", helper],
            input=payload, capture_output=True, text=True, timeout=30.0,
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

    if kind == "enclosing":
        return str(data) if data else None
    if isinstance(data, list) and len(data) == 2:
        try:
            return (int(data[0]), int(data[1]))
        except (TypeError, ValueError):
            return None
    if isinstance(data, list) and len(data) == 3:
        try:
            return (int(data[0]), int(data[1]), str(data[2]))
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
#  Public dispatch — thin lru_cache wrappers with subprocess fallback
# ---------------------------------------------------------------------------

def find_scope(base: str, name: str, path: str = "",
               scope_hint: str = "") -> Optional[tuple[int, int, str]]:
    """Return `(start_char, end_char, kind)` for the named scope,
    or None.

    Memoized by `(base, name, path, scope_hint)` — the actual base
    string, not its hash.  See module docstring, CACHE FIX.

    Subprocess fallback (2026-11-06): if `_find_scope_cached`
    returns None AND in-process tree-sitter is unavailable AND the
    repo .venv has it, retry via `_scope_query_subprocess`.  This is
    the fix that closes the 2026-10-30 run's "scope X could not be
    located structurally" residuals for embedded-JS/HTML scopes that
    the running interpreter couldn't parse.  See module docstring,
    SUBPROCESS FALLBACK.
    """
    if not base or not name:
        return None
    result = _find_scope_cached(base, name, path, scope_hint)
    if result is not None:
        return result
    if not _has_inprocess_ast() and _has_venv_ast():
        r = _scope_query_subprocess("scope", base, name, path)
        if isinstance(r, tuple) and len(r) == 3:
            return r
    return None


@lru_cache(maxsize=_SCOPE_CACHE_MAX)
def _find_scope_cached(base: str, name: str, path: str,
                       scope_hint: str):
    return _find_scope_uncached(base, name, path, scope_hint)


def _find_scope_uncached(base, name, path, scope_hint):
    ext = Path(path).suffix.lower() if path else ""

    if ext in (".py", ".pyi"):
        result = _find_python_scope(base, name)
        if result is not None:
            return result
        # Skip tree-sitter-html on .py files that cannot contain an
        # HTML element.  A `.py` source never has `<details>` as
        # syntax; the only way it appears is inside a triple-quoted
        # string, so this guard also implies the string tier exists.
        # Case-sensitive because HTML in practice is lowercase; a
        # mixed-case `<Details>` falls through to embedded-JS
        # scanning (which parses every triple-quoted string) —
        # correct, just slower, and vanishingly rare.
        if "<details" in base or "<summary" in base:
            result = _find_html_scope(base, name)
            if result is not None:
                return result
        if '"""' in base or "'''" in base:
            return _find_embedded_js_scope(base, name)
        return None

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        return _find_js_scope(base, name)

    if ext == ".html":
        return _find_html_scope(base, name)

    # Unknown / empty extension: try each language in turn, ending
    # with embedded JS inside Python strings.
    result = _find_python_scope(base, name)
    if result is not None:
        return result
    result = _find_js_scope(base, name)
    if result is not None:
        return result
    result = _find_html_scope(base, name)
    if result is not None:
        return result
    return _find_embedded_js_scope(base, name)


def find_enclosing_scope_name(base: str, anchor_pos: int,
                               path: str = "") -> Optional[str]:
    """Return the name of the innermost function/class whose span
    contains `anchor_pos`, or None.

    Memoized by `(base, anchor_pos, path)`.  See module docstring,
    CACHE FIX.

    Subprocess fallback (2026-11-06): same tier as `find_scope`.
    """
    if not base or anchor_pos < 0:
        return None
    result = _find_enclosing_scope_name_cached(base, anchor_pos, path)
    if result is not None:
        return result
    if not _has_inprocess_ast() and _has_venv_ast():
        r = _scope_query_subprocess(
            "enclosing", base, "", path, anchor_pos)
        if isinstance(r, str):
            return r
    return None


@lru_cache(maxsize=_SCOPE_CACHE_MAX)
def _find_enclosing_scope_name_cached(base: str, anchor_pos: int,
                                       path: str):
    return _find_enclosing_scope_name_uncached(base, anchor_pos, path)


def _find_enclosing_scope_name_uncached(base, anchor_pos, path):
    ext = Path(path).suffix.lower() if path else ""
    candidates: list = []

    if ext in (".py", ".pyi") or not ext:
        # Index-driven lookup.  Iterates declarations, not tree
        # nodes, and reuses the cached index across calls.
        for _name, nodes in _python_decl_index(base).items():
            for node in nodes:
                if not isinstance(node, (ast.FunctionDef,
                                          ast.AsyncFunctionDef,
                                          ast.ClassDef)):
                    continue
                start = _python_start_char(base, node)
                end = _python_end_char(base, node)
                if start <= anchor_pos < end:
                    candidates.append((node.name, end - start))

    if (ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
            or not ext):
        for node_name, start, end in _iter_js_scope_spans(base):
            if start <= anchor_pos < end:
                candidates.append((node_name, end - start))

    if ext in (".py", ".pyi") or not ext:
        for node_name, start, end in _iter_embedded_js_scope_spans(base):
            if start <= anchor_pos < end:
                candidates.append((node_name, end - start))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def find_scope_body_span(base: str, name: str,
                          path: str = "") -> Optional[tuple[int, int]]:
    """Return `(body_open_char, body_close_char)` for the named
    scope, or None.

    Memoized by `(base, name, path)`.  See module docstring,
    CACHE FIX.

    Subprocess fallback (2026-11-06): same tier as `find_scope`.
    """
    if not base or not name:
        return None
    result = _find_scope_body_span_cached(base, name, path)
    if result is not None:
        return result
    if not _has_inprocess_ast() and _has_venv_ast():
        r = _scope_query_subprocess("body_span", base, name, path)
        if isinstance(r, tuple) and len(r) == 2:
            return r
    return None


@lru_cache(maxsize=_SCOPE_CACHE_MAX)
def _find_scope_body_span_cached(base: str, name: str, path: str):
    return _find_scope_body_span_uncached(base, name, path)


def _find_scope_body_span_uncached(base, name, path):
    ext = Path(path).suffix.lower() if path else ""

    if ext in (".py", ".pyi"):
        result = _find_python_body_span(base, name)
        if result is not None:
            return result
        if "<details" in base or "<summary" in base:
            result = _find_html_body_span(base, name)
            if result is not None:
                return result
        if '"""' in base or "'''" in base:
            return _find_embedded_js_body_span(base, name)
        return None

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        return _find_js_body_span(base, name)
    if ext == ".html":
        return _find_html_body_span(base, name)

    result = _find_python_body_span(base, name)
    if result is not None:
        return result
    result = _find_js_body_span(base, name)
    if result is not None:
        return result
    result = _find_html_body_span(base, name)
    if result is not None:
        return result
    return _find_embedded_js_body_span(base, name)


# ---------------------------------------------------------------------------
#  Python scope resolution — index-driven  (NEW 2026-11-05)
# ---------------------------------------------------------------------------

def _find_python_scope(base: str,
                        name: str) -> Optional[tuple[int, int, str]]:
    # Matches original behavior: assignments are valid "scopes" here
    # (used by the object-literal-replace path), so no type filter.
    candidates = _python_decl_index(base).get(name, [])
    if not candidates:
        return None
    node = min(candidates, key=lambda n: n.lineno)
    return (_python_start_char(base, node),
            _python_end_char(base, node),
            "python")


def _find_python_body_span(base, name):
    candidates = [
        n for n in _python_decl_index(base).get(name, [])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef))
    ]
    if not candidates:
        return None
    node = min(candidates, key=lambda n: n.lineno)
    if not getattr(node, "body", None):
        return None
    body_first = node.body[0]
    body_last = node.body[-1]
    if (body_first.lineno == body_last.end_lineno
            and body_first.lineno == node.lineno):
        # One-line def: `def foo(): pass` — no useful body span.
        return None
    lines = base.split("\n")
    body_open = sum(len(lines[i]) + 1
                    for i in range(body_first.lineno - 1))
    last_line_idx = body_last.end_lineno - 1
    body_close = sum(len(lines[i]) + 1
                     for i in range(last_line_idx))
    if last_line_idx < len(lines):
        body_close += len(lines[last_line_idx])
    return (body_open, body_close)


def _python_start_char(base: str, node) -> int:
    """Char offset of the node's first line, extended up through
    decorators if present."""
    lineno = node.lineno  # 1-based
    for dec in getattr(node, "decorator_list", []) or []:
        if dec.lineno < lineno:
            lineno = dec.lineno
    lines = base.split("\n")
    start = 0
    for i in range(lineno - 1):
        start += len(lines[i]) + 1
    return start


def _python_end_char(base: str, node) -> int:
    """Char offset one past the node's last line (includes trailing
    newline when the file has one)."""
    end_lineno = node.end_lineno  # 1-based inclusive
    lines = base.split("\n")
    end = 0
    for i in range(min(end_lineno, len(lines))):
        end += len(lines[i]) + 1
    return min(end, len(base))


# ---------------------------------------------------------------------------
#  JavaScript / TypeScript
# ---------------------------------------------------------------------------

def _line_starts(base: str) -> list:
    """0-based char offset of each line's start."""
    starts = [0]
    for i, ch in enumerate(base):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _char_of(base: str, line_starts: list, point: tuple) -> int:
    """Convert tree-sitter `(row, col)` to char offset."""
    row, col = point
    if row >= len(line_starts):
        return len(base)
    return line_starts[row] + col


def _walk(node):
    yield node
    for c in node.children:
        yield from _walk(c)


def _node_text(node) -> str:
    t = node.text
    if isinstance(t, bytes):
        return t.decode("utf-8", "replace")
    return t or ""


def _js_decl_name(node) -> Optional[str]:
    """Extract the name from a JS declaration node."""
    if node.type == "export_statement":
        for child in node.children:
            n = _js_decl_name(child)
            if n:
                return n
        return None
    if node.type in ("function_declaration",
                     "generator_function_declaration",
                     "class_declaration"):
        for c in node.children:
            if c.type == "identifier":
                return _node_text(c)
        return None
    if node.type in ("lexical_declaration", "variable_declaration"):
        for c in node.children:
            if c.type == "variable_declarator":
                for cc in c.children:
                    if cc.type == "identifier":
                        return _node_text(cc)
        return None
    return None


def _find_js_scope(base: str, name: str) -> Optional[tuple[int, int, str]]:
    from .ast_anchor import _get_parser
    parser = _get_parser("javascript")
    if parser is None:
        return None
    try:
        tree = parser.parse(base.encode("utf-8"))
    except Exception:
        return None
    if tree is None or tree.root_node.has_error:
        return None

    line_starts = _line_starts(base)
    for node in _walk(tree.root_node):
        if _js_decl_name(node) != name:
            continue
        start = _char_of(base, line_starts, node.start_point)
        end = _char_of(base, line_starts, node.end_point)
        if end < len(base):
            if base[end] == "\n":
                end += 1
            else:
                nl = base.find("\n", end)
                end = len(base) if nl == -1 else nl + 1
        return (start, end, "js")
    return None


def _iter_js_scope_spans(base: str):
    """Yield (name, start_char, end_char) for every named JS scope."""
    from .ast_anchor import _get_parser
    parser = _get_parser("javascript")
    if parser is None:
        return
    try:
        tree = parser.parse(base.encode("utf-8"))
    except Exception:
        return
    if tree is None or tree.root_node.has_error:
        return
    line_starts = _line_starts(base)
    for node in _walk(tree.root_node):
        n = _js_decl_name(node)
        if not n:
            continue
        start = _char_of(base, line_starts, node.start_point)
        end = _char_of(base, line_starts, node.end_point)
        if end < len(base):
            if base[end] == "\n":
                end += 1
            else:
                nl = base.find("\n", end)
                end = len(base) if nl == -1 else nl + 1
        yield n, start, end


def _find_js_body_span(base, name):
    from .ast_anchor import _get_parser
    parser = _get_parser("javascript")
    if parser is None:
        return None
    try:
        tree = parser.parse(base.encode("utf-8"))
    except Exception:
        return None
    if tree is None or tree.root_node.has_error:
        return None
    line_starts = _line_starts(base)
    for node in _walk(tree.root_node):
        if _js_decl_name(node) != name:
            continue
        body_node = _find_js_body_node(node)
        if body_node is None:
            return None
        body_open = _char_of(
            base, line_starts, body_node.start_point) + 1
        body_close = _char_of(
            base, line_starts, body_node.end_point) - 1
        return (body_open, body_close)
    return None


def _find_js_body_node(node):
    """Locate the `{ ... }` interior node of a JS declaration.

    Handles function / generator-function / class / lexically-scoped
    declaration bodies.  Also returns the node itself for `object`
    and `array` literals — this is what makes
    `const board = { ... }` have a body span (interior of the
    object literal).  Without it, `find_scope_body_span(base,
    "board", "x.js")` returned None for every object literal, and
    the ellipsis-splice path declined every `const X = { ... }`
    edit.
    """
    if node.type == "export_statement":
        for child in node.children:
            n = _find_js_body_node(child)
            if n is not None:
                return n
        return None
    if node.type in ("function_declaration",
                     "generator_function_declaration"):
        for child in node.children:
            if child.type == "statement_block":
                return child
        return None
    if node.type == "class_declaration":
        for child in node.children:
            if child.type == "class_body":
                return child
        return None
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            n = _find_js_body_node(child)
            if n is not None:
                return n
        return None
    if node.type == "variable_declarator":
        for child in node.children:
            n = _find_js_body_node(child)
            if n is not None:
                return n
        return None
    if node.type == "arrow_function":
        for child in node.children:
            if child.type == "statement_block":
                return child
        return None
    # Object / array literal: the node itself is the "body".
    if node.type in ("object", "array"):
        return node
    return None


# ---------------------------------------------------------------------------
#  HTML
# ---------------------------------------------------------------------------

def _html_tag_name(node) -> Optional[str]:
    for child in node.children:
        if child.type in ("start_tag", "self_closing_tag"):
            for c in child.children:
                if c.type == "tag_name":
                    return _node_text(c).lower()
    return None


def _html_summary_of(node, base: str) -> str:
    """Return the text of the first `<summary>` descendant, or ""."""
    line_starts = _line_starts(base)
    for child in node.children:
        if child.type == "element" and _html_tag_name(child) == "summary":
            start = _char_of(base, line_starts, child.start_point)
            end = _char_of(base, line_starts, child.end_point)
            text = base[start:end]
            m = re.search(r"<summary[^>]*>(.*?)</summary>",
                          text, re.DOTALL | re.IGNORECASE)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _find_html_scope(base: str, name: str) -> Optional[tuple[int, int, str]]:
    try:
        import tree_sitter_html  # type: ignore
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        parser = Parser(Language(tree_sitter_html.language()))
        tree = parser.parse(base.encode("utf-8"))
    except Exception:
        return None
    if tree is None:
        return None

    line_starts = _line_starts(base)
    for node in _walk(tree.root_node):
        if node.type != "element":
            continue
        if _html_tag_name(node) != "details":
            continue
        summary = _html_summary_of(node, base)
        if summary != name:
            continue
        start = _char_of(base, line_starts, node.start_point)
        end = _char_of(base, line_starts, node.end_point)
        if end < len(base):
            if base[end] == "\n":
                end += 1
            else:
                nl = base.find("\n", end)
                end = len(base) if nl == -1 else nl + 1
        return (start, end, "html")
    return None


def _find_html_body_span(base, name):
    try:
        import tree_sitter_html  # type: ignore
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        parser = Parser(Language(tree_sitter_html.language()))
        tree = parser.parse(base.encode("utf-8"))
    except Exception:
        return None
    if tree is None:
        return None
    line_starts = _line_starts(base)
    for node in _walk(tree.root_node):
        if node.type != "element":
            continue
        if _html_tag_name(node) != "details":
            continue
        summary = _html_summary_of(node, base)
        if summary != name:
            continue
        open_tag = None
        end_tag = None
        for child in node.children:
            if child.type == "start_tag" and open_tag is None:
                open_tag = child
            elif child.type == "end_tag":
                end_tag = child
        if open_tag is None or end_tag is None:
            return None
        body_open = _char_of(
            base, line_starts, open_tag.end_point)
        body_close = _char_of(
            base, line_starts, end_tag.start_point)
        return (body_open, body_close)
    return None


# ===========================================================================
#  Embedded JS inside Python triple-quoted strings
# ===========================================================================

def _iter_embedded_js_decls(base: str):
    """Yield `(name, node, content_start, content, line_starts)` for
    every named JS declaration found inside a triple-quoted string
    of `base` (assumed to be Python source).

    Returns early (yields nothing) when:
      · `base` doesn't parse as Python,
      · tree-sitter-javascript is unavailable,
      · no triple-quoted string is long enough AND looks like JS.

    `content_start` is the char offset (in `base`) of the first char
    of the string's content — the position right after the opening
    `\"\"\"` or `'''`.  `line_starts` is the per-line offset table
    for `content`, used to convert tree-sitter `(row, col)` to a
    char offset inside the string.

    Callers translate `node.start_point` / `node.end_point` to outer
    coordinates by: `content_start + _char_of(content, line_starts,
    point)`.

    The `len(content) < 100` gate matches
    `patch_bounds._embedded_js_top_level_stmt_ranges`.  Small
    strings are docstrings or code examples; parsing them as JS
    produces false positives without changing any real edit.

    Fast-path: if `base` contains no triple-quoted string at all,
    the loop below can never yield — return immediately.  This
    saves `parse_source(base)` + a `_get_parser("javascript")` call
    + N `_looks_like_js` scans for the majority of `.py` files
    (which have docstrings, not embedded JS).
    """
    if '"""' not in base and "'''" not in base:
        return
    from .ast_anchor import _get_parser
    from .validate_output import _iter_triple_quoted, _looks_like_js
    try:
        parse_source(base)
    except SyntaxError:
        return
    parser = _get_parser("javascript")
    if parser is None:
        return
    for content_start, content in _iter_triple_quoted(base):
        if len(content) < _MIN_EMBEDDED_JS_LEN:
            continue
        if not _looks_like_js(content):
            continue
        try:
            tree = parser.parse(content.encode("utf-8"))
        except Exception:
            continue
        if tree is None or tree.root_node.has_error:
            continue
        line_starts = _line_starts(content)
        for node in _walk(tree.root_node):
            n = _js_decl_name(node)
            if not n:
                continue
            yield (n, node, content_start, content, line_starts)


def _find_embedded_js_scope(base: str,
                             name: str) -> Optional[tuple[int, int, str]]:
    """Return `(start_char, end_char, "js")` for the named JS
    declaration inside a Python triple-quoted string, in
    outer-file coordinates, or None.

    See module docstring, EMBEDDED JS IN PYTHON STRINGS.
    """
    for js_name, node, content_start, content, line_starts in \
            _iter_embedded_js_decls(base):
        if js_name != name:
            continue
        inner_start = _char_of(
            content, line_starts, node.start_point)
        inner_end = _char_of(content, line_starts, node.end_point)
        return (content_start + inner_start,
                content_start + inner_end,
                "js")
    return None


def _find_embedded_js_body_span(
        base: str, name: str) -> Optional[tuple[int, int]]:
    """Return `(body_open_char, body_close_char)` for the named JS
    declaration inside a Python triple-quoted string, in outer-file
    coordinates, or None.

    Handles `function foo() { ... }` (interior of the `{}`) and
    `const board = { ... }` (interior of the object literal).  See
    module docstring, EMBEDDED JS IN PYTHON STRINGS.
    """
    for js_name, node, content_start, content, line_starts in \
            _iter_embedded_js_decls(base):
        if js_name != name:
            continue
        body_node = _find_js_body_node(node)
        if body_node is None:
            return None
        inner_open = _char_of(
            content, line_starts, body_node.start_point) + 1
        inner_close = _char_of(
            content, line_starts, body_node.end_point) - 1
        return (content_start + inner_open,
                content_start + inner_close)
    return None


def _iter_embedded_js_scope_spans(base: str):
    """Yield `(name, start_char, end_char)` for every JS declaration
    inside a Python triple-quoted string, in outer-file coordinates.

    Used by `find_enclosing_scope_name`.  See module docstring,
    EMBEDDED JS IN PYTHON STRINGS.
    """
    for js_name, node, content_start, content, line_starts in \
            _iter_embedded_js_decls(base):
        inner_start = _char_of(
            content, line_starts, node.start_point)
        inner_end = _char_of(content, line_starts, node.end_point)
        yield (js_name,
               content_start + inner_start,
               content_start + inner_end)
