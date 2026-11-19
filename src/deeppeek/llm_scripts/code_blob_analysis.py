"""code_blob_analysis.py — programmatic heuristics for code blob classification.

Answers two questions about an extracted code blob:

  1. Is it a COMPLETE FILE, or a FRAGMENT?
  2. What should its filename be?

No LLM.  The whole point is that many cases are answerable from
structure alone:

  · A Python block starting with a module docstring at column 0
    is a file.  Fragments rarely start with a docstring.
  · A block sitting under `## N/M — \`path/file.py\`` in the
    document was named by the author.  The header is the filename.
  · A block whose content matches `# ... existing code` is a
    fragment.  So is one whose first non-empty line is indented.
  · `package main` (Go), `use foo::Bar` (Rust), `#include <...>`
    (C), `<!DOCTYPE html>` (HTML), `#!...` (shebang) are file
    headers, not fragment starters.

Each signal gets a signed weight.  Sum, clamp to [-1, +1].
|score| >= 0.5 → confident answer, skip the LLM.  Otherwise the
caller falls back to the LLM with these signals included as hints.

Naming: candidate filenames come from (highest to lowest confidence):

  1. The markdown heading line directly above the block
  2. A "File: path" / "Save as `path`" line just above the block
  3. Other filename mentions within the block's context window
  4. Filename hints inside the content (`@file` JSDoc, docstring
     self-reference)

The top candidate's `confidence` field says how strong the source
was: "high", "medium", "low".  The caller decides whether to use
it directly or defer to the LLM.

Public API:

    analyze(block: dict, context_text: str) -> dict

    Keys:
        score               float    -1.0 .. +1.0
        confidence          str      "high_full_file" | "high_fragment" | "ambiguous"
        is_full_file        bool|None   None when ambiguous
        signals             list[Signal]
        filename_candidates list[dict]  each {"name", "source", "confidence"}
        reasoning           str

Stdlib only.  `ast` for Python structure.  Text scanning is
hand-rolled (no regex) — filename tokens are found by a character
class walk, matching the extractor's policy of preferring explicit
scanners over regex for anything that has to be reliable across
weird inputs.
"""
from __future__ import annotations

import ast


# ---- extensions we recognize as "likely a code filename" ---------

_CODE_EXTS = frozenset([
    # Python
    "py", "pyi",
    # JS / TS
    "js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx",
    # Rust / Go / C-family
    "rs", "go", "c", "h", "cc", "cpp", "cxx", "hpp", "hxx",
    # JVM / .NET-ish
    "java", "kt", "kts", "scala",
    # Ruby / Perl / shell
    "rb", "pl", "pm", "sh", "bash", "zsh",
    # Config / data
    "json", "jsonc", "yaml", "yml", "toml", "ini", "cfg",
    # Web
    "html", "htm", "css", "scss", "sass", "less",
    # Docs / markup
    "md", "markdown", "txt", "xml", "svg",
    # Data / misc
    "sql", "php", "lua", "swift", "clj", "cljs",
    "ex", "exs", "hs", "elm", "dart", "vue", "svelte",
])


# ---- text scanning (hand-rolled) ---------------------------------

def find_filenames(text: str) -> list[str]:
    """Return all `path/to/file.ext` tokens in `text` where `ext` is
    a known code extension.

    Character-class walk, no regex.  A token is a run of characters
    from `[\\w._/-]`; rejected when its basename has no extension,
    its extension isn't in `_CODE_EXTS`, or it looks like a version
    string / IP / URL fragment rather than a file path.
    """
    results: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if not (c.isalnum() or c in "_.-/"):
            i += 1
            continue
        j = i
        while j < n and (text[j].isalnum() or text[j] in "_.-/"):
            j += 1
        token = text[i:j]
        i = j
        if _looks_like_filename(token):
            results.append(token)
    return results


def _looks_like_filename(token: str) -> bool:
    if "." not in token:
        return False
    if token.startswith("/") or token.endswith("/"):
        return False
    if len(token) > 200 or token.count("/") > 10:
        return False
    basename = token.rsplit("/", 1)[-1]
    if basename.startswith(".") or "." not in basename:
        return False
    stem, ext = basename.rsplit(".", 1)
    if not stem or not ext:
        return False
    if ext.lower() not in _CODE_EXTS:
        return False
    # Reject pure-version tokens like "1.2.3"
    if all(c.isdigit() or c in "._" for c in token):
        return False
    return True


# ---- context window ---------------------------------------------

def near_handle_lines(context_text: str, handle: str,
                      before_lines: int = 8,
                      after_lines: int = 0) -> str:
    """Return the lines around `handle` in `context_text`."""
    if not handle:
        return ""
    lines = context_text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if handle in line:
            idx = i
            break
    if idx is None:
        return ""
    start = max(0, idx - before_lines)
    end = min(len(lines), idx + after_lines + 1)
    return "\n".join(lines[start:end])


# ---- negative-signal detectors ----------------------------------

def has_elision(content: str) -> bool:
    """True when the content has clear elision markers.

    Detects (per line, stripped): `...` alone, `# rest of`,
    `// rest of`, `# existing`, `// existing`, `# similarly for`,
    `// similarly for`, `/* ... */`, `/* ...`.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "...":
            return True
        for marker in ("# rest of", "// rest of",
                       "# existing", "// existing",
                       "# similarly for", "// similarly for",
                       "/* ... */", "/* ..."):
            if marker in stripped:
                return True
    return False


def starts_indented(content: str) -> bool:
    """True when the first non-empty line has leading whitespace."""
    for line in content.splitlines():
        if line.strip():
            return line[0] in (" ", "\t")
    return False


# ---- language analyzers -----------------------------------------

def _analyze_python(content: str) -> list[tuple[str, float]]:
    signals: list[tuple[str, float]] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return signals

    has_docstring = False
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        has_docstring = True
        signals.append(("python_module_docstring", +0.4))

    import_count = sum(
        1 for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
    )
    if import_count >= 3:
        signals.append((f"python_imports({import_count})", +0.3))
    elif import_count >= 1:
        signals.append((f"python_imports({import_count})", +0.2))

    def_count = sum(
        1 for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    class_count = sum(
        1 for n in tree.body if isinstance(n, ast.ClassDef)
    )
    total = def_count + class_count
    if total >= 3:
        signals.append((f"python_definitions({total})", +0.4))
    elif total == 2:
        signals.append(("python_two_definitions", +0.3))
    elif total == 1:
        signals.append(("python_one_definition", +0.1))

    if has_docstring and import_count >= 1 and total >= 1:
        signals.append(("python_typical_file_structure", +0.2))

    return signals


def _analyze_js(content: str) -> list[tuple[str, float]]:
    signals: list[tuple[str, float]] = []
    has_import = False
    has_export = False
    has_strict = False
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("import ") or s.startswith("from "):
            has_import = True
        if (s.startswith('"use strict"')
                or s.startswith("'use strict'")):
            has_strict = True
        if (s.startswith("export ")
                or s.startswith("module.exports")
                or s.startswith("exports.")):
            has_export = True
    if has_strict:
        signals.append(("js_use_strict", +0.2))
    if has_import and has_export:
        signals.append(("js_imports_and_exports", +0.4))
    elif has_import:
        signals.append(("js_imports", +0.2))
    elif has_export:
        signals.append(("js_exports", +0.2))
    return signals


def _analyze_rust(content: str) -> list[tuple[str, float]]:
    signals: list[tuple[str, float]] = []
    if content.startswith("#!["):
        signals.append(("rust_crate_attribute", +0.3))
    lines = content.splitlines()
    if any(l.strip().startswith("use ") for l in lines[:20]):
        signals.append(("rust_use_statements", +0.3))
    if any(l.strip().startswith("pub ") for l in lines):
        signals.append(("rust_public_items", +0.2))
    return signals


def _analyze_go(content: str) -> list[tuple[str, float]]:
    for line in content.splitlines()[:5]:
        if line.strip().startswith("package "):
            return [("go_package_declaration", +0.5)]
    return []


def _analyze_generic(content: str,
                     lang: str) -> list[tuple[str, float]]:
    signals: list[tuple[str, float]] = []
    first_line = content.split("\n", 1)[0]
    if first_line.startswith("#!"):
        signals.append(("shebang", +0.2))
    head = content[:500].lower()
    for marker in ("copyright", "spdx-license-identifier",
                   "mit license", "apache license"):
        if marker in head:
            signals.append(("license_header", +0.2))
            break
    if lang in ("html", "htm"):
        if content.lstrip().lower().startswith("<!doctype"):
            signals.append(("html_doctype", +0.3))
    if lang in ("c", "cpp", "c++", "h", "hpp"):
        for line in content.splitlines()[:20]:
            if line.strip().startswith("#include"):
                signals.append(("c_includes", +0.3))
                break
    return signals


# ---- header detection -------------------------------------------

_FULL_FILE_KW = (
    "save as", "saved as", "file:", "full file", "complete file",
    "here's the file", "here is the file", "complete implementation",
)

_SOFT_FULL_KW = ("here's", "here is", "below is")

_FRAGMENT_KW = (
    "add this", "add these", "insert this", "insert these",
    "modify the", "update the", "change the", "replace the",
    "in the function", "inside the class",
    "here's the change", "here is the change",
    "here's the patch", "here is the patch",
    "here's the fix", "here is the fix",
    "only the", "just the",
    "the relevant", "the affected",
    "you'll need to", "you need to",
)


def _check_header(near_text: str):
    """Return (is_full_file_header, is_fragment_header, filenames)."""
    if not near_text:
        return False, False, []

    filenames = find_filenames(near_text)
    lower = near_text.lower()

    is_fragment = any(kw in lower for kw in _FRAGMENT_KW)

    is_full = False
    for line in near_text.splitlines():
        s = line.strip()
        if s.startswith("#") and find_filenames(s):
            is_full = True
            break
    if not is_full and not is_fragment and filenames:
        for kw in _FULL_FILE_KW:
            if kw in lower:
                is_full = True
                break
        if not is_full:
            for kw in _SOFT_FULL_KW:
                if kw in lower:
                    is_full = True
                    break

    return is_full, is_fragment, filenames


# ---- filename hints from content --------------------------------

def _filenames_from_content(content: str, lang: str) -> list[str]:
    hints: list[str] = []
    if lang in ("python", "py", "python3", "py3"):
        try:
            tree = ast.parse(content)
            doc = ast.get_docstring(tree)
            if doc:
                for fn in find_filenames(doc):
                    if fn.endswith((".py", ".pyi")):
                        hints.append(fn)
        except SyntaxError:
            pass
    for line in content.splitlines()[:15]:
        if "@file" in line:
            hints.extend(find_filenames(line))
    return hints


# ---- main analysis ---------------------------------------------

class Signal:
    __slots__ = ("name", "weight", "detail")
    def __init__(self, name: str, weight: float, detail: str = ""):
        self.name = name
        self.weight = weight
        self.detail = detail
    def __repr__(self) -> str:
        sign = "+" if self.weight > 0 else ""
        return f"{sign}{self.weight:.2f} {self.name}"


def analyze(block: dict, context_text: str) -> dict:
    """Analyze `block` against `context_text` (the stripped file)."""
    content = block.get("content", "")
    lang = (block.get("lang") or "").lower()
    handle = block.get("handle", "")

    signals: list[Signal] = []

    near = near_handle_lines(context_text, handle, before_lines=8)
    is_full_header, is_frag_header, near_filenames = \
        _check_header(near)
    if is_full_header:
        signals.append(Signal("full_file_header_in_context", +0.5,
                              detail=near.splitlines()[-1]
                              if near.splitlines() else ""))
    if is_frag_header:
        signals.append(Signal("fragment_header_in_context", -0.4))

    if has_elision(content):
        signals.append(Signal("elision_markers", -0.6))
    if starts_indented(content):
        signals.append(Signal("starts_indented", -0.4))

    if lang in ("python", "py", "python3", "py3"):
        for name, w in _analyze_python(content):
            signals.append(Signal(name, w))
    elif lang in ("js", "javascript", "jsx", "ts", "typescript",
                  "tsx"):
        for name, w in _analyze_js(content):
            signals.append(Signal(name, w))
    elif lang == "rust":
        for name, w in _analyze_rust(content):
            signals.append(Signal(name, w))
    elif lang == "go":
        for name, w in _analyze_go(content):
            signals.append(Signal(name, w))

    for name, w in _analyze_generic(content, lang):
        signals.append(Signal(name, w))

    score = sum(s.weight for s in signals)
    score = max(-1.0, min(1.0, score))

    if score >= 0.5:
        confidence = "high_full_file"
        is_full_file: bool | None = True
    elif score <= -0.5:
        confidence = "high_fragment"
        is_full_file = False
    else:
        confidence = "ambiguous"
        is_full_file = None

    # Filename candidates
    candidates: list[dict] = []
    seen: set[str] = set()

    for fn in near_filenames:
        if fn in seen:
            continue
        candidates.append({
            "name": fn,
            "source": "context_near_handle",
            "confidence": "high" if is_full_header else "medium",
        })
        seen.add(fn)

    if context_text:
        wide_filenames = find_filenames(context_text)
        wide_counts: dict[str, int] = {}
        for fn in wide_filenames:
            wide_counts[fn] = wide_counts.get(fn, 0) + 1
        for fn, cnt in wide_counts.items():
            if fn in seen or cnt > 3:
                continue
            candidates.append({
                "name": fn,
                "source": "context",
                "confidence": "low",
            })
            seen.add(fn)

    for fn in _filenames_from_content(content, lang):
        if fn in seen:
            continue
        candidates.append({
            "name": fn,
            "source": "content",
            "confidence": "medium",
        })
        seen.add(fn)

    order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: order.get(c["confidence"], 3))

    # Reasoning summary
    if is_full_file is True:
        lead = f"FULL FILE (score={score:+.2f})"
    elif is_full_file is False:
        lead = f"FRAGMENT (score={score:+.2f})"
    else:
        lead = f"ambiguous (score={score:+.2f})"
    parts = [lead]
    if signals:
        parts.append("signals: " + ", ".join(s.name for s in signals))
    if candidates:
        parts.append("filenames: "
                     + ", ".join(c["name"] for c in candidates[:3]))
    reasoning = "; ".join(parts)

    return {
        "score": score,
        "confidence": confidence,
        "is_full_file": is_full_file,
        "signals": signals,
        "filename_candidates": candidates,
        "reasoning": reasoning,
    }
