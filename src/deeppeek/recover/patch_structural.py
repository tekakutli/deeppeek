"""
patch_structural — structural-append fallback.

Insert `body` into `base` at a structurally-inferred point, when no
other heuristic has fired.  This is the "last resort" path:
text-structural reasoning only, no AST (see `ast_anchor` for the
optional AST path).  Two container cases:

  · Direct — body and base share a language.  Append after the last
    top-level declaration (if the body is a declaration) or inside
    the last top-level function (if the body is a statement).

  · Embedded — body is JS, base is Python with a JS-in-a-string
    literal.  Locate the dominant string literal, append inside it.

STABLE: this heuristic rarely changes.  Its contract is "insert at
the end of the most structurally-appropriate container"; adding a
new container shape means a new `_append_*` helper, not a change to
existing helpers.

FIX 7: refuse to append a Python statement keyword at module level.
A `try:` / `for:` / `while:` / `with:` block appended at column 0
references function-local names that don't exist at module scope;
the resulting file parses cleanly (they're valid statements) but is
broken at runtime.  This is the inpaint_server.py `output_rotation`
corruption shape: the try/except was appended after `RAW_ROUTES` at
module level.  `validate_output`'s module-level check catches it
post-write; refusing here is fail-closed earlier.

FIRST-TOKEN LANGUAGE DETECTION (2026-11-05)
===========================================
`_infer_body_lang` previously relied on keyword-count scoring, which
was fragile: a `def _rotate_about_center(...) -> QImage:` block
whose body didn't happen to contain "import " or "self." scored
`py = 2, js = 0` (still Python, but by a thin margin), and a
`function foo(...) {` block scored `js = 1, py = 0` and returned
None because `js >= 2` was required.  The comfyui_krita.py failure
was this: the two helper defs scored below the threshold and
`_try_structural_append` bailed, leaving `paste_image`'s calls to
undefined names.

The fix: trust the FIRST TOKEN.  `def ` / `async def ` / `class ` /
`@` → Python.  `function ` / `export ` / `const ` / `let ` /
`var ` / `async function ` → JavaScript.  The keyword-count scoring
remains as a fallback for ambiguous bodies whose first token names
neither (e.g. a bare assignment).
"""

import os
import re
import sys

from .patch_text import (
    _first_word,
    _reindent_body_to_indent,
    _find_identifier_block_end_line,
)


_DECL_KEYWORDS   = frozenset({"const", "let", "var", "function",
                               "class", "def"})
_STMT_KEYWORDS   = frozenset({"if", "for", "while", "do", "try",
                               "throw", "return", "switch", "raise",
                               "with", "assert"})
_JS_LANGS        = frozenset({"javascript", "typescript", "tsx"})


def _infer_body_lang(body):
    """Best-effort language ID without tree-sitter.

    First-token detection comes first: a body whose first non-blank
    line starts with `def ` / `async def ` / `class ` / `@` is
    Python; `function ` / `export ` / `const ` / `let ` / `var ` /
    `async function ` is JavaScript.  This is more reliable than
    counting keywords in the body, and it is what
    `_try_structural_append` needs to route a top-level `def` block
    to `_append_decl_after_last`.

    Fall back to keyword scoring for bodies whose first token is
    ambiguous (an assignment, a bare `if`, etc.).
    """
    text = body[:800]
    first_line = text.lstrip().split("\n", 1)[0].strip()

    # Direct first-token detection: highest confidence.
    if first_line.startswith(("def ", "async def ", "class ", "@")):
        return "python"
    if first_line.startswith(("function ", "async function ",
                              "export ", "export function ",
                              "export default ", "export class ",
                              "const ", "let ", "var ")):
        return "javascript"

    # Fall back to heuristic scoring for ambiguous shapes.
    js = 0
    py = 0
    if "=>" in text:
        js += 2
    if "function " in text or "const " in text or "let " in text \
            or "var " in text:
        js += 1
    if text.lstrip().startswith(("if (", "for (", "while (", "try {")):
        js += 1
    if "document." in text or "window." in text or "board." in text:
        js += 1
    if "def " in text:
        py += 2
    if "self." in text:
        py += 1
    if "import " in text and ("\n" in text):
        if "import {" not in text and "import *" not in text:
            py += 1

    if js > py and js >= 2:
        return "javascript"
    if py > js and py >= 2:
        return "python"
    return None


def _try_structural_append(base, body, path):
    """Insert `body` into `base` at a structurally-inferred point."""
    if not body or not body.strip():
        return None
    body_lines = body.rstrip().split("\n")
    first = ""
    for ln in body_lines:
        if ln.strip():
            first = ln.lstrip()
            break
    if not first:
        return None
    w = _first_word(first)
    is_decl = w in _DECL_KEYWORDS
    is_stmt = w in _STMT_KEYWORDS
    if not (is_decl or is_stmt):
        return None

    body_lang = _infer_body_lang(body)
    if body_lang is None:
        return None

    from .validate_output import _js_balance
    braces, parens, brackets, unclosed = _js_balance(body)
    if braces != 0 or parens != 0 or brackets != 0 or unclosed:
        print(f"  [structural] {path}: refusing to append "
              f"structurally-unbalanced block "
              f"(braces={braces:+d} parens={parens:+d} "
              f"brackets={brackets:+d})", file=sys.stderr)
        return None

    ext = os.path.splitext(path)[1].lower()
    from .ast_anchor import _LANG_BY_EXT
    container_lang = _LANG_BY_EXT.get(ext)
    if container_lang is None:
        return None

    # FIX 7: refuse to append a Python statement keyword at module
    # level.  See module docstring.
    if container_lang == "python" and w in _STMT_KEYWORDS:
        print(f"  [structural] {path}: refusing to append Python "
              f"statement keyword {w!r} at module level "
              f"(refs to function locals would be undefined)",
              file=sys.stderr)
        return None

    if body_lang == container_lang:
        if is_decl:
            return _append_decl_after_last(base, body, body_lang)
        return _append_stmt_in_last_function(base, body, body_lang)

    if body_lang in _JS_LANGS and container_lang == "python":
        return _append_embedded_in_python_string(
            base, body, body_lang, is_decl)

    return None


def _find_matching_decl_block(content, block_first_line):
    m = re.match(
        r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?)\s*;?\s*$',
        block_first_line)
    if not m:
        return None
    block_rhs = m.group(2)

    def abstract(s):
        s = re.sub(r'"[^"]*"', '""', s)
        s = re.sub(r"'[^']*'", "''", s)
        return s

    block_pattern = abstract(block_rhs)
    lines = content.split("\n")
    matches = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line[0] in (" ", "\t"):
            continue
        lm = re.match(
            r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?)\s*;?\s*$',
            line.strip())
        if not lm:
            continue
        existing_rhs = lm.group(2)
        if abstract(existing_rhs) != block_pattern:
            continue
        existing_ident = lm.group(1)
        end = _find_identifier_block_end_line(content, existing_ident)
        if end is not None:
            matches.append(end + 1)
    if not matches:
        return None
    return matches[-1]


def _find_last_top_level_js_decl_block_end(content):
    lines = content.split("\n")
    last_end = None
    for i, line in enumerate(lines):
        if not line.strip() or line[0] in (" ", "\t"):
            continue
        m = re.match(
            r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=',
            line.strip())
        if m:
            ident = m.group(1)
            end = _find_identifier_block_end_line(content, ident)
            if end is not None:
                last_end = end + 1
    return last_end


def _append_decl_after_last(content, body, lang):
    if lang not in _JS_LANGS:
        stripped = content.rstrip("\n")
        trailing = content[len(stripped):]
        return stripped + "\n\n" + body.rstrip() + trailing

    first_line = ""
    for ln in body.split("\n"):
        if ln.strip():
            first_line = ln.strip()
            break
    if not first_line:
        return None

    insert_line = _find_matching_decl_block(content, first_line)
    if insert_line is None:
        insert_line = _find_last_top_level_js_decl_block_end(content)
    if insert_line is None:
        stripped = content.rstrip("\n")
        trailing = content[len(stripped):]
        return stripped + "\n\n" + body.rstrip() + trailing

    lines = content.split("\n")
    result = "\n".join(
        lines[:insert_line] + [body.rstrip()] + lines[insert_line:])
    return result if result != content else None


def _find_last_top_level_js_function_close(content):
    lines = content.split("\n")
    starts = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or line[0] in (" ", "\t"):
            continue
        if re.match(
                r'(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(',
                stripped):
            starts.append(i)
    if not starts:
        return None
    last_start = starts[-1]

    brace_line = last_start
    brace_col = lines[brace_line].find("{")
    while brace_col < 0 and brace_line < len(lines) - 1:
        brace_line += 1
        brace_col = lines[brace_line].find("{")
    if brace_col < 0:
        return None

    depth = 0
    for j in range(brace_line, len(lines)):
        start_col = brace_col if j == brace_line else 0
        for ch in lines[j][start_col:]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_indent = "  "
                    for k in range(brace_line + 1, len(lines)):
                        if lines[k].strip():
                            indent = lines[k][:len(lines[k]) -
                                              len(lines[k].lstrip())]
                            body_indent = indent
                            break
                    return (j, body_indent)
    return None


def _append_stmt_in_last_function(content, body, lang):
    if lang not in _JS_LANGS:
        return None
    found = _find_last_top_level_js_function_close(content)
    if found is None:
        return None
    close_line, body_indent = found

    reindented = _reindent_body_to_indent(body, body_indent)
    lines = content.split("\n")
    result = "\n".join(
        lines[:close_line] + reindented.split("\n") + lines[close_line:])
    return result if result != content else None


def _append_embedded_in_python_string(base, body, body_lang, is_decl):
    from .ast_anchor import _TRIPLE_DOUBLE_RE, _TRIPLE_SINGLE_RE
    all_matches = list(_TRIPLE_DOUBLE_RE.finditer(base)) \
                  + list(_TRIPLE_SINGLE_RE.finditer(base))
    if not all_matches:
        return None
    all_matches.sort(key=lambda m: m.start())

    for m in reversed(all_matches):
        content = m.group(1)
        if not content or len(content) < 100:
            continue
        if is_decl:
            new_content = _append_decl_after_last(content, body, body_lang)
        else:
            new_content = _append_stmt_in_last_function(
                content, body, body_lang)
        if new_content is None or new_content == content:
            continue
        content_start = m.start(1)
        return (base[:content_start] + new_content
                + base[content_start + len(content):])
    return None
