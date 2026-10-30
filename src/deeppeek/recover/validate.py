"""
recover.validate — Pass 7 (cross-validate imports), plus passes 7.5,
7.6, 7.6b, 7.7, and 7.8 (JS cross-validation, missing-export demotion,
missing-Python-export demotion, JS export repair, and the universal
finding demotion).

Pass 7 is a pure read over the recovered content.

Pass 8 (verify files) and Pass 8.5 (symbol resolution) live in
`verify.py` — extracted 2026-11-05 because they operate on
`rec.content` *after* pass 6 has mutated it, while pass 7 operates
before.  No helper is shared between the two families.
`pass8_verify`, `pass8_5_check_symbols`, and `_validate_sanity_
attribution` are re-exported at the bottom of this module, so
existing imports continue to resolve.

PASS 7 (2026-11-05 refactor)
============================
`pass7_cross_validate` accepts an optional `blocks_by_idx` mapping
and is a three-phase extraction:

  Phase 1: extract imports from *touched* records only (recovered
           files + pre-existing files modified by this run).
           Untouched pre-existing files contribute nothing to
           imports — their own imports are the user's business, and
           they haven't changed.

  Phase 2: resolve every import and collect the set of target
           records.  Only those targets need `_py_exports` called
           on them.

  Phase 3: check every (importer, target, name) triple and
           report missing modules / exports.

Correctness payoff: untouched pre-existing files still count as
on-disk targets via `_rec_will_be_on_disk`, so their exports are
extracted *when a recovered file imports from them*.  Files not
imported from are never parsed.

Performance payoff: an `--apply-to` run against a large pre-existing
tree previously parsed every file's imports AND every file's
exports.  Now it parses only the touched files' imports and only
the targets those imports resolve to.  Roughly: 2N → (touched +
targets).

PASS 7.5
========
Extends the import/export cross-check to JS modules and to JS
embedded inside `.py` string literals.

PASS 7.6 / 7.6b / 7.8
=====================
Pass 7.6 demotes records on JS missing-export findings.  Pass 7.6b
is the Python analogue.  Pass 7.8 is the universal safety net that
demotes both sides of any finding.

JS MODULE RESOLUTION (2026-11-05)
=================================
`_resolve_js_module` previously tried only one candidate: the
file-relative resolution `<dirname(from_path)>/<module>.<ext>`.  A
`playgrounds/inpaint/pg_panel_bindings.py` importing
`./pgkit/remote_client.js` never resolved — the real file is
`playgrounds/pgkit/remote_client.py`, which is where the browser's
`./pgkit/` resolves relative to the page's document root.  The
resolver had no upward walk, no basename fallback, and no way to
recover from the mismatch; the resulting `missing_js_module`
findings were pure noise.

The fallback chain:

  1. Direct file-relative resolution (`dirname(from_path)/…`).
  2. Walk up parent directories up to five levels.
  3. Basename fallback: exactly one record whose basename matches.
  4. Give up (return None).

Every step checks `_rec_will_be_on_disk` on the target, so an
unrelated record that happens to share a basename but was never
written this run does not produce a resolution.

PARSE-CACHE ROUTING (2026-11-05)
================================
`_py_exports` and `_py_imports` now route through `parse_source`
(which is memoized) instead of `ast.parse`.  Pass 7 iterates imports
across every touched record; on a large `--apply-to` run the same
content is re-parsed across passes 6, 8, 8.4b, 8.5, 8.7d, and 8.10.
Routing through the cache means the parse is one lookup after the
first.

RESULT CACHE (2026-11-05)
=========================
The `parse_source` cache only memoizes the parse; the subsequent
`ast.walk` / `tree.body` iteration was re-done on every call.  Pass
7 is called from `pass7_cross_validate` (once per touched record)
*and* from `write/_scan._aggregate_missing_imports` (once per
record with content) — same `_py_imports(body)` re-walking the tree
twice per record.  `@lru_cache(maxsize=1024)` on both helpers makes
the second and subsequent calls a dict lookup on `body` itself.

Return types are frozen (`frozenset`, `tuple`) so callers cannot
mutate a cached value.  Every existing caller iterates; none mutate.
"""

import ast
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .core import FileRecord, _basename
from ._ast import parse_source


# ============================================================================
#  Pass 7 — Python import/export cross-validate
# ============================================================================

def pass7_cross_validate(records: dict[str, FileRecord],
                          blocks_by_idx: Optional[dict] = None) -> dict:
    """Cross-validate Python imports against recovered declarations.

    Three-phase as of 2026-11-05, motivated by an early-eval
    slowdown: previously, `_py_exports` and `_py_imports` were
    called on every record with a body, including hundreds of
    untouched pre-existing files.  Both call `parse_source`.  Now:

      Phase 1: extract imports from *touched* records.
      Phase 2: resolve every import and collect needed targets.
      Phase 3: extract exports only for the targets.
      Phase 4: report missing modules and exports.

    `blocks_by_idx` lets the pass fall back to
    `blocks_by_idx[rec.last_full_idx].body` when `rec.content is
    None` (a record pass 6 did not visit).
    """
    blocks_by_idx = blocks_by_idx or {}

    # ---- Phase 1: imports from touched records only ----
    imports: dict[str, tuple] = {}
    for path, rec in records.items():
        if rec.preexisting_base and not rec.patches and not rec.pairs:
            continue
        body = rec.content
        if body is None and rec.last_full_idx is not None:
            block = blocks_by_idx.get(rec.last_full_idx)
            if block is not None:
                body = block.body
        if body is None:
            continue
        if Path(path).suffix.lower() == ".py":
            imports[path] = _py_imports(body)

    # ---- Phase 2: resolve once, collect needed targets ----
    resolved: dict[tuple[str, str], Optional[str]] = {}
    for path, imps in imports.items():
        for module, _name in imps:
            key = (path, module)
            if key not in resolved:
                resolved[key] = _resolve_module(module, records, path)

    needed_targets: set[str] = {
        t for t in resolved.values() if t is not None
    }

    # ---- Phase 3: extract exports only for targets ----
    exports: dict[str, frozenset] = {}
    for path in needed_targets:
        rec = records.get(path)
        if rec is None:
            continue
        body = rec.content
        if body is None and rec.last_full_idx is not None:
            block = blocks_by_idx.get(rec.last_full_idx)
            if block is not None:
                body = block.body
        if body is not None:
            exports[path] = _py_exports(body)

    # ---- Phase 4: report missing modules and exports ----
    problems: list[dict] = []
    seen_missing_modules: set[tuple[str, str]] = set()
    seen_missing_exports: set[tuple[str, str, str]] = set()

    for path, imps in imports.items():
        for module, name in imps:
            target = resolved.get((path, module))
            if target is None:
                if _is_likely_local(module, records):
                    clean = module.lstrip(".")
                    if not clean:
                        continue
                    top = clean.split(".", 1)[0]
                    top_recovered = any(
                        _rec_will_be_on_disk(r)
                        and (_basename(p) == top + ".py"
                             or Path(p).as_posix()
                                == top + "/__init__.py")
                        for p, r in records.items()
                    )
                    display = clean if top_recovered else top
                    key = (path, display)
                    if key in seen_missing_modules:
                        continue
                    seen_missing_modules.add(key)
                    problems.append({
                        "type": "missing_module",
                        "from": path,
                        "module": display,
                    })
                continue
            if name not in exports.get(target, frozenset()):
                key = (path, target, name)
                if key in seen_missing_exports:
                    continue
                seen_missing_exports.add(key)
                problems.append({
                    "type": "missing_export",
                    "from": path,
                    "target": target,
                    "name": name,
                })

    return {
        "exports": {p: sorted(s) for p, s in exports.items()},
        "imports": {p: [list(t) for t in imps]
                    for p, imps in imports.items()},
        "problems": problems,
    }


# ============================================================================
#  Pass 7.5 — JS import/export cross-validation
# ============================================================================

def pass7_5_js_validate(records: dict) -> list:
    """Return list of problems for JS modules.

    Each problem is a dict with `type` in
    {`missing_js_module`, `missing_js_export`}, plus the same shape
    as pass7's problems.

    Guard (2026-11-05): skip the "extract embedded JS" step for
    files that are obviously Python modules (3+ top-level defs).
    """
    from .symbols import (
        _is_js_lang, _extract_js_imports_with_src,
        _extract_dominant_js_string, _extract_js_exports,
    )
    from .symbols import _looks_like_python_module

    problems = []
    seen = set()

    for path, rec in records.items():
        if rec.content is None:
            continue

        js_source = None
        if _is_js_lang(path):
            js_source = rec.content
        elif (path.endswith(".py")
              and not _looks_like_python_module(rec.content)):
            js_source = _extract_dominant_js_string(rec.content)
        else:
            continue

        if js_source is None:
            continue

        imports = _extract_js_imports_with_src(js_source)
        for name, module in imports:
            if not module.startswith("."):
                continue

            target_path = _resolve_js_module(module, path, records)
            if target_path is None:
                key = (path, module)
                if key not in seen:
                    seen.add(key)
                    problems.append({
                        "type": "missing_js_module",
                        "from": path,
                        "module": module,
                    })
                continue

            target_rec = records[target_path]
            target_js = None
            if _is_js_lang(target_path):
                target_js = target_rec.content
            elif (target_path.endswith(".py")
                  and not _looks_like_python_module(target_rec.content)):
                target_js = _extract_dominant_js_string(
                    target_rec.content)

            if target_js is None:
                continue

            exports = _extract_js_exports(target_js)
            if name not in exports:
                key = (path, target_path, name)
                if key in seen:
                    continue
                seen.add(key)
                problems.append({
                    "type": "missing_js_export",
                    "from": path,
                    "target": target_path,
                    "name": name,
                })

    return problems


# ============================================================================
#  Pass 7.6 — demote on missing JS export
# ============================================================================

_JS_DECL_RE_FOR_MISSING_EXPORT = re.compile(
    r'\b(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)'
)


def _js_target_declares_but_does_not_export(
        target_rec: FileRecord, name: str) -> bool:
    """True iff `target_rec.content` declares `name` but does not
    export it.
    """
    from .symbols import (
        _is_js_lang, _extract_dominant_js_string, _extract_js_exports,
        _looks_like_python_module,
    )
    if target_rec.content is None:
        return False
    js = None
    if _is_js_lang(target_rec.path):
        js = target_rec.content
    elif (target_rec.path.endswith(".py")
          and not _looks_like_python_module(target_rec.content)):
        js = _extract_dominant_js_string(target_rec.content)
    if js is None:
        return False
    exports = _extract_js_exports(js)
    if name in exports:
        return False
    for m in _JS_DECL_RE_FOR_MISSING_EXPORT.finditer(js):
        if m.group(1) == name:
            return True
    return False


def pass7_6_demote_on_missing_export(
        records: dict,
        js_problems: list,
        verbose: bool = False) -> int:
    n_demoted = 0
    for prob in js_problems:
        if prob.get("type") != "missing_js_export":
            continue
        from_path = prob["from"]
        target_path = prob["target"]
        name = prob["name"]

        if target_path.endswith(".py"):
            for path, role in ((target_path, "target"),
                               (from_path, "importer")):
                if path not in records:
                    continue
                rec = records[path]
                if not any(name in w for w in rec.warnings):
                    rec.warnings.append(
                        f"advisory: missing_js_export {name!r} "
                        f"between {from_path} and {target_path} "
                        f"({role}); not demoting because the target "
                        f"is a .py container and the export check "
                        f"runs against embedded JS"
                    )
            if verbose:
                print(f"  [pass 7.6] advisory only (target is .py): "
                      f"{name!r} between {from_path} and {target_path}",
                      file=sys.stderr)
            continue

        if target_path not in records:
            continue
        target_rec = records[target_path]
        if not _js_target_declares_but_does_not_export(
                target_rec, name):
            continue

        if target_rec.trust != "LOW":
            target_rec.trust = "LOW"
            target_rec.verification["missing_export"] = name
            target_rec.warnings.append(
                f"missing_js_export: {name!r} is declared in this "
                f"file but not exported; imported by {from_path}")
            n_demoted += 1
            if verbose:
                print(f"  [pass 7.6] {target_path}: missing export "
                      f"{name!r} (imported by {from_path}); "
                      f"demoted to LOW", file=sys.stderr)

        if from_path in records:
            from_rec = records[from_path]
            if from_rec.trust != "LOW":
                from_rec.trust = "LOW"
                from_rec.verification.setdefault(
                    "missing_export_deps", []).append(
                        {"module": target_path, "name": name})
                from_rec.warnings.append(
                    f"missing_js_export: imports {name!r} from "
                    f"{target_path} but the export is not present "
                    f"(declared but not exported)")
                n_demoted += 1
                if verbose:
                    print(f"  [pass 7.6] {from_path}: missing "
                          f"import {name!r} from {target_path}; "
                          f"demoted to LOW", file=sys.stderr)

    return n_demoted


# ============================================================================
#  Pass 7.6b — demote on missing Python export
# ============================================================================

def pass7_6b_demote_on_missing_python_export(
        records: dict,
        problems: list,
        verbose: bool = False) -> int:
    """Demote both sides of a Python `missing_export` finding."""
    n_demoted = 0

    for prob in problems:
        if prob.get("type") != "missing_export":
            continue

        from_path = prob.get("from")
        target_path = prob.get("target")
        name = prob.get("name")

        if target_path in records:
            target_rec = records[target_path]
            if target_rec.trust != "LOW":
                target_rec.trust = "LOW"
                target_rec.verification.setdefault(
                    "missing_python_export", []).append(name)
                target_rec.warnings.append(
                    f"missing_python_export: {name!r} is imported by "
                    f"{from_path} but not declared at this file's "
                    f"top level; the import will fail at daemon reload"
                )
                n_demoted += 1
                if verbose:
                    print(f"  [pass 7.6b] {target_path}: missing "
                          f"export {name!r} (imported by {from_path}); "
                          f"demoted to LOW", file=sys.stderr)

        if from_path in records:
            from_rec = records[from_path]
            if from_rec.trust != "LOW":
                from_rec.trust = "LOW"
                from_rec.verification.setdefault(
                    "missing_python_export_deps", []).append(
                        {"module": target_path, "name": name})
                from_rec.warnings.append(
                    f"missing_python_export: imports {name!r} from "
                    f"{target_path} but the target does not declare "
                    f"it; the import will fail at daemon reload"
                )
                n_demoted += 1
                if verbose:
                    print(f"  [pass 7.6b] {from_path}: missing import "
                          f"{name!r} from {target_path}; demoted to LOW",
                          file=sys.stderr)

    return n_demoted


# ============================================================================
#  Pass 7.7 — repair JS export findings
# ============================================================================

def _repair_missing_export(rec, name, verbose=False):
    """Add `export ` before the declaration of `name` in rec.content.
    Returns True if the repair succeeded and re-validates.
    """
    from .validate_output import validate_content
    from .symbols import (
        _is_js_lang, _extract_dominant_js_string, _extract_js_exports,
        _looks_like_python_module,
    )

    content = rec.content
    if content is None:
        return False

    pattern = re.compile(
        r'^([ \t]*)((?:async\s+)?(?:function|class|const|let|var)\s+'
        + re.escape(name) + r'\b)',
        re.MULTILINE,
    )
    m = pattern.search(content)
    if m is None:
        if verbose:
            print(f"  [repair] {rec.path}: no declaration of "
                  f"{name!r} found", file=sys.stderr)
        return False

    line_start = content.rfind("\n", 0, m.start()) + 1
    prefix_on_line = content[line_start:m.start(2)]
    if re.search(r'\bexport\b', prefix_on_line):
        return False

    insert_at = m.start(2)
    repaired = content[:insert_at] + "export " + content[insert_at:]

    err = validate_content(repaired, rec.path)
    if err is not None:
        if verbose:
            print(f"  [repair] {rec.path}: adding `export` before "
                  f"{name!r} broke validation: {err}", file=sys.stderr)
        return False

    if _is_js_lang(rec.path):
        js = repaired
    elif (rec.path.endswith(".py")
          and not _looks_like_python_module(repaired)):
        js = _extract_dominant_js_string(repaired)
    else:
        js = None
    if js is None or name not in _extract_js_exports(js):
        if verbose:
            print(f"  [repair] {rec.path}: regex matched but "
                  f"{name!r} still not exported", file=sys.stderr)
        return False

    rec.content = repaired
    rec.warnings.append(
        f"auto-repaired: added `export` before {name!r} "
        f"declaration (imported by another file)")
    if verbose:
        print(f"  [repair] {rec.path}: added `export` before {name!r}",
              file=sys.stderr)
    return True


def pass7_7_repair_js_imports(records, js_problems, verbose=False):
    """Attempt to repair `missing_js_export` findings by adding
    `export` to the target's declaration.  Returns
    `(repaired, remaining)`."""
    repaired = []
    remaining = []
    repaired_set = set()
    for prob in js_problems:
        if prob.get("type") != "missing_js_export":
            remaining.append(prob)
            continue
        target = prob.get("target")
        name = prob.get("name")
        key = (target, name)
        if key in repaired_set:
            repaired.append({
                "problem": prob,
                "repair": f"added `export` before {name!r}",
                "target": target,
            })
            continue
        target_rec = records.get(target)
        if target_rec is None:
            remaining.append(prob)
            continue
        if _repair_missing_export(target_rec, name, verbose):
            repaired_set.add(key)
            repaired.append({
                "problem": prob,
                "repair": f"added `export` before {name!r}",
                "target": target,
            })
        else:
            remaining.append(prob)
    return repaired, remaining


# ============================================================================
#  Pass 7.8 — universal finding demotion
# ============================================================================

def pass7_8_demote_on_any_finding(records: dict,
                                  findings: list,
                                  verbose: bool = False) -> int:
    """Universal safety net: any missing-export finding demotes both
    sides.
    """
    n_demoted = 0
    for prob in findings:
        ptype = prob.get("type")
        if ptype not in ("missing_export", "missing_js_export"):
            continue
        from_path = prob.get("from")
        target_path = prob.get("target")
        name = prob.get("name")

        for path, role in ((target_path, "target"),
                           (from_path, "importer")):
            if not path or path not in records:
                continue
            rec = records[path]
            if rec.trust == "LOW":
                continue
            rec.trust = "LOW"
            rec.verification.setdefault(
                f"{ptype}_demoted", []).append(name)
            rec.warnings.append(
                f"{ptype}: {name!r} missing between {from_path} and "
                f"{target_path} — {role} demoted to LOW"
            )
            n_demoted += 1
            if verbose:
                print(f"  [pass 7.8] {path}: {role} demoted "
                      f"({ptype}, {name!r})", file=sys.stderr)
    return n_demoted


# ============================================================================
#  Module resolution and file-presence checks
# ============================================================================

def _resolve_js_module(module: str, from_path: str,
                       records: dict) -> Optional[str]:
    """Resolve a relative JS import to the record that emits it.

    See the module docstring, JS MODULE RESOLUTION section.  Motivating
    case: `playgrounds/inpaint/pg_panel_bindings.py` importing
    `./pgkit/remote_client.js` — the resolver must reach
    `playgrounds/pgkit/remote_client.py`, which is five levels up
    from the direct-relative candidate and shares only the basename.
    """
    base = module
    for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx"):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break

    dirname = os.path.dirname(from_path)

    # 1. Direct file-relative resolution (the browser's page-relative
    #    semantics when the page is in the same dir as the .py file).
    for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"):
        cand = os.path.normpath(
            os.path.join(dirname, base + ext)
        ).replace("\\", "/")
        if cand in records and _rec_will_be_on_disk(records[cand]):
            return cand

    # 2. Walk up directories.  A page served from playgrounds/inpaint/
    #    can resolve `./pgkit/` to playgrounds/pgkit/ when the server
    #    mounts playgrounds/ as the document root.
    parent = dirname
    for _ in range(5):  # five levels is enough for any real project
        parent = os.path.dirname(parent)
        if not parent:
            break
        for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"):
            cand = os.path.normpath(
                os.path.join(parent, base + ext)
            ).replace("\\", "/")
            if cand in records and _rec_will_be_on_disk(records[cand]):
                return cand

    # 3. Basename fallback.  `./pgkit/remote_client.js` names a file
    #    whose basename is `remote_client`.  If exactly one record has
    #    that basename (across any extension), use it.
    basename = os.path.basename(base)
    matches = [
        path for path in records
        if os.path.basename(path).rsplit(".", 1)[0] == basename
        and _rec_will_be_on_disk(records[path])
    ]
    if len(matches) == 1:
        return matches[0]

    return None


@lru_cache(maxsize=1024)
def _py_exports(body: str) -> frozenset:
    """Return top-level names declared in `body`.

    Routes through `parse_source` (memoized) instead of `ast.parse`,
    then caches the *result* on the body string.  See the module
    docstring, PARSE-CACHE ROUTING and RESULT CACHE.

    Returns a `frozenset` so callers cannot mutate the cached value.
    """
    try:
        tree = parse_source(body)
    except SyntaxError:
        return frozenset()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            out.add(node.target.id)
    return frozenset(out)


@lru_cache(maxsize=1024)
def _py_imports(body: str) -> tuple:
    """Return `(module, name)` pairs from `body`'s `from` imports.

    Routes through `parse_source` (memoized) instead of `ast.parse`,
    then caches the *result* on the body string.  See the module
    docstring, PARSE-CACHE ROUTING and RESULT CACHE.

    Returns a `tuple[tuple[str, str], ...]` so callers cannot mutate
    the cached value.  All existing callers iterate; none mutate.
    """
    try:
        tree = parse_source(body)
    except SyntaxError:
        return ()
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            level = node.level or 0
            mod = "." * level + (node.module or "")
            if not mod:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.append((mod, alias.name))
    return tuple(out)


def _rec_will_be_on_disk(rec: FileRecord) -> bool:
    """True iff a record's file will exist in the target tree after
    the run.

    Two cases:
      * Content will be written this run: `rec.content is not None`
      * Pre-existing file, unchanged: `rec.preexisting_base` is True
        (the file was loaded from disk, and if it isn't written, the
        original stays)
    """
    if rec.content is not None:
        return True
    if rec.preexisting_base:
        return True
    return False


_EXTERNAL_NAMESPACES = ("pgkit", "playground_")


def _resolve_module(module: str, records: dict[str, FileRecord],
                    from_path: str) -> Optional[str]:
    if not module.startswith((".", "/")):
        top = module.split(".", 1)[0]
        if hasattr(sys, "stdlib_module_names"):
            if top in sys.stdlib_module_names:
                return None
        for ns in _EXTERNAL_NAMESPACES:
            if top == ns or top.startswith(ns + "."):
                return None

    if module.startswith("."):
        level = 0
        while level < len(module) and module[level] == ".":
            level += 1
        rest = module[level:]
        if not rest:
            return None

        parent = os.path.dirname(from_path)
        for _ in range(level - 1):
            parent = os.path.dirname(parent)

        parts = rest.split(".")

        candidate = "/".join(([parent] if parent else []) + parts)
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = candidate + ext
            rec = records.get(full)
            if rec is not None and _rec_will_be_on_disk(rec):
                return full

        last = parts[-1]
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = last + ext
            rec = records.get(full)
            if rec is not None and _rec_will_be_on_disk(rec):
                return full
        return None

    if module.startswith("/"):
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = module + ext
            rec = records.get(full)
            if rec is not None and _rec_will_be_on_disk(rec):
                return full
        return None

    parts    = module.split(".")
    top_name = parts[0]
    top_file = top_name + ".py"
    top_pkg  = top_name + "/__init__.py"
    sub_file = "/".join(parts) + ".py" if len(parts) > 1 else None

    for path, rec in records.items():
        if not _rec_will_be_on_disk(rec):
            continue
        if _basename(path) == top_file:
            return path

    for path, rec in records.items():
        if not _rec_will_be_on_disk(rec):
            continue
        if Path(path).as_posix() == top_pkg:
            if sub_file is None:
                return path
            for sub_path, sub_rec in records.items():
                if not _rec_will_be_on_disk(sub_rec):
                    continue
                if Path(sub_path).as_posix() == sub_file:
                    return sub_path
            return None

    return None


def _is_likely_local(module: str,
                     records: Optional[dict] = None) -> bool:
    if module.startswith((".", "/")):
        return True
    top = module.split(".", 1)[0]
    if records is not None:
        for ext in (".py", ".js", ".mjs", ".ts", ".tsx"):
            candidate = top + ext
            for p in records:
                if _basename(p) == candidate:
                    return True
    for ns in _EXTERNAL_NAMESPACES:
        if top == ns or top.startswith(ns + "."):
            return False
    if hasattr(sys, "stdlib_module_names"):
        if top in sys.stdlib_module_names:
            return False
    return "_" in module


# ============================================================================
#  Re-exports for backward compatibility
# ============================================================================
#
# These lived in this module before the 2026-11-05 split.  Tests and
# any out-of-tree callers import them from `deeppeek.recover.validate`;
# keep the re-exports stable even as `verify.py` grows.
#
# `pipeline.py` and `reverify.py` import these from `verify.py`
# directly, which is the honest path — the re-exports exist only so
# that imports written against the pre-split module continue to
# resolve without churn.

from .verify import (  # noqa: E402, F401
    pass8_verify,
    pass8_5_check_symbols,
    _validate_sanity_attribution,
)
