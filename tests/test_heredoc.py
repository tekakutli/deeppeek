"""Regression tests for the heredoc prompt text.

The prompt shipped by `deeppeek prompt heredoc` is a standing
order that teaches the AI how to emit files. These tests guard
the properties the prompt must have, matching the rules in
docs/EMISSION.md.

Key properties:

  1. Actionable markers. The prompt must contain the specific
     markers an AI needs to follow: PYEOF (the canonical
     heredoc delimiter), chr(96) (the backtick workaround), and
     the fence-wrapping rule (rule 3 of EMISSION.md).

  2. Standing order. The prompt must use forward-looking
     language and must tell the AI to re-emit already-broken
     files.

  3. Consistent with EMISSION.md. When the document says
     "wrap the bash in a markdown fence", the prompt must say
     the same. When the document says "use chr(96) in the
     body", the prompt must say the same.

Whitespace handling: prompt text is wrapped for readability, so
any two-word phrase can land on either side of a newline.
Text-matching tests normalise whitespace before matching (see
`_norm`) rather than requiring the prompt to break lines at
specific points.
"""
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.prompts.heredoc import HEREDOC_PROMPT


def _norm(text: str) -> str:
    """Lowercase and collapse all whitespace runs to a single space."""
    return re.sub(r"\s+", " ", text.lower())


def test_prompt_is_not_empty():
    assert HEREDOC_PROMPT.strip()
    assert len(HEREDOC_PROMPT) > 1000, (
        "prompt is too short to be a comprehensive instruction")


def test_prompt_mentions_heredoc_marker():
    """PYEOF is the canonical heredoc delimiter."""
    assert "PYEOF" in HEREDOC_PROMPT
    assert "python3 - <<'PYEOF'" in HEREDOC_PROMPT


def test_prompt_mentions_chr96_workaround():
    """The backtick workaround must appear as both a single-
    backtick reference and a fence construction."""
    assert "chr(96)" in HEREDOC_PROMPT
    assert "chr(96) * 3" in HEREDOC_PROMPT


def test_prompt_mentions_fence_wrapping():
    """Rule 3 of EMISSION.md: wrap the whole bash in a markdown
    fence. The prompt must teach this."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("markdown code fence" in lower
            or "markdown fence" in lower
            or "wrap the whole bash" in lower), (
        "prompt must mention the fence-wrapping rule (rule 3)")


def test_prompt_mentions_four_backtick_escape():
    """The four-backtick escape hatch is required when the body
    itself must display three-backtick examples."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("four-backtick" in lower
            or "four backtick" in lower
            or "4-backtick" in lower
            or "4 backtick" in lower), (
        "prompt must mention the four-backtick escape hatch")


def test_prompt_is_standing_order():
    """The instruction must be forward-looking, not task-scoped."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("standing order" in lower
            or "from now on" in lower
            or "from this point forward" in lower), (
        "prompt must clearly state the rule is standing, not per-task")


def test_prompt_is_recovery_aware():
    """A conversation already broken by raw-fence emission must
    be healable in place. The prompt must tell the AI to re-emit."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("re-emit" in lower
            or "reemit" in lower
            or "reprint" in lower), (
        "prompt must tell the AI to re-emit already-broken files")


def test_prompt_covers_non_code_files():
    """The constraint applies to markdown, docs, configs, not
    just source files."""
    lower = _norm(HEREDOC_PROMPT)
    assert "markdown" in lower or "documentation" in lower
    assert "config" in lower or "tests" in lower or "test " in lower


def test_prompt_forbids_raw_backticks_in_body():
    """Rule 2 of EMISSION.md: no literal backticks in the body.
    The prompt must teach this explicitly, naming chr(96) as the
    workaround."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("never put a literal backtick" in lower
            or "never put a raw backtick" in lower
            or "no literal backtick" in lower), (
        "prompt must explicitly forbid literal backticks in the body")


def test_prompt_explains_quoting():
    """The <<'PYEOF' quoting is what makes the body safe from
    bash expansion."""
    lower = _norm(HEREDOC_PROMPT)
    assert ("quoted heredoc" in lower
            or "quoting" in lower
            or "single quotes around pyeof" in lower), (
        "prompt must explain why the heredoc delimiter is quoted")


def main():
    tests = [
        ("prompt is not empty", test_prompt_is_not_empty),
        ("prompt mentions heredoc marker",
         test_prompt_mentions_heredoc_marker),
        ("prompt mentions chr96 workaround",
         test_prompt_mentions_chr96_workaround),
        ("prompt mentions fence wrapping",
         test_prompt_mentions_fence_wrapping),
        ("prompt mentions four-backtick escape",
         test_prompt_mentions_four_backtick_escape),
        ("prompt is standing order", test_prompt_is_standing_order),
        ("prompt is recovery aware", test_prompt_is_recovery_aware),
        ("prompt covers non-code files",
         test_prompt_covers_non_code_files),
        ("prompt forbids raw backticks in body",
         test_prompt_forbids_raw_backticks_in_body),
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
