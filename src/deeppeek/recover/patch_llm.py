"""
patch_llm — prompt template and diff application for the LLM patch
path.

The pipeline's last-resort heuristic: ask a small model to produce
a unified diff that transforms `base` into `base + patch_intent`,
then apply the diff mechanically.  The mechanical application
(`_manual_apply_unified`) is the stable part; the prompt will evolve
as the pipeline's context engineering improves.

Kept in its own module because the LLM interaction is a distinct
concern from the deterministic heuristics — a deployment that runs
with `--llm-mode off` never touches this module, and a deployment
that runs with an LLM only visits it on heuristic failure.

No project imports beyond core constants.
"""

import re

from .core import FENCE, MAX_TOKENS_DIFF


def _diff_prompt(path, n_lines, base, ctx, patch_body):
    return (
        "You are applying a diff instruction to a base file.  This is\n"
        "purely mechanical - do not improve, reformat, or reorganise\n"
        "anything outside the described change.\n\n"
        f"Base file (path: {path}, {n_lines} lines):\n"
        f"{FENCE}\n{base}\n{FENCE}\n\n"
        "Diff instruction (the change to apply):\n"
        f"{ctx}\n\n"
        f"{FENCE}\n{patch_body}\n{FENCE}\n\n"
        "Produce a unified diff that transforms the base file into one\n"
        "that includes the described change.  Output ONLY the diff inside\n"
        f"a {FENCE}diff{FENCE} fenced block.  Do NOT reproduce the base file.\n\n"
        "If the change is ambiguous, output:\n"
        f"{FENCE}diff\nFAILED: <one-sentence reason>\n{FENCE}\n"
    )


def _try_llm_patch(base, patch, llm, path):
    n_lines = base.count("\n") + 1
    if n_lines > 1500:
        print(f"  [LLM:diff] skip: base is {n_lines} lines (max 1500)",
              file=__import__("sys").stderr)
        return None
    prompt = _diff_prompt(
        path=path, n_lines=n_lines, base=base,
        ctx=patch.get("context_before", "")[:300],
        patch_body=patch["body"][:2000])
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


def _manual_apply_unified(base, diff_text):
    base_lines = base.splitlines(keepends=False)
    out = []
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
