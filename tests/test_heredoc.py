"""Regression tests for the heredoc prompt text.

These guard two properties the prompt depends on:

  1. The prompt ITSELF must be backtick-free — no literal backtick,
     no triple-backtick fence.  If a prompt contained a raw fence,
     the same GUI that mangles file emissions would mangle the
     instruction that fixes file emissions.

  2. The prompt must contain the specific markers an AI would
     follow: PYEOF (canonical heredoc delimiter), chr(96) (the
     backtick workaround), and language that makes the order
     STANDING rather than per-task.

Plus a few structural sanity checks on length, coverage, and
recovery awareness.

Whitespace handling: prompt text is wrapped for readability at ~72
columns, so any two-word phrase can land on either side of a newline.
Text-matching tests therefore normalise whitespace before matching
(see `_norm`) rather than requiring the prompt to break lines at
specific points.  This keeps the tests semantic — "the prompt
mentions X" — instead of brittle — "the prompt mentions X with a
single space and no newline".
"""
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.prompts.heredoc import HEREDOC_PROMPT


def _norm(text: str) -> str:
    """Lowercase and collapse all whitespace runs to a single space.

    The prompt is wrapped for readability; two-word phrases can span
    a newline.  Normalising before matching makes the checks
    insensitive to where the author chose to break a line.
    """
    return re.sub(r"\s+", " ", text.lower())


def test_prompt_is_not_empty():
    assert HEREDOC_PROMPT.strip()
    assert len(HEREDOC_PROMPT) > 1000, (
        "prompt is too short to be a comprehensive instruction")


def test_prompt_has_no_raw_backticks():
    """The whole point of the prompt is to replace backticks.  If it
    contained any itself, it would be self-defeating."""
    assert "`" not in HEREDOC_PROMPT, (
        "prompt contains a literal backtick; the constraint it asks "
        "for is 'use chr(96), never a raw backtick'.  The prompt "
        "must obey the same rule it prescribes.")


def test_prompt_has_no_triple_backtick_fence():
    """Stronger than the single-backtick check: no fenced code block
    anywhere in the prompt text."""
    assert "```" not in HEREDOC_PROMPT, (
        "prompt contains a triple-backtick fence; file content must "
        "be emitted via heredoc, and the instruction must not "
        "itself use a markdown fence.")


def test_prompt_mentions_heredoc_marker():
    """PYEOF is the canonical heredoc delimiter used in every
    existing prompt (evolve, reprint_markdown).  If it's missing,
    the AI would invent its own and lose the safety properties."""
    assert "PYEOF" in HEREDOC_PROMPT
    assert "python3 - <<'PYEOF'" in HEREDOC_PROMPT


def test_prompt_mentions_chr96_workaround():
    """The backtick workaround is the non-obvious bit.  Both
    `chr(96)` and `chr(96) * 3` must be present so the AI sees both
    a single-backtick and a fence construction."""
    assert "chr(96)" in HEREDOC_PROMPT
    assert "chr(96) * 3" in HEREDOC_PROMPT


def test_prompt_is_standing_order():
    """The instruction must be forward-looking, not task-scoped.
    This is the difference between a useful constraint and one the
    AI ignores on the next turn."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("standing order" in lower
            or "from now on" in lower
            or "from this point forward" in lower), (
        "prompt must clearly state the rule is standing, not per-task")


def test_prompt_is_recovery_aware():
    """A conversation already broken by raw-fence emission must be
    healable in place.  The prompt must tell the AI to re-emit."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("re-emit" in lower
            or "reemit" in lower
            or "reprint" in lower), (
        "prompt must tell the AI to re-emit already-broken files")


def test_prompt_covers_non_code_files():
    """The constraint applies to markdown, docs, configs, etc., not
    just source files.  If only code is mentioned, the AI will
    happily emit a broken README."""
    lower = _norm(HEREDOC_PROMPT)
    assert "markdown" in lower or "documentation" in lower
    assert "config" in lower or "tests" in lower or "test " in lower


def test_prompt_forbids_raw_markdown():
    """The prohibition must be explicit.  An AI reading only 'use
    heredoc' might assume a markdown fence *around* a heredoc is
    fine."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("do not emit file content as raw markdown" in lower
            or "never emit file content as raw markdown" in lower), (
        "prompt must explicitly forbid emitting file content as "
        "raw markdown")


def test_prompt_forbids_wrapping_bash_in_fence():
    """The most common mistake: wrapping the heredoc in a markdown
    code fence.  Must be explicitly prohibited."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("do not wrap bash in a markdown code fence" in lower
            or "never wrap the bash itself in a markdown code fence"
            in lower), (
        "prompt must explicitly forbid wrapping the emitted bash "
        "in a markdown code fence")


def test_prompt_explains_quoting():
    """The `<<'PYEOF'` quoting (single-quote around the delimiter)
    is what makes the body safe from bash expansion.  Without the
    explanation, an AI might use `<<PYEOF` (unquoted) and get its
    dollar signs and backticks silently expanded."""
    lower = _norm(HEREDOC_PROMPT)
    assert "quoted heredoc" in lower or "quoting" in lower, (
        "prompt must explain why the heredoc delimiter is quoted")


def main():
    tests = [
        ("prompt is not empty", test_prompt_is_not_empty),
        ("prompt has no raw backticks",
         test_prompt_has_no_raw_backticks),
        ("prompt has no triple-backtick fence",
         test_prompt_has_no_triple_backtick_fence),
        ("prompt mentions heredoc marker",
         test_prompt_mentions_heredoc_marker),
        ("prompt mentions chr96 workaround",
         test_prompt_mentions_chr96_workaround),
        ("prompt is standing order", test_prompt_is_standing_order),
        ("prompt is recovery aware", test_prompt_is_recovery_aware),
        ("prompt covers non-code files",
         test_prompt_covers_non_code_files),
        ("prompt forbids raw markdown",
         test_prompt_forbids_raw_markdown),
        ("prompt forbids wrapping bash in fence",
         test_prompt_forbids_wrapping_bash_in_fence),
        ("prompt explains quoting", test_prompt_explains_quoting),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS: {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {name}\n        {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {name}\n         {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\nAll {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
