r"""
patch_misc_insert — context insert, object-entry insert, and
                     named-dict insert.

Three insert heuristics:

  _try_context_insert      2–30 lines, unique first-or-last-line
                           anchor; body inserted adjacent to that
                           anchor line
  _try_object_entry_insert 2–5 lines of `key: value,` entries; prose
                           names a function like `_get()`, the
                           entries land inside its innermost object
                           literal
  _try_named_dict_insert   multi-line dict entry (or several single-
                           line entries); prose names a dict variable
                           (backticked or a common name like `args`),
                           the entries land before the dict's `}`

MEDIUM-STABLE: these have specific, well-defined shapes and rarely
need to grow.  Isolating them keeps the churn-heavy scope and anchor
heuristics from crowding this code in review.

DICT-KEY GRAMMAR (2026-10-28)
=============================
Earlier versions matched keys as `[A-Za-z_$][\w$]*` — plain
identifiers only.  The shape being placed is most often a
`@command` decorator's `args` dict entry:

    "--output_rotation": {"type": "float", "default": 0.0,
                          "help": "Degrees to rotate ..."},

`_DICT_ENTRY_START_RE` was refusing this.  Fixed by widening the
grammar to `[-A-Za-z_$][\w$\-.]*`.

DICT-VAR-NAME EXTRACTION (2026-10-28d)
======================================
`_extract_dict_var_names` used to look at prose only — backtick-
wrapped identifiers, then a fallback set.  When the prose named a
non-dict variable (e.g. the method `comfy_generate`) but not the
dict (`args`), the heuristic found no candidates and placement
failed.  Fixed by adding `args` as a fallback when the body's first
key is a dashed flag.

SCOPE-AWARE CANDIDATE SELECTION (2026-10-28f)
=============================================
`_try_named_dict_insert` used to pick `candidates[0]` — the first
`args={...}` in the file — regardless of which `@command` method
the prose named.

Motivating incident (`kritomatic_daemon/handlers/comfyui.py`):
prose said *"In handlers/comfyui.py, find the `comfy_generate`
command decorator. Add: `--output_rotation`..."*, but the first
`args={...}` in the file belongs to `comfy_connect`.  Patch 62
(the `--output_rotation` entry) landed in `comfy_connect` instead
of `comfy_generate`.  Then patch 95 (the full args replacement)
correctly targeted `comfy_generate` via `_try_object_literal_splice`
(the scoring heuristic correctly picked the block whose body shares
11 of 18 lines).  End state: both functions declared
`--output_rotation`, but only `comfy_connect` was flagged by lint
(pass 8.4 records only the first violation via `break`), so the
diagnostic pointed at the wrong function.

Fix: when `_extract_scope_name_from_prose` names a function AND the
function has a `@command(args={...})` decorator, prefer that args
dict over file-order pick.  If the entry already exists in the
scoped dict, return `base` (already-present); otherwise splice the
entry in.  Only fall back to file-order pick when no scope is named
(backward compat: `test_dict_entry_with_leading_dashes_placed` and
similar tests use prose without a scope).
"""

import re

from .patch_predicates import _context_insert_ok
from .patch_text import _find_matching_brace


# ===========================================================================
#  Context insert
# ===========================================================================

_CONTEXT_INSERT_MAX_LINES = 30
_CONTEXT_INSERT_MIN_ANCHOR_CHARS = 8

_STRUCTURAL_LINE_RE = re.compile(
    r"^\s*"
    r"[}\])]+"
    r"[;,)]*"
    r"\s*$"
)


def _try_context_insert(base, body):
    if not body or not body.strip():
        return None
    body_lines = body.rstrip("\n").split("\n")
    if len(body_lines) < 2 or len(body_lines) > _CONTEXT_INSERT_MAX_LINES:
        return None

    first_stripped = body_lines[0].strip()
    if re.match(r'^(?:export\s+)?(?:const|let|var|function|class)\s+\w+',
                first_stripped):
        return None
    if re.match(r'^(?:def|class)\s+\w+', first_stripped):
        return None
    if first_stripped.startswith("<details") or "<summary>" in body[:200]:
        return None
    if first_stripped.startswith("@"):
        first_indent = len(body_lines[0]) - len(body_lines[0].lstrip())
        if first_indent > 0:
            return None

    def _unique(line):
        stripped = line.strip()
        return (len(stripped) >= _CONTEXT_INSERT_MIN_ANCHOR_CHARS
                and base.count(stripped) == 1)

    def _absent(line):
        stripped = line.strip()
        if not stripped:
            return True
        if _STRUCTURAL_LINE_RE.match(stripped):
            return True
        return stripped not in base

    def _filter_new(lines):
        return [ln for ln in lines if _absent(ln)]

    def _reindent_to(new_lines, anchor_indent):
        if not new_lines:
            return []
        first_indent_len = None
        for ln in new_lines:
            if ln.strip():
                first_indent_len = len(ln) - len(ln.lstrip(" \t"))
                break
        if first_indent_len is None:
            first_indent_len = 0
        out = []
        for ln in new_lines:
            if not ln.strip():
                out.append("")
                continue
            ln_indent = len(ln) - len(ln.lstrip(" \t"))
            relative = max(0, ln_indent - first_indent_len)
            out.append(anchor_indent + " " * relative + ln.lstrip(" \t"))
        return out

    last_line = body_lines[-1]
    if _unique(last_line) and _context_insert_ok(last_line, body):
        candidates = _filter_new(body_lines[:-1])
        if candidates:
            anchor_stripped = last_line.strip()
            anchor_idx = base.find(anchor_stripped)
            line_start = base.rfind("\n", 0, anchor_idx) + 1
            anchor_indent = base[line_start:anchor_idx]
            new_lines = _reindent_to(candidates, anchor_indent)
            insertion = "\n".join(new_lines) + "\n"
            result = base[:line_start] + insertion + base[line_start:]
            if result != base:
                return result

    first_line = body_lines[0]
    first_line_opens = first_line.count("{") - first_line.count("}")
    if (first_line_opens <= 0 and _unique(first_line)
            and _context_insert_ok(first_line, body)):
        candidates = _filter_new(body_lines[1:])
        if candidates:
            anchor_stripped = first_line.strip()
            anchor_idx = base.find(anchor_stripped)
            line_end = base.find("\n", anchor_idx)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            anchor_indent = (
                first_line[:len(first_line) - len(first_line.lstrip(" \t"))])
            new_lines = _reindent_to(candidates, anchor_indent)
            insertion = "\n".join(new_lines) + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    return None


# ===========================================================================
#  Object-entry insert (function-anchored)
# ===========================================================================

_OBJECT_ENTRY_RE = re.compile(
    r'^\s*["\']?[-A-Za-z_$][\w$\-.]*["\']?\s*:\s*.+,?\s*$')
_PROSE_IDENTIFIER_RE = re.compile(r'`([A-Za-z_$][\w$]*)\s*\(\s*\)`')
_PROSE_IDENTIFIER_ALT_RE = re.compile(r'\b([A-Za-z_$][\w$]*)\s*\(\s*\)')


def _try_object_entry_insert(base, body, context_before):
    body_lines = [ln for ln in body.strip().splitlines() if ln.strip()]
    if not (2 <= len(body_lines) <= 5):
        return None
    if not all(_OBJECT_ENTRY_RE.match(ln) for ln in body_lines):
        return None

    matches = _PROSE_IDENTIFIER_RE.findall(context_before)
    if not matches:
        matches = _PROSE_IDENTIFIER_ALT_RE.findall(context_before)
    if not matches:
        return None
    ident = matches[-1]

    func_re = re.compile(
        r'(?:^|\n)\s*(?:export\s+)?(?:async\s+)?'
        r'(?:function|def)\s+' + re.escape(ident) + r'\s*[\(\{]',
        re.MULTILINE)
    func_match = func_re.search(base)
    if not func_match:
        return None

    func_body_open = base.find("{", func_match.end() - 1)
    if func_body_open == -1:
        return None
    func_body_close = _find_matching_brace(base, func_body_open, is_python=False)
    if func_body_close == -1:
        return None

    func_body = base[func_body_open:func_body_close]
    assign_re = re.compile(r'=\s*\{')
    assign_matches = list(assign_re.finditer(func_body))
    if not assign_matches:
        return None

    last_assign = assign_matches[-1]
    obj_open = func_body.find("{", last_assign.start()) + func_body_open
    obj_close = _find_matching_brace(base, obj_open, is_python=False)
    if obj_close == -1:
        return None

    line_start = base.rfind("\n", 0, obj_close) + 1
    obj_indent = base[line_start:obj_close]
    entry_indent = obj_indent + "  "

    new_entries = []
    for ln in body_lines:
        new_entries.append(entry_indent + ln.strip())

    insertion = "\n".join(new_entries) + "\n"
    result = base[:line_start] + insertion + base[line_start:]
    if result == base:
        return None
    return result


# ===========================================================================
#  Named-dict insert (variable-anchored, multi-line entry)
# ===========================================================================

# FIX (2026-10-28): widened key grammar.
_DICT_ENTRY_START_RE = re.compile(
    r'^\s*["\']?[-A-Za-z_$][\w$\-.]*["\']?\s*:\s*')

_PROSE_VAR_RE = re.compile(r'`([A-Za-z_][\w]*)`')

_COMMON_DICT_NAMES = frozenset({
    "args", "kwargs", "params", "options",
})

# FIX (2026-10-28): key grammar widened to match `_DICT_ENTRY_START_RE`.
_ENTRY_OUTER_KEY_RE = re.compile(
    r'^\s*["\']?([-A-Za-z_$][\w$\-.]*)["\']?\s*:\s*')


def _extract_dict_var_names(prose: str,
                            body_first_line: str = "") -> list:
    """Backtick-wrapped identifiers first (most reliable), then a
    whitelist of common dict names found as bare words in the prose,
    then `args` if the body's first key is a CLI flag.
    """
    names = []
    if prose:
        names = list(_PROSE_VAR_RE.findall(prose))

    for cand in _COMMON_DICT_NAMES:
        if prose and re.search(r'\b' + re.escape(cand) + r'\b', prose):
            if cand not in names:
                names.append(cand)

    if body_first_line:
        m = _ENTRY_OUTER_KEY_RE.match(body_first_line)
        if m and m.group(1).startswith("-"):
            if "args" not in names:
                names.append("args")

    return names


def _dict_var_regex(var_name: str) -> "re.Pattern":
    return re.compile(
        r'(?:^|\n)[ \t]*(?:(?:export\s+)?(?:const|let|var)\s+)?'
        r'(?:[A-Za-z_][\w]*\.)*'
        + re.escape(var_name) + r'\s*[=:]\s*\{',
        re.MULTILINE)


def _existing_entry_indent(base: str, open_brace: int,
                            close_brace: int, default_delta: str) -> str:
    """Detect the indent used by existing entries in a dict."""
    line_start = base.rfind("\n", 0, close_brace) + 1
    close_indent = base[line_start:close_brace]
    scan = base.find("\n", open_brace)
    if scan == -1:
        return close_indent + default_delta
    scan += 1
    while scan < close_brace:
        next_nl = base.find("\n", scan)
        if next_nl == -1 or next_nl > close_brace:
            break
        line = base[scan:next_nl]
        stripped = line.strip()
        if stripped and stripped not in ("}", "},", "};"):
            return line[:len(line) - len(line.lstrip())]
        scan = next_nl + 1
    return close_indent + default_delta


def _reindent_entry(body_lines: list, target_indent: str) -> list:
    """Reindent body lines so the first content line sits at
    `target_indent`, preserving relative indent of subsequent lines.
    """
    if not body_lines:
        return []
    first_indent = 0
    for ln in body_lines:
        if ln.strip():
            first_indent = len(ln) - len(ln.lstrip(" \t"))
            break
    out = []
    for ln in body_lines:
        if not ln.strip():
            out.append("")
            continue
        ln_indent = len(ln) - len(ln.lstrip(" \t"))
        rel = max(0, ln_indent - first_indent)
        out.append(target_indent + " " * rel + ln.lstrip(" \t"))
    return out


# ===========================================================================
#  Scope-aware target discovery (2026-10-28f)
# ===========================================================================

def _find_args_in_command_decorator_of(base, scope_name, var_names):
    """Return (open_brace, close_brace) for the args dict inside the
    `@command(...)` decorator that decorates `def <scope_name>`.

    Motivating case (comfyui.py): prose says *"find the
    `comfy_generate` command decorator. Add: `--output_rotation`..."*.
    Without this, `_try_named_dict_insert` picks the first `args={...}`
    in the file, which belongs to `comfy_connect`.  With it, the
    args dict in `comfy_generate`'s decorator is targeted.

    `var_names` is the candidate dict names (e.g. `["args"]`).  Only
    keys that appear in `var_names` are considered.  Returns None if
    `def <scope_name>` doesn't exist, or its decorator has no matching
    args dict.
    """
    if not scope_name or not base:
        return None
    def_re = re.compile(
        r'(?m)^[ \t]*(?:async\s+)?def\s+' + re.escape(scope_name) +
        r'\s*\(\s*self\b'
    )
    def_m = def_re.search(base)
    if not def_m:
        return None
    def_pos = def_m.start()

    # Find the LAST `@command(` before the def line.  Using "last"
    # because a function may have multiple decorators (`@foo\n@command
    # (...)\ndef ...`) and only the closest one is the one whose args
    # dict this is.
    command_re = re.compile(r'(?m)^[ \t]*@command\s*\(')
    last_command = None
    for cm in command_re.finditer(base, 0, def_pos):
        last_command = cm
    if last_command is None:
        return None
    cmd_start = last_command.start()

    region = base[cmd_start:def_pos]
    for var_name in var_names:
        name_re = re.compile(
            r'(?:^|\n)[ \t]*' + re.escape(var_name) + r'\s*[=:]\s*\{',
            re.MULTILINE,
        )
        for m in name_re.finditer(region):
            open_brace = cmd_start + m.end() - 1
            close_brace = _find_matching_brace(
                base, open_brace, is_python=True)
            if close_brace == -1 or close_brace >= def_pos:
                continue
            return (open_brace, close_brace)
    return None


def _scoped_dict_keys(base, open_brace, close_brace):
    """Return the top-level keys of a dict literal."""
    block = base[open_brace:close_brace + 1]
    return set(re.findall(
        r'^\s*["\']?([-A-Za-z_$][\w$\-.]*)["\']?\s*:',
        block, re.MULTILINE))


def _splice_dict_entry(base, body, open_brace, close_brace):
    """Insert `body` as an entry at the end of the dict
    `{open_brace:close_brace}`, before its closing `}` line.
    """
    entry_indent = _existing_entry_indent(
        base, open_brace, close_brace, default_delta="    ")
    body_lines = body.strip().split("\n")
    new_lines = _reindent_entry(body_lines, entry_indent)
    line_start = base.rfind("\n", 0, close_brace) + 1
    insertion = "\n".join(new_lines) + "\n"
    result = base[:line_start] + insertion + base[line_start:]
    return result if result != base else None


def _try_named_dict_insert(base, body, context_before):
    """Insert a dict entry into a variable-named dict.

    Scope-first target selection (2026-10-28f): when the prose names
    a function AND that function's `@command(args={...})` decorator
    exists, prefer that args dict.  If the entry key is already in it,
    return `base` (already-present).  Fall back to file-order pick
    when no scope is named, preserving backward compat.
    """
    stripped = body.strip()
    if not stripped:
        return None
    first_line = stripped.split("\n", 1)[0]
    if not _DICT_ENTRY_START_RE.match(first_line):
        return None
    if re.match(r'^\s*["\']?(?:def|function|class)\b', first_line):
        return None

    entry_key_match = _ENTRY_OUTER_KEY_RE.match(first_line)
    entry_key = entry_key_match.group(1) if entry_key_match else None

    var_names = _extract_dict_var_names(context_before or "", first_line)
    if not var_names:
        return None

    # Scope-aware path.  Only fires when both (a) the prose names a
    # specific function and (b) that function has a `@command(args={...})`
    # decorator whose args dict is findable.  Otherwise fall back to
    # file-order pick (backward compat with tests that use prose
    # without a scope).
    try:
        from .patch_scope_insert import _extract_scope_name_from_prose
        scope_name = _extract_scope_name_from_prose(context_before or "")
    except ImportError:
        scope_name = None

    if scope_name and entry_key is not None:
        scoped_dict = _find_args_in_command_decorator_of(
            base, scope_name, var_names)
        if scoped_dict is not None:
            open_brace, close_brace = scoped_dict
            if entry_key in _scoped_dict_keys(base, open_brace, close_brace):
                return base  # already-present in the scoped dict
            return _splice_dict_entry(base, body, open_brace, close_brace)

    # Fallback: file-order candidates, first non-conflicting wins.
    for var_name in reversed(var_names):
        pat = _dict_var_regex(var_name)
        candidates = []
        for m in pat.finditer(base):
            open_brace = m.end() - 1
            if open_brace < 0 or base[open_brace] != "{":
                continue
            close_brace = _find_matching_brace(base, open_brace)
            if close_brace == -1:
                continue
            if entry_key is not None:
                if entry_key in _scoped_dict_keys(
                        base, open_brace, close_brace):
                    continue
            candidates.append((m, open_brace, close_brace))
        if not candidates:
            continue
        m, open_brace, close_brace = candidates[0]
        return _splice_dict_entry(base, body, open_brace, close_brace)

    return None
