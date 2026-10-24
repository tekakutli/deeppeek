"""
recover.sanity — reject writes that would corrupt the target.

A patch may *match* perfectly and still produce a wrong file: a
`_delete_exact_block` that ate more than intended, an LLM that
invented context, a replace that swapped for an empty body.  The
matcher cannot know it went wrong; the shape of the result can.

Rules:

  · If the base already existed and the result loses more than 50%
    of its bytes or lines, mark suspicious.
  · If the base already existed and the result is empty, mark
    suspicious.
  · If the base already existed and the result has no common
    long-line (>40 chars) content with the base, mark suspicious.
  · Newly-created files (no base) are not checked — there's nothing
    to compare against.

Suspicious results are *not* written.  The base is preserved and
the file is marked `sanity_rejected` in the manifest.  This is the
correct behaviour: a half-applied patch is worse than a skipped
one, because the user has to find and repair the damage.  A
skipped file is visible (the manifest tells them) and leaves the
target intact.
"""

from typing import Optional


MAX_SHRINK_RATIO = 0.5      # loss > 50% → suspicious
MIN_COMMON_LINES = 3        # at least 3 long lines shared


def is_suspicious(base: Optional[str], result: str) -> tuple[bool, str]:
    """
    Return (suspicious, reason).  `reason` is empty when not
    suspicious.

    `base` is the source we started from — the base full print, or
    None for a new file.  `result` is what the patcher produced.
    """
    if base is None:
        return False, ""

    base_bytes = len(base.encode("utf-8"))
    result_bytes = len(result.encode("utf-8"))

    if base_bytes == 0:
        return False, ""

    if result_bytes == 0:
        return True, "result is empty"

    if result_bytes / base_bytes < MAX_SHRINK_RATIO:
        return True, (
            f"result is {result_bytes}B, "
            f"{100 * result_bytes / base_bytes:.0f}% of base "
            f"({base_bytes}B) — likely over-deletion"
        )

    base_lines = base.splitlines()
    result_lines = result.splitlines()
    if len(base_lines) >= 10:
        if len(result_lines) / len(base_lines) < MAX_SHRINK_RATIO:
            return True, (
                f"result has {len(result_lines)} lines, "
                f"{100 * len(result_lines) / len(base_lines):.0f}% of "
                f"base ({len(base_lines)})"
            )

    long_base = {
        ln.strip() for ln in base_lines if len(ln.strip()) > 40
    }
    long_result = {
        ln.strip() for ln in result_lines if len(ln.strip()) > 40
    }
    if len(long_base) >= 5:
        common = len(long_base & long_result)
        if common < MIN_COMMON_LINES:
            return True, (
                f"only {common} long line(s) shared with base "
                f"({len(long_base)} long lines in base) — probably "
                f"a different file"
            )

    return False, ""
