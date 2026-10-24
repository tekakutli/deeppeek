"""
recover.apply_patches — Pass 6 (apply pairs, then patches, then
sanity-check the result).

Order matters:

  1. Pairs run first.  They operate on the base full print; a
     replace pair's `find(old)` needs the base in its original
     shape.  Running after patch application would find the old
     block already mutated.

  2. Individual patches run second, over the possibly-paired
     content.  Diff patches and deletions still apply; a deletion
     that overlaps a pair deletion is correctly `already-present`.

  3. Sanity gate runs on the final content, before the file is
     handed to `write`.  If the result is implausible (much smaller
     than base, empty, or shares no long lines with base), the base
     is restored and `sanity_rejected` is set.

New in 2026-11-01: `_try_replace_pair`, `_apply_scoped`,
`_sanity_gate`.  The single biggest architectural shift is that
prose instructions become *structured edits* rather than free-text
hints interpreted (badly) by the deletion patcher.
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


def pass6_apply_patches(records: dict[str, FileRecord],
                        blocks_by_idx: dict[int, Block],
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

        # ---- pairs first ----
        pair_records: list[dict] = []
        if rec.pairs:
            content, pair_records = _apply_pairs(content, rec.pairs)

        # ---- patches ----
        all_deterministic = True
        all_ok = True
        for patch in rec.patches:
            result = _try_deterministic_patch(content, patch)
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
            rec.warnings.append(
                f"patch idx={patch['idx']} could not be applied")
            all_ok = False

        # ---- record results ----
        rec.verification["patches_applied"] = sum(
            1 for p in rec.patches if p.get("applied_by") != "failed")
        rec.verification["pairs_applied"] = sum(
            1 for r in pair_records if r["applied_by"] == "deterministic")

        if pair_records:
            n_failed = sum(
                1 for r in pair_records if r["applied_by"] == "failed")
            if n_failed:
                rec.verification["pairs_incomplete"] = True
                rec.warnings.append(
                    f"{n_failed} replace pair(s) failed to apply")

        # ---- sanity gate ----
        suspicious, reason = _sanity_gate(base, content)
        if suspicious:
            rec.sanity_rejected = True
            content = base
            rec.trust = "LOW"
            rec.warnings.append(f"sanity gate: {reason} — reverted to base")
            rec.verification["sanity_rejected"] = True
            print(f"  [sanity] {path}: {reason} — reverting to base",
                  file=sys.stderr)
        else:
            rec.trust = trust_cap
            if not all_ok:
                rec.verification["patches_incomplete"] = True
                rec.warnings.append(
                    "one or more orphan patches failed; base content "
                    "written without them")
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
        print(f"  {' + '.join(parts)} applied{suffix}", file=sys.stderr)
        if total_noop:
            print(f"  {total_noop} patch(es) already-present "
                  f"(no-op, not failures)", file=sys.stderr)


# ============================================================================
# Pairs
# ============================================================================

def _apply_pairs(base: str,
                 pairs: list[dict]) -> tuple[str, list[dict]]:
    """Apply every pair sequentially.  Returns (new_base, records)."""
    records: list[dict] = []
    current = base
    for pair in pairs:
        if pair["old_idx"] == -1:
            # Scoped: only new half; find scope by name
            result = _apply_scoped(
                current, pair["scope_name"], pair["new_body"])
        else:
            result = _try_replace_pair(
                current, pair["old_body"], pair["new_body"])
        if result is None:
            records.append({"applied_by": "failed", "pair": pair})
            continue
        current = result
        records.append({"applied_by": "deterministic", "pair": pair})
    return current, records


def _try_replace_pair(base: str, old: str, new: str) -> Optional[str]:
    """
    Find `old` in `base`, replace with `new`.

    Tries, in order:
      1. exact substring (must appear exactly once)
      2. whitespace-normalized substring
      3. unique "longest common block" match

    Returns None on any ambiguity or absence.
    """
    if not old.strip():
        return None

    # 1. Exact, unique.
    if old in base:
        if base.count(old) == 1:
            result = base.replace(old, new, 1)
            if result != base:
                return result
        return None

    # 2. Whitespace-normalized.
    def _norm(s: str) -> str:
        return "\n".join(ln.rstrip() for ln in s.splitlines())

    base_norm = _norm(base)
    old_norm = _norm(old)
    if old_norm in base_norm and base_norm.count(old_norm) == 1:
        pos = base_norm.find(old_norm)
        result = base_norm[:pos] + new + base_norm[pos + len(old_norm):]
        if result != base_norm:
            return result
    return None


# Scope recognisers for `_apply_scoped`.  Each entry is
# (compiled pattern, how-to-get-indent).  The pattern must produce
# a match whose `.start()` is the first character of the scope
# (the `<` in `<details>`, the `d` in `def`, the `f` in `function`).
_SCOPE_PATTERNS = [
    # HTML <details> with named <summary>
    lambda name: re.compile(
        r"<details\b[^>]*>\s*<summary>\s*" + re.escape(name) +
        r"\s*</summary>.*?</details>",
        re.DOTALL | re.IGNORECASE,
    ),
    # Python def / class — top level, ends at next top-level
    # def/class/decorator or EOF.
    lambda name: re.compile(
        r"(?:^|\n)(?:def|class)\s+" + re.escape(name) +
        r"\b.*?(?=\n(?:def|class|@)|\Z)",
        re.DOTALL,
    ),
    # JS function / class
    lambda name: re.compile(
        r"(?:^|\n)(?:export\s+)?(?:async\s+)?"
        r"(?:function|class)\s+" + re.escape(name) + r"\b.*?"
        r"(?:\n\}|\Z)",
        re.DOTALL,
    ),
]


def _apply_scoped(base: str, scope_name: str,
                  new_body: str) -> Optional[str]:
    """Find the named scope in `base` and replace it with `new_body`.

    Recognised scope shapes: HTML `<details>` with matching
    `<summary>`, Python top-level `def`/`class`, JS `function`/
    `class`.  Returns None if no scope matches unambiguously.
    """
    if not scope_name or not new_body.strip():
        return None

    for make_pattern in _SCOPE_PATTERNS:
        try:
            pattern = make_pattern(scope_name)
        except re.error:
            continue
        m = pattern.search(base)
        if not m:
            continue
        # If the match included a leading newline, adjust start.
        match_text = m.group(0)
        prefix_newline = ""
        suffix_newline = ""
        if match_text.startswith("\n"):
            prefix_newline = "\n"
            start = m.start() + 1
        else:
            start = m.start()
        # If we matched up to (but not including) a trailing
        # newline, the new_body should preserve the same boundary.
        end = m.end()
        if match_text.endswith("\n"):
            suffix_newline = "\n"
            end = end - 1
        # Normalize new_body's indentation to match base's.
        line_start = base.rfind("\n", 0, start) + 1
        base_indent = base[line_start:start]
        reindented = _reindent_body(base_indent, new_body)
        if prefix_newline:
            return base[:line_start] + reindented + \
                   suffix_newline + base[end:]
        return base[:start] + reindented + base[end:]

    return None


def _reindent_body(base_indent: str, body: str) -> str:
    """Re-indent `body` so its first line starts at `base_indent`.
    Interior relative indentation is preserved."""
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
# Sanity gate
# ============================================================================

def _sanity_gate(base: str, result: str) -> tuple[bool, str]:
    from .sanity import is_suspicious
    return is_suspicious(base, result)


# ============================================================================
# Individual patch application (unchanged from before)
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
    """Remove the code block that `body` describes from `base`."""
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
    body_lines = [ln.rstrip() for ln in stripped.splitlines()
                  if ln.strip()]
    if not body_lines:
        return False
    base_nonempty = [
        ln.rstrip() for ln in base.splitlines() if ln.strip()
    ]
    n = len(body_lines)
    for i in range(len(base_nonempty) - n + 1):
        if base_nonempty[i:i+n] == body_lines:
            return False
    return True


def _try_raw_diff_patch(base: str, body: str) -> Optional[str]:
    """Apply a raw `+`/`-` diff block (no `@@` hunk headers) to `base`."""
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
        result: list[str] = base_lines[:start]
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

    adds: list[str] = []
    dels: list[str] = []
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
    def norm(s: str) -> list[str]:
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


def _try_deterministic_patch(base: str, patch: dict) -> Optional[str]:
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

    m = re.match(r'^(export\s+(?:const|let)\s+(\w+)\s*=\s*)\{',
                  body_stripped)
    if not m:
        return None
    varname = m.group(2)

    assign_re = re.compile(
        r'^(export\s+(?:const|let)\s+' + re.escape(varname) +
        r'\s*=\s*)\{',
        re.MULTILINE)
    base_m = assign_re.search(base)
    if not base_m:
        return None

    patch_open = body_stripped.index("{", m.start())
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
    out: list[str] = []
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
