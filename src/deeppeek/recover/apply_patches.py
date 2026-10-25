"""
recover.apply_patches — Pass 6 (apply pairs, then patches, then
sanity-check the result).

Order matters:

  1. Pairs run first (scoped replaces and replace pairs).
  2. Individual patches run second, over the possibly-paired content.
  3. Sanity gate runs on the final content.

FAIL-CLOSED
===========
Any failed patch or failed pair sets `rec.trust = "LOW"`.  Under
default `--min-trust MEDIUM`, the file goes to `low_trust/` and the
target tree is preserved.  Override with `--min-trust LOW`.

INSERTION HEURISTICS (in `_try_deterministic_patch`, in order)
==============================================================
  1. Deletion (explicit `_is_deletion_instruction`).
  2. Raw diff (`_try_raw_diff_patch`).
  3. Ellipsis-splice (`_try_ellipsis_splice`):
       Body carries a `...` marker (line that is just `...`, or
       `// ...`, or `/* ... */`, etc.).  The block shows a prefix
       (new scope opening + new/old body lines), `...`, and a
       suffix (scope closing).  Insert only the genuinely-new lines
       at their position within the base scope, preserve everything
       else.  This handles the very common "here's the new start of
       the function, rest unchanged" idiom.
  4. Object-literal splice (`export const X = { ... }` against base).
  5. Context-insert (`_try_context_insert`): body has a unique anchor
     *inside* it.  New lines (absent from base) inserted before/after.
  6. Object-entry-insert (`_try_object_entry_insert`): body is
     `key: value,` lines AND prose names `func()` — insert entries
     into `func`'s last `= { ... }` object.
  7. Insert-after-anchor (`_try_insert_after_anchor`): prose names an
     anchor.  Three anchor types:
        a. Call + first arg: `` `funcName("arg"` ``
        b. Identifier: `` `ident` `` → matched as `if (ident) { ... }`
           or `const ident = ...;`
        c. Statement content: any backtick-quoted content >= 6 chars
           that appears exactly once in base — insert after its line.

DIAGNOSTICS (2026-11-01d)
=========================
When a patch fails, `_diagnose_patch_failure` returns a
human-readable reason that names the block's shape and why no
heuristic could place it.  When a pair fails, `_diagnose_replace_
failure` / `_diagnose_scoped_failure` return specific reasons.

Truncation handling (2026-11-01e)
=================================
The sanity gate no longer refuses a patch merely because its *body*
has an ellipsis.  It refuses when the *final content* has a
truncation marker not present in *base* — which only happens if
something went wrong.  Ellipsis blocks are now correctly handled by
`_try_ellipsis_splice` before the sanity gate fires.  If the splice
itself fails, the block fail-closes with a specific reason and the
target is preserved.
"""

import re
import sys
from typing import Optional

from .core import (
    Block, FileRecord, FENCE, MAX_TOKENS_DIFF,
    _is_deletion_instruction,
    _is_pure_deletion_diff_strict,
    _looks_like_diff_body,
    _strip_diff_prefixes,
)
from .llm import LLMClient
from .prose import _extract_scope_from_body


_EXPORT_CONST_OBJECT_RE = re.compile(
    r'^(export\s+(?:const|let)\s+(\w+)\s*=\s*)\{'
)


# ---------------------------------------------------------------------------
# Truncation marker detection (used by the sanity gate)
# ---------------------------------------------------------------------------

_TRUNCATION_PATTERNS = [
    re.compile(r'^\s*\.\.\.\s*$', re.MULTILINE),
    re.compile(r'^\s*<\s*\.\.\.\s*>\s*$', re.MULTILINE),
    re.compile(r'//\s*\.\.\.', re.MULTILINE),
    re.compile(r'/\*\s*\.\.\.\s*\*/'),
    re.compile(r'#\s*\.\.\.', re.MULTILINE),
    re.compile(r'<!--\s*\.\.\.\s*-->'),
]


def _has_truncation_marker(text: str) -> bool:
    """True iff `text` carries a truncation-style marker."""
    if not text:
        return False
    for pat in _TRUNCATION_PATTERNS:
        if pat.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# Ellipsis detection (line-based, stricter than truncation markers)
# ---------------------------------------------------------------------------

def _is_ellipsis_line(line: str) -> bool:
    """True iff `line` is exactly an ellipsis marker.

    Handles:
      ...
      // ...
      # ...
      /* ... */
      * ...
      <!-- ... -->

    Excludes:
      ...args         (spread operator)
      function f(...args)
      const s = "...";
      // ... for details   (has content after ellipsis)
    """
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    # Strip comment prefix.
    for prefix in ("//", "#", "/*", "*", "<!--", "--"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
            break
    # Strip comment suffix.
    for suffix in ("*/", "-->"):
        if stripped.endswith(suffix):
            stripped = stripped[:-len(suffix)].strip()
            break
    return stripped in ("...", "\u2026")


def _has_ellipsis_marker(body: str) -> bool:
    """True iff `body` has at least one ellipsis-marker line."""
    if not body:
        return False
    for ln in body.splitlines():
        if _is_ellipsis_line(ln):
            return True
    return False


def _split_at_ellipsis(body: str) -> tuple:
    """Split body at the first ellipsis line.

    Returns (prefix_lines, suffix_lines).  Both lists exclude ellipsis
    lines.  Additional ellipsis lines in the suffix (multi-marker
    blocks) are stripped from the returned suffix — their content is
    treated as body and will be filtered by the already-in-base check.
    """
    lines = body.splitlines()
    first = None
    for i, ln in enumerate(lines):
        if _is_ellipsis_line(ln):
            first = i
            break
    if first is None:
        return lines, []
    prefix = lines[:first]
    suffix = [ln for ln in lines[first + 1:]
              if not _is_ellipsis_line(ln)]
    return prefix, suffix


# ---------------------------------------------------------------------------
# Scope detection helpers (byte-accurate, for splice)
# ---------------------------------------------------------------------------

def _find_scope_in_base(base: str, scope_name: str) -> Optional[tuple]:
    """Locate scope_name in base and return (decl_start, body_open,
    body_close, scope_end) byte positions, or None.

    HTML `<details>` scopes are deliberately unsupported by the
    ellipsis-splice path — the same HTML block still works via the
    full-replace scoped path in `_apply_scoped`.
    """
    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        m = pattern.search(base)
        if not m:
            continue
        matched = base[m.start():m.end()]
        if matched.lstrip().startswith("<details"):
            return None  # HTML ellipsis-splice unsupported
        brace_open = base.find("{", m.start(), m.end())
        if brace_open == -1:
            continue
        brace_close = _find_matching_brace(base, brace_open)
        if brace_close == -1:
            continue
        return (m.start(), brace_open + 1, brace_close, brace_close + 1)
    return None


def _matches_scope_declaration(line: str, scope_name: str) -> bool:
    """True iff `line` looks like the declaration of `scope_name`."""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    esc = re.escape(scope_name)
    if re.match(r'^(?:async\s+)?def\s+' + esc + r'\s*\(', stripped):
        return True
    if re.match(r'^class\s+' + esc + r'\b', stripped):
        return True
    if re.match(r'^(?:export\s+)?(?:async\s+)?function\s+' + esc +
                r'\s*\(', stripped):
        return True
    if re.match(r'^(?:export\s+)?(?:const|let|var)\s+' + esc +
                r'\s*=', stripped):
        return True
    if re.match(r'^(?:export\s+)?(?:async\s+)?class\s+' + esc +
                r'\b', stripped):
        return True
    return False


_SCOPE_CLOSE_RE = re.compile(r'^\s*(?:\}|\};|\};?\s*\}\s*;?|</details>)\s*$',
                             re.IGNORECASE)


def _looks_like_scope_close(line: str) -> bool:
    return bool(_SCOPE_CLOSE_RE.match(line))


# ---------------------------------------------------------------------------
# Line splicing helpers
# ---------------------------------------------------------------------------

def _insert_lines_in_inner(inner: str, new_lines: list,
                            before_anchor: Optional[str] = None,
                            after_anchor: Optional[str] = None,
                            default_position: str = "top") -> str:
    """Insert `new_lines` into `inner` (the text inside a scope's braces).

    `inner` is the raw text between the scope's opening `{` and its
    closing `}`.  Its natural shape is `\\n  body\\n` (or `""` for an
    empty scope).  Lines in `new_lines` are reindented to match the
    scope's body indent.
    """
    if not new_lines:
        return inner
    lines = inner.split("\n")

    # Determine indent from first non-blank line.
    indent = "  "
    for ln in lines:
        if ln.strip():
            stripped = ln.lstrip(" \t")
            indent = ln[:len(ln) - len(stripped)]
            break

    # Reindent.
    first_indent = None
    for ln in new_lines:
        if ln.strip():
            first_indent = len(ln) - len(ln.lstrip(" \t"))
            break
    if first_indent is None:
        first_indent = 0
    indented = []
    for ln in new_lines:
        if not ln.strip():
            indented.append("")
        else:
            relative = len(ln) - len(ln.lstrip(" \t")) - first_indent
            indented.append(indent + " " * max(0, relative) + ln.lstrip(" \t"))

    # Empty inner → wrap in newlines.
    if not lines or (len(lines) == 1 and lines[0] == ""):
        return "\n" + "\n".join(indented) + "\n"

    # Find insert position.
    insert_at = None
    if before_anchor is not None:
        for i, ln in enumerate(lines):
            if ln.strip() == before_anchor:
                insert_at = i
                break
    if insert_at is None and after_anchor is not None:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == after_anchor:
                insert_at = i + 1
                break
    if insert_at is None:
        if default_position == "top":
            insert_at = 1 if lines[0] == "" else 0
        else:
            insert_at = len(lines) - 1 if lines[-1] == "" else len(lines)

    return "\n".join(lines[:insert_at] + indented + lines[insert_at:])


# ---------------------------------------------------------------------------
# Ellipsis splice — the main new path
# ---------------------------------------------------------------------------

def _try_ellipsis_splice(base: str, body: str,
                         context_before: str = "") -> Optional[str]:
    """Handle a scope-replace block that carries an `...` marker.

    Shape:
        <scope opening>
        <new body lines, some may already be in base>
        ...
        <new body lines, some may already be in base>
        <scope closing>

    Algorithm:
      1. Extract the scope name from the block body.
      2. Locate the scope in base (byte-accurate).
      3. Split the block at `...` into prefix and suffix line lists.
      4. Verify the prefix's first line matches the scope declaration.
      5. Strip scope open (from prefix) and scope close (from suffix).
      6. Filter both halves to lines NOT already in base's scope body.
      7. Find anchors: the first prefix line that exists in base, and
         the last suffix line that exists in base.
      8. Insert new prefix lines just before that prefix anchor (or at
         the top of the scope body if no anchor).
      9. Insert new suffix lines just after that suffix anchor (or at
         the bottom of the scope body if no anchor).
     10. Splice the resulting inner back into base.

    Returns the modified base, or None if any step fails.  Returning
    None causes the caller to fail-closed with a reason from
    `_diagnose_patch_failure` / `_diagnose_scoped_failure`.
    """
    if not _has_ellipsis_marker(body):
        return None

    scope_name, _kind = _extract_scope_from_body(body)
    if not scope_name:
        return None

    scope_info = _find_scope_in_base(base, scope_name)
    if scope_info is None:
        return None
    decl_start, body_open, body_close, scope_end = scope_info

    prefix_lines, suffix_lines = _split_at_ellipsis(body)
    if not prefix_lines:
        return None

    if not _matches_scope_declaration(prefix_lines[0], scope_name):
        return None

    inner_prefix = prefix_lines[1:]
    if suffix_lines and _looks_like_scope_close(suffix_lines[-1]):
        inner_suffix = suffix_lines[:-1]
    else:
        inner_suffix = suffix_lines

    base_scope_inner = base[body_open:body_close]
    base_stripped_set = {
        ln.strip() for ln in base_scope_inner.splitlines() if ln.strip()
    }

    new_prefix = [ln for ln in inner_prefix
                  if ln.strip() and ln.strip() not in base_stripped_set]
    new_suffix = [ln for ln in inner_suffix
                  if ln.strip() and ln.strip() not in base_stripped_set]

    if not new_prefix and not new_suffix:
        return None

    prefix_anchor = None
    for ln in inner_prefix:
        if ln.strip() and ln.strip() in base_stripped_set:
            prefix_anchor = ln.strip()
            break

    suffix_anchor = None
    for ln in reversed(inner_suffix):
        if ln.strip() and ln.strip() in base_stripped_set:
            suffix_anchor = ln.strip()
            break

    result_inner = base_scope_inner

    if new_prefix:
        result_inner = _insert_lines_in_inner(
            result_inner, new_prefix,
            before_anchor=prefix_anchor,
            default_position="top")

    if new_suffix:
        result_inner = _insert_lines_in_inner(
            result_inner, new_suffix,
            after_anchor=suffix_anchor,
            default_position="bottom")

    return base[:body_open] + result_inner + base[body_close:]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def pass6_apply_patches(records: dict,
                        blocks_by_idx: dict,
                        llm: Optional[LLMClient]) -> None:
    for path, rec in records.items():
        if rec.state not in ("SINGLE_FULL", "MULTI_FULL", "FULL_PLUS_PATCH"):
            continue
        if rec.is_deleted:
            continue
        if rec.last_full_idx is None:
            continue
        base_block = blocks_by_idx.get(rec.last_full_idx)
        if not base_block:
            rec.warnings.append("base block not found")
            rec.trust = "LOW"
            continue
        base = base_block.body
        content = base

        trust_cap = "MEDIUM" if rec.dedup_reason == "last_full" else "HIGH"

        pair_records: list = []
        if rec.pairs:
            content, pair_records = _apply_pairs(content, rec.pairs)
            rec.verification["pair_records"] = pair_records

        all_deterministic = True
        all_ok = True
        for patch in rec.patches:
            result = _try_deterministic_patch(content, patch, path)
            if result is not None:
                content = result
                patch["applied_by"] = "deterministic"
                continue

            if _patch_already_present(patch["body"], content):
                patch["applied_by"] = "already-present"
                continue

            if _diff_already_applied(patch["body"], content):
                patch["applied_by"] = "already-present"
                continue

            if _is_deletion_instruction(
                    patch["body"], patch.get("context_before", "")):
                if _deletion_already_applied(patch["body"], content):
                    patch["applied_by"] = "already-present"
                    continue

            all_deterministic = False
            if llm is not None:
                result = _try_llm_patch(content, patch, llm, path)
                if result is not None:
                    content = result
                    patch["applied_by"] = "llm"
                    continue

            patch["applied_by"] = "failed"
            patch["failure_reason"] = _diagnose_patch_failure(
                patch, content, base)
            rec.warnings.append(
                f"patch idx={patch['idx']} could not be applied "
                f"({patch['failure_reason']})")
            all_ok = False

        rec.verification["patches_applied"] = sum(
            1 for p in rec.patches if p.get("applied_by") != "failed")
        rec.verification["pairs_applied"] = sum(
            1 for r in pair_records if r["applied_by"] == "deterministic")

        if pair_records:
            n_failed = sum(
                1 for r in pair_records if r["applied_by"] == "failed")
            if n_failed:
                rec.verification["pairs_incomplete"] = True
                reasons = [
                    r.get("reason", "unknown")
                    for r in pair_records
                    if r["applied_by"] == "failed"
                ]
                rec.warnings.append(
                    f"{n_failed}/{len(pair_records)} replace pair(s) "
                    f"failed: " + "; ".join(reasons))

        suspicious, reason = _sanity_gate(base, content)
        if suspicious:
            # Truncation check: refuse only if content has a marker
            # that base doesn't.  A successfully-spliced ellipsis block
            # leaves no `...` behind, so this only fires when
            # something went wrong (defense in depth).
            if (_has_truncation_marker(content)
                    and not _has_truncation_marker(base)):
                reason = (
                    "truncation marker (e.g. `...`) present in result "
                    "but not in base; refusing to write truncated "
                    "content")
            rec.sanity_rejected = True
            content = base
            rec.trust = "LOW"
            rec.verification["sanity_reason"] = reason
            rec.warnings.append(f"sanity gate: {reason} — reverted to base")
            rec.verification["sanity_rejected"] = True
            print(f"  [sanity] {path}: {reason} — reverting to base",
                  file=sys.stderr)
        else:
            had_failure = (
                not all_ok
                or rec.verification.get("pairs_incomplete", False)
            )
            if had_failure:
                rec.trust = "LOW"
                rec.verification["patches_incomplete"] = (
                    rec.verification.get("patches_incomplete", False)
                    or not all_ok
                )
                rec.warnings.append(
                    "patch or pair application failed; file not written "
                    "to target under default --min-trust "
                    "(override with --min-trust LOW to force-write)")
                print(f"  [pass 6] {path}: incomplete application -> LOW "
                      f"trust", file=sys.stderr)
            else:
                rec.trust = trust_cap
                if all_ok and not all_deterministic and rec.patches:
                    rec.verification["via_llm"] = True

        rec.content = content

    total_patches = sum(len(r.patches) for r in records.values())
    total_pairs = sum(len(r.pairs) for r in records.values())
    total_applied = sum(
        sum(1 for p in r.patches
            if p.get("applied_by") in ("deterministic", "llm"))
        for r in records.values()
    )
    total_pairs_applied = sum(
        r.verification.get("pairs_applied", 0)
        for r in records.values()
    )
    total_noop = sum(
        sum(1 for p in r.patches
            if p.get("applied_by") == "already-present")
        for r in records.values()
    )
    total_failed = total_patches - total_applied - total_noop
    files_with_failures = sum(
        1 for r in records.values()
        if any(p.get("applied_by") == "failed" for p in r.patches)
        or r.verification.get("pairs_incomplete")
    )
    n_sanity = sum(
        1 for r in records.values() if r.sanity_rejected
    )
    if total_patches or total_pairs:
        parts = []
        if total_patches:
            parts.append(f"{total_applied}/{total_patches} patch(es)")
        if total_pairs:
            parts.append(f"{total_pairs_applied}/{total_pairs} pair(s)")
        suffix = ""
        if total_failed:
            suffix = (f" ({total_failed} failed across "
                      f"{files_with_failures} file(s) — see warnings.txt)")
        if n_sanity:
            suffix += f" [{n_sanity} file(s) sanity-rejected]"
        if files_with_failures:
            suffix += " [fail-closed: LOW trust, not written to target]"
        print(f"  {' + '.join(parts)} applied{suffix}", file=sys.stderr)
        if total_noop:
            print(f"  {total_noop} patch(es) already-present "
                  f"(no-op, not failures)", file=sys.stderr)


# ============================================================================
# Pairs
# ============================================================================

def _apply_pairs(base: str, pairs: list) -> tuple:
    records: list = []
    current = base
    for pair in pairs:
        if pair["old_idx"] == -1:
            result = _apply_scoped(
                current, pair["scope_name"], pair["new_body"])
            if result is None:
                reason = _diagnose_scoped_failure(
                    current, pair["scope_name"],
                    pair.get("new_body", ""))
                records.append({
                    "applied_by": "failed", "pair": pair,
                    "reason": reason,
                })
                continue
        else:
            result = _try_replace_pair(
                current, pair["old_body"], pair["new_body"])
            if result is None:
                reason = _diagnose_replace_failure(
                    current, pair["old_body"])
                records.append({
                    "applied_by": "failed", "pair": pair,
                    "reason": reason,
                })
                continue
        current = result
        records.append({"applied_by": "deterministic", "pair": pair})
    return current, records


def _diagnose_scoped_failure(base: str, scope_name: str,
                              new_body: str = "") -> str:
    if new_body and _has_ellipsis_marker(new_body):
        extracted, _kind = _extract_scope_from_body(new_body)
        if not extracted:
            return ("ellipsis block: no scope declaration at body's "
                    "start; cannot determine placement")
        if _find_scope_in_base(base, extracted) is None:
            return (f"ellipsis block: scope {extracted!r} not found "
                    f"in base")
        return (f"ellipsis block: scope {extracted!r} found but "
                f"prefix/suffix contained no new lines to place")
    if not scope_name:
        return "scope name is empty"
    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        matches = pattern.findall(base)
        count = len(matches)
        if count == 0:
            continue
        if count > 1:
            return (f"scope {scope_name!r} appears {count} times in base "
                    f"(ambiguous)")
        return (f"scope {scope_name!r} matched but replacement "
                f"produced no change")
    return f"scope {scope_name!r} not found in base"


def _diagnose_replace_failure(base: str, old_body: str) -> str:
    if not old_body or not old_body.strip():
        return "old_body is empty"
    count = base.count(old_body)
    if count == 0:
        def _norm(s):
            return "\n".join(ln.rstrip() for ln in s.splitlines())
        base_norm = _norm(base)
        old_norm = _norm(old_body)
        count_norm = base_norm.count(old_norm)
        if count_norm == 1:
            return ("old_body matches only after whitespace normalization "
                    "(line-ending or trailing-whitespace mismatch)")
        if count_norm > 1:
            return (f"old_body (whitespace-normalized) appears "
                    f"{count_norm} times (ambiguous)")
        first_line = old_body.strip().split("\n")[0]
        if first_line and first_line not in base:
            return (f"old_body's first line "
                    f"({first_line[:60]!r}) not found in base")
        first_count = base.count(first_line)
        lines = old_body.strip().split("\n")
        second_hint = ""
        if len(lines) >= 2:
            second = lines[1].strip()
            if second and second in base:
                second_hint = (
                    f"; second line also found "
                    f"({base.count(second)} occurrence(s)) but block "
                    f"still doesn't match — likely state drift between "
                    f"conversation and base")
            elif second:
                second_hint = (
                    f"; second line ({second[:50]!r}) not found in base "
                    f"— likely state drift between conversation and base")
        return (f"old_body's first line found ({first_count} "
                f"occurrence(s)){second_hint}")
    if count > 1:
        return f"old_body appears {count} times (ambiguous)"
    return "old_body matched but replacement produced no change"


def _diagnose_patch_failure(patch: dict, content: str,
                             base: str) -> str:
    body = patch.get("body", "") or ""
    stripped = body.strip()
    if not stripped:
        return "empty body"

    if _has_ellipsis_marker(body):
        name, _kind = _extract_scope_from_body(body)
        if not name:
            return ("ellipsis block: no scope declaration at body's "
                    "start; cannot determine placement")
        if _find_scope_in_base(content, name) is None:
            return f"ellipsis block: scope {name!r} not found in base"
        return (f"ellipsis block: scope {name!r} found but "
                f"prefix/suffix contained no new lines to place")

    ctx = patch.get("context_before", "") or ""

    if _looks_like_diff_body(stripped):
        return ("looks like a diff but could not be applied to base "
                "(context mismatch or already applied elsewhere)")
    if _EXPORT_CONST_OBJECT_RE.match(stripped):
        return ("looks like an `export const X = { ... }` splice but "
                "no matching base declaration was found for the "
                "variable")
    if _is_deletion_instruction(stripped, ctx):
        return ("looks like a deletion but the target block is not "
                "present verbatim in base (deletion may already be "
                "applied, or the block has shifted)")

    count_full = content.count(stripped)
    if count_full > 1:
        return (f"body appears {count_full} times in the current file "
                f"(ambiguous — no unique anchor)")
    if count_full == 1:
        return ("body already appears verbatim in the current file "
                "(already-applied but not detected as a no-op; the "
                "surrounding context may differ)")

    body_lines = stripped.splitlines()

    m_scope = re.search(
        r"<summary>\s*([A-Za-z][\w \-]+?)\s*</summary>",
        stripped, re.IGNORECASE)
    if m_scope:
        name = m_scope.group(1).strip()
        return (f"body looks like a `<details>` scope-replace for "
                f"{name!r}, but no matching scope was found in base")
    m_def = re.match(
        r"^(?:export\s+)?(?:async\s+)?(?:function|def|class)\s+(\w+)",
        stripped)
    if m_def:
        return (f"body looks like a scope-replace for "
                f"{m_def.group(1)!r}, but no matching scope was "
                f"found in base")

    if 2 <= len(body_lines) <= 30:
        last = body_lines[-1].strip()
        first = body_lines[0].strip()
        last_in_base = bool(last) and (last in content)
        first_in_base = bool(first) and (first in content)
        if not last_in_base and not first_in_base:
            return (f"body is {len(body_lines)} lines; neither its "
                    f"first line nor its last line appears in base, "
                    f"so context-insert has no anchor")
        if last_in_base or first_in_base:
            return (f"body is {len(body_lines)} lines; a "
                    f"first-or-last-line anchor exists in base but no "
                    f"insert heuristic fired (near-miss: blank lines, "
                    f"indentation, or intermediate context may differ)")

    anchors = re.findall(r'`([^`\n]+)`', ctx)
    if anchors:
        anchor_reports = []
        for a in anchors[-3:]:
            if a.strip() and a in content:
                anchor_reports.append(f"`{a[:40]}` present")
            elif a.strip():
                anchor_reports.append(f"`{a[:40]}` absent")
        if anchor_reports:
            return ("no heuristic matched; prose anchors: " +
                    ", ".join(anchor_reports))

    return (f"no heuristic matched (body is {len(body_lines)} lines, "
            f"first line: {body_lines[0][:60]!r})")


def _try_replace_pair(base: str, old: str, new: str) -> Optional[str]:
    if not old.strip():
        return None
    if old in base and base.count(old) == 1:
        result = base.replace(old, new, 1)
        if result != base:
            return result
    def _norm(s):
        return "\n".join(ln.rstrip() for ln in s.splitlines())
    base_norm = _norm(base)
    old_norm = _norm(old)
    if old_norm in base_norm and base_norm.count(old_norm) == 1:
        pos = base_norm.find(old_norm)
        result = base_norm[:pos] + new + base_norm[pos + len(old_norm):]
        if result != base_norm:
            return result
    return None


_SCOPE_PATTERNS = [
    lambda name: re.compile(
        r"<details\b[^>]*>\s*<summary>\s*" + re.escape(name) +
        r"\s*</summary>.*?</details>",
        re.DOTALL | re.IGNORECASE,
    ),
    lambda name: re.compile(
        r"(?:^|\n)(?:def|class)\s+" + re.escape(name) +
        r"\b.*?(?=\n(?:def|class|@)|\Z)",
        re.DOTALL,
    ),
    lambda name: re.compile(
        r"(?:^|\n)(?:export\s+)?(?:async\s+)?"
        r"(?:function|class)\s+" + re.escape(name) + r"\b.*?"
        r"(?:\n\}|\Z)",
        re.DOTALL,
    ),
    lambda name: re.compile(
        r"(?:^|\n)(?:export\s+)?(?:const|let|var)\s+" + re.escape(name) +
        r"\s*=\s*\{.*?\n\};?",
        re.DOTALL,
    ),
]


def _apply_scoped(base: str, scope_name: str,
                  new_body: str) -> Optional[str]:
    if not scope_name or not new_body.strip():
        return None
    # Ellipsis blocks use the partial-splice path.
    if _has_ellipsis_marker(new_body):
        return _try_ellipsis_splice(base, new_body)
    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        m = pattern.search(base)
        if not m:
            continue
        m_start = m.start()
        m_end = m.end()
        if m_start < len(base) and base[m_start] == "\n":
            indent_pos = m_start + 1
            base_indent = ""
        else:
            indent_pos = base.rfind("\n", 0, m_start) + 1
            base_indent = base[indent_pos:m_start]
        while m_end > m_start and base[m_end - 1] == "\n":
            m_end -= 1
        reindented = _reindent_body(base_indent, new_body)
        return base[:indent_pos] + reindented + base[m_end:]
    return None


def _reindent_body(base_indent: str, body: str) -> str:
    if not body:
        return body
    lines = body.split("\n")
    first = lines[0]
    orig_indent_len = len(first) - len(first.lstrip(" \t"))
    adjusted = [base_indent + first.lstrip(" \t")]
    for ln in lines[1:]:
        if not ln.strip():
            adjusted.append("")
            continue
        ln_indent = len(ln) - len(ln.lstrip(" \t"))
        relative = max(0, ln_indent - orig_indent_len)
        adjusted.append(base_indent + " " * relative + ln.lstrip(" \t"))
    return "\n".join(adjusted)


# ============================================================================
# Context-insert
# ============================================================================

_CONTEXT_INSERT_MAX_LINES = 30
_CONTEXT_INSERT_MIN_ANCHOR_CHARS = 8


def _try_context_insert(base: str, body: str) -> Optional[str]:
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

    def _unique(line: str) -> bool:
        stripped = line.strip()
        return (len(stripped) >= _CONTEXT_INSERT_MIN_ANCHOR_CHARS
                and base.count(stripped) == 1)

    def _absent(line: str) -> bool:
        stripped = line.strip()
        return not stripped or stripped not in base

    def _filter_new(lines: list) -> list:
        return [ln for ln in lines if _absent(ln)]

    def _reindent_to(new_lines: list, anchor_indent: str) -> list:
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
    if _unique(last_line):
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
    if _unique(first_line):
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
                first_line[:len(first_line) - len(first_line.lstrip(" \t"))]
            )
            new_lines = _reindent_to(candidates, anchor_indent)
            insertion = "\n".join(new_lines) + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    return None


# ============================================================================
# Object-entry-insert
# ============================================================================

_OBJECT_ENTRY_RE = re.compile(
    r'^\s*["\']?[A-Za-z_$][\w$]*["\']?\s*:\s*.+,?\s*$'
)
_PROSE_IDENTIFIER_RE = re.compile(r'`([A-Za-z_$][\w$]*)\s*\(\s*\)`')
_PROSE_IDENTIFIER_ALT_RE = re.compile(r'\b([A-Za-z_$][\w$]*)\s*\(\s*\)')


def _try_object_entry_insert(base: str, body: str,
                             context_before: str) -> Optional[str]:
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
        re.MULTILINE,
    )
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


# ============================================================================
# Insert-after-anchor
# ============================================================================

_PROSE_BACKTICK_IDENT_RE = re.compile(r'`([A-Za-z_$][\w$]*)`')
_PROSE_CALL_ARG_RE = re.compile(
    r'`([A-Za-z_$][\w$]*)\s*\(\s*["\']([^"\']+)["\']'
)
_BACKTICK_CONTENT_RE = re.compile(r'`([^`\n]+)`')
_IDENT_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*$')
_CALL_ONLY_RE = re.compile(r'^[A-Za-z_$][\w$]*\s*\(')


def _try_insert_after_anchor(base: str, body: str,
                             context_before: str) -> Optional[str]:
    body_stripped = body.strip()
    if not body_stripped:
        return None
    body_lines = body_stripped.splitlines()
    if len(body_lines) < 2 or len(body_lines) > 40:
        return None

    # Anchor type 1: call+arg.
    call_matches = _PROSE_CALL_ARG_RE.findall(context_before)
    for func_name, arg_str in reversed(call_matches):
        pattern_str = func_name + '("' + arg_str
        idx = base.find(pattern_str)
        if idx == -1:
            continue
        paren_open = base.find("(", idx + len(func_name))
        if paren_open == -1:
            continue
        paren_close = _find_matching_paren(base, paren_open)
        if paren_close == -1:
            continue
        line_end = base.find("\n", paren_close)
        if line_end == -1:
            line_end = len(base)
        else:
            line_end += 1
        insertion = body_stripped.rstrip() + "\n"
        result = base[:line_end] + insertion + base[line_end:]
        if result != base:
            return result

    # Anchor type 2: identifier in if/const form.
    ident_matches = _PROSE_BACKTICK_IDENT_RE.findall(context_before)
    for ident in reversed(ident_matches):
        if_re = re.compile(
            r'(?:^|\n)\s*if\s*\(\s*' + re.escape(ident) + r'\s*\)\s*\{',
            re.MULTILINE,
        )
        if_match = if_re.search(base)
        if if_match:
            block_open = base.rfind("{", if_match.start(), if_match.end())
            if block_open == -1:
                block_open = base.find("{", if_match.start())
            if block_open == -1:
                continue
            block_close = _find_matching_brace(
                base, block_open, is_python=False)
            if block_close == -1:
                continue
            line_end = base.find("\n", block_close)
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            insertion = body_stripped.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result
            continue

        const_re = re.compile(
            r'(?:^|\n)\s*(?:const|let|var)\s+' + re.escape(ident) +
            r'\s*=[^\n]*;',
            re.MULTILINE,
        )
        const_match = const_re.search(base)
        if const_match:
            line_end = base.find("\n", const_match.end())
            if line_end == -1:
                line_end = len(base)
            else:
                line_end += 1
            insertion = body_stripped.rstrip() + "\n"
            result = base[:line_end] + insertion + base[line_end:]
            if result != base:
                return result

    # Anchor type 3: statement content.
    statement_matches = _BACKTICK_CONTENT_RE.findall(context_before)
    for raw in reversed(statement_matches):
        stripped = raw.strip()
        if len(stripped) < 6:
            continue
        if _IDENT_ONLY_RE.match(stripped):
            continue
        if _CALL_ONLY_RE.match(stripped):
            continue
        if base.count(stripped) != 1:
            continue

        idx = base.find(stripped)
        line_start = base.rfind("\n", 0, idx) + 1
        line_lead = ""
        p_scan = line_start
        while p_scan < len(base) and base[p_scan] in " \t":
            line_lead += base[p_scan]
            p_scan += 1
        anchor_indent = line_lead
        line_end = base.find("\n", idx)
        if line_end == -1:
            line_end = len(base)
        else:
            line_end += 1

        reindented = _reindent_body(anchor_indent, body_stripped)
        insertion = reindented.rstrip() + "\n"
        result = base[:line_end] + insertion + base[line_end:]
        if result != base:
            return result

    return None


def _find_matching_paren(text: str, open_idx: int) -> int:
    if open_idx >= len(text) or text[open_idx] != "(":
        return -1
    depth = 0
    i = open_idx
    in_str: Optional[str] = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            in_str = c
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ============================================================================
# Sanity gate
# ============================================================================

def _sanity_gate(base: str, result: str) -> tuple:
    from .sanity import is_suspicious
    return is_suspicious(base, result)


# ============================================================================
# Individual patch application
# ============================================================================

def _looks_like_python_fragment(text: str) -> bool:
    for line in text.splitlines()[:5]:
        s = line.strip()
        if not s:
            continue
        if s.startswith(("import ", "from ", "def ", "class ",
                         "async def ", "@")):
            return True
        if s.startswith(("export ", "const ", "let ", "var ",
                         "function ", "async function ")):
            return False
    return True


def _delete_exact_block(base: str, body: str) -> Optional[str]:
    is_diff = _is_pure_deletion_diff_strict(body) or _looks_like_diff_body(body)
    stripped = _strip_diff_prefixes(body) if is_diff else body
    body_lines = [ln.rstrip() for ln in stripped.splitlines()]
    while body_lines and not body_lines[0]:
        body_lines.pop(0)
    while body_lines and not body_lines[-1]:
        body_lines.pop()
    if not body_lines:
        return None
    base_lines_keepends = base.splitlines(keepends=True)
    base_lines = [ln.rstrip("\n").rstrip() for ln in base_lines_keepends]
    n = len(body_lines)
    matches = []
    for i in range(len(base_lines) - n + 1):
        if base_lines[i:i+n] == body_lines:
            matches.append(i)
    if len(matches) != 1:
        return None
    start = matches[0]
    end = start + n
    new_lines = base_lines_keepends[:start] + base_lines_keepends[end:]
    new_text = "".join(new_lines)
    if new_text == base:
        return None
    return new_text


def _deletion_already_applied(body: str, base: str) -> bool:
    stripped = _strip_diff_prefixes(body)
    body_lines = [ln.rstrip() for ln in stripped.splitlines() if ln.strip()]
    if not body_lines:
        return False
    base_nonempty = [ln.rstrip() for ln in base.splitlines() if ln.strip()]
    n = len(body_lines)
    for i in range(len(base_nonempty) - n + 1):
        if base_nonempty[i:i+n] == body_lines:
            return False
    return True


def _try_raw_diff_patch(base: str, body: str) -> Optional[str]:
    diff_lines = body.splitlines()
    if not any(ln.startswith(("+", "-")) for ln in diff_lines):
        return None
    if any(ln.startswith(("+++", "---", "@@")) for ln in diff_lines):
        return None
    first_anchor = None
    for ln in diff_lines:
        if not ln:
            continue
        prefix = ln[:1]
        if prefix in (" ", "-"):
            first_anchor = ln[1:].rstrip()
            break
    if first_anchor is None:
        return None
    base_lines = base.splitlines(keepends=True)
    anchors = [
        idx for idx, ln in enumerate(base_lines)
        if ln.rstrip() == first_anchor
    ]
    if not anchors:
        return None
    for start in anchors:
        result: list = base_lines[:start]
        pos = start
        i = 0
        ok = True
        while i < len(diff_lines):
            line = diff_lines[i]
            i += 1
            if not line:
                line = " "
            prefix = line[:1]
            rest = line[1:]
            if prefix == " ":
                if pos >= len(base_lines) or (
                        base_lines[pos].rstrip() != rest.rstrip()):
                    ok = False
                    break
                result.append(base_lines[pos])
                pos += 1
            elif prefix == "-":
                if pos >= len(base_lines) or (
                        base_lines[pos].rstrip() != rest.rstrip()):
                    ok = False
                    break
                pos += 1
            elif prefix == "+":
                result.append(rest + "\n")
        if ok:
            result.extend(base_lines[pos:])
            new_text = "".join(result)
            if new_text == base:
                return None
            return new_text
    return None


def _diff_already_applied(body: str, base: str) -> bool:
    if not _looks_like_diff_body(body):
        return False
    adds: list = []
    dels: list = []
    for ln in body.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            adds.append(ln[1:].rstrip())
        elif ln.startswith("-") and not ln.startswith("---"):
            dels.append(ln[1:].rstrip())
    base_lines_set = {ln.rstrip() for ln in base.splitlines()}
    for a in adds:
        if a and a not in base_lines_set:
            return False
    dels_nonempty = [d for d in dels if d.strip()]
    if dels_nonempty:
        base_lines_nonempty = [
            ln.rstrip() for ln in base.splitlines() if ln.strip()
        ]
        n = len(dels_nonempty)
        for i in range(len(base_lines_nonempty) - n + 1):
            if base_lines_nonempty[i:i + n] == dels_nonempty:
                return False
    return True


def _patch_already_present(body: str, base: str) -> bool:
    def norm(s):
        return [ln.strip() for ln in s.splitlines() if ln.strip()]
    body_lines = norm(body)
    if not body_lines:
        return True
    base_lines = norm(base)
    n = len(body_lines)
    if n > len(base_lines):
        return False
    for i in range(len(base_lines) - n + 1):
        if base_lines[i:i+n] == body_lines:
            return True
    return False


def _try_deterministic_patch(base: str, patch: dict,
                            path: str = "") -> Optional[str]:
    raw_body = patch["body"]
    body_stripped = raw_body.strip()
    if not body_stripped:
        return None

    context_before = patch.get("context_before", "")

    if _is_deletion_instruction(raw_body, context_before):
        deleted = _delete_exact_block(base, raw_body)
        if deleted is not None:
            return deleted

    if _looks_like_diff_body(raw_body):
        diffed = _try_raw_diff_patch(base, raw_body)
        if diffed is not None:
            return diffed

    # Ellipsis blocks: dedicated splice path.  Do NOT fall through —
    # any other heuristic would insert the `...` line verbatim.
    if _has_ellipsis_marker(raw_body):
        return _try_ellipsis_splice(base, raw_body, context_before)

    m = _EXPORT_CONST_OBJECT_RE.match(body_stripped)
    if m:
        varname = m.group(2)
        result = _try_object_literal_splice(base, body_stripped, varname)
        if result is not None:
            return result
        return None

    inserted = _try_context_insert(base, raw_body)
    if inserted is not None:
        return inserted

    inserted = _try_object_entry_insert(base, raw_body, context_before)
    if inserted is not None:
        return inserted

    inserted = _try_insert_after_anchor(base, raw_body, context_before)
    if inserted is not None:
        return inserted

    # AST anchor: structural placement for blocks with no text anchor.
    # Requires the `[ast]` extra (tree-sitter + tree-sitter-javascript).
    # Import errors and any ambiguity are silently caught and fall
    # through to fail-closed.
    if path:
        try:
            from .ast_anchor import try_ast_anchor
        except ImportError:
            try_ast_anchor = None
        if try_ast_anchor is not None:
            ast_result = try_ast_anchor(base, raw_body, path,
                                         context_before)
            if ast_result is not None:
                line_idx, indent, evidence = ast_result
                print(f"  [ast] {path}: {evidence}", file=sys.stderr)
                reindented = _reindent_body(indent, raw_body.strip())
                lines = base.split("\n")
                new_lines = reindented.split("\n")
                result = "\n".join(
                    lines[:line_idx] + new_lines + lines[line_idx:])
                if result != base:
                    return result

    return None


def _try_object_literal_splice(base: str, body_stripped: str,
                                varname: str) -> Optional[str]:
    assign_re = re.compile(
        r'^(export\s+(?:const|let)\s+' + re.escape(varname) +
        r'\s*=\s*)\{',
        re.MULTILINE)
    base_m = assign_re.search(base)
    if not base_m:
        return None
    body_m = re.match(
        r'^(export\s+(?:const|let)\s+' + re.escape(varname) +
        r'\s*=\s*)\{',
        body_stripped)
    patch_open = body_stripped.index("{", body_m.start())
    patch_is_py = _looks_like_python_fragment(body_stripped)
    patch_close = _find_matching_brace(
        body_stripped, patch_open, is_python=patch_is_py)
    if patch_close < 0:
        return None
    base_open = base.index("{", base_m.start())
    base_is_py = _looks_like_python_fragment(base[base_m.start():])
    base_close = _find_matching_brace(
        base, base_open, is_python=base_is_py)
    if base_close < 0:
        return None
    base_head = base[:base_m.start()]
    base_tail = base[base_close + 1:]
    body_trimmed = body_stripped.rstrip()
    if body_trimmed.endswith(";"):
        body_trimmed = body_trimmed[:-1].rstrip()
        body_had_semi = True
    else:
        body_had_semi = False
    tail_lstripped = base_tail.lstrip()
    if tail_lstripped.startswith(";"):
        base_tail = tail_lstripped[1:]
        base_had_semi = True
    else:
        base_had_semi = False
    sep = ";" if (body_had_semi or base_had_semi) else ""
    result = base_head + body_trimmed + sep + base_tail
    if result == base:
        return None
    return result


def _find_matching_brace(text: str, open_idx: int,
                          is_python: bool = False) -> int:
    if open_idx >= len(text) or text[open_idx] != "{":
        return -1
    depth = 0
    i = open_idx
    in_str: Optional[str] = None
    in_line_comment = False
    in_block_comment = False
    while i < len(text):
        c = text[i]
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'", chr(96)):
            in_str = c
            i += 1
            continue
        if is_python and c == "#":
            in_line_comment = True
            i += 1
            continue
        if not is_python and c == "/" and i + 1 < len(text):
            if text[i + 1] == "/":
                in_line_comment = True
                i += 2
                continue
            if text[i + 1] == "*":
                in_block_comment = True
                i += 2
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _diff_prompt(path: str, n_lines: int,
                 base: str, ctx: str, patch_body: str) -> str:
    return (
        "You are applying a diff instruction to a base file.  This is\n"
        "purely mechanical - do not improve, reformat, or reorganise\n"
        "anything outside the described change.\n"
        "\n"
        f"Base file (path: {path}, {n_lines} lines):\n"
        f"{FENCE}\n"
        f"{base}\n"
        f"{FENCE}\n"
        "\n"
        "Diff instruction (the change to apply):\n"
        f"{ctx}\n"
        "\n"
        f"{FENCE}\n"
        f"{patch_body}\n"
        f"{FENCE}\n"
        "\n"
        "Produce a unified diff (standard @@ -a,b +c,d @@ hunks) that\n"
        "transforms the base file into one that includes the described\n"
        f"change.  Output ONLY the diff inside a {FENCE}diff{FENCE} \n"
        "fenced block.  Do NOT reproduce the base file.\n"
        "\n"
        "If the change is ambiguous, output:\n"
        f"{FENCE}diff\n"
        "FAILED: <one-sentence reason>\n"
        f"{FENCE}\n"
    )


def _try_llm_patch(base: str, patch: dict,
                    llm: LLMClient, path: str) -> Optional[str]:
    n_lines = base.count("\n") + 1
    if n_lines > 1500:
        print(f"  [LLM:diff] skip: base is {n_lines} lines (max 1500)",
              file=sys.stderr)
        return None
    prompt = _diff_prompt(
        path=path,
        n_lines=n_lines,
        base=base,
        ctx=patch.get("context_before", "")[:300],
        patch_body=patch["body"][:2000],
    )
    resp = llm.call(prompt, max_tokens=MAX_TOKENS_DIFF, tag="diff")
    if not resp:
        return None
    resp = resp.strip()
    if FENCE in resp:
        m = re.search(
            re.escape(FENCE) + r"(?:diff)?\s*\n(.*?)" + re.escape(FENCE),
            resp, re.DOTALL)
        if m:
            resp = m.group(1).strip()
    if resp.startswith("FAILED"):
        return None
    if not resp:
        return None
    return _manual_apply_unified(base, resp)


def _manual_apply_unified(base: str, diff_text: str) -> Optional[str]:
    base_lines = base.splitlines(keepends=False)
    out: list = []
    pos = 0
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@', line)
        if m:
            old_start = int(m.group(1)) - 1
            while pos < old_start and pos < len(base_lines):
                out.append(base_lines[pos])
                pos += 1
            i += 1
            continue
        if line.startswith(" "):
            want = line[1:]
            if pos >= len(base_lines) or base_lines[pos] != want:
                return None
            out.append(want)
            pos += 1
            i += 1
            continue
        if line.startswith("-"):
            pos += 1
            i += 1
            continue
        if line.startswith("+"):
            out.append(line[1:])
            i += 1
            continue
        i += 1
    while pos < len(base_lines):
        out.append(base_lines[pos])
        pos += 1
    return "\n".join(out) + "\n"
