"""
recover.validate — Pass 7 (cross-validate imports) and Pass 8
(verify files).

Pass 7 is a pure read over the recovered content; it walks every
Python import in every recovered file and reports the ones that do
not resolve to a recovered target, de-duplicated per (file, module)
pair.  The concatenation model reported one problem per imported
*name*, which turned `from playground_fonts import A, B, C` into
three identical `missing_module` lines; this version de-dupes.

THREE FALSE-POSITIVE SOURCES ELIMINATED
=======================================
The pass-7 report used to be dominated by noise.  All three
classes are now handled:

    · Framework namespaces (`pgkit.*`, `playground_*`).  Resolved at
      runtime by the kit's `pgkit_modules()` manifest, not by
      anything the conversation was trying to recover.
      `_EXTERNAL_NAMESPACES` lists them; `_is_likely_local` rejects
      any module whose first dotted segment matches.  Shared with
      `write._aggregate_missing_imports` so both reports stay
      silent on the same inputs.

    · Contentless records.  A header that never got a full print
      sits in `records` with `content = None`.  `_resolve_module`
      skips any such record; the import falls through to
      `_is_likely_local`, which reports it as `missing_module`
      rather than as a `missing_export` from an empty file.

    · Stdlib modules shadowing a local basename.  `import
      http.server` used to match a recovered `server.py` via the
      dotted-last-segment fallback.  `_resolve_module` now checks
      the top segment against `sys.stdlib_module_names` and
      `_EXTERNAL_NAMESPACES` *before* any resolution attempt, so
      dotted stdlib modules are never considered candidates for
      local resolution.

FLAT-LAYOUT RESOLUTION
======================
`from .extension.install import install` in a nested package
(`dsx/extension/install.py`) arrives as the module reference
`.extension.install`.  In the flat output layout, the recovered
file is stored under its bare basename (`install.py`) — the
`dsx/extension/` directory prefix is gone.  `_resolve_module`
first tries the joined path (`extension/install.py`); if that
fails, it falls back to the last dotted segment's basename
(`install.py`).  The fallback is only used for *relative*
imports; absolute imports resolve strictly by basename so a
stray `import server` cannot accidentally match a project-local
`server.py` when `server` really refers to a stdlib module.

`_py_imports` additionally preserves `node.level` — the count of
leading dots in `from .paths import x` — so that relative
semantics survive into `_resolve_module`'s relative branch.

Pass 8 is nominally a read but can mutate: when `ast.parse` fails
on a `.py` body whose last non-blank character looks like a clean
statement terminator, a small set of "closer" appends are tried and
the first that parses is written back.  This is a truncation-repair
heuristic for artefacts of the markdown extraction pipeline (an
ES-module JS body whose closing `\"\"\"` was cut off; a Python
literal whose trailing bracket was truncated).  The repair is
append-only and preserves the truncated prefix verbatim, so a
mid-file structural error cannot be masked.

Pass 8 also promotes `last_full` picks that pass `ast.parse` and
carry no *structural* patch failures.  Rationale (see the block
below the `pass8_verify` body):

    `last_full` means dedup had multiple full prints and picked
    the later one as a tiebreaker, capping trust at MEDIUM.  Once
    the pick parses cleanly, the only question the tiebreaker was
    hedging — "is this the right revision?" — is answered by the
    fact that the picked content is syntactically valid and is
    the *later* of the candidates.

The py-AST helpers `_py_exports`, `_py_imports`, `_resolve_module`,
`_is_likely_local` live here because both pass 7 and pass 9 need
them.
"""

import ast
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .core import FileRecord, _basename


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
                    # Relative imports carry their leading dots
                    # (e.g. ".paths"); display the display-friendly
                    # form without them.  A bare "." means
                    # ``from . import x`` — an attribute on the
                    # package __init__, not a recoverable file.
                    display = module.lstrip(".")
                    if not display:
                        continue
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
    """Return ``(module_with_level_prefix, name)`` pairs.

    ``node.level`` in the AST is the count of leading dots in the
    import statement — ``from .paths import x`` has level 1,
    ``from .. import x`` has level 2.  The original code dropped
    this and passed the bare module name to ``_resolve_module``,
    which treated it as an absolute import.  For flat layouts that
    happened to work (``paths`` == ``paths.py``); for nested
    packages it silently resolved to the wrong file or nothing at
    all.  Prefixing the module name with ``"." * level`` preserves
    the semantics so ``_resolve_module``'s relative branch fires.

    ``ast.walk`` (not ``tree.body``) so function-level imports are
    captured too — the dsx extension subcommands import lazily
    inside their ``cmd_ext_*`` handlers.
    """
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


# Framework namespaces — anything imported from these is resolved by
# the kit's `pgkit_modules()` manifest at runtime, not by the
# recovered set.  Reporting them as missing is noise: the
# conversation was never going to recover `pgkit.html_shell` because
# pgkit is the extraction tool's own dependency tree, and
# `playground_fonts` is a shared-library module outside the
# playground being extracted.  Adding a name here silences both the
# import-graph report (pass 7) and the missing-files report (pass 9).
_EXTERNAL_NAMESPACES = ("pgkit", "playground_")


def _resolve_module(module: str, records: dict[str, FileRecord],
                    from_path: str) -> Optional[str]:
    """Resolve a module reference to a recovered file path.

    Three things are checked before the recovered set is even
    consulted:

      1. Stdlib tops.  ``import http.server`` must never match a
         local ``server.py`` via the last-segment fallback below.
         The top segment is compared against
         ``sys.stdlib_module_names``; if it matches, the reference
         cannot resolve to a recovered file.

      2. External namespaces (``pgkit.*``, ``playground_*``).  Same
         reasoning — resolved by the kit's own module manifest, not
         by the recovered set.

      3. Content-bearing records.  A header that was never given a
         full print sits in ``records`` with ``content = None``;
         treating it as if it were a valid file produced false
         ``missing_export`` errors.  Skipping contentless records
         here makes the caller fall through to
         ``_is_likely_local``, which correctly reports the file as
         missing.

    Relative imports (``from .paths import x``) resolve against the
    directory of ``from_path``, walking up ``level - 1`` directories
    first, with a **basename fallback** for the flat output layout:
    when ``dsx/extension/install.py`` has been flattened to
    ``install.py``, the joined path ``extension/install.py`` does
    not exist but the basename ``install.py`` does.

    Absolute imports resolve by basename against the recovered set.
    A dotted absolute module (``dsx.extension.paths``) additionally
    tries the last segment's basename (``paths.py``); this is only
    reached for imports whose top segment is *not* stdlib or
    external, which the check above guarantees.
    """
    # ----- 1 & 2: skip stdlib and external namespaces -----
    if not module.startswith((".", "/")):
        top = module.split(".", 1)[0]
        if hasattr(sys, "stdlib_module_names"):
            if top in sys.stdlib_module_names:
                return None
        for ns in _EXTERNAL_NAMESPACES:
            if top == ns or top.startswith(ns + "."):
                return None

    # ----- 3: relative imports -----
    if module.startswith("."):
        level = 0
        while level < len(module) and module[level] == ".":
            level += 1
        rest = module[level:]
        if not rest:
            # ``from . import x`` — attribute on the package
            # __init__, not a recoverable file.
            return None

        parent = os.path.dirname(from_path)
        for _ in range(level - 1):
            parent = os.path.dirname(parent)

        parts = rest.split(".")

        # Try the full joined path first (non-flat layouts).
        candidate = "/".join(([parent] if parent else []) + parts)
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = candidate + ext
            rec = records.get(full)
            if rec is not None and rec.content is not None:
                return full

        # Flat-layout fallback: the directory components of the
        # package path were lost during extraction, so
        # `from .extension.install import x` arrives as
        # `.extension.install` but the file lives at the bare
        # basename `install.py`.
        last = parts[-1]
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = last + ext
            rec = records.get(full)
            if rec is not None and rec.content is not None:
                return full
        return None

    # ----- absolute-path form (rare) -----
    if module.startswith("/"):
        for ext in ("", ".py", ".js", ".mjs", ".ts", ".tsx"):
            full = module + ext
            rec = records.get(full)
            if rec is not None and rec.content is not None:
                return full
        return None

    # ----- absolute module name -----
    # Match by basename against the recovered set (records use
    # basenames as keys).
    for path, rec in records.items():
        if rec.content is None:
            continue
        if _basename(path) == module + ".py":
            return path
        # Dotted absolute module (``dsx.extension.paths``) →
        # last-segment basename match.  Only reached here for
        # imports whose top segment is *not* stdlib or external,
        # which guarantees we never confuse `http.server` with a
        # local `server.py`.
        last = module.rsplit(".", 1)[-1]
        if last != module and _basename(path) == last + ".py":
            return path
    return None


def _is_likely_local(module: str,
                     records: Optional[dict] = None) -> bool:
    """True if `module` looks like it belongs to the project being
    recovered.

    Signals, in priority order:

      1. Relative or absolute-path import → local by definition.
      2. A file with the module's basename exists in `records` as
         a header (even with `content = None`) → local.  This
         catches non-underscore-prefixed modules like `paths`,
         `models`, `slicer` that the original `"_" in module`
         heuristic missed.
      3. First dotted segment matches a framework namespace
         (``pgkit``, ``playground_``) → external.
      4. First dotted segment is in ``sys.stdlib_module_names`` →
         external.
      5. Underscore in the module name → local (the original
         heuristic, kept as a last-resort signal for projects that
         don't fit any of the above).
    """
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


# Clean statement terminators — the last non-blank character of
# `body` must be one of these for `_try_repair_python` to attempt an
# append.  A file whose last line ends mid-expression (`foo.bar(`)
# is not "repaired" by appending a closer: the append would silently
# swallow the truncation and produce a body that parses but is
# semantically incomplete.
_REPAIR_TERMINATORS = frozenset(';})]"\'>')


def _try_repair_python(body: str, err: SyntaxError) -> Optional[str]:
    """Try appending a plausible closer when ast.parse fails on `body`.

    Only fires when the last non-blank character of the body looks
    like a clean statement terminator (`;`, `}`, `)`, `]`, `"`, `'`,
    `>`).  A body that ends mid-expression (e.g. `foo.bar(`) is not
    repaired — the append would produce a file that parses but is
    semantically incomplete.

    The repair is append-only: the returned body's prefix is
    `body.rstrip()`, verbatim.  Nothing is rewritten, deleted, or
    reformatted.

    Returns the repaired source, or None if no repair works.
    """
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


# Real diff shapes — a `last_full` pick's failed patches count as
# "structural" only if their body looks like one of these.  A prose
# snippet that happened to quote a line of the base does not.
_STRUCTURAL_PATCH_EXPORT_RE = re.compile(
    r'^export\s+(?:const|let|var)\s+\w+\s*=\s*\{')


def _has_structural_failures(rec: FileRecord) -> bool:
    """True if any failed patch body looks like a real diff.

    Used by `pass8_verify`'s `last_full` promotion: a pick whose only
    patch failures are non-structural (prose snippets, partial-line
    quotes) can be promoted to HIGH, because the failures are noise
    and not evidence that the base content is incomplete.  A pick
    whose failures *do* look like real diffs stays at MEDIUM — the
    base is likely missing a real change.
    """
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


def pass8_verify(records: dict[str, FileRecord]) -> None:
    for path, rec in records.items():
        if rec.content is None:
            continue
        v = rec.verification

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

        # Promote `last_full` picks to HIGH after a successful parse.
        #
        # `last_full` means dedup had multiple full prints and picked
        # the later one as a tiebreaker, capping trust at MEDIUM.
        # Once the pick parses cleanly, the only question the
        # tiebreaker was hedging — "is this the right revision?" —
        # is answered by the fact that the picked content is valid
        # syntax and is the later of the candidates.  The failed
        # patches, if any, are audited in
        # `verification.patches_incomplete` and in `warnings.txt`;
        # if they look structural, we keep MEDIUM; otherwise the
        # failures are noise and HIGH is appropriate.
        if (path.endswith(".py")
                and rec.dedup_reason == "last_full"
                and rec.trust == "MEDIUM"
                and v.get("ast_parse", "").startswith("ok")):
            if v.get("patches_incomplete") and _has_structural_failures(rec):
                # Genuine failure — keep MEDIUM, note why.
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

        if rec.patches and rec.content:
            total_missing = 0
            for patch in rec.patches:
                if patch.get("applied_by") == "failed":
                    continue
                missing = []
                for pl in patch["body"].splitlines():
                    s = pl.strip()
                    if not s:
                        continue
                    if s.startswith(("//", "#")):
                        continue
                    if s not in rec.content:
                        missing.append(s[:60])
                total_missing += len(missing)
            if total_missing:
                v["patch_presence"] = f"{total_missing} missing lines"
                rec.warnings.append(
                    f"patch presence: {total_missing} lines not found")
                rec.trust = "LOW"
                print(f"  [verify] patch presence FAIL in {path}: "
                      f"{total_missing} missing lines", file=sys.stderr)
            else:
                v["patch_presence"] = "ok"
