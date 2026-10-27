"""
recover.symbols — post-patch symbol resolution check.

Catches the class of patch-application error where a correct block
is applied to a file in which its own free identifiers do not
resolve.  Concrete shape (from a real conversation):

    A `pgkitRegisterField("inpaint.enabled", ...)` block was applied
    to `pg_panel_bindings.py`.  That file imports
    `pgkitRegisterAction`, `pgkitRegisterLevel`, and `pgkitNotifyAll`
    from `./pgkit/remote_client.js` — but NOT `pgkitRegisterField`.
    At JS module load time the browser throws `ReferenceError`,
    aborts the boot module, and the canvas blanks.

    The block correctly belonged to `pg_panel_scrubs.py`, which DOES
    import `pgkitRegisterField`.  But deeppeek also added a copy to
    `pg_panel_bindings.py` because the block was emitted as a
    standalone partial and attribution leaned on prose filenames.

The check:

    For each record that had at least one patch or pair applied,
    extract every BARE (non-method) function call introduced by the
    patch that was not already present in the base.  For each such
    call `foo(...)`, verify that `foo` is:

      1. imported in the destination content, OR
      2. declared at the destination's top-level scope, OR
      3. exported by another record that was recovered in this run, OR
      4. a language builtin (console, document, Math, print, len, ...).

    Any call that fails all four is flagged.  Presence of a single
    unresolved call demotes the record to LOW trust under the
    default `--min-trust MEDIUM`, so the target file is preserved
    and the attempt lands in `deeppeek/low_trust/`.

Coverage
========

    .js / .mjs / .jsx / .ts / .tsx
        Full JS extraction: import forms (default, named, namespace,
        aliased), const/let/var/function/class declarations, bare
        calls (method calls excluded by negative lookbehind on
        `.`, `?.`, `!.`).

    .py  (pure Python)
        AST-based extraction: Import, ImportFrom, FunctionDef,
        AsyncFunctionDef, ClassDef, Assign, AnnAssign, Call.

    .py  (JS embedded inside a triple-quoted string)
        The dominant triple-quoted string is extracted and analysed
        as JS.  This is the playground pattern
        (`PANEL_BINDINGS_JS = r\"\"\"...\"\"\"`).  Python-level imports
        and the JS-level imports are kept separate — a JS call must
        resolve against the JS scope, not the Python scope.

False positives
===============

The check errs toward keeping the record.  A name counts as
resolved if it appears anywhere in the destination's import or
declaration set, regardless of scope.  Function-local names,
recursive self-calls, closures, and arrow-function parameters
are all considered resolved.  Only *bare, statement-position,
top-of-file-scope-unresolved* calls are flagged, which is the
narrow class the check exists to catch.
"""
from __future__ import annotations

import ast
import re
from typing import Optional

from .core import FileRecord


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_unresolved_calls(record: FileRecord,
                          base_content: Optional[str],
                          all_records: dict) -> list[str]:
    """Return the sorted list of unresolved bare-call names.

    Empty list means every newly-introduced call resolves.
    Non-empty means the record fail-closes.

    For .py files, patched and base content MUST be analysed via the
    same code path.  If the patched content extracts as embedded JS,
    base is forced to extract the same way (skipping the
    strong-signal threshold for short bodies) so `call_p - call_b`
    is a meaningful set difference.  Without this, a patched .py
    whose embedded JS crossed the length threshold while base's JS
    did not would be analysed as JS on the patched side and as
    Python on the base side — producing false unresolved calls for
    every call that was already in the base JS.  That is the exact
    shape of the pg_panel_fields.py and pg_panel_scrubs.py fixtures,
    where a small base JS string grew past _MIN_LONG_JS_BODY only
    after the patch landed.
    """
    if record.content is None or not record.content.strip():
        return []
    if not record.patches and not record.pairs:
        return []

    is_js_content = False

    if _is_js_lang(record.path):
        is_js_content = True
        imp_p, decl_p, call_p = _analyze_js(record.content)
        if base_content:
            imp_b, decl_b, call_b = _analyze_js(base_content)
        else:
            imp_b, decl_b, call_b = set(), set(), set()
    elif record.path.endswith(".py"):
        embedded_p = _extract_dominant_js_string(record.content)
        if embedded_p is not None:
            is_js_content = True
            embedded_b = (
                _extract_dominant_js_string(base_content, force=True)
                if base_content else None
            )
            imp_p, decl_p, call_p = _analyze_js(embedded_p)
            if embedded_b is not None:
                imp_b, decl_b, call_b = _analyze_js(embedded_b)
            else:
                imp_b, decl_b, call_b = set(), set(), set()
        else:
            imp_p, decl_p, call_p = _analyze_python(record.content)
            if base_content:
                imp_b, decl_b, call_b = _analyze_python(base_content)
            else:
                imp_b, decl_b, call_b = set(), set(), set()
    else:
        return []

    new_calls = call_p - call_b - imp_p - decl_p

    resolvable = imp_p | decl_p | imp_b | decl_b

    # Cross-file resolution: names exported by any other recovered
    # record resolve as if imported.
    for other_path, other_rec in all_records.items():
        if other_path == record.path:
            continue
        if other_rec.content is None:
            continue
        resolvable |= _extract_exported(other_rec.content, other_path)

    # Builtin sets are language-specific.  Embedded-JS .py files use
    # JS builtins (document, window, parseInt, ...) because that is
    # what the JS body actually sees at runtime.
    if is_js_content:
        resolvable |= _JS_BUILTINS
    else:
        resolvable |= _PY_BUILTINS

    return sorted(new_calls - resolvable)



# ---------------------------------------------------------------------------
# Language detection and dispatch
# ---------------------------------------------------------------------------

_JS_EXTS = frozenset({".js", ".mjs", ".jsx", ".ts", ".tsx"})


def _is_js_lang(path: str) -> bool:
    return any(path.endswith(ext) for ext in _JS_EXTS)


_JS_SIGNALS = (
    "const ", "let ", "var ", "function ", "=>", "import ",
    "export ", "require(",
)

# Strong signals: markers that essentially never appear in
# Python prose.  For short embedded strings (see
# _MIN_LONG_JS_BODY), one of these must be present for the
# string to be treated as JS.  Otherwise a docstring that
# happens to mention imports ("import x" is valid Python)
# would be analysed as code.  Short strings that contain only
# weak signals are skipped.
_JS_STRONG_SIGNALS = (
    "import {", "import *", "require(",
    'from "', "from '",
)

# Length below which a candidate JS string must carry a
# _JS_STRONG_SIGNALS marker to be trusted.  Above this
# length, any _JS_SIGNALS marker suffices.
_MIN_LONG_JS_BODY = 200


def _analyze(content: str, path: str):
    if _is_js_lang(path):
        return _analyze_js(content)
    if path.endswith(".py"):
        embedded = _extract_dominant_js_string(content)
        if embedded is not None:
            return _analyze_js(embedded)
        return _analyze_python(content)
    return set(), set(), set()


def _extract_dominant_js_string(py_content: str,
                                force: bool = False) -> Optional[str]:
    """Return the largest triple-quoted string that looks like JS, or
    None.

    Playground .py files that hold JS in a raw string constant are
    the motivating case.  The algorithm:

      1. Scan every triple-quoted string literal in the file.
      2. A string is a JS candidate if it contains at least one
         signal from `_JS_SIGNALS` (the keyword markers that
         distinguish code from prose).
      3. For strings shorter than `_MIN_LONG_JS_BODY`, the candidate
         must additionally contain at least one signal from
         `_JS_STRONG_SIGNALS` — an unambiguous JS marker.  This
         rejects short docstrings that happen to mention a keyword
         (`import x` is valid Python prose, `import {x}` is not)
         while accepting genuinely short embedded JS such as a small
         wiring block.
      4. Among candidates, return the largest.  If none qualify,
         return None.

    The length threshold is not a hard cutoff for candidates; it is
    the boundary below which the weaker signal set is insufficient.

    `force=True` skips the strong-signal requirement for short
    bodies.  Used when the caller has already committed to treating
    this content as embedded JS (e.g. extracting base content after
    the patched content has already extracted as JS).  The same
    rules must apply to both sides so `call_p - call_b` is a
    meaningful set difference rather than an accidental comparison
    between a JS patch and a Python base.
    """
    best = None
    pat = re.compile(
        r'(?:[rbufRBUF]{0,2})("""|\'\'\')(.*?)\1',
        re.DOTALL,
    )
    for m in pat.finditer(py_content):
        body = m.group(2)
        if not body:
            continue
        if not any(sig in body for sig in _JS_SIGNALS):
            continue
        if not force and len(body) < _MIN_LONG_JS_BODY:
            if not any(sig in body for sig in _JS_STRONG_SIGNALS):
                continue
        if best is None or len(body) > len(best):
            best = body
    return best



# ---------------------------------------------------------------------------
# JavaScript extraction
# ---------------------------------------------------------------------------

_JS_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "try", "catch", "finally", "throw", "return", "break", "continue",
    "function", "class", "new", "typeof", "instanceof", "in", "of",
    "delete", "void", "await", "yield", "async", "super", "this",
    "constructor", "get", "set", "static", "extends", "import", "export",
    "default", "from", "as", "with", "debugger", "true", "false", "null",
    "undefined",
})

_JS_BUILTINS = frozenset({
    "Object", "Array", "String", "Number", "Boolean", "Date", "RegExp",
    "Error", "TypeError", "RangeError", "SyntaxError", "Map", "Set",
    "WeakMap", "WeakSet", "Promise", "Symbol", "BigInt", "Proxy",
    "Reflect", "Intl", "JSON", "Math", "console",
    "window", "document", "globalThis", "self",
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "queueMicrotask",
    "atob", "btoa", "alert", "confirm", "prompt",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent",
    "encodeURI", "decodeURI", "escape", "unescape",
    "structuredClone", "URL", "URLSearchParams",
    "Event", "EventTarget", "CustomEvent", "EventSource",
    "WebSocket", "XMLHttpRequest", "FormData", "File", "FileReader",
    "Blob", "Image", "Audio", "Video", "OffscreenCanvas",
    "HTMLCanvasElement", "CanvasRenderingContext2D",
    "DataTransfer", "DragEvent", "KeyboardEvent", "MouseEvent",
})


_JS_IMPORT_RE = re.compile(
    r'\bimport\s+'
    r'(?:'
    r'\*\s+as\s+([A-Za-z_$][\w$]*)'
    r'|([A-Za-z_$][\w$]*)'
    r'|\{\s*([^}]+?)\s*\}'
    r')'
    r'\s+from\s*["\'][^"\']+["\']'
)


def _extract_js_imports(content: str) -> set:
    names = set()
    for m in _JS_IMPORT_RE.finditer(content):
        if m.group(1):
            names.add(m.group(1))
        elif m.group(2):
            names.add(m.group(2))
        elif m.group(3):
            for part in m.group(3).split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    names.add(part.split(" as ", 1)[1].strip())
                else:
                    names.add(part)
    return names

_JS_IMPORT_WITH_SRC_RE = re.compile(
    r'import\s+'
    r'(?:'
    r'\*\s+as\s+([A-Za-z_$][\w$]*)'
    r'|([A-Za-z_$][\w$]*)'
    r'|\{\s*([^}]+?)\s*\}'
    r')'
    r'\s+from\s*["\']([^"\']+)["\']'
)


def _extract_js_imports_with_src(content: str):
    """Return [(imported_name, module_specifier), ...]."""
    out = []
    blanked = _blank_js_comments(content)
    for m in _JS_IMPORT_WITH_SRC_RE.finditer(blanked):
        module = m.group(4)
        if m.group(1):
            out.append((m.group(1), module))
        elif m.group(2):
            out.append((m.group(2), module))
        elif m.group(3):
            for part in m.group(3).split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    part = part.split(" as ", 1)[1].strip()
                out.append((part, module))
    return out

_JS_DECL_RE = re.compile(
    r'(?:^|\n)[ \t]*(?:export\s+)?(?:async\s+)?'
    r'(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)'
)


def _extract_js_decls(content: str) -> set:
    return {m.group(1) for m in _JS_DECL_RE.finditer(content)}


_JS_CALL_RE = re.compile(r'\b([A-Za-z_$][\w$]*)\s*\(')
_METHOD_PREFIX_RE = re.compile(r'(?:\.|\?\.|!\.)\s*$')


def _extract_js_calls(content: str) -> set:
    names = set()
    for m in _JS_CALL_RE.finditer(content):
        pre = content[max(0, m.start() - 3):m.start()]
        if _METHOD_PREFIX_RE.search(pre):
            continue
        name = m.group(1)
        if name in _JS_KEYWORDS:
            continue
        names.add(name)
    return names


def _blank_js_comments(text: str) -> str:
    """Return a same-length copy of `text` with `//` line comments and
    `/* */` block comments replaced by spaces.

    Preserves offsets so regex matches on the result map 1:1 to the
    input.  String literals are NOT blanked: this is the extractor for
    imports (module paths live inside strings) and declarations
    (declaration keywords never sit inside a string).

    A line comment `// when hidden (full-canvas mode)` produces the
    false call `hidden` if we scan raw text.  Blanking first removes
    the entire comment span, so nothing inside it matches the call
    regex.
    """
    if not text:
        return text
    out = list(text)
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str is not None:
            if c == "\\" and i + 1 < n:
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
                while i < n and text[i] != "\n":
                    out[i] = " "
                    i += 1
                continue
            if text[i + 1] == "*":
                out[i] = " "
                out[i + 1] = " "
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    out[i] = " "
                    i += 1
                if i + 1 < n:
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                continue
        i += 1
    return "".join(out)


def _blank_js_comments_and_strings(text: str) -> str:
    """Return a same-length copy of `text` with comments AND string
    literals replaced by spaces.

    Preserves offsets.  Used by the call extractor: a `word (` inside
    a comment or a string is prose, not a call.  Without this, the
    comment `// when hidden (full-canvas mode)` produces a false
    `hidden` call, and a JS string like `"bar ()"` produces a false
    `bar` call.

    Regex literals are NOT handled.  Distinguishing `/pattern/flags`
    from division requires tracking whether `/` sits in operator or
    operand position, which needs a proper JS lexer.  Playground
    files rarely use bare regex literals; when they do and the
    pattern contains `word (`, that produces a false positive.
    Acceptable for now; a future fix can add operator-position
    tracking without changing this function's contract.
    """
    if not text:
        return text
    out = list(text)
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str is not None:
            out[i] = " "
            if c == "\\" and i + 1 < n:
                out[i + 1] = " "
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            out[i] = " "
            in_str = c
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    out[i] = " "
                    i += 1
                continue
            if text[i + 1] == "*":
                out[i] = " "
                out[i + 1] = " "
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    out[i] = " "
                    i += 1
                if i + 1 < n:
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                continue
        i += 1
    return "".join(out)


def _analyze_js(content: str):
    """Extract imports, declarations, and calls from JS content.

    Imports and declarations are extracted from a copy with only
    comments blanked (module paths must survive intact).
    Calls are extracted from a copy with comments AND strings
    blanked (neither can contain a real call).  All three passes see
    the same underlying code; the split only affects what each regex
    is allowed to match.
    """
    no_comments = _blank_js_comments(content)
    no_strings  = _blank_js_comments_and_strings(content)
    return (
        _extract_js_imports(no_comments),
        _extract_js_decls(no_comments),
        _extract_js_calls(no_strings),
    )


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------

_PY_BUILTINS = frozenset({
    "print", "len", "range", "str", "int", "float", "dict", "list",
    "set", "tuple", "bool", "type", "isinstance", "issubclass",
    "getattr", "setattr", "hasattr", "delattr",
    "iter", "next", "enumerate", "zip", "map", "filter", "sorted",
    "sum", "min", "max", "abs", "round", "pow", "divmod",
    "open", "input", "repr", "id", "hash", "callable", "super",
    "vars", "dir", "locals", "globals", "eval", "exec",
    "compile", "format", "bytes", "bytearray", "memoryview",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration",
    "NotImplementedError", "FileNotFoundError", "PermissionError",
    "OSError", "IOError", "BaseException", "KeyboardInterrupt",
    "SystemExit", "None", "True", "False", "self", "cls",
})


def _analyze_python(content: str):
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set(), set(), set()

    imports, decls, calls = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                imports.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
            decls.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    decls.add(tgt.id)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
    return imports, decls, calls


# ---------------------------------------------------------------------------
# Export extraction (cross-file resolution)
# ---------------------------------------------------------------------------

_JS_EXPORT_DECL_RE = re.compile(
    r'(?:^|\n)\s*export\s+(?:async\s+)?'
    r'(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)'
)
_JS_EXPORT_BRACE_RE = re.compile(r'\bexport\s*\{\s*([^}]+?)\s*\}')
_JS_EXPORT_DEFAULT_RE = re.compile(
    r'\bexport\s+default\s+(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)'
)


def _extract_js_exports(content: str) -> set:
    names = set()
    for m in _JS_EXPORT_DECL_RE.finditer(content):
        names.add(m.group(1))
    for m in _JS_EXPORT_BRACE_RE.finditer(content):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            if " as " in part:
                names.add(part.split(" as ", 1)[1].strip())
            else:
                names.add(part)
    for m in _JS_EXPORT_DEFAULT_RE.finditer(content):
        names.add(m.group(1))
    return names


def _extract_python_exports(content: str) -> set:
    """Python has no explicit export: every top-level name is importable."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _extract_exported(content: str, path: str) -> set:
    if _is_js_lang(path):
        return _extract_js_exports(content)
    if path.endswith(".py"):
        embedded = _extract_dominant_js_string(content)
        if embedded is not None:
            return _extract_js_exports(embedded)
        return _extract_python_exports(content)
    return set()
