"""
patch_scope_insert — insert into a named class or function (FIX 8).

Two failure shapes motivate this heuristic:

  1. `inpaint_server.py` — a `try:/except:` fragment that belongs
     inside `handle_inpaint`, after the existing payload-read
     try/except.  The fragment cannot self-declare a scope
     (`try:` isn't a function), the prose anchor (`payload reads`)
     is too vague for `_try_insert_after_anchor`, and structural
     append correctly refuses to put `try:` at module level.

  2. `kritomatic_daemon/handlers/comfyui.py` — a `@command`-decorated
     method body that belongs inside the `ComfyUIHandler` class.
     No scope-decl header (no `def` at column 0 — it's a method at
     4-space indent), no anchor line, and structural append refuses
     to append a decorated function outside a class.

Both share the same intent: "here is an indented body; put it inside
the named scope from the prose".  The heuristic is deliberately
conservative:

  · The body must be consistently indented (all content lines >= 2).
  · The body must NOT carry its own top-level declaration (that's a
    scope-replace or structural-append case, handled elsewhere).
  · The prose must explicitly name the target scope with a structural
    pattern (`in X`, `to X`, ...).  Bare mentions don't fire.

The anchor (when present) is only used to find the *position*; the
scope name determines the *container*.  Without an anchor, insert at
end of scope.

ANCHOR-DERIVED SCOPE (2026-11-05)
=================================
When the prose has an anchor ("after the existing `timeout` parse")
but no explicit scope name ("in `_comfy_generate_impl`"), the scope
can be derived from the anchor's position: find the innermost
class/function declaration whose span contains the anchor.  This
covers the comfyui.py `try/except` fragment, whose prose pointed at
a position but not a container.

The derived scope is only trusted when the anchor occurs at least
once (otherwise there's nothing to derive from); a derived scope
that can't be located falls through to the same fail-closed return
the named-scope path uses.

HIGH CHURN: this is the newest heuristic (FIX 8).  It picks up new
prose patterns, new container shapes, and new anchor idioms as they
surface in the wild.  Isolating it means iterating on it does not
require reading the ~800 lines of anchor / replace / structural code
in the sibling modules.
"""

import re

from .patch_text import (
    _find_matching_brace,
    _reindent_body,
)


_SCOPE_NAME_FROM_PROSE_RES = (
    # "in `Name`", "inside `Name`", "within `Name`"
    re.compile(r'\b(?:in|inside|within)\s+`([A-Za-z_][\w$]*)`',
               re.IGNORECASE),
    # "to `Name`", "add … to `Name`"
    re.compile(r'\bto\s+`([A-Za-z_][\w$]*)`', re.IGNORECASE),
    # "for `Name`"
    re.compile(r'\bfor\s+`([A-Za-z_][\w$]*)`', re.IGNORECASE),
    # "the `Name` function/class/method"
    re.compile(r'\bthe\s+`([A-Za-z_][\w$]*)`\s+'
               r'(?:function|class|method|block|section)\b',
               re.IGNORECASE),
)

_AFTER_ANCHOR_RE = re.compile(
    r'\bafter\s+(?:the\s+)?(?:existing\s+)?`([^`\n]+)`',
    re.IGNORECASE,
)
_AFTER_ANCHOR_EXISTING_RE = re.compile(
    r'\bafter\s+(?:the\s+)?existing\s+([A-Za-z_][\w.]*)',
    re.IGNORECASE,
)

_SCOPE_DECL_HEAD_RE = re.compile(
    r'^(?:class|def|function|const|let|var|export|async|interface|type)\b'
)
_STMT_KEYWORD_HEAD_RE = re.compile(
    r'^(?:try|if|for|while|with|elif|else|except|finally|return|yield)\b'
)


def _extract_scope_name_from_prose(prose):
    if not prose:
        return None
    for pat in _SCOPE_NAME_FROM_PROSE_RES:
        m = pat.search(prose)
        if m:
            return m.group(1)
    return None


def _extract_after_anchor(prose):
    if not prose:
        return None
    m = _AFTER_ANCHOR_RE.search(prose)
    if m:
        return m.group(1).strip()
    m = _AFTER_ANCHOR_EXISTING_RE.search(prose)
    if m:
        return m.group(1)
    return None


def _find_enclosing_scope_name(base: str, anchor: str) -> "str | None":
    """Return the innermost class/function name containing `anchor`'s
    last occurrence in `base`, or None.

    "Last occurrence" — not first — because docstring mentions of the
    anchor appear near the top of the file, while the code that
    actually uses it is much lower.  Taking the last occurrence lands
    on the code, not the docstring.

    "Innermost" — not outermost — because a class method inside a
    class is a more useful container for an inserted statement than
    the class itself.  Walk top-level declarations top-down and keep
    the last one whose start precedes the anchor; that gives the
    innermost enclosing class/function.
    """
    if not base or not anchor:
        return None

    positions = []
    for pat in (
        re.escape(anchor) + r'\s*\(',
        re.escape(anchor) + r'\s*\.',
        re.escape(anchor) + r'\s*\[',
        re.escape(anchor),
    ):
        for m in re.finditer(pat, base):
            positions.append(m.start())
        if positions:
            break
    if not positions:
        return None

    target_pos = max(positions)

    decl_re = re.compile(
        r'(?m)^([ \t]*)(?:async\s+)?(?:def|function|class)\s+(\w+)')

    innermost = None
    for m in decl_re.finditer(base):
        if m.start() >= target_pos:
            break
        # If this declaration's body extends past target_pos, it's a
        # candidate container.
        body_open = base.find("{", m.end())
        if body_open == -1:
            # Python `def`: body extends to the next line at-or-below
            # this indent.  Use the enclosing-decl heuristic directly.
            if _python_decl_contains(base, m.start(), m.group(1),
                                     target_pos):
                innermost = m.group(2)
                continue
        else:
            body_close = _find_matching_brace(
                base, body_open, is_python=False)
            if body_close != -1 and m.start() <= target_pos < body_close:
                innermost = m.group(2)
                continue
            # Python `def foo():` may still have a `:` before any `{`;
            # check the Python case as a fallback.
            if _python_decl_contains(base, m.start(), m.group(1),
                                     target_pos):
                innermost = m.group(2)
    return innermost


def _python_decl_contains(base: str, decl_start: int, decl_indent: str,
                          target_pos: int) -> bool:
    """True iff a Python-style def/class at `decl_start` with indent
    `decl_indent` has a body extending past `target_pos`.

    The declaration's body ends at the next non-blank line whose indent
    is <= `decl_indent`.  A target within that range is inside the
    declaration.
    """
    if target_pos <= decl_start:
        return False
    end_of_line = base.find("\n", decl_start)
    if end_of_line == -1:
        return False
    decl_indent_len = len(decl_indent)
    scan = end_of_line + 1
    while scan < len(base):
        next_nl = base.find("\n", scan)
        if next_nl == -1:
            next_nl = len(base)
        line = base[scan:next_nl]
        if line.strip():
            line_indent_len = len(line) - len(line.lstrip())
            if line_indent_len <= decl_indent_len:
                # Declaration body ends before target_pos?
                return target_pos < scan
        if scan > target_pos:
            return True
        scan = next_nl + 1
    return target_pos > decl_start


def _find_scope_body_range(base, scope_name):
    """Return (scope_start, scope_end, body_indent_str) for a named
    class or function, or None.

    `scope_end` is the character index just AFTER the scope's closing
    brace (JS) or at the first line at-or-below the declaration's
    indent (Python).  `body_indent_str` is the leading whitespace of
    the body's first content line.
    """
    esc = re.escape(scope_name)

    # ---- Python class ----
    m = re.search(r'(?m)^([ \t]*)class\s+' + esc + r'\b', base)
    if m:
        return _python_scope_range(base, m, m.group(1))

    # ---- Python def / async def ----
    m = re.search(
        r'(?m)^([ \t]*)(?:async\s+)?def\s+' + esc + r'\s*\(',
        base)
    if m:
        return _python_scope_range(base, m, m.group(1))

    # ---- JS class ----
    m = re.search(
        r'(?m)^([ \t]*)(?:export\s+)?(?:async\s+)?class\s+'
        + esc + r'\b', base)
    if m:
        return _js_scope_range(base, m)

    # ---- JS function ----
    m = re.search(
        r'(?m)^([ \t]*)(?:export\s+)?(?:async\s+)?function\s+'
        + esc + r'\s*\(', base)
    if m:
        return _js_scope_range(base, m)

    return None


def _python_scope_range(base, decl_match, decl_indent):
    line_end = base.find('\n', decl_match.end())
    if line_end == -1:
        line_end = len(base)
    scan = line_end + 1
    body_indent = None
    while scan < len(base):
        nl = base.find('\n', scan)
        if nl == -1:
            nl = len(base)
        line = base[scan:nl]
        if not line.strip():
            scan = nl + 1
            continue
        ln_indent = line[:len(line) - len(line.lstrip())]
        if len(ln_indent) <= len(decl_indent):
            return (decl_match.start(), scan,
                    body_indent or (decl_indent + "    "))
        if body_indent is None:
            body_indent = ln_indent
        scan = nl + 1
    return (decl_match.start(), len(base),
            body_indent or (decl_indent + "    "))


def _js_scope_range(base, decl_match):
    open_brace = base.find("{", decl_match.end())
    if open_brace == -1:
        return None
    close_brace = _find_matching_brace(base, open_brace, is_python=False)
    if close_brace == -1:
        return None
    body_indent = "  "
    scan = open_brace + 1
    while scan < close_brace:
        nl = base.find('\n', scan)
        if nl == -1 or nl > close_brace:
            break
        line = base[scan:nl]
        if line.strip():
            body_indent = line[:len(line) - len(line.lstrip())]
            break
        scan = nl + 1
    return (decl_match.start(), close_brace + 1, body_indent)


def _find_after_anchor_end(base, anchor, scope_start, scope_end):
    """Find where to insert AFTER the anchor's enclosing block, within
    the scope.  Returns char index or None.
    """
    if not anchor:
        return None
    scope_text = base[scope_start:scope_end]

    # Locate the anchor within the scope.  Prefer `anchor(` call form
    # (the "existing payload reads" case reads as a noun — the code
    # reality is `payload.get(` / `payload[` — but `payload` alone is
    # also fine; rfind finds the last).
    positions = []
    ident = anchor.strip()
    for pat in (
        re.escape(ident) + r'\s*\(',
        re.escape(ident) + r'\s*\.',
        re.escape(ident) + r'\s*\[',
        re.escape(ident),
    ):
        for m in re.finditer(pat, scope_text):
            positions.append(m.start())
        if positions:
            break

    if not positions:
        return None

    last_pos = max(positions)
    line_start = scope_text.rfind('\n', 0, last_pos) + 1
    line_end = scope_text.find('\n', last_pos)
    if line_end == -1:
        line_end = len(scope_text)
    line_text = scope_text[line_start:line_end]
    if not line_text.strip():
        return None
    line_indent = len(line_text) - len(line_text.lstrip())

    # Scan forward for the end of the enclosing block.  `except:`,
    # `finally:`, `else:` continue a try block — skip past them.
    continuation_re = re.compile(r'^(?:except|finally|else|elif)\b')
    scan = line_end + 1
    while scan < len(scope_text):
        next_nl = scope_text.find('\n', scan)
        if next_nl == -1:
            next_nl = len(scope_text)
        next_line = scope_text[scan:next_nl]
        if not next_line.strip():
            scan = next_nl + 1
            continue
        next_indent = len(next_line) - len(next_line.lstrip())
        if next_indent < line_indent:
            if not continuation_re.match(next_line.strip()):
                return scope_start + scan
        scan = next_nl + 1

    return scope_end


def _try_insert_into_named_scope(base, body, context_before, path=""):
    """Insert a scope-shaped body (nested statement or decorated method)
    into a named class/function from the prose.

    Fires only when:
      · body is consistently indented (all content lines >= 2);
      · body does NOT start with a top-level declaration keyword;
      · prose names the target scope with a structural pattern, OR
        the prose has an anchor whose enclosing scope can be derived;
      · the scope exists in base.

    Never crashes the dispatcher: any raise inside scope detection is
    caught by `_try_deterministic_patch`'s outer wrapper.
    """
    if not body or not body.strip():
        return None

    body_stripped = body.lstrip("\n").rstrip()
    body_lines = body_stripped.split("\n")
    first_content = next((ln for ln in body_lines if ln.strip()), "")
    if not first_content:
        return None
    first_indent = len(first_content) - len(first_content.lstrip())
    if first_indent < 2:
        return None  # top-level, not scope-shaped

    for ln in body_lines:
        if not ln.strip():
            continue
        ln_indent = len(ln) - len(ln.lstrip())
        if ln_indent < first_indent:
            return None  # body has a column-0 line: not scope-shaped

    first_stripped = first_content.lstrip()
    if _SCOPE_DECL_HEAD_RE.match(first_stripped):
        return None  # carries its own declaration → scope-replace territory
    if not _STMT_KEYWORD_HEAD_RE.match(first_stripped) \
            and not first_stripped.startswith("@"):
        # Not a statement keyword, not a decorator — could be a
        # method continuation?  Let it through only if the prose
        # names a scope; the guard below decides.  But we want to
        # avoid firing on arbitrary indented prose.
        pass

    scope_name = _extract_scope_name_from_prose(context_before)
    if not scope_name:
        # Anchor-derived scope: the prose names a *position* but not
        # a container.  Find the container by locating the anchor in
        # base and walking up to its enclosing class/function.
        anchor = _extract_after_anchor(context_before)
        if anchor:
            scope_name = _find_enclosing_scope_name(base, anchor)
    if not scope_name:
        return None

    scope_range = _find_scope_body_range(base, scope_name)
    if scope_range is None:
        return None
    scope_start, scope_end, body_indent = scope_range

    anchor = _extract_after_anchor(context_before)
    if anchor:
        insert_pos = _find_after_anchor_end(
            base, anchor, scope_start, scope_end)
        if insert_pos is None:
            insert_pos = scope_end
    else:
        insert_pos = scope_end

    reindented = _reindent_body(body_indent, body_stripped)
    prefix = base[:insert_pos]
    suffix = base[insert_pos:]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    # `reindented + "\n"` already provides a trailing newline; adding
    # a separator on top produced a spurious blank line before the
    # following content.  Suffix either starts with `\n` (a blank
    # line the caller meant to preserve) or with content (joins
    # directly after the trailing newline).
    result = prefix + reindented + "\n" + suffix
    return result if result != base else None
