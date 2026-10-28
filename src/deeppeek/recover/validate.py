"""
recover.validate — Pass 7 (cross-validate imports) and Pass 8
(verify files), plus Pass 7.5 (JS cross-validation),
Pass 7.6b (Python missing-export demotion), and Pass 7.8
(universal finding demotion).

Pass 7 is a pure read over the recovered content.

Pass 7.5 (NEW) extends the import/export cross-check to JS modules
and to JS embedded inside `.py` string literals.  The pg_masks.py
failure case: `pg_masks.py` imports `_rotateMask` from
`./pg_state.js`, and `pg_state.py`'s `STATE_JS` string literal
does not export `_rotateMask`.  At browser load time this throws
a JS `ReferenceError`, blanking the playground canvas silently.
The `.py` file itself parses fine.  Pass 7.5 catches this by
extracting the JS exports from the target's string literal and
checking every imported name against them.

Pass 7.6 (FIX 2) DEMOTES the involved records to LOW trust.
Previously, cross-validation findings were informational only:
they were recorded in `cross_validate.problems` and set the exit
code, but the files involved were still written to the target at
their original trust level.  See `pass7_6_demote_on_missing_export`.

Pass 7.6b (NEW) is the Python analogue of pass 7.6. Cross-validate
can emit `missing_export` findings for Python imports; when the
target doesn't declare the name at top level, the importer will
fail at load time. Both sides are demoted to LOW. Under default
`--min-trust MEDIUM`, the target tree is preserved; the user reads
the warning, fixes the missing export, and reruns.

Pass 7.8 (NEW) is the universal safety net: any `missing_export`
or `missing_js_export` finding that survives the specific passes
above still demotes both sides. Passes 7.6 and 7.6b are the
specific handlers; 7.8 is what runs if they missed anything.

Pass 8 verifies ast.parse for .py files and checks that patches took
effect.

NEW (2026-10-30c) **Trust the patcher for `deterministic`.**

The verifier no longer re-checks `deterministic` patches.  A
deterministic patch is one that matched its anchor exactly and
mutated the exact lines it named, via a helper (`_delete_exact_block`,
`_try_raw_diff_patch`, or the object-literal splice) that is correct
by construction.  Re-checking the *final* content afterwards produces
false failures whenever a later patch on the same file edited the
same lines again.

The specific shape that broke the panel-fade conversation:

    patch 1 (deletion, "delete this block"): applies
    patch 2 (diff, deletion of the same block): sees block already
        gone, marks itself `already-present`
    verify: patch 1 is deterministic, block is gone -> the
        deleted block's lines look "missing" -> trust drops to LOW
    result: pg_state.py is written to .deeppeek/low_trust/ even
        though both patches succeeded, and the tree version is
        unchanged.

Verify now skips `deterministic` and `already-present` patches
entirely.  Only `llm` patches are re-checked — they can hallucinate
context and need the presence/absence gate.

The same skip applies to `already-present`: those were no-ops at
application time; the content may have evolved since.

The diff-aware and deletion-aware checks remain for `llm` patches.

Three false-positive classes were eliminated in pass 7 earlier:
framework namespaces (`pgkit.*`), contentless records, stdlib
modules shadowing local basenames.  All are handled by
`_resolve_module` and `_is_likely_local` below, and shared with
`write._aggregate_missing_imports`.

NEW (2026-11-03) **Pass 8.5 — symbol resolution.**

`pass8_5_check_symbols` cross-references patch-introduced bare calls
against each file's imports and declarations.  An unresolved call
introduced by a patch almost certainly means the block was
attributed to the wrong file.  Demote the record to LOW trust so
the fail-closed policy preserves the target.  See `symbols.py` for
the extraction rules and the motivating case.

NEW (2026-11-05) **Pre-existing untouched files count as recovered.**

`_resolve_module` and `_rec_will_be_on_disk` previously required
`rec.content is not None` — i.e., the file had to be written *this
run* — to count as a resolvable module. Pre-existing files loaded
from `--apply-to` and left untouched have `content is None` because
no new content was written, but they are on disk and imports from
them resolve fine at runtime. Treating them as missing produced
spurious `missing_module` findings for every pre-existing untouched
file referenced by a recovered file, which (a) polluted
`MISSING_FILES.txt` with files that are actually present, and (b)
made the pass 7.8 universal demotion fire spuriously on importers
that were perfectly fine.

The fix: a record counts as "recovered" if either (i) its content
will be written this run, or (ii) it's a pre-existing base loaded
from disk. Both cases leave the file present in the target tree.
"""

import ast
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .core import (
    FileRecord, _basename,
    _is_deletion_instruction,
    _looks_like_diff_body,
    _strip_diff_prefixes,
)


# ============================================================================
#  Pass 7 — Python import/export cross-validate
# ============================================================================

def pass7_cross_validate(records: dict[str, FileRecord]) -> dict:
    exports: dict[str, set[str]] = {}
    imports: dict[str, list[tuple[str, str]]] = {}

    for path, rec in records.items():
        if rec.content is None:
            continue
        ext = Path(path).suffix.lower()
        if ext == ".py":
            exports[path] = _py_exports(rec.content)
            imports[path] = _py_imports(rec.content)

    problems: list[dict] = []
    seen_missing_modules: set[tuple[str, str]] = set()
    seen_missing_exports: set[tuple[str, str, str]] = set()

    for path, imps in imports.items():
        for module, name in imps:
            target = _resolve_module(module, records, path)
            if target is None:
                if _is_likely_local(module, records):
                    clean = module.lstrip(".")
                    if not clean:
                        continue
                    # Report the TOP-LEVEL only when the top-level
                    # is itself missing; report the full sub-path
                    # when the top-level IS recovered but the
                    # specific submodule is not.  This keeps the
                    # problem list consistent with
                    # `write._aggregate_missing_imports` (which
                    # reports `pg_comfy.py`, not `image.py`, when
                    # `from pg_comfy.image import X` fails to
                    # resolve and `pg_comfy.py` is not recovered).
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
            if name not in exports.get(target, set()):
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
#
# Catches: pg_masks.py imports _rotateMask from ./pg_state.js, and
# pg_state.py's STATE_JS string literal doesn't export _rotateMask.
# The .py file parses fine; the JS ReferenceError only fires in the
# browser at module-load time.  Without this pass, deeppeek writes
# the file, reports HIGH trust, and the user discovers the bug days
# later when the playground blanks.

def pass7_5_js_validate(records: dict) -> list:
    """Return list of problems for JS modules.

    Each problem is a dict with `type` in
    {`missing_js_module`, `missing_js_export`},
    plus the same shape as pass7's problems so they can be merged
    into the manifest's `cross_validate.problems` list.
    """
    from .symbols import (
        _is_js_lang, _extract_js_imports_with_src,
        _extract_dominant_js_string, _extract_js_exports,
    )

    problems = []
    seen = set()

    for path, rec in records.items():
        if rec.content is None:
            continue

        js_source = None
        if _is_js_lang(path):
            js_source = rec.content
        elif path.endswith(".py"):
            js_source = _extract_dominant_js_string(rec.content)

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
            elif target_path.endswith(".py"):
                target_js = _extract_dominant_js_string(
                    target_rec.content)

            if target_js is None:
                continue

            exports = _extract_js_exports(target_js)
            if name not in exports:
                key = (path, target_path, name)
                if key not in seen:
                    seen.add(key)
                    problems.append({
                        "type": "missing_js_export",
                        "from": path,
                        "target": target_path,
                        "name": name,
                    })

    return problems


# ============================================================================
#  Pass 7.6 — demote on missing JS export (FIX 2)
# ============================================================================
#
# Cross-validation findings are trust signals, not just
# informational.  A `missing_js_export` finding means an importer
# will throw ReferenceError at module load time; if the target file
# *declares but does not export* the missing name, the whole
# ecosystem is broken (the playground will blank).  Both sides must
# fail-closed so the target tree is preserved and the user can fix
# the export manually.

_JS_DECL_RE_FOR_MISSING_EXPORT = re.compile(
    r'\b(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)'
)


def _js_target_declares_but_does_not_export(
        target_rec: FileRecord, name: str) -> bool:
    """True iff `target_rec.content` declares `name` but does not
    export it.  Uses the same JS-extraction path as symbols.py so
    embedded string literals are handled identically.
    """
    from .symbols import (
        _is_js_lang, _extract_dominant_js_string, _extract_js_exports,
    )
    if target_rec.content is None:
        return False
    js = None
    if _is_js_lang(target_rec.path):
        js = target_rec.content
    elif target_rec.path.endswith(".py"):
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
    """Demote records that participate in a `missing_js_export`
    finding where the target DECLARES the missing name but doesn't
    export it.

    Returns the number of records demoted (may be 0).

    Both the importer and the target are demoted: the importer will
    throw at module load; the target needs its `export` fixed.  If
    the target does not actually declare the name, this is a
    "missing module" case, handled elsewhere — do not demote here,
    the record is likely MISSING or PARTIAL_ONLY already.
    """
    n_demoted = 0
    for prob in js_problems:
        if prob.get("type") != "missing_js_export":
            continue
        from_path = prob["from"]
        target_path = prob["target"]
        name = prob["name"]

        if target_path not in records:
            continue
        target_rec = records[target_path]
        if not _js_target_declares_but_does_not_export(
                target_rec, name):
            continue

        # Demote the target: the export is missing, so anything that
        # imports the name will break.  Fail-closed.
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

        # Demote the importer: at module load, the import will
        # throw.  Fail-closed.
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
#
# The Python analogue of pass 7.6. `pass7_cross_validate` emits
# `missing_export` findings when a Python importer references a name
# its target does not declare at top level. Without this pass, those
# findings only set the exit code — the file that will fail at
# import time is still written to the target at HIGH trust. The
# daemon then dies at reload, and every command routed through it
# dies with it.
#
# Motivating failure: `kritomatic_daemon/handlers/comfyui.py` imports
# `build_krita_resources`, `paste_animation`, and `resize_canvas` from
# `kritomatic_daemon/utils/comfyui_krita.py`. When that target was
# truncated by a partial-reprint of two functions, all three imports
# broke. Both records shipped at HIGH trust and the daemon crashed.

def pass7_6b_demote_on_missing_python_export(
        records: dict,
        problems: list,
        verbose: bool = False) -> int:
    """Demote both sides of a Python `missing_export` finding.

    Analogous to `pass7_6_demote_on_missing_export` for JS. When
    `handlers/comfyui.py` imports `build_krita_resources` from
    `utils/comfyui_krita.py` and the recovered comfyui_krita.py
    doesn't declare it at top level, the daemon crashes at import
    time — the whole handler tree fails, and every command routed
    through it dies. That is strictly worse than not writing the
    record at all.

    The importer (whose import will fail) and the target (whose
    declaration is missing) are both demoted to LOW. Under default
    `--min-trust MEDIUM` neither reaches the target tree; the user
    reads the warning, fixes the missing export, and reruns.
    """
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
    Returns True if the repair succeeded and re-validates."""
    from .validate_output import validate_content
    from .symbols import (
        _is_js_lang, _extract_dominant_js_string, _extract_js_exports,
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
        return False  # already exported

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
    elif rec.path.endswith(".py"):
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
    `(repaired, remaining)` where `repaired` is a list of
    `{problem, repair, target}` dicts and `remaining` is the list of
    problems that could not be repaired."""
    repaired = []
    remaining = []
    repaired_set = set()  # (target_path, name)
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
#
# A single place that demotes on ANY `missing_export` /
# `missing_js_export` finding, regardless of how it was generated.
# Passes 7.6 and 7.6b are the specific handlers; this pass is the
# safety net that keeps working when a future cross-validate change
# introduces a new finding type without a matching specific demoter.
#
# `missing_module` findings are NOT demoted here. `_is_likely_local`
# is a heuristic (the underscore rule fires for `pg_comfy`, which is
# also a real PyPI package); a false positive demotion would
# fail-close a working file. Missing-module findings remain
# informational; the shield (pass 8.8) catches the underlying file
# corruption that is the real risk.

def pass7_8_demote_on_any_finding(records: dict,
                                  findings: list,
                                  verbose: bool = False) -> int:
    """Universal safety net: any missing-export finding demotes both
    sides.

    Returns the number of record-demotions (may double-count a record
    if it appears in multiple findings; the important thing is that
    every finding has been acted on).
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

    `./pg_state.js` imported from `pg_masks.py` resolves to
    `pg_state.py` when that file's `STATE_JS` string literal
    contains the actual JS.  Try each plausible extension in order.
    """
    base = module
    for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx"):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break

    dirname = os.path.dirname(from_path)
    for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"):
        cand = os.path.normpath(
            os.path.join(dirname, base + ext)
        ).replace("\\", "/")
        if cand in records and _rec_will_be_on_disk(records[cand]):
            return cand

    return None


def _py_exports(body: str) -> set[str]:
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return set()
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
    return out


def _py_imports(body: str) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return []
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
    return out


def _rec_will_be_on_disk(rec: FileRecord) -> bool:
    """True iff a record's file will exist in the target tree after
    the run.

    Two cases:
      * Content will be written this run: `rec.content is not None`
      * Pre-existing file, unchanged: `rec.preexisting_base` is True
        (the file was loaded from disk, and if it isn't written, the
        original stays)

    The pre-existing case is the load-bearing fix over the old test
    `rec.content is not None`. A pre-existing file that was loaded
    from `--apply-to` and never rewritten has `content = None` — no
    new content was produced for it. But the file IS on disk, and
    imports from it resolve at runtime. Treating it as missing
    produced spurious `missing_module` findings for every pre-existing
    untouched file that a recovered file imported from — and, with
    pass 7.8 in place, spurious demotions of perfectly working
    importers.
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

    # Bare (possibly dotted) module name.  Resolve the TOP-LEVEL
    # component first: that is the level at which deeppeek can
    # decide "recovered / not recovered".
    #
    #   · `pg_comfy.py` is recovered  → `pg_comfy.image` resolves
    #     (whether `image` is a real submodule, a package submodule,
    #     or an attribute on a single-file module).
    #   · `pg_comfy/__init__.py` is recovered  → the package exists;
    #     a dotted submodule is missing unless its own file is
    #     recovered.
    #   · Neither is recovered  → return None.
    #
    # Prior logic reduced every dotted import to its LAST
    # component and searched for that basename anywhere in the
    # recovery set.  That produced spurious `missing_module`
    # entries for `from pg_comfy.image import X` whenever an
    # unrelated `image.py` happened to exist elsewhere, and -- once
    # `write._aggregate_missing_imports` was fixed -- left the two
    # artefacts (MISSING_FILES.txt and manifest.run.cross_validate)
    # disagreeing about what is actually missing.
    parts    = module.split(".")
    top_name = parts[0]
    top_file = top_name + ".py"
    top_pkg  = top_name + "/__init__.py"
    sub_file = "/".join(parts) + ".py" if len(parts) > 1 else None

    # Single-file module takes priority: if `pg_comfy.py` is
    # recovered, every dotted path below it is an attribute
    # access at runtime, not a distinct file.
    for path, rec in records.items():
        if not _rec_will_be_on_disk(rec):
            continue
        if _basename(path) == top_file:
            return path

    # Package form: only a specific sub-path resolves.
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


_REPAIR_TERMINATORS = frozenset(';})]"\'>')


def _try_repair_python(body: str, err: SyntaxError) -> Optional[str]:
    stripped = body.rstrip()
    if not stripped:
        return None

    if stripped[-1] not in _REPAIR_TERMINATORS:
        return None

    candidates = [
        stripped + '\n"""',
        stripped + "\n'''",
        stripped + "\n)",
        stripped + "\n}",
        stripped + "\n]",
        stripped + "\n);",
        stripped + '\n"""\n',
    ]
    for cand in candidates:
        try:
            ast.parse(cand)
            return cand
        except SyntaxError:
            continue
    return None


_STRUCTURAL_PATCH_EXPORT_RE = re.compile(
    r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{')


def _has_structural_failures(rec: FileRecord) -> bool:
    for p in rec.patches:
        if p.get("applied_by") != "failed":
            continue
        body = p["body"].strip()
        if not body:
            continue
        first_line = body.split("\n", 1)[0].strip()
        if _STRUCTURAL_PATCH_EXPORT_RE.match(first_line):
            return True
        if body.startswith("@@ ") or "\n@@ " in body:
            return True
    return False


def _diff_lines_for_verify(body: str) -> tuple[list[str], list[str]]:
    adds: list[str] = []
    dels: list[str] = []
    for pl in body.splitlines():
        if pl.startswith("+") and not pl.startswith("+++"):
            content = pl[1:].rstrip()
            if content.strip():
                adds.append(content)
        elif pl.startswith("-") and not pl.startswith("---"):
            content = pl[1:].rstrip()
            if content.strip():
                dels.append(content)
    return adds, dels


def pass8_verify(records: dict[str, FileRecord],
                 blocks_by_idx: Optional[dict] = None) -> None:
    from .validate_output import validate_content

    # ---- global patch-conflict detection ----
    patch_usage = {}
    for _p, _r in records.items():
        for _pp in _r.patches:
            _i = _pp.get("idx")
            if _i is None:
                continue
            patch_usage.setdefault(_i, []).append(_p)
    for _p, _r in records.items():
        if _r.content is None:
            continue
        _v = _r.verification
        _conflicts = []
        for _pp in _r.patches:
            _i = _pp.get("idx")
            if _i is None:
                continue
            _others = [op for op in patch_usage.get(_i, []) if op != _p]
            if _others:
                _conflicts.append({
                    "patch_idx": _i,
                    "other_records": _others,
                })
        if _conflicts:
            _v["patch_conflict"] = _conflicts
            _r.trust = "LOW"
            _r.warnings.append(
                "patch-conflict: %d block(s) claimed by another "
                "record - likely a duplicated source block"
                % len(_conflicts))
            print(f"  [verify] {_p}: {len(_conflicts)} patch(es) "
                  f"cross-claimed by another record; "
                  f"demoting to LOW", file=sys.stderr)

    for path, rec in records.items():
        if rec.content is None:
            continue
        v = rec.verification

        base_for_check = None
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block is not None:
                base_for_check = base_block.body

        # Declarations that a deletion patch was explicitly told to
        # remove must not trigger the presence regression check.
        allow_missing = set()
        if base_for_check:
            from .core import _is_deletion_instruction, _strip_diff_prefixes
            for p in rec.patches:
                if p.get("applied_by") != "deterministic":
                    continue
                body = p.get("body", "")
                if not body:
                    continue
                if not _is_deletion_instruction(
                        body, p.get("context_before", "")):
                    continue
                stripped = _strip_diff_prefixes(body)
                for m in __import__("re").finditer(
                        r"(?:const|let|var|function|class)\s+"
                        r"([A-Za-z_$][\w$]*)",
                        stripped):
                    allow_missing.add(m.group(1))

        # Deduplicate blocks that were applied twice.
        from .validate_output import _dedup_duplicate_blocks
        from .validate_output import _dedup_repeated_statements
        from .validate_output import _LAST_DEDUP_DETAILS
        import deeppeek.recover.validate_output as _vo

        _skip_dedup = (
            rec.preexisting_base
            and not rec.pairs
            and not any(
                p.get("applied_by") != "already-present"
                for p in rec.patches
            )
        )

        if _skip_dedup:
            n_removed = 0
            _dedup_details = []
        else:
            _vo._LAST_DEDUP_DETAILS = []
            deduped, n_removed = _dedup_duplicate_blocks(
                rec.content, path)
            _dedup_details = list(_vo._LAST_DEDUP_DETAILS)
            _patch_bodies = [p.get("body", "") for p in rec.patches]
            _vo._LAST_DEDUP_DETAILS = []
            deduped2, n_removed2 = _dedup_repeated_statements(
                deduped, path,
                base=base_for_check,
                patch_bodies=_patch_bodies)
            _dedup_details.extend(_vo._LAST_DEDUP_DETAILS)
            if n_removed2 > 0:
                deduped = deduped2
                n_removed += n_removed2

        if n_removed > 0:
            rec.content = deduped
            rec.verification["deduped_blocks"] = n_removed
            if _dedup_details:
                rec.verification["deduped_detail"] = _dedup_details
            print(f"  [dedup] {path}: removed {n_removed} duplicate "
                  f"block(s)", file=sys.stderr)

        structural_err = validate_content(
            rec.content, path, base=base_for_check,
            allow_missing=allow_missing)
        if structural_err:
            v["content_valid"] = f"FAIL: {structural_err}"

            repair_result = None
            repair_raised = None
            if blocks_by_idx is not None:
                try:
                    from .repair import try_repair
                    repair_result = try_repair(
                        rec=rec,
                        structural_err=structural_err,
                        blocks_by_idx=blocks_by_idx,
                        path=path,
                    )
                except Exception as e:
                    repair_raised = str(e)
                    print(f"  [repair] {path}: repair attempt "
                          f"raised: {e}", file=sys.stderr)

            if repair_raised is not None:
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = (
                    f"repair attempt raised: {repair_raised}")
            elif repair_result is not None and \
                 repair_result.get("repaired_content"):
                rec.repair_content = repair_result["repaired_content"]
                v["sanity_repair_available"] = True
                v["sanity_repair_closers"] = repair_result["closers_added"]
                v["sanity_repair_reason"] = None
                print(f"  [repair] {path}: suggestion produced "
                      f"(+{repair_result['closers_added']} closer(s))",
                      file=sys.stderr)
            elif repair_result is not None:
                reason = (repair_result.get("refused_reason")
                          or "refused (no reason recorded)")
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = reason
                print(f"  [repair] {path}: refused ({reason})",
                      file=sys.stderr)
            else:
                v["sanity_repair_available"] = False
                v["sanity_repair_reason"] = (
                    "not applicable (not a brace-unbalance failure)")

            rec.trust = "LOW"
            rec.sanity_rejected = True
            if rec.patches:
                try:
                    _sanity_blocks = blocks_by_idx
                except NameError:
                    _sanity_blocks = None
                if _sanity_blocks is not None:
                    from .apply_patches import attribute_sanity_failure
                    _culprit = attribute_sanity_failure(rec, _sanity_blocks)
                    if _culprit is not None:
                        rec.verification['sanity_caused_by'] = _culprit
            rec.verification["sanity_reason"] = structural_err
            rec.warnings.append(
                f"content validation failed: {structural_err}")
            print(f"  [verify] content validation FAIL in {path}: "
                  f"{structural_err}", file=sys.stderr)
            continue
        v["content_valid"] = "ok"

        if path.endswith(".py"):
            try:
                ast.parse(rec.content)
                v["ast_parse"] = "ok"
            except SyntaxError as e:
                repaired = _try_repair_python(rec.content, e)
                if repaired is not None:
                    rec.content = repaired
                    v["ast_parse"] = "ok (auto-repaired)"
                    rec.warnings.append(
                        f"ast.parse failed at line {e.lineno} "
                        f"({e.msg}); auto-repaired by appending a "
                        f"plausible closer")
                    print(
                        f"  [verify] ast.parse FAIL in {path} — "
                        f"auto-repaired (was: line {e.lineno}: {e.msg})",
                        file=sys.stderr,
                    )
                else:
                    msg = f"line {e.lineno}: {e.msg}"
                    v["ast_parse"] = f"FAIL: {msg}"
                    rec.trust = "LOW"
                    rec.warnings.append(f"ast.parse failed: {msg}")
                    print(f"  [verify] ast.parse FAIL in {path}: {msg}",
                          file=sys.stderr)

        if (path.endswith(".py")
                and rec.dedup_reason == "last_full"
                and rec.trust == "MEDIUM"
                and v.get("ast_parse", "").startswith("ok")):
            if v.get("patches_incomplete") and _has_structural_failures(rec):
                v["last_full_promotion_blocked"] = (
                    "structural patch failure(s)")
                print(f"  [verify] {path}: last_full promotion blocked "
                      f"(structural patch failure)",
                      file=sys.stderr)
            else:
                rec.trust = "HIGH"
                v["last_full_promoted"] = True
                if v.get("patches_incomplete"):
                    rec.warnings.append(
                        "last_full promoted to HIGH after ast.parse "
                        "OK; failed patches were non-structural "
                        "(prose snippets) — see verification")
                    print(f"  [verify] {path}: last_full promoted to "
                          f"HIGH (ast.parse OK, failed patches are "
                          f"non-structural)",
                          file=sys.stderr)

        # ---- patch_presence ----
        if rec.patches and rec.content:
            content_lines = {
                ln.rstrip() for ln in rec.content.splitlines()
                if ln.strip()
            }
            content_lines_nonempty = [
                ln.rstrip() for ln in rec.content.splitlines()
                if ln.strip()
            ]

            total_missing_adds = 0
            total_deletions_not_removed = 0

            for patch in rec.patches:
                status = patch.get("applied_by")

                if status == "failed":
                    continue
                if status == "already-present":
                    continue
                if status == "skipped_weak_attribution":
                    continue
                if status == "deterministic":
                    continue

                body = patch["body"]

                if _looks_like_diff_body(body):
                    adds, dels = _diff_lines_for_verify(body)

                    for a in adds:
                        if a not in content_lines:
                            total_missing_adds += 1

                    if dels:
                        n = len(dels)
                        block_still_present = False
                        for i in range(
                                len(content_lines_nonempty) - n + 1):
                            if content_lines_nonempty[i:i + n] == dels:
                                block_still_present = True
                                break
                        if block_still_present:
                            total_deletions_not_removed += 1

                    continue

                if _is_deletion_instruction(
                        body, patch.get("context_before", "")):
                    stripped = _strip_diff_prefixes(body)
                    body_lines = [
                        ln.rstrip() for ln in stripped.splitlines()
                        if ln.strip()
                    ]
                    if body_lines:
                        n = len(body_lines)
                        for i in range(
                                len(content_lines_nonempty) - n + 1):
                            if content_lines_nonempty[i:i + n] == body_lines:
                                total_deletions_not_removed += 1
                                break
                    continue

                for pl in body.splitlines():
                    s = pl.strip()
                    if not s:
                        continue
                    if s.startswith(("//", "#")):
                        continue
                    if s not in rec.content:
                        total_missing_adds += 1

            if total_deletions_not_removed:
                v["patch_presence"] = (
                    f"{total_deletions_not_removed} deletion block(s) "
                    f"still present"
                )
                rec.warnings.append(
                    f"patch presence: "
                    f"{total_deletions_not_removed} deletion block(s) "
                    f"that should have been removed are still in the "
                    f"file"
                )
                rec.trust = "LOW"
                print(f"  [verify] patch presence FAIL in {path}: "
                      f"{total_deletions_not_removed} deletion block(s) "
                      f"did not take effect", file=sys.stderr)
            elif total_missing_adds:
                v["patch_presence"] = (
                    f"{total_missing_adds} missing lines")
                rec.warnings.append(
                    f"patch presence: {total_missing_adds} lines "
                    f"not found")
                rec.trust = "LOW"
                print(f"  [verify] patch presence FAIL in {path}: "
                      f"{total_missing_adds} missing lines",
                      file=sys.stderr)
            else:
                v["patch_presence"] = "ok"


def pass8_5_check_symbols(records: dict[str, FileRecord],
                          blocks_by_idx: Optional[dict] = None) -> None:
    """Cross-reference patch-introduced bare calls against each file's
    imports and declarations.

    An unresolved call introduced by a patch almost certainly means
    the block was attributed to the wrong file — e.g. a
    `pgkitRegisterField(...)` block landing in a module that imports
    `pgkitRegisterAction` but not `pgkitRegisterField`, which throws
    `ReferenceError` at load time and blanks the target's UI.

    Records with at least one unresolved call are demoted to LOW
    trust so the fail-closed policy preserves the target and routes
    the attempt into `deeppeek/low_trust/`.  The failure, the call
    names, and the file path are all recorded in `warnings.txt` and
    `manifest.json`.

    Runs after `pass8_verify` so structural / parse failures already
    have their say first.  A record that already fail-closed for
    structural reasons is not re-demoted here; only the
    `unresolved_calls` list is recorded for completeness.
    """
    from .symbols import find_unresolved_calls

    for path, rec in records.items():
        if rec.content is None:
            continue
        if rec.is_deleted:
            continue
        if not (rec.patches or rec.pairs):
            continue

        base = None
        if blocks_by_idx is not None and rec.last_full_idx is not None:
            base_block = blocks_by_idx.get(rec.last_full_idx)
            if base_block is not None:
                base = base_block.body

        unresolved = find_unresolved_calls(rec, base, records)
        if not unresolved:
            continue

        rec.verification["unresolved_calls"] = unresolved
        rec.trust = "LOW"
        rec.warnings.append(
            "unresolved symbol reference(s) introduced by patch: "
            + ", ".join(unresolved)
            + " (patch almost certainly targeted the wrong file)"
        )
        preview = ", ".join(unresolved[:5])
        suffix = " ..." if len(unresolved) > 5 else ""
        print(f"  [symbols] {path}: {len(unresolved)} unresolved "
              f"call(s): {preview}{suffix}",
              file=sys.stderr)
