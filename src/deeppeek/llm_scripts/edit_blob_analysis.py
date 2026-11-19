"""edit_blob_analysis.py — programmatic heuristics for edit-request classification.

Answers two questions about an extracted code blob:

  1. Is it an EDIT REQUEST (a fragment / patch / replacement that the
     surrounding conversation is asking to APPLY TO AN EXISTING FILE),
     or is it something else (a brand-new file, an isolated snippet for
     reference, a throwaway illustration)?
  2. Which file is it editing?

Same policy as `code_blob_analysis.py`: no LLM, stdlib only, hand-rolled
character-class scanning instead of regex for anything that has to be
reliable across weird inputs.

Signal families:

  EDIT-positive
    · "edit header in context":  'add this to', 'insert this',
      'modify the', 'update the', 'replace the', 'here's the patch',
      'here's the fix', 'apply this', etc.        (+0.6)
    · same header AND a filename nearby            (+0.3, stacks)
    · diff markers: `@@ -N,M +N,M @@`, `diff --git`,
      `<<<<<<<`…`>>>>>>>`, or `--- a/`…`+++ b/`      (+0.6)
    · elision + indented start                     (+0.5)
    · elision alone                                (+0.2)
    · starts indented alone                        (+0.1)
    · Python fragment shape (no docstring, ≤1 top-level def) (+0.1)

  EDIT-negative
    · "full-file header in context" (markdown heading naming a file
      with no edit keywords, or 'here's the complete file', 'save as')  (-0.5)
    · "create-file header in context" ('create a new file', 'new file:') (-0.6)
    · full-file Python structure (docstring + imports + ≥2 defs,
      or `if __name__ == "__main__"` guard)         (-0.4)
    · shebang / license header / #include block / Go `package` / Rust `use` (-0.2)

Sum, clamp to [-1, +1].  |score| ≥ 0.5 → confident answer, skip the
LLM.  Otherwise the caller falls back to the LLM with these signals
attached as hints.

Target-file candidates (highest to lowest confidence):

  1. Unified-diff header (`--- a/path`, `+++ b/path`, `diff --git`)
  2. Filename in the header/context window near `{{handle}}`
  3. Any filename mentioned anywhere in the context
  4. Filename self-reference inside the content (`# path/to.py`,
     `@file` JSDoc, Python docstring)

Public API:

    analyze_edit(block: dict, context_text: str) -> dict

    Keys:
        score              float    -1.0 .. +1.0
        confidence         str      "high_edit" | "high_not_edit" | "ambiguous"
        is_edit_request    bool|None   None when ambiguous
        edit_kind          str|None    "patch"|"fragment"|"replacement"|"insertion"
        signals            list[Signal]
        target_candidates  list[dict]  {"name", "source", "confidence"}
        reasoning          str
"""
from __future__ import annotations

import ast


# ---- extensions we recognize as "likely a code filename" ---------
# (kept identical to code_blob_analysis.py so filename detection
#  behaves the same in both scripts)

_CODE_EXTS = frozenset([
    "py", "pyi",
    "js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx",
    "rs", "go", "c", "h", "cc", "cpp", "cxx", "hpp", "hxx",
    "java", "kt", "kts", "scala",
    "rb", "pl", "pm", "sh", "bash", "zsh",
    "json", "jsonc", "yaml", "yml", "toml", "ini", "cfg",
    "html", "htm", "css", "scss", "sass", "less",
    "md", "markdown", "txt", "xml", "svg",
    "sql", "php", "lua", "swift", "clj", "cljs",
    "ex", "exs", "hs", "elm", "dart", "vue", "svelte",
])


# ---- text scanning (hand-rolled) ---------------------------------

def find_filenames(text: str) -> list[str]:
    """Return all `path/to/file.ext` tokens in `text` whose extension
    is a known code extension."""
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
    if all(c.isdigit() or c in "._" for c in token):
        return False
    return True


def near_handle_lines(context_text: str, handle: str,
                      before_lines: int = 10,
                      after_lines: int = 0) -> str:
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


# ---- fragment / diff detectors -----------------------------------

def has_elision(content: str) -> bool:
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
    for line in content.splitlines():
        if line.strip():
            return line[0] in (" ", "\t")
    return False


def has_diff_markers(content: str) -> bool:
    """True only when the content clearly *is* a unified diff / patch."""
    lines = content.splitlines()
    n = len(lines)
    for i, line in enumerate(lines[:40]):
        s = line.lstrip()
        if s.startswith("@@ -") or s.startswith("@@ "):
            return True
        if s.startswith("diff --git "):
            return True
        if s.startswith("--- "):
            for j in range(i + 1, min(i + 6, n)):
                if lines[j].lstrip().startswith("+++ "):
                    return True
        if s.startswith("<<<<<<<"):
            for j in range(i + 1, n):
                if lines[j].lstrip().startswith(">>>>>>>"):
                    return True
    return False


def diff_target_files(content: str) -> list[str]:
    """Extract target paths from unified-diff / git-diff headers."""
    results: list[str] = []

    def _push(tok: str) -> None:
        if tok.startswith("a/") or tok.startswith("b/"):
            tok = tok[2:]
        if tok.startswith("/dev/null"):
            return
        if _looks_like_filename(tok):
            results.append(tok)

    for line in content.splitlines()[:40]:
        s = line.strip()
        if s.startswith("diff --git "):
            parts = s[len("diff --git "):].split()
            for p in parts[:2]:
                _push(p)
            continue
        if s.startswith("--- ") or s.startswith("+++ "):
            rest = s[4:].strip()
            tok = rest.split()[0] if rest else ""
            if tok:
                _push(tok)
    return results


# ---- edit / full-file / create-file header keywords --------------

_EDIT_HEADER_KW = (
    "add this", "add these", "insert this", "insert these",
    "modify the", "modify this", "update the", "update this",
    "change the", "change this", "replace the", "replace this",
    "remove the", "remove this", "delete the", "delete this",
    "in the function", "inside the class", "in the method",
    "here's the change", "here is the change",
    "here's the patch", "here is the patch",
    "here's the fix", "here is the fix",
    "here's the update", "here is the update",
    "here's the diff", "here is the diff",
    "the relevant", "the affected",
    "you'll need to", "you need to",
    "apply this", "apply these",
    "patch for", "diff for",
    "edit this", "edit the",
    "fix for", "fixes for",
)

_FULL_FILE_KW = (
    "save as", "saved as", "file:", "full file", "complete file",
    "here's the file", "here is the file", "complete implementation",
    "the complete file", "the entire file",
)

_CREATE_FILE_KW = (
    "create file", "new file", "add file", "create a new",
    "add a new file", "create the file", "a brand new file",
)


def _check_edit_header(near_text: str):
    """Return (is_edit, is_full, is_create, filenames)."""
    if not near_text:
        return False, False, False, []
    filenames = find_filenames(near_text)
    lower = near_text.lower()

    is_edit = any(kw in lower for kw in _EDIT_HEADER_KW)
    is_create = any(kw in lower for kw in _CREATE_FILE_KW)

    is_full = any(kw in lower for kw in _FULL_FILE_KW)
    if not is_full and not is_edit and filenames:
        for line in near_text.splitlines():
            s = line.strip()
            if s.startswith("#") and find_filenames(s):
                is_full = True
                break
    return is_edit, is_full, is_create, filenames


# ---- language structure analyzers --------------------------------

def _analyze_python_structure(content: str):
    """Return dict or None if not parseable."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    has_doc = False
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        has_doc = True
    import_count = sum(
        1 for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom)))
    top_defs = sum(
        1 for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)))
    has_main = False
    for n in tree.body:
        if isinstance(n, ast.If):
            t = n.test
            if (isinstance(t, ast.Compare) and
                    isinstance(t.left, ast.Name) and
                    t.left.id == "__name__"):
                has_main = True
                break
    return {
        "has_docstring":   has_doc,
        "import_count":    import_count,
        "top_defs":        top_defs,
        "has_main_guard":  has_main,
    }


def _analyze_generic_structure(content: str, lang: str):
    """Return list of (signal_name, weight).  All negative — they
    indicate full-file structure, which weakly argues against
    "fragment edit"."""
    out: list[tuple[str, float]] = []

    head = content[:500].lower()
    for marker in ("copyright", "spdx-license-identifier",
                   "mit license", "apache license"):
        if marker in head:
            out.append(("license_header", -0.2))
            break

    first = content.split("\n", 1)[0]
    if first.startswith("#!"):
        out.append(("shebang", -0.2))

    if lang in ("c", "cpp", "c++", "h", "hpp", "cc", "cxx"):
        for line in content.splitlines()[:20]:
            if line.strip().startswith("#include"):
                out.append(("c_includes", -0.2))
                break

    if lang == "rust":
        for line in content.splitlines()[:20]:
            if line.strip().startswith("use "):
                out.append(("rust_use_statements", -0.2))
                break

    if lang == "go":
        for line in content.splitlines()[:5]:
            if line.strip().startswith("package "):
                out.append(("go_package_declaration", -0.3))
                break

    if lang in ("html", "htm"):
        if content.lstrip().lower().startswith("<!doctype"):
            out.append(("html_doctype", -0.2))

    return out


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
        if line.lstrip().startswith("#") and find_filenames(line):
            # `# path/to/file.py` at the top of a fragment is a strong hint
            for fn in find_filenames(line):
                hints.append(fn)
            break
    return hints


# ---- main analysis ----------------------------------------------

class Signal:
    __slots__ = ("name", "weight", "detail")
    def __init__(self, name: str, weight: float, detail: str = ""):
        self.name = name
        self.weight = weight
        self.detail = detail
    def __repr__(self) -> str:
        sign = "+" if self.weight > 0 else ""
        return f"{sign}{self.weight:.2f} {self.name}"


def analyze_edit(block: dict, context_text: str) -> dict:
    content = block.get("content", "")
    lang = (block.get("lang") or "").lower()
    handle = block.get("handle", "")

    signals: list[Signal] = []

    near = near_handle_lines(context_text, handle, before_lines=10)
    is_edit_hdr, is_full_hdr, is_create_hdr, near_filenames = \
        _check_edit_header(near)

    if is_edit_hdr:
        signals.append(Signal("edit_header_in_context", +0.6))
        if near_filenames:
            signals.append(Signal("edit_header_with_target", +0.3))
    if is_create_hdr:
        signals.append(Signal("create_file_header_in_context", -0.6))
    if is_full_hdr and not is_edit_hdr and not is_create_hdr:
        signals.append(Signal("full_file_header_in_context", -0.5))

    if has_diff_markers(content):
        signals.append(Signal("diff_markers", +0.6))

    elision = has_elision(content)
    indented = starts_indented(content)
    if elision and indented:
        signals.append(Signal("elision_and_indented_start", +0.5))
    elif elision:
        signals.append(Signal("elision_markers", +0.2))
    elif indented:
        signals.append(Signal("starts_indented", +0.1))

    if lang in ("python", "py", "python3", "py3"):
        py = _analyze_python_structure(content)
        if py is not None:
            if (not py["has_docstring"]
                    and py["top_defs"] <= 1
                    and py["import_count"] <= 1
                    and not py["has_main_guard"]):
                signals.append(Signal("python_fragment_shape", +0.1))
            if (py["has_docstring"]
                    and py["import_count"] >= 2
                    and py["top_defs"] >= 2):
                signals.append(Signal("python_full_file_shape", -0.4))
            if py["has_main_guard"]:
                signals.append(Signal("python_main_guard", -0.4))

    for name, w in _analyze_generic_structure(content, lang):
        signals.append(Signal(name, w))

    score = max(-1.0, min(1.0, sum(s.weight for s in signals)))

    if score >= 0.5:
        confidence = "high_edit"
        is_edit: bool | None = True
    elif score <= -0.5:
        confidence = "high_not_edit"
        is_edit = False
    else:
        confidence = "ambiguous"
        is_edit = None

    # ---- edit_kind ----
    edit_kind: str | None = None
    if is_edit:
        if has_diff_markers(content):
            edit_kind = "patch"
        elif is_full_hdr and not elision and not indented:
            edit_kind = "replacement"
        elif starts_indented(content) or elision:
            edit_kind = "fragment"
        else:
            edit_kind = "insertion"

    # ---- target-file candidates ----
    candidates: list[dict] = []
    seen: set[str] = set()

    for fn in diff_target_files(content):
        if fn not in seen:
            candidates.append({
                "name": fn, "source": "diff_header",
                "confidence": "high",
            })
            seen.add(fn)

    for fn in near_filenames:
        if fn in seen:
            continue
        candidates.append({
            "name": fn,
            "source": "context_near_handle",
            "confidence": "high" if (is_edit_hdr or is_full_hdr) else "medium",
        })
        seen.add(fn)

    if context_text:
        for fn in find_filenames(context_text):
            if fn in seen:
                continue
            candidates.append({
                "name": fn, "source": "context",
                "confidence": "low",
            })
            seen.add(fn)

    for fn in _filenames_from_content(content, lang):
        if fn in seen:
            continue
        candidates.append({
            "name": fn, "source": "content",
            "confidence": "medium",
        })
        seen.add(fn)

    order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: order.get(c["confidence"], 3))

    # ---- reasoning ----
    if is_edit is True:
        lead = f"EDIT REQUEST (score={score:+.2f}, kind={edit_kind})"
    elif is_edit is False:
        lead = f"NOT AN EDIT (score={score:+.2f})"
    else:
        lead = f"ambiguous (score={score:+.2f})"
    parts = [lead]
    if signals:
        parts.append("signals: " + ", ".join(s.name for s in signals))
    if candidates:
        parts.append(
            "targets: " + ", ".join(c["name"] for c in candidates[:3]))
    reasoning = "; ".join(parts)

    return {
        "score":             score,
        "confidence":        confidence,
        "is_edit_request":   is_edit,
        "edit_kind":         edit_kind,
        "signals":           signals,
        "target_candidates": candidates,
        "reasoning":         reasoning,
    }
