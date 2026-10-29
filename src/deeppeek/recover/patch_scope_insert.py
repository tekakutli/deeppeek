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

SCOPE-HINT FALLBACK (2026-11-05)
================================
`_try_insert_into_named_scope` now accepts an optional `scope_hint`
parameter.  When the fence's own prose is scope-silent (e.g. "And in
every failure return:"), `_extract_scope_name_from_prose` returns
`None` and the caller passes the enclosing H2-derived scope from
`Block.scope_hint`.

Motivating case: block 634 in the 2026-10-28 run.  Prose: "And in
every failure return:".  Enclosing H2: "## Fix 3 — surface
per-command errors in `handle_inpaint`".  Pass 1 attached
`scope_hint="handle_inpaint"` to the block; pass 4 used it for
attribution; but the patch dict built in `pass6_apply_patches` lost
it, so the dispatcher had nothing to insert into.  The block
orphaned, went to the residual as `orphan_unattributed`.

The fix: `pass6_apply_patches` copies `scope_hint` onto the patch
dict, and this heuristic accepts it as a second-class source of the
scope name.  Prose continues to win when present (the fence's own
context is more specific than the enclosing section's).

ANCHOR-DERIVED SCOPE (2026-11-05)
=================================
When the prose has an anchor ("after the existing `timeout` parse")
but no explicit scope name ("in `_comfy_generate_impl`"), the scope
can be derived from the anchor's position: find the innermost
class/function declaration whose span contains the anchor.  This
covers the comfyui.py `try/except` fragment, whose prose pointed at
a position but not a container.

STRUCTURAL VALIDATION (2026-11-02)
==================================
After building the candidate result, `_verify_scope_insertion`
parses both `base` and `result` and verifies that any declaration
in the inserted body landed as a DIRECT member of the target scope.

Motivating failure (test_40, comfyui.py method-insert): the
inserter once produced a valid-Python but semantically wrong result
— the new method nested inside an existing sibling method at
class-body indent.  It parses cleanly (nested def with its own
body) but is dead code.  The AST check catches it: a `def new_cmd`
that isn't a direct child of the class is rejected.

Fail-closed: if the AST check fails, `_try_insert_into_named_scope`
returns None and the pipeline falls through to other heuristics
(or fail-closed at the record level).

SCOPE PROSE EXTENSION (2026-10-28e)
===================================
`_extract_scope_name_from_prose` also matches "find the `X`
command/method/function/..." and "the `X` command/method/function/..."
shapes.  Motivating case: `kritomatic_daemon/handlers/comfyui.py`
prose reads "In `handlers/comfyui.py`, find the `comfy_generate`
command decorator. Add:" — with the extension, the extractor
returns `comfy_generate`, which
`patch_anchor_insert._anchor_within_callee` then resolves to
`_comfy_generate_impl`.

HIGH CHURN: this is the newest heuristic (FIX 8).
"""

import ast
import re
import sys
import textwrap

from .patch_text import (
    _find_matching_brace,
    _reindent_body,
)


_SCOPE_NAME_FROM_PROSE_RES = (
    re.compile(r'\b(?:in|inside|within)\s+`([A-Za-z_][\w$]*)`',
               re.IGNORECASE),
    re.compile(r'\bto\s+`([A-Za-z_][\w$]*)`', re.IGNORECASE),
    re.compile(r'\bfor\s+`([A-Za-z_][\w$]*)`', re.IGNORECASE),
    re.compile(r'\bthe\s+`([A-Za-z_][\w$]*)`\s+'
               r'(?:function|class|method|block|section)\b',
               re.IGNORECASE),
    re.compile(r'\bfind\s+the\s+`([A-Za-z_][\w$]*)`\s+'
               r'(?:command|method|function|class|handler|endpoint|'
               r'decorator|implementation)\b',
               re.IGNORECASE),
    re.compile(r'\bthe\s+`([A-Za-z_][\w$]*)`\s+'
               r'(?:command|method|function|class|handler|endpoint|'
               r'decorator|implementation)\b',
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
        body_open = base.find("{", m.end())
        if body_open == -1:
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
            if _python_decl_contains(base, m.start(), m.group(1),
                                     target_pos):
                innermost = m.group(2)
    return innermost


def _python_decl_contains(base: str, decl_start: int, decl_indent: str,
                          target_pos: int) -> bool:
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
                return target_pos < scan
        if scan > target_pos:
            return True
        scan = next_nl + 1
    return target_pos > decl_start


def _find_scope_body_range(base, scope_name):
    esc = re.escape(scope_name)

    m = re.search(r'(?m)^([ \t]*)class\s+' + esc + r'\b', base)
    if m:
        return _python_scope_range(base, m, m.group(1))

    m = re.search(
        r'(?m)^([ \t]*)(?:async\s+)?def\s+' + esc + r'\s*\(',
        base)
    if m:
        return _python_scope_range(base, m, m.group(1))

    m = re.search(
        r'(?m)^([ \t]*)(?:export\s+)?(?:async\s+)?class\s+'
        + esc + r'\b', base)
    if m:
        return _js_scope_range(base, m)

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
    if not anchor:
        return None
    scope_text = base[scope_start:scope_end]

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


def _verify_scope_insertion(base: str, result: str,
                              scope_name: str,
                              inserted_body: str,
                              path: str = "") -> bool:
    """Return True iff `result` correctly placed the inserted body
    into `scope_name`'s body.

    For `.py` destinations only.  Returns True for non-`.py` (no
    check).  Fails closed on parse errors.

    Conditions:
      1. Both `base` and `result` parse as Python.
      2. `scope_name` exists in both ASTs (its body may have changed).
      3. Any FunctionDef/ClassDef in `inserted_body` appears as a
         DIRECT child of `scope_name` in `result`.

    Motivating failure (test_40, `comfyui.py` method-insert): a
    `@command`-decorated method inserted at the wrong indent lands
    nested inside a sibling method.  Valid Python, dead code.  The
    direct-child check catches this: `def new_cmd` nested inside
    `def existing_cmd` is not a direct child of the class.
    """
    if not path.endswith(".py"):
        return True

    try:
        base_tree = ast.parse(base)
        result_tree = ast.parse(result)
    except SyntaxError:
        return False

    def _find_scope(tree, name):
        for node in ast.walk(tree):
            if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    return node
        return None

    result_scope = _find_scope(result_tree, scope_name)
    if result_scope is None:
        return False

    try:
        body_tree = ast.parse(textwrap.dedent(inserted_body))
    except SyntaxError:
        return False

    new_decls = {
        n.name for n in body_tree.body
        if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if not new_decls:
        # Non-decl insertion (e.g., try/except).  `ast.parse` already
        # passed; no direct-membership check applies.
        return True

    direct_member_names = {
        n.name for n in result_scope.body
        if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    return new_decls.issubset(direct_member_names)


def _try_insert_into_named_scope(base, body, context_before, path="",
                                  *, scope_hint=""):
    """Insert a scope-shaped body (nested statement or decorated method)
    into a named class/function from the prose.

    Fires only when:
      · body is consistently indented (all content lines >= 2);
      · body does NOT start with a top-level declaration keyword;
      · prose names the target scope with a structural pattern, OR
        the enclosing H2's `scope_hint` names it, OR
        the prose has an anchor whose enclosing scope can be derived;
      · the scope exists in base;
      · for `.py` destinations, the AST check
        (`_verify_scope_insertion`) confirms the inserted body's
        declarations are direct children of the target scope.

    Precedence for the scope name (2026-11-05): the fence's own
    prose wins when it names a scope; the `scope_hint` from the
    enclosing H2 is a fallback when the prose is scope-silent.
    Motivating case: block 634 in the 2026-10-28 run, where the
    prose was "And in every failure return:" but the H2 was "##
    Fix 3 — surface per-command errors in `handle_inpaint`".
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
        return None

    for ln in body_lines:
        if not ln.strip():
            continue
        ln_indent = len(ln) - len(ln.lstrip())
        if ln_indent < first_indent:
            return None

    first_stripped = first_content.lstrip()
    if _SCOPE_DECL_HEAD_RE.match(first_stripped):
        return None

    # Prose wins; H2 scope_hint is a fallback when prose is silent.
    scope_name = _extract_scope_name_from_prose(context_before)
    if not scope_name:
        scope_name = scope_hint or None
    if not scope_name:
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

    # Verify the reindent produced the expected first-line indent.
    # `_reindent_body` guarantees this, but the check documents the
    # invariant and would catch a future regression in `_reindent_body`
    # before the bug propagates.
    if reindented:
        _first_reindented = reindented.split("\n")[0]
        _actual_indent = _first_reindented[
            :len(_first_reindented) - len(_first_reindented.lstrip())
        ]
        if _actual_indent != body_indent:
            return None

    prefix = base[:insert_pos]
    suffix = base[insert_pos:]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    result = prefix + reindented + "\n" + suffix

    if result == base:
        return None

    if not _verify_scope_insertion(
            base, result, scope_name, reindented, path):
        print(f"  [named-scope] {path}: insertion into "
              f"{scope_name!r} rejected by AST structural check "
              f"(member not a direct child of the scope)",
              file=sys.stderr)
        return None

    return result