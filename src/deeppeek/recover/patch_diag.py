"""
patch_diag — explain why a patch failed.

Called only on the failure path.  The explanations this module
produces flow into:

  · warnings.txt's [PATCH_FAILED] / [PAIR_FAILED] lines
  · manifest.decisions[path].patch_failure_reasons
  · manifest.decisions[path].pair_failures
  · the final-summary block that names the first line of every
    failed patch

Every diagnostic function is wrapped in a try/except in its caller
(`_diagnose_patch_failure` catches `_diagnose_patch_failure_inner`'s
raises; the pair-failure diagnostics are called from
`patch_pairs._apply_pairs`, which has its own per-pair exception
boundary).  A bug in a diagnostic must never turn a fail-closed
patch into a silent skip.
"""

import re

from .patch_text import _has_ellipsis_marker
from .patch_scope import _find_scope_in_base, _SCOPE_PATTERNS
from .patch_heuristics import _OBJECT_LITERAL_ASSIGNMENT_RE
from .prose import _extract_scope_from_body
from .core import _is_deletion_instruction, _looks_like_diff_body


def _find_closest_window(base, old_body, threshold=0.4):
    """Return (line_no, similarity, first_line_of_window) for
    the closest match to old_body in base, or None."""
    def _toks(s):
        return set(re.findall(r"[A-Za-z_$][\w$]*|[0-9]+|\S", s))

    base_lines = base.split("\n")
    old_lines = old_body.split("\n")
    n = len(old_lines)
    if n == 0 or len(base_lines) < n:
        return None

    old_toks = _toks(old_body)
    if not old_toks:
        return None

    best = None
    for i in range(len(base_lines) - n + 1):
        window = "\n".join(base_lines[i:i + n])
        window_toks = _toks(window)
        if not window_toks:
            continue
        sim = len(old_toks & window_toks) / len(old_toks | window_toks)
        if sim >= threshold and (best is None or sim > best[1]):
            snippet = window.strip().split("\n")[0][:60]
            best = (i + 1, sim, snippet)

    return best


def _diagnose_scoped_failure(base, scope_name, new_body=""):
    if new_body and _has_ellipsis_marker(new_body):
        extracted, _kind = _extract_scope_from_body(new_body)
        if not extracted:
            return ("ellipsis block: no scope declaration at body's "
                    "start; cannot determine placement")
        if _find_scope_in_base(base, extracted) is None:
            return f"ellipsis block: scope {extracted!r} not found in base"
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


def _diagnose_replace_failure(base, old_body, new_body=""):
    if not old_body or not old_body.strip():
        return "old_body is empty"

    old_present = bool(old_body in base)
    new_present = bool(new_body and new_body.strip() and new_body in base)

    if old_present and new_present:
        return ("both old and new bodies present in base — ambiguous "
                "(partial prior application, or old appears as a "
                "comment/example elsewhere); fail-closed")

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
        if _has_ellipsis_marker(old_body):
            return ("old_body contains an ellipsis marker; "
                    "the AI abbreviated the change and it "
                    "cannot be matched literally.  Use a "
                    "scoped-replace or a narrower pair.")
        first_line = old_body.strip().split("\n")[0]
        if first_line and first_line not in base:
            closest = _find_closest_window(base, old_body)
            if closest is not None:
                line_no, sim, snippet = closest
                return (f"old_body's first line "
                        f"({first_line[:60]!r}) not found in base; "
                        f"closest match at line {line_no} "
                        f"(token similarity {sim:.2f}): "
                        f"{snippet!r} - base may be a pre-patch state")
            return (f"old_body's first line "
                    f"({first_line[:60]!r}) not found in base; "
                    f"no similar region either - base is likely a "
                    f"state that predates this edit")
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

    try:
        base_lines = base.splitlines()
        old_lines = old_body.splitlines()
        n = len(old_lines)
        best = 0.0
        if n > 0 and len(base_lines) >= n:
            for i in range(len(base_lines) - n + 1):
                window = base_lines[i:i+n]
                scores = []
                for a, b in zip(old_lines, window):
                    a_s = a.strip()
                    b_s = b.strip()
                    if a_s == b_s:
                        scores.append(1.0)
                    elif not a_s or not b_s:
                        scores.append(0.0)
                    else:
                        la, lb = len(a_s), len(b_s)
                        if lb < la or lb > la * 2:
                            scores.append(0.0)
                        else:
                            scores.append(0.5)
                if scores:
                    avg = sum(scores) / len(scores)
                    if avg > best:
                        best = avg
        if best > 0.3:
            return (f"old_body did not match exactly; best fuzzy "
                    f"similarity was {best:.2f} (below 0.75 threshold)")
    except Exception:
        pass
    return "old_body matched but replacement produced no change"


def _diagnose_patch_failure(patch, content, base):
    """Explain why a patch could not be applied.

    Returns a human-readable string suitable for warnings.txt and
    the manifest's ``patch_failure_reasons``.  This function is on
    the cold-failure path: if it raises, the whole pass would die.
    The wrapper below catches any exception and falls back to a
    generic message so a single weird block can never take out
    the pass.

    Never raises.
    """
    try:
        return _diagnose_patch_failure_inner(patch, content, base)
    except Exception as e:
        return (f"diagnostic raised {type(e).__name__}: {e}; "
                f"patch could not be applied")


def _diagnose_patch_failure_inner(patch, content, base):
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

    if _is_deletion_instruction(stripped, ctx):
        return ("looks like a deletion but the target block is not "
                "present verbatim in base (deletion may already be "
                "applied, or the block has shifted)")
    if _looks_like_diff_body(stripped):
        return ("looks like a diff but could not be applied to base "
                "(context mismatch or already applied elsewhere)")
    if _OBJECT_LITERAL_ASSIGNMENT_RE.match(stripped):
        return ("looks like an object-literal assignment splice but "
                "no matching base declaration was found for the "
                "variable")

    count_full = content.count(stripped)
    if count_full > 1:
        return (f"body appears {count_full} times in the current file "
                f"(ambiguous - no unique anchor)")
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
                f"{m_def.group(1)!r}, but no matching scope was found "
                f"in base")

    if 2 <= len(body_lines) <= 30:
        last = body_lines[-1].strip()
        first = body_lines[0].strip()
        last_in_base = bool(last) and (last in content)
        first_in_base = bool(first) and (first in content)
        if not last_in_base and not first_in_base:
            return (f"body is {len(body_lines)} lines; neither its "
                    f"first line nor its last line appears in base, so "
                    f"context-insert has no anchor")
        if last_in_base or first_in_base:
            return (f"body is {len(body_lines)} lines; a first-or-last-"
                    f"line anchor exists in base but no insert heuristic "
                    f"fired (near-miss: blank lines, indentation, or "
                    f"intermediate context may differ)")

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
