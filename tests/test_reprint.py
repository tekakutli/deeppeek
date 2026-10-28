"""Tests for deeppeek prompt reprint - residual-driven file list
and prompt formatting."""
import contextlib
import io
import re
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from deeppeek.recover.residual import (
    parse_residual_entries, unique_residual_paths,
)
from deeppeek.prompts.reprint import (
    _build_prompt, _format_file_list, run,
)


def _norm(text):
    """Lowercase and collapse whitespace.  Assertions that
    span line breaks in the prompt template need this --
    prose wrapping is cosmetic, not semantic."""
    return re.sub(r"\s+", " ", text.lower())


def _sample_residual():
    return "\n".join([
        "# Residual from deeppeek recovery",
        "",
        "<!-- deeppeek: idx=10 outcome=failed note='missing anchor' -->",
        "## File: `foo.py`",
        "",
        "```py",
        "def foo():",
        "    pass",
        "```",
        "",
        "<!-- deeppeek: idx=20 outcome=not_written -->",
        "## File: `bar.py`",
        "",
        "```py",
        "def bar():",
        "    pass",
        "```",
        "",
        "<!-- deeppeek: idx=30 outcome=skipped_unplaceable -->",
        "## File: `foo.py`",
        "",
        "```py",
        "def foo():",
        "    return 1",
        "```",
        "",
        "<!-- deeppeek: idx=40 outcome=orphan_unattributed -->",
        "",
        "```py",
        "orphan()",
        "```",
    ])


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_basic():
    assert parse_residual_entries(_sample_residual()) == [
        ("foo.py", "failed"),
        ("bar.py", "not_written"),
        ("foo.py", "skipped_unplaceable"),
        (None, "orphan_unattributed"),
    ]


def test_unique_paths_dedup_and_sorted():
    assert unique_residual_paths(_sample_residual()) == [
        "bar.py", "foo.py",
    ]


def test_unique_paths_empty_when_only_orphans():
    text = (
        "<!-- deeppeek: idx=0 outcome=orphan_unattributed -->\n"
        "\n"
        "```py\n"
        "orphan()\n"
        "```\n"
    )
    assert unique_residual_paths(text) == []


def test_header_inside_fence_ignored():
    """A header-shaped line inside the recovered file body must
    not count as a residual header."""
    text = (
        "<!-- deeppeek: idx=0 outcome=failed -->\n"
        "## File: `real.py`\n"
        "\n"
        "```py\n"
        "# ## File: `fake.py` <- code body, not header\n"
        "def x():\n"
        "    pass\n"
        "```\n"
    )
    assert parse_residual_entries(text) == [("real.py", "failed")]


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def test_format_file_list():
    assert _format_file_list(["a.py", "b/c.py"]) == (
        "  - a.py\n"
        "  - b/c.py"
    )


def test_build_prompt_contains_each_file():
    p = _build_prompt(["foo.py", "bar.js"])
    assert "foo.py" in p, f"foo.py missing from prompt: {p!r}"
    assert "bar.js" in p, f"bar.js missing from prompt: {p!r}"
    text = _norm(p)
    # The load-bearing instruction is "reprint every file, in full".
    # Do NOT couple this test to the exact surrounding wording --
    # if the template gets reworded the AI still has to know what
    # to do.  Diagnostic messages surface the actual prompt on
    # failure so a wording shift is visible at a glance.
    assert "in full" in text, (
        f"'in full' missing from prompt: {p!r}")


def test_run_missing_residual():
    with tempfile.TemporaryDirectory() as tmp:
        args = Namespace(
            residual=Path(tmp) / "missing.md",
            type_timeout=5.0, port=8765, dry_run=True,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run(args)
        assert rc == 1
        assert "no residual file" in err.getvalue()


def test_run_empty_residual():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.md"
        p.write_text("# nothing here\n", encoding="utf-8")
        args = Namespace(
            residual=p, type_timeout=5.0, port=8765, dry_run=True,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run(args)
        assert rc == 1
        assert "contains no residual blocks" in err.getvalue()


def test_run_only_orphans_errors():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.md"
        p.write_text(
            "<!-- deeppeek: idx=0 outcome=orphan_unattributed -->\n\n"
            "```py\n"
            "orphan()\n"
            "```\n",
            encoding="utf-8",
        )
        args = Namespace(
            residual=p, type_timeout=5.0, port=8765, dry_run=True,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run(args)
        assert rc == 1
        assert "nothing to reprint" in err.getvalue()


def test_run_dry_run_prints_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.md"
        p.write_text(_sample_residual(), encoding="utf-8")
        args = Namespace(
            residual=p, type_timeout=5.0, port=8765, dry_run=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run(args)
        assert rc == 0
        text = out.getvalue()
        assert "foo.py" in text
        assert "bar.py" in text


def main():
    tests = [
        ("parse basic", test_parse_basic),
        ("unique paths dedup and sorted",
         test_unique_paths_dedup_and_sorted),
        ("unique paths empty when only orphans",
         test_unique_paths_empty_when_only_orphans),
        ("header inside fence ignored",
         test_header_inside_fence_ignored),
        ("format file list", test_format_file_list),
        ("build prompt contains each file",
         test_build_prompt_contains_each_file),
        ("run missing residual", test_run_missing_residual),
        ("run empty residual", test_run_empty_residual),
        ("run only orphans errors", test_run_only_orphans_errors),
        ("run dry run prints prompt",
         test_run_dry_run_prints_prompt),
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
        print(f"\n{failed}/{len(tests)} test(s) failed")
        return 1
    print(f"\nAll {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
