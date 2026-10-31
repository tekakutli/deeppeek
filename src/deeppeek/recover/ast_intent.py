"""
recover.ast_intent — structural intent extraction.

A "structural intent" is the set of things a patch *declares* that
must exist somewhere in the resulting content for the patch to have
landed:

  · dict keys (`"--output_rotation": {...}`, `foo: bar,`)
  · top-level names (def / class / function / const / let / var / assign)
  · function parameters (including kw-only)
  · decorator kwarg keys (`@command(args={...})` declares `args`)

This module is the single source of truth for "did this patch's
declarations land?" — replacing the line-equivalence heuristic in
`consistency._body_content_present()` and the fast-path
`patch_already._patch_already_present()` for structured shapes.

Why AST, not regex
==================
The motivating case (`comfyui.py`, 2026-10-30 run, patch idx 428):
the patch body declared a new dict entry `"--output_rotation": ...`
inside a `@command(args={...})` decorator. The target already
contained that key, but the help-text wording differed enough that
95% line-equivalence said "not present", the pass flipped the patch
to `failed`, reconcile excluded it, and the file shipped without the
declaration.

A structural check would have said: "the key `--output_rotation` is
present in the `args` dict of the `comfy_generate` decorator." Done.
That is what this module does.

GENERALIZATION
==============
Every entry in `_STRUCTURAL_KEY`'s whitelist is a *language-level*
structural concept:

  python      def, class, args, kwonlyargs, decorator kwargs, dict keys
  javascript  function, class, const/let/var assignment, object keys

No project-specific names. A new language is a new branch in
`_extract_intent`, not a new special case at a call site.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

from ._ast import parse_source


# ===========================================================================
#  Shape of an "intent"
# ===========================================================================
#
# An Intent records what a body declares, grouped by kind so callers
# can check different kinds against different parts of the target
# (e.g. decorator kwarg keys live in the *decorator*, not the module
# top level).

class Intent:
    __slots__ = ("dict_keys", "decl_names", "param_names",
                 "decorator_kwarg_keys", "lang")

    def __init__(self, lang: str = ""):
        self.lang = lang
        self.dict_keys: set[str] = set()
        self.decl_names: set[str] = set()
        self.param_names: set[str] = set()
        self.decorator_kwarg_keys: set[str] = set()

    def is_empty(self) -> bool:
        return not (self.dict_keys or self.decl_names
                    or self.param_names or self.decorator_kwarg_keys)

    def __repr__(self) -> str:
        parts = []
        if self.dict_keys:
            parts.append(f"dict_keys={sorted(self.dict_keys)}")
        if self.decl_names:
            parts.append(f"decl_names={sorted(self.decl_names)}")
        if self.param_names:
            parts.append(f"param_names={sorted(self.param_names)}")
        if self.decorator_kwarg_keys:
            parts.append(
                f"decorator_kwarg_keys={sorted(self.decorator_kwarg_keys)}")
        return f"Intent(lang={self.lang!r}, {', '.join(parts) or 'empty'})"


# ===========================================================================
#  Extraction — Python
# ===========================================================================

def _collect_dict_keys(node: ast.AST) -> set[str]:
    """String keys of every dict literal inside `node`."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Dict):
            continue
        for k in sub.keys:
            if (isinstance(k, ast.Constant)
                    and isinstance(k.value, str)):
                out.add(k.value)
    return out


def _collect_decl_names(tree: ast.AST) -> set[str]:
    """Top-level and nested def / class / assign / annotated-assign names.

    Nested names are included because a patch that inserts a method
    into a class declares that method's name in the body; the check
    must find it anywhere in the target, not only at module top
    level. False-positive risk is low: the same name at a different
    nesting level is almost always the same declaration.
    """
    out: set[str] = set()
    for sub in ast.walk(tree):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(sub.name)
        elif isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
        elif isinstance(sub, ast.AnnAssign):
            if isinstance(sub.target, ast.Name):
                out.add(sub.target.id)
    return out


def _collect_param_names(tree: ast.AST) -> set[str]:
    """Every function's parameters, including kw-only."""
    out: set[str] = set()
    for sub in ast.walk(tree):
        if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Lambda)):
            continue
        args = sub.args
        for a in (args.posonlyargs + args.args + args.kwonlyargs):
            out.add(a.arg)
        if args.vararg:
            out.add(args.vararg.arg)
        if args.kwarg:
            out.add(args.kwarg.arg)
    return out


def _collect_decorator_kwarg_keys(tree: ast.AST) -> set[str]:
    """Every keyword-argument key of every decorator call in `tree`.

    The motivating case is `@command(args={...})` — the kwarg key is
    `args`. But the same code picks up `@something(foo=1, bar=2)`
    for any decorator, which is the right generalization.
    """
    out: set[str] = set()
    for sub in ast.walk(tree):
        if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        for dec in getattr(sub, "decorator_list", []) or []:
            # @foo(...) — call form
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg:
                        out.add(kw.arg)
    return out


def _extract_intent_python(body: str, path: str) -> Intent:
    try:
        tree = parse_source(body, filename=path or "<intent>")
    except SyntaxError:
        # A non-parsing body still has meaningful dict-key/decl
        # signal via regex fallback — but keep it structural by only
        # attempting the regex for keys (which are unambiguous).
        return _extract_intent_via_dict_key_regex(body, "python")

    intent = Intent(lang="python")
    intent.dict_keys = _collect_dict_keys(tree)
    intent.decl_names = _collect_decl_names(tree)
    intent.param_names = _collect_param_names(tree)
    intent.decorator_kwarg_keys = _collect_decorator_kwarg_keys(tree)
    return intent


# ===========================================================================
#  Extraction — JavaScript / TypeScript
# ===========================================================================

def _extract_intent_js(body: str, path: str) -> Intent:
    """
    tree-sitter-based extraction. Falls back to a conservative regex
    for object keys when the grammar is unavailable (fail-closed:
    the caller treats an empty intent as "no structural signal" and
    falls back to the older line-equivalence check).
    """
    intent = Intent(lang="javascript")

    try:
        from .ast_anchor import _get_parser
        parser = _get_parser("javascript")
    except Exception:
        parser = None
    if parser is not None:
        try:
            tree = parser.parse(body.encode("utf-8"))
        except Exception:
            tree = None
        if tree is not None and not tree.root_node.has_error:
            intent.decl_names = _js_decl_names(tree.root_node)
            intent.param_names = _js_param_names(tree.root_node)
            intent.dict_keys = _js_object_keys(tree.root_node)
            return intent

    # Grammar unavailable or body unparseable: last-resort regex.
    intent.dict_keys = _js_object_key_regex(body)
    return intent


def _js_decl_names(root) -> set[str]:
    from .scope_ast import _walk, _js_decl_name
    out: set[str] = set()
    for node in _walk(root):
        n = _js_decl_name(node)
        if n:
            out.add(n)
    return out


def _js_param_names(root) -> set[str]:
    from .scope_ast import _walk, _node_text
    out: set[str] = set()

    def _collect_params(node):
        for c in node.children:
            if c.type == "identifier":
                out.add(_node_text(c))
            elif c.type in ("object_pattern", "array_pattern",
                             "rest_pattern", "assignment_pattern"):
                _collect_params(c)

    for node in _walk(root):
        if node.type in ("formal_parameters", "parameters"):
            _collect_params(node)
    return out


def _js_object_keys(root) -> set[str]:
    from .scope_ast import _walk, _node_text
    out: set[str] = set()
    for node in _walk(root):
        if node.type != "pair":
            continue
        key = node.child_by_field_name("key")
        if key is None:
            continue
        if key.type in ("property_identifier", "identifier",
                        "string", "number"):
            txt = _node_text(key)
            # Strip quotes from string keys.
            if (len(txt) >= 2 and txt[0] in ('"', "'")
                    and txt[-1] == txt[0]):
                txt = txt[1:-1]
            if txt:
                out.add(txt)
    return out


# ===========================================================================
#  Regex fallbacks — used only when no grammar is available
# ===========================================================================

import re

_JS_OBJECT_KEY_RE = re.compile(
    r'(?:^|[\s,{])["\']?([A-Za-z_$][\w$.-]*)["\']?\s*:\s*'
    r'(?!:)',
    re.MULTILINE,
)


def _js_object_key_regex(body: str) -> set[str]:
    """
    Conservative regex fallback for JS object keys. Under-detects:
    will not find keys defined across multi-line weird shapes, but
    the important thing is that it does not over-detect. Anything
    it *does* return is genuinely a key shape.
    """
    return set(_JS_OBJECT_KEY_RE.findall(body))


_PY_DICT_KEY_RE = re.compile(
    r'["\']([^"\']+)["\']\s*:\s*',
    re.MULTILINE,
)


def _extract_intent_via_dict_key_regex(body: str, lang: str) -> Intent:
    """
    Last-resort path when the body does not parse.

    This is not how we *want* to identify structural intent, but a
    non-parsing body still has unambiguous string-literal keys.
    Downstream checks treat an empty result as "no signal" and fall
    back to the pre-fix behavior.
    """
    intent = Intent(lang=lang)
    intent.dict_keys = set(_PY_DICT_KEY_RE.findall(body))
    return intent


# ===========================================================================
#  Public API
# ===========================================================================

def extract_intent(body: str, path: str) -> Intent:
    """
    Return what `body` structurally declares.

    Dispatch is on path extension. Unknown extensions attempt Python
    then JS, returning whichever yields a non-empty intent. The
    caller treats an empty intent as "no structural check possible".
    """
    if not body or not body.strip():
        return Intent(lang="")
    ext = Path(path).suffix.lower() if path else ""

    if ext == ".py":
        return _extract_intent_python(body, path)

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        return _extract_intent_js(body, path)

    # Unknown extension: try Python first (more likely than JS for
    # bodies without a path), then JS.
    py = _extract_intent_python(body, path)
    if not py.is_empty():
        return py
    return _extract_intent_js(body, path)


def intent_satisfied_in(intent: Intent, target: str,
                         path: str) -> bool:
    """
    True iff every declared thing in `intent` is structurally
    present in `target`.

    Empty intent → True (nothing to verify).
    """
    if intent is None or intent.is_empty():
        return True
    if not target or not target.strip():
        return False

    target_intent = extract_intent(target, path)

    # Every kind is checked independently. A missing decl name is
    # *not* necessarily a failure — the patch may be a rewrite that
    # reuses a name from elsewhere in the target — but every name
    # here appears in the target at *some* nesting level, so absence
    # is genuinely a red flag. Treat presence of the whole set as
    # required; treat presence of any *one* as informational.
    def _subset(need: set[str], have: set[str]) -> bool:
        return not need or need.issubset(have)

    return (
        _subset(intent.dict_keys, target_intent.dict_keys)
        and _subset(intent.decl_names, target_intent.decl_names)
        and _subset(intent.param_names, target_intent.param_names)
        and _subset(intent.decorator_kwarg_keys,
                    target_intent.decorator_kwarg_keys)
    )


def intent_satisfied_somewhere(intent: Intent, target: str,
                                path: str) -> bool:
    """
    Softer variant: True iff *at least one* declared structural thing
    is present in `target`.

    Used by the fast-path already-applied check where the body may
    be a rewrite that changes some names but keeps others.
    """
    if intent is None or intent.is_empty():
        return False
    if not target or not target.strip():
        return False

    target_intent = extract_intent(target, path)
    return bool(
        intent.dict_keys & target_intent.dict_keys
        or intent.decl_names & target_intent.decl_names
        or intent.param_names & target_intent.param_names
        or intent.decorator_kwarg_keys
           & target_intent.decorator_kwarg_keys
    )


def describe_missing(intent: Intent, target: str,
                     path: str) -> Optional[str]:
    """
    If `intent` is not fully satisfied in `target`, return a short
    human-readable description of what is missing. Otherwise return
    None.
    """
    if intent is None or intent.is_empty():
        return None
    if not target or not target.strip():
        return "target is empty"
    target_intent = extract_intent(target, path)

    missing_parts: list[str] = []
    diff = intent.dict_keys - target_intent.dict_keys
    if diff:
        missing_parts.append(f"dict keys {sorted(diff)}")
    diff = intent.decl_names - target_intent.decl_names
    if diff:
        missing_parts.append(f"declarations {sorted(diff)}")
    diff = intent.param_names - target_intent.param_names
    if diff:
        missing_parts.append(f"params {sorted(diff)}")
    diff = (intent.decorator_kwarg_keys
            - target_intent.decorator_kwarg_keys)
    if diff:
        missing_parts.append(f"decorator kwargs {sorted(diff)}")
    if not missing_parts:
        return None
    return "structural intent missing: " + ", ".join(missing_parts)
