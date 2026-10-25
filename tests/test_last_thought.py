"""Unit tests for the last-thought extraction logic."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.prompts.last_thought import _extract_last_thought


def test_no_header():
    assert _extract_last_thought("nothing here") is None


def test_single_section():
    text = (
        "# Title\n\n"
        "## Assistant\n\n"
        "### Thought Process\n\n"
        "> line1\n"
        "> line2\n"
    )
    assert _extract_last_thought(text) == (
        "### Thought Process\n\n> line1\n> line2\n"
    )


def test_picks_last_section():
    text = (
        "## Assistant\n\n"
        "### Thought Process\n\n"
        "> first\n\n"
        "## Assistant\n\n"
        "### Thought Process\n\n"
        "> second\n"
    )
    assert _extract_last_thought(text) == (
        "### Thought Process\n\n> second\n"
    )


def test_stops_at_content():
    """The 'not thought part' (assistant content after thinking) is
    dropped."""
    text = (
        "## Assistant\n\n"
        "### Thought Process\n\n"
        "> thinking line\n\n"
        "Actual response content here.\n"
    )
    assert _extract_last_thought(text) == (
        "### Thought Process\n\n> thinking line\n"
    )


def test_stops_at_message_separator():
    text = (
        "## Assistant\n\n"
        "### Thought Process\n\n"
        "> thinking line\n\n"
        "---\n\n"
        "## User\n\n"
        "next prompt\n"
    )
    assert _extract_last_thought(text) == (
        "### Thought Process\n\n> thinking line\n"
    )


def test_ignores_header_in_content():
    """A stray `### Thought Process` inside content (not followed by
    `> `) must not be mistaken for a real section."""
    text = (
        "## Assistant\n\n"
        "### Thought Process\n\n"
        "> real thinking\n\n"
        "Here's an example:\n\n"
        "### Thought Process\n\n"
        "This is just text, not a real thought section.\n"
    )
    assert _extract_last_thought(text) == (
        "### Thought Process\n\n> real thinking\n"
    )


def test_empty_section_returns_none():
    text = (
        "## Assistant\n\n"
        "### Thought Process\n\n"
    )
    assert _extract_last_thought(text) is None


def test_handles_crlf():
    text = (
        "## Assistant\r\n\r\n"
        "### Thought Process\r\n\r\n"
        "> line1\r\n"
        "> line2\r\n"
    )
    assert _extract_last_thought(text) == (
        "### Thought Process\n\n> line1\n> line2\n"
    )


def main():
    tests = [
        ("no header", test_no_header),
        ("single section", test_single_section),
        ("picks last section", test_picks_last_section),
        ("stops at content", test_stops_at_content),
        ("stops at separator", test_stops_at_message_separator),
        ("ignores header in content", test_ignores_header_in_content),
        ("empty section returns none", test_empty_section_returns_none),
        ("handles CRLF", test_handles_crlf),
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
