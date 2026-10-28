"""
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
identifiers only.  That missed every CLI-flag-shaped key
(`"--output_rotation"`, `"-f"`) and every dotted key
(`board.inpaintContext`), i.e. exactly the keys programmers write
in a `@command(args={...})` dict or a config literal.

The shape being placed is most often a `@command` decorator's
`args` dict entry:

    "--output_rotation": {"type": "float", "default": 0.0,
                          "help": "Degrees to rotate ..."},

`_DICT_ENTRY_START_RE` was refusing this.  The body went through
every other heuristic in turn, failed them all, and the record
demoted to LOW trust — for a straightforwardly placeable entry.

Fix: widen the grammar to `[-A-Za-z_$][\w$\-.]*`.  That matches:

    plain            args, params, config
    CLI flag         --foo, -f, --output_rotation
    dotted           foo.bar, board.inpaintContext

Quoted-key (`"some key"`) still requires quoting, matching the
regex's `["\']?` optional prefix.  The `-` leading char is safe
because dict-key position is determined by the trailing `:`, not
the leading char — a subtraction like `a - b` inside a value never
reaches the `:` check on its own line.

DICT-VAR-NAME EXTRACTION (2026-10-28d)
======================================
`_extract_dict_var_names` used to look at prose only — backtick-
wrapped identifiers, then a fallback set (`args`, `kwargs`,
`params`, `options`).  When the prose named a non-dict variable
(e.g. the method `comfy_generate`) but not the dict (`args`), the
heuristic found no candidates and the placement failed.

Motivating case (2026-10-28 comfyui.py, block idx=62): prose said
"find the `comfy_generate` command decorator.  Add:".  Only
`comfy_generate` was backtick-wrapped, `args` never appeared, and
`_extract_dict_var_names` returned `["comfy_generate"]`.  There is
no `comfy_generate = {` in base, so no candidates matched and the
`--output_rotation` entry stayed unplaceable.

Fix: when the body's first line is a dashed key (`--flag`, `-f`),
the body is almost certainly a `@command(args=...)` decorator
entry, and `args` is the conventional dict name.  `args` is added
to the candidate list as a fallback.  This keeps the fix general
(dashed-key body → try `args`) without special-casing any single
conversation.
"""

import re

from .patch_predicates import _context_insert_ok
from .patch_text import _find_matching_brace


# ===========================================================================
#  Context insert
# ===========================================================================

_CONTEXT_INSERT_MAX_LINES = 30
_CONTEXT_INSERT_MIN_ANCHOR_CHARS = 8

# A "structural line" is a bare closing delimiter — `}`, `});`, `};`,
# `]`, `])` etc., optionally followed by `;` or `,`.  These appear in
# every JS file at the end of every block, so `stripped not in base` is
# always False for them (base contains `}` somewhere).  Dropping them
# while keeping the corresponding opener is what silently corrupted the
# pg_panel_fields.py case: `_filter_new` was treating a `}` from a
# balanced body as "already present, remove it" — leaving the inserted
# `if (...) {` with no closer.
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
    # FIX: a scope-shaped decorated method (`@command(...)` at non-zero
    # indent) must not be context-inserted inline — it wants to land
    # inside the container named by prose, handled by
    # `_try_insert_into_named_scope`.  Context insert would otherwise
    # nest it inside whatever existing block the anchor heuristic finds.
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
    # The "insert body[1:] after a unique first line" strategy only
    # works when the first line is a STANDALONE statement.  If the
    # first line is a block opener, body[1:] contains the closer for
    # that opener — inserting body[1:] after base's matching opener
    # means the inserted closer closes base's opener, and base's
    # original closer becomes unmatched (-1 brace).  Refuse and let
    # the caller fall through to `_try_insert_after_anchor`.
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
#
# FIX (2026-10-28): widen the key grammar.  See module docstring,
# DICT-KEY GRAMMAR section.

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
#
# The pg_panel_scrubs.py / comfyui.py `--output_rotation` shape: the AI
# prints a single verbose dict entry, spanning multiple lines, and the
# prose names the dict variable it belongs in (`args`).  The body is
# not a multi-entry block of single-line `key: value,` items (which
# `_try_object_entry_insert` handles); it's one entry with a nested
# multi-line value.
#
# This heuristic:
#   · confirms the body starts with a dict-entry line (`"key": {` or
#     `key: {` or `"--flag": {`);
#   · extracts a plausible dict-variable name from the prose —
#     backtick-wrapped first, then a whitelist of common dict names,
#     then `args` if the body's first key is a dashed flag;
#   · finds that variable's `= {` (or `: {`) in base;
#   · inserts the body before the dict's closing `}` line, at the
#     indent the existing entries use (detected from the first
#     existing entry, so a 2-space or 4-space style both work);
#   · skips any candidate dict that already contains every key in the
#     body (that would produce a duplicate-key insert).

# FIX (2026-10-28): widened key grammar.  See module docstring.
_DICT_ENTRY_START_RE = re.compile(
    r'^\s*["\']?[-A-Za-z_$][\w$\-.]*["\']?\s*:\s*')

_PROSE_VAR_RE = re.compile(r'`([A-Za-z_][\w]*)`')

# Fallback dict-variable names when prose doesn't backtick-wrap them.
# Deliberately narrow: "add to the args dict" is unambiguous; "add to
# the config" is not (config could be a file path, an object, a
# method...).  When the prose is clear, the backtick path handles it.
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

    The last is a 2026-10-28d fix: a body whose first line starts
    with `"--flag"` (a dashed key) is almost certainly a
    `@command(args=...)` decorator entry.  `args` is the conventional
    dict name; adding it as a fallback closes the shape where the
    prose names the method (`comfy_generate`) but not the dict
    (`args`), and no candidate found a dict in base.

    Order matters: backticked identifiers are most reliable and come
    first; the dashed-key fallback comes after the whitelist so an
    explicit prose mention of `kwargs` or `params` still takes
    precedence over the implicit `args` default.
    """
    names = []
    if prose:
        names = list(_PROSE_VAR_RE.findall(prose))

    for cand in _COMMON_DICT_NAMES:
        if prose and re.search(r'\b' + re.escape(cand) + r'\b', prose):
            if cand not in names:
                names.append(cand)

    # Dashed-key fallback (2026-10-28d): see docstring.
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
    """Detect the indent used by existing entries in a dict, falling
    back to `close_indent + default_delta` when the dict is empty.

    The first non-close line after `open_brace` decides.  We skip
    lines whose stripped content is `}` or `},` because those close
    nested values, not entries.
    """
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


def _try_named_dict_insert(base, body, context_before):
    stripped = body.strip()
    if not stripped:
        return None
    first_line = stripped.split("\n", 1)[0]
    if not _DICT_ENTRY_START_RE.match(first_line):
        return None
    # Reject if the body looks like a signature (a `def foo(x) {`):
    # that's `_try_signature_replace` territory.  The entry-start
    # regex allows a dict key named `def`, but a `def foo(` in prose
    # context is much more likely a signature than a dict entry with
    # a key literally called `def`.
    if re.match(r'^\s*["\']?(?:def|function|class)\b', first_line):
        return None

    entry_key_match = _ENTRY_OUTER_KEY_RE.match(first_line)
    entry_key = entry_key_match.group(1) if entry_key_match else None

    # FIX (2026-10-28d): pass the body's first line so
    # `_extract_dict_var_names` can add `args` as a fallback when the
    # body is a dashed-key entry (a `@command(args=...)` decorator arg).
    var_names = _extract_dict_var_names(context_before or "", first_line)
    if not var_names:
        return None

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
                block = base[open_brace:close_brace + 1]
                # FIX (2026-10-28): widened key grammar so a CLI-flag
                # key like `--output_rotation` is correctly detected as
                # pre-existing.
                existing_keys = set(re.findall(
                    r'^\s*["\']?([-A-Za-z_$][\w$\-.]*)["\']?\s*:',
                    block, re.MULTILINE))
                if entry_key in existing_keys:
                    continue
            candidates.append((m, open_brace, close_brace))
        if not candidates:
            continue

        # Prefer the outermost dict whose closing brace comes first.
        # For `args={...}` inside `@command(...)` this is the only
        # candidate; when several `args={}` dicts exist we pick the
        # first one whose key set doesn't already contain the entry.
        m, open_brace, close_brace = candidates[0]

        entry_indent = _existing_entry_indent(
            base, open_brace, close_brace, default_delta="    ")

        body_lines = stripped.split("\n")
        new_lines = _reindent_entry(body_lines, entry_indent)

        line_start = base.rfind("\n", 0, close_brace) + 1
        insertion = "\n".join(new_lines) + "\n"
        result = base[:line_start] + insertion + base[line_start:]
        if result != base:
            return result

    return None
