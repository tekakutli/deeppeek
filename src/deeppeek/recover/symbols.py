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

    .py
        ALWAYS analysed as Python first.  When the file is a JS
        container (few top-level defs, one dominant triple-quoted
        JS string), the embedded JS is ALSO analysed and the two
        results are unioned.  This catches both a missing Python
        helper (a `.py` file that lost a top-level `def`) and a
        missing JS import inside an embedded string.

        A previous version treated the branches as exclusive —
        "if embedded JS is present, analyse as JS only".  That
        missed every Python-level unresolved call in files that
        hold both.  The motivating failure: `comfyui_krita.py`
        imported `_rotate_about_center` and `_center_crop` in its
        `paste_image` body but the blocks that would have defined
        them never applied.  `_looks_like_python_module` now skips
        the embedded-JS heuristic for files with substantial
        Python structure, so `comfyui_krita.py` (22 top-level
        defs) is analysed as Python and the missing calls are
        flagged.

False positives
===============

The check errs toward keeping the record.  A name counts as
resolved if it appears anywhere in the destination's import or
declaration set, regardless of scope.  Function-local names,
recursive self-calls, closures, and arrow-function parameters
are all considered resolved.  Only *bare, statement-position,
top-of-file-scope-unresolved* calls are flagged, which is the
narrow class the check exists to catch.

Signature-mismatch is NOT covered by this pass.  A patch that
correctly defines `foo(a)` but is called as `foo(a, b)` does not
produce an unresolved call — the call resolves, the invocation
fails.  That is a separate class (call-arity / keyword-arg
checking) and would be a distinct pass.

CROSS-FILE EXPORT INDEX (2026-11-05)
====================================
`find_unresolved_references` used to re-derive the cross-file export
set on every call — O(N) `_extract_exported` unions per caller
record, hence O(N²) unions per pass.  The cached `_extract_exported`
made each individual union cheap, but the loop itself dominated on
large `--apply-to` runs.

The caller can now precompute the union once and pass it as
`global_exports`.  `pass8_5_check_symbols` and
`reconcile._find_minimum_exclusions` both do this — the former
turns pass 8.5 from O(N²) into O(N + K); the latter removes the
export-set derivation from every exclusion-search trial.

When `global_exports` is None (the default), the function falls back
to the pre-fix O(N) loop.  That path is preserved for ad-hoc callers
(tests, scripts) that only have a small `all_records`.

MEMOIZATION (2026-11-05)
========================
Every helper below that is a pure function of its string argument
is decorated with `@lru_cache(maxsize=None)`.  Motivation:

  · `find_unresolved_references` iterates every OTHER record in the
    run, calling `_extract_exported(other.content, other_path)` for
    each.  Before this cache, that loop was O(N) full AST walks +
    regex scans per call, and `pass8_5_check_symbols` calls it once
    per record — O(T × N) walks per pass.

  · `reconcile._count_failures` calls `find_unresolved_references`
    once per exclusion-search step (O(P²) per record), re-analyzing
    the same content many times per run.

With the cache, `_extract_exported` on a repeated `(content, path)`
is a dict lookup.  `_analyze_python_with_sigs(content)` and
`_analyze_js(content)` are likewise one lookup after the first call.

Cache bounds: `maxsize=None` (unbounded), safe because:
  · The number of distinct `content` strings in a run is bounded by
    the number of records, typically <1000.
  · Each cache lives for the duration of the pipeline run; nothing
    persists across `deeppeek recover` invocations.
  · Memory cost: ~1 KB per content per helper, ~few MB total.

RETURN TYPE SAFETY
==================
Cached values must not be mutated by callers, or the cache is
corrupted for subsequent hits.  Two patterns are used:

  · `_extract_exported`, `_extract_python_exports`, and
    `_extract_js_exports` return `frozenset` — callers cannot
    mutate them.  (The internal `_extract_exported` union is
    written as `frozenset | frozenset`, producing a new frozenset
    each time; the cached value is never in-place mutated.)

  · `_analyze_python_with_sigs` and `_analyze_js` return
    `(frozenset, frozenset, dict, dict)` and
    `(frozenset, frozenset, frozenset)`.  The frozensets are
    immutable; the two dicts in `_analyze_python_with_sigs` carry
    `list` values that the current callers
    (`find_unresolved_references`, `_check_signature_mismatches`)
    treat as read-only.  A future caller that mutates them must
    copy first, or refactor the function to return immutable types.
"""
from __future__ import annotations

import ast
import re
from functools import lru_cache
from typing import Optional

from .core import FileRecord
from ._ast import parse_source


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_unresolved_references(record: FileRecord,
                               base_content: Optional[str],
                               all_records: dict,
                               global_exports: Optional[set] = None,
                               ) -> tuple[list, list]:
    """Return (unresolved_bare_calls, signature_mismatches).

    `unresolved_bare_calls` is the sorted list of new call names that
    no import / decl / builtin / cross-file export resolves.

    `signature_mismatches` is a list of `(call_name, description)`
    for calls whose args are incompatible with the resolved
    definition's signature.  Populated only for Python files whose
    callee is declared in the same content (not imported).

    `global_exports` — optional precomputed union of `_extract_exported`
    over every record in `all_records`.  When supplied, the O(N) loop
    over `all_records` is replaced by a single set-union.  See the
    module docstring, CROSS-FILE EXPORT INDEX.

    Empty lists mean the record is clean.
    """
    if record.content is None or not record.content.strip():
        return [], []
    if not record.patches and not record.pairs:
        return [], []

    is_js_content = False

    if _is_js_lang(record.path):
        is_js_content = True
        imp_p, decl_p, call_p = _analyze_js(record.content)
        sigs_p = {}  # JS signature checking not implemented
        if base_content:
            imp_b, decl_b, call_b = _analyze_js(base_content)
        else:
            imp_b, decl_b, call_b = set(), set(), set()
    elif record.path.endswith(".py"):
        imp_p, decl_p, call_map_p, sigs_p = _analyze_python_with_sigs(
            record.content)
        call_p = set(call_map_p.keys())
        if base_content:
            imp_b, decl_b, call_map_b, sigs_b = \
                _analyze_python_with_sigs(base_content)
        else:
            imp_b, decl_b, call_map_b, sigs_b = set(), set(), {}, {}
        call_b = set(call_map_b.keys())

        if not _looks_like_python_module(record.content):
            embedded_p = _extract_dominant_js_string(record.content)
            if embedded_p is not None:
                is_js_content = True
                jimp_p, jdecl_p, jcall_p = _analyze_js(embedded_p)
                imp_p = imp_p | jimp_p
                decl_p = decl_p | jdecl_p
                call_p = call_p | jcall_p
                embedded_b = (
                    _extract_dominant_js_string(base_content, force=True)
                    if base_content else None
                )
                if embedded_b is not None:
                    jimp_b, jdecl_b, jcall_b = _analyze_js(embedded_b)
                    imp_b = imp_b | jimp_b
                    decl_b = decl_b | jdecl_b
                    call_b = call_b | jcall_b
    else:
        return [], []

    new_calls = call_p - call_b - imp_p - decl_p

    # `_analyze_python_with_sigs` and `_analyze_js` return frozensets
    # (cached; immutable).  `resolvable` below is mutated via `|=`,
    # so it must be a plain `set`.  See the module docstring's
    # RETURN TYPE SAFETY section.
    resolvable = set()
    resolvable.update(imp_p)
    resolvable.update(decl_p)
    resolvable.update(imp_b)
    resolvable.update(decl_b)

    # Cross-file resolution.  With a precomputed `global_exports`, one
    # set-union replaces the O(N) loop over `all_records`.  Without,
    # fall back to the pre-fix loop (ad-hoc callers only).
    if global_exports is not None:
        resolvable |= global_exports
    else:
        for other_path, other_rec in all_records.items():
            if other_path == record.path:
                continue
            if other_rec.content is None:
                continue
            resolvable |= _extract_exported(
                other_rec.content, other_path)

    if is_js_content:
        resolvable |= _JS_BUILTINS
    else:
        resolvable |= _PY_BUILTINS

    unresolved = sorted(new_calls - resolvable)

    mismatched = []
    if not is_js_content and sigs_p:
        # Compute patch-introduced call *sites* via multiset diff.
        # The old `touched = new_calls & sigs_p.keys()` filtered
        # against `new_calls`, which had already had `decl_p`
        # (declared names) subtracted — so a call to a function
        # declared in the same file never got signature-checked.
        #
        # We want call sites that appeared in `content` but not in
        # `base`, regardless of whether the callee is declared
        # (resolved) or not (unresolved).  `unresolved` handles the
        # latter; this handles the former.
        from collections import Counter
        touched_calls = {}
        touched_sigs = {}
        for name in sigs_p:
            sites_p = call_map_p.get(name, [])
            sites_b = call_map_b.get(name, [])
            diff = Counter(sites_p) - Counter(sites_b)
            if diff:
                touched_calls[name] = list(diff.elements())
                touched_sigs[name] = sigs_p[name]
        mismatched = _check_signature_mismatches(
            touched_calls, touched_sigs)

    return unresolved, mismatched


# Backwards-compatible alias — the old name is used in tests.
def find_unresolved_calls(record, base_content, all_records):
    unresolved, _ = find_unresolved_references(
        record, base_content, all_records)
    return unresolved


# ---------------------------------------------------------------------------
# Module-vs-container discrimination
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _looks_like_python_module(py_content: str) -> bool:
    """True iff the file has substantial top-level Python structure.

    A file with 3+ top-level def/class declarations is a real Python
    module, not a JS container.  Used by `find_unresolved_calls` and
    `validate.pass7_5_js_validate` to skip the "extract embedded JS
    string" heuristic for files that are obviously Python-first.
    That heuristic is meant for playground .py files that are
    essentially one big raw JS string; running it on
    `comfyui_krita.py` would analyse its docstrings as JS and miss
    every Python call in the file.

    The threshold of 3 is calibrated against the actual tree:
      · comfyui_krita.py:     22 top-level defs   → module
      · pg_panel_bindings.py: 0 top-level defs, 1 big JS constant → container
      · pg_state.py:          0 top-level defs, 1 big JS constant → container
    Any file that reaches 3+ Python defs is code, not a container.

    Files that fail to parse as Python (should not happen post-
    verify) return False so the old behaviour is preserved.

    Cached.  The `parse_source` call inside is itself memoized, so
    the cost on a cache hit here is 1 walk avoided; on a miss it's
    one parse-cache lookup + one tree walk.
    """
    if not py_content:
        return False
    try:
        tree = parse_source(py_content)
    except SyntaxError:
        return False
    return sum(
        1 for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
    ) >= 3


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


@lru_cache(maxsize=None)
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

    Cached on `(py_content, force)`.  The `force` kwarg is part of
    the cache key, so callers that pass `force=True` and callers
    that use the default `False` do not collide.
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


def _extract_js_imports(content: str) -> frozenset:
    """Return the imported names in `content` as a frozenset.

    Called only from `_analyze_js`, which is itself cached; caching
    this independently would be redundant.  Return type is frozen
    so the outer `_analyze_js` can safely return the result as part
    of its cached tuple.
    """
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
    return frozenset(names)

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


def _extract_js_decls(content: str) -> frozenset:
    """Return the top-level JS declaration names as a frozenset.

    Called only from `_analyze_js`; frozen so the outer cached
    tuple is immutable.
    """
    return frozenset(m.group(1) for m in _JS_DECL_RE.finditer(content))


_JS_CALL_RE = re.compile(r'\b([A-Za-z_$][\w$]*)\s*\(')
_METHOD_PREFIX_RE = re.compile(r'(?:\.|\?\.|!\.)\s*$')


def _extract_js_calls(content: str) -> frozenset:
    """Return the bare-call names in `content` as a frozenset.

    Called only from `_analyze_js`; frozen so the outer cached
    tuple is immutable.
    """
    names = set()
    for m in _JS_CALL_RE.finditer(content):
        pre = content[max(0, m.start() - 3):m.start()]
        if _METHOD_PREFIX_RE.search(pre):
            continue
        name = m.group(1)
        if name in _JS_KEYWORDS:
            continue
        names.add(name)
    return frozenset(names)


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


@lru_cache(maxsize=None)
def _analyze_js(content: str):
    """Extract imports, declarations, and calls from JS content as
    `(frozenset, frozenset, frozenset)`.

    Imports and declarations are extracted from a copy with only
    comments blanked (module paths must survive intact).
    Calls are extracted from a copy with comments AND strings
    blanked (neither can contain a real call).  All three passes see
    the same underlying code; the split only affects what each regex
    is allowed to match.

    Cached; frozen return so callers cannot mutate the shared value.
    The three regex passes over the two blanked copies are the cost
    being amortized.
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

@lru_cache(maxsize=None)
def _analyze_python_with_sigs(content: str):
    """Return `(imports, decls, calls_by_name, sigs_by_name)`.

    `imports` and `decls` are frozensets — cached, so callers cannot
    mutate them.  `calls_by_name[name]` is a list of
    `(pos_arg_count, frozenset(kw_names))`; `sigs_by_name[name]` is
    `(pos_arity, pos_names, kwonly, has_vararg, has_kwarg)`.

    Enables signature-level checking: a call `foo(1, k=2)` is
    compatible with `def foo(x, k):` but not with `def foo(x):`.

    Cached.  Callers (`find_unresolved_references`,
    `_check_signature_mismatches`) read the returned dicts but do
    not mutate them.  A future caller that mutates must copy
    first — otherwise the cached value is corrupted for every
    subsequent record.
    """
    try:
        tree = parse_source(content)
    except SyntaxError:
        return frozenset(), frozenset(), {}, {}

    imports, decls = set(), set()
    calls: dict = {}
    sigs: dict = {}

    def _record_sig(node):
        args = node.args
        pos_arity = len(args.posonlyargs) + len(args.args)
        pos_names = frozenset(
            a.arg for a in (args.posonlyargs + args.args)
        )
        kwonly = frozenset(a.arg for a in args.kwonlyargs)
        sigs[node.name] = (
            pos_arity, pos_names, kwonly,
            args.vararg is not None,
            args.kwarg is not None,
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imports.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decls.add(node.name)
            _record_sig(node)
        elif isinstance(node, ast.ClassDef):
            decls.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    decls.add(tgt.id)
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name)):
            kw = frozenset(kw.arg for kw in node.keywords if kw.arg)
            calls.setdefault(node.func.id, []).append(
                (len(node.args), kw))

    return frozenset(imports), frozenset(decls), calls, sigs


def _check_signature_mismatches(calls: dict, sigs: dict) -> list:
    """Return [(name, description)] for calls whose arguments are
    incompatible with the resolved definition.

    Python's calling convention: a positional-or-keyword parameter
    accepts either a positional argument or a keyword argument with
    its name.  Only *kw-only* parameters (`def foo(*, x)`) reject
    positional arguments.  So a call `foo(a=1, b=2)` is legal for
    `def foo(a, b)`.  The `allowed` set is `pos_names | kwonly`,
    not `kwonly` alone.

    Conservative otherwise: only flags when the callee's signature
    definitively rejects the call — arity overflow beyond a non-
    vararg signature, or keyword args the signature cannot bind.
    """
    problems = []
    for name, sites in calls.items():
        sig = sigs.get(name)
        if sig is None:
            continue  # class, imported, or from an outer scope
        pos_arity, pos_names, kwonly, has_vararg, has_kwarg = sig
        for n_args, kw in sites:
            if not has_vararg and n_args > pos_arity:
                problems.append(
                    (name, f"called with {n_args} positional arg(s) "
                           f"but declares at most {pos_arity}"))
                continue
            if not has_kwarg:
                allowed = pos_names | kwonly
                extra = kw - allowed
                if extra:
                    problems.append(
                        (name, f"called with unexpected keyword "
                               f"argument(s) {sorted(extra)}; "
                               f"declares {sorted(allowed) or 'none'}"))
    return problems

def _analyze_python(content: str):
    try:
        tree = parse_source(content)
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


@lru_cache(maxsize=None)
def _extract_js_exports(content: str) -> frozenset:
    """Return the set of names exported by JS content as a frozenset.

    Cached; frozen so callers cannot mutate the shared value.
    """
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
    return frozenset(names)


@lru_cache(maxsize=None)
def _extract_python_exports(content: str) -> frozenset:
    """Python has no explicit export: every top-level name is importable.

    Returns a `frozenset` — callers cannot mutate the cached value.
    """
    try:
        tree = parse_source(content)
    except SyntaxError:
        return frozenset()
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
    return frozenset(names)


@lru_cache(maxsize=None)
def _extract_exported(content: str, path: str) -> frozenset:
    """Return the names exported by `content` interpreted as `path`'s
    language.

    For a `.py` file: always Python exports, PLUS JS exports from
    any embedded JS string when the file is genuinely a container.
    A Python module (3+ top-level defs) exports its Python names;
    the docstring-derived "embedded JS" is not trusted.

    Guarded by `_looks_like_python_module` for the same reason
    `find_unresolved_references` is: a Python docstring mentioning
    "import" or "function" must not be treated as a JS export
    surface.

    Cached.  Returns frozenset: the union is written as
    `frozenset | frozenset` (which produces a new value, never
    mutating the cached input), and callers do
    `resolvable |= _extract_exported(...)` (which mutates
    `resolvable`, not the returned value).
    """
    if _is_js_lang(path):
        return _extract_js_exports(content)  # already frozenset
    if path.endswith(".py"):
        exports = _extract_python_exports(content)  # frozenset
        if not _looks_like_python_module(content):
            embedded = _extract_dominant_js_string(content)
            if embedded is not None:
                exports = exports | _extract_js_exports(embedded)
        return exports
    return frozenset()
