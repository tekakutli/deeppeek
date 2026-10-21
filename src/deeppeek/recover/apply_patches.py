"""
recover.apply_patches — Pass 6 (apply partial patches onto the base
full print).

DIFFICULT.  Owns:

    FIX [5]   Aggregate patch-failure summary: one line at the end
              instead of one per file.  Per-file detail stays in
              warnings.txt and the manifest.
    FIX [11]  A failed orphan patch now demotes the file's confidence
              to trust_cap (HIGH/MEDIUM) rather than dropping it to
              LOW.  A file with a valid base print and one missing
              patch is far more useful than no file; the incomplete
              state is recorded in verification.patches_incomplete.

    NEW (2026-10-20)
              No-op patch detection.  A patch whose body is a
              contiguous run of non-blank lines already present in
              the base is labelled `applied_by = "already-present"`
              instead of `failed`.  Consequence: the structural-
              failure gate in `pass8_verify._has_structural_failures`
              no longer fires for these patches, so a `last_full`
              pick whose only "failures" are recipe snippets that
              illustrate the file's own content gets promoted to
              HIGH instead of being silently demoted to LOW and
              moved to `files_low_trust/`.

              Live symptom that prompted this: the cable playground
              dump (DeepSeek chat export) prints `pg_dispatch.py`
              twice — once in Phase 3, once (fully rewritten) in
              the optimization phase — and the optimization
              response's own "here's the replacement code" recipe
              sections were being attributed as patches to the same
              file.  Those snippets were already in the base; the
              deterministic and LLM patchers both correctly
              returned None; `applied_by = "failed"` fired; the
              `last_full` promotion was blocked; and the fully
              correct file was written to `files_low_trust/` while
              `--min-trust HIGH` kept it out of `files/`.
"""

import re
import sys
from typing import Optional

from .core import (
    Block, FileRecord, FENCE, MAX_TOKENS_DIFF,
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

            # NEW (2026-10-20): a patch whose body is already in the
            # base has nothing to apply.  Counted as a no-op, not a
            # failure — so it does not trip the structural-failure
            # gate in pass 8 that decides whether a `last_full` pick
            # can be promoted to HIGH.
            if _patch_already_present(patch["body"], content):
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

        # FIX [11]: incomplete patches do not silently kill the file
        rec.trust = trust_cap
        if not all_ok:
            rec.verification["patches_incomplete"] = True
            rec.warnings.append(
                "one or more orphan patches failed; base content "
                "written without them")
        if all_ok and not all_deterministic:
            rec.verification["via_llm"] = True

    # FIX [5]: aggregate summary — one line, no noise.
    #
    # NEW (2026-10-20): `total_noop` counts patches that were
    # already in the base (labelled `already-present`).  Those are
    # neither "applied" (the tool did not modify the base for them)
    # nor "failed" (nothing was wrong).  `total_failed` therefore
    # subtracts both buckets.  A file with 1 det + 3 already-present
    # patches now reports `0 failed`, not `3 failed`, and its
    # `last_full` pick can be promoted to HIGH.
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


def _patch_already_present(body: str, base: str) -> bool:
    """True if `body` appears as a contiguous run of non-blank
    lines in `base` (whitespace-normalised).

    A recipe section in the conversation often shows a snippet
    that is *already* in the base file — the "here's the change"
    fence, in a response that then reprints the whole file.  When
    we try to apply such a snippet as a patch, it fails not
    because anything is broken but because the patch was already
    applied before we saw it.  Labelling it
    `applied_by = "already-present"` keeps the audit trail clean
    without contaminating the structural-failure gate in
    `pass8_verify._has_structural_failures` that decides whether
    a `last_full` pick can be promoted to HIGH.

    Rationale for the exact test:

      · A patch body that is a *verbatim subsequence* of the base
        is a redundant illustration, not a change.  This is a
        sequence-membership test, not an edit-distance test: the
        only thing a fuzzy distance would buy us is a way to
        miss the exact-match case where the patch body has been
        truncated mid-line but the surrounding lines match.  That
        case is rare in recipe snippets (which are usually whole
        function bodies, not partial lines) and not worth the
        false-positive risk that a distance threshold introduces.

      · Both body and base are whitespace-normalised (strip + drop
        empty lines) before the test.  The recipe text often
        re-indents the fence body relative to the base file; the
        bare-token comparison is what we care about.

      · The body's line count being larger than the base's line
        count short-circuits to False — a "patch" larger than the
        whole file cannot be a no-op.

      · An empty body returns True — there is nothing to apply,
        which is trivially the already-present case.  This
        matches `_try_deterministic_patch`'s early return for
        empty bodies.

    The helper is deliberately small and pure; it is called once
    per patch, and the patches are already bounded by the size of
    the recovered records, so no further optimisation is needed.
    """
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
    body_stripped = patch["body"].strip()
    if not body_stripped:
        return None

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
    return base_head + body_trimmed + sep + base_tail


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
