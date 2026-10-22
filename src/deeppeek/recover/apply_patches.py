"""
recover.apply_patches — Pass 6 (apply partial patches onto the base
full print).

DIFFICULT.  Owns:

    FIX [5]   Aggregate patch-failure summary.

    FIX [11]  A failed orphan patch demotes trust to trust_cap, not
              LOW.

    NEW (2026-10-20)  No-op patch detection (`already-present`).

    NEW (2026-10-21)  Three new deterministic patch shapes.

    NEW (2026-10-21b) Bug 1 fix — the deterministic patcher does
              not report byte-identical output as an applied
              change.

    NEW (2026-10-29)  Three correctness fixes.

    NEW (2026-10-30)  Raw-diff patcher.

    NEW (2026-10-30b) Deletion-instruction routing and idempotency.

    NEW (2026-10-31)  Three fixes for the panel-fade regression:

              1. `_delete_exact_block` only strips diff prefixes when
                 the body is diff-shaped.  For a raw code body, it
                 preserves the exact source text.  Prior versions
                 stripped one leading space from every indented line,
                 silently corrupting the match and forcing the patch
                 through the object-literal fallback.

              2. `_try_deterministic_patch` returns None (no-op) when
                 the object-literal splice produces text identical
                 to `base`.  The caller then checks
                 `_patch_already_present` and marks the patch
                 `already-present`, not `deterministic`.

              3. `_try_raw_diff_patch` treats empty lines as context
                 blanks.  A markdown processor that strips trailing
                 whitespace turns a ` ` context blank into ``, which
                 the walker used to skip and then mismatched the
                 following `-` line against the wrong base line.
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
        content = base_block.body

        trust_cap = "MEDIUM" if rec.dedup_reason == "last_full" else "HIGH"

        if not rec.patches:
            rec.content = content
            rec.verification["patches_applied"] = 0
            rec.trust = trust_cap
            continue

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

        rec.content = content
        rec.verification["patches_applied"] = sum(
            1 for p in rec.patches if p.get("applied_by") != "failed")

        rec.trust = trust_cap
        if not all_ok:
            rec.verification["patches_incomplete"] = True
            rec.warnings.append(
                "one or more orphan patches failed; base content "
                "written without them")
        if all_ok and not all_deterministic:
            rec.verification["via_llm"] = True

    total_patches = sum(len(r.patches) for r in records.values())
    total_applied = sum(
        sum(1 for p in r.patches
            if p.get("applied_by") in ("deterministic", "llm"))
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
    if total_patches:
        suffix = ""
        if total_failed:
            suffix = (f" ({total_failed} failed across "
                      f"{files_with_failures} file(s) — see warnings.txt)")
        print(f"  {total_applied}/{total_patches} patches applied{suffix}",
              file=sys.stderr)
        if total_noop:
            print(f"  {total_noop} patch(es) already-present "
                  f"(no-op, not failures)", file=sys.stderr)


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
    """Remove the code block that `body` describes from `base`.

    `body` may be:

      · a raw code block (no diff prefixes) — used verbatim, so
        indentation inside the block is preserved exactly
      · a pure-deletion diff (all `-` / unprefixed lines) — diff
        prefixes are stripped first

    FIX (2026-10-31): the strip is conditional.  A raw code body has
    no diff prefixes to strip, and stripping one leading space from
    every indented line corrupts the exact match (base has 2-space
    indent, stripped body has 1-space indent, no match).
    """
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
    """True if the code block described by `body` is ABSENT from
    `base`.
    """
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
    """Apply a raw `+`/`-` diff block (no `@@` hunk headers) to `base`.

    FIX (2026-10-31): empty lines in the diff body are treated as
    context blanks.  Some markdown processors strip the trailing
    space from a ` ` (context blank) line, leaving an empty string.
    The walker used to `continue` on empty lines — the base cursor
    did not advance, and the following `-` line mismatched.  A
    well-formed diff body has no truly empty line in the middle: a
    blank in the diff always carries one of `+`, `-`, ` `.  An empty
    line is therefore a whitespace-stripped ` ` context, and treating
    it as such recovers the intended walk.
    """
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
                # FIX: whitespace-stripped context blank.
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
    """True if a diff's net effect is already present in `base`."""
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
    """True if `body` appears as a contiguous run of non-blank
    lines in `base` (whitespace-normalised)."""
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
    """Try each patch shape in order; return the modified base or None.

    FIX (2026-10-31): the object-literal path returns `None` when
    its result equals `base` (byte-identical).  Without this, a
    patch whose object is identical to the base's existing object
    is marked `deterministic` with content unchanged, and any
    subsequent diff patch on the same file runs against a content
    that never had the object removed — passing the diff to the
    walker with the deletion lines still in place, which then
    correctly fails and reports `failed`.
    """
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

    # FIX: no-op check.  If the splice produced byte-identical
    # content, the object literal in the patch is the same as the
    # one in base — nothing to change.
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
