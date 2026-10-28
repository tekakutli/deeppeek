"""Tests for `deeppeek prompt snippet`."""
import argparse
import contextlib
import io
import sys
from argparse import Namespace
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.prompts.snippet import SNIPPETS, run, register


def test_snippets_non_empty():
    assert SNIPPETS, "expected at least one snippet"


def test_implement_and_tests_present():
    assert "implement_and_tests" in SNIPPETS
    body = SNIPPETS["implement_and_tests"]
    assert "implement the fixes" in body
    assert "print every file" in body
    assert "tests" in body


def test_register_accepts_name():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    register(sub)
    args = parser.parse_args(["snippet", "implement_and_tests"])
    assert args.name == "implement_and_tests"
    assert args.list is False


def test_register_accepts_list():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    register(sub)
    args = parser.parse_args(["snippet", "--list"])
    assert args.list is True
    assert args.name is None


def test_list_outputs_names():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = run(Namespace(list=True, name=None))
    assert rc == 0
    assert "implement_and_tests" in out.getvalue()


def test_missing_name_errors():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = run(Namespace(list=False, name=None))
    assert rc == 1
    assert "missing snippet name" in err.getvalue()


def test_unknown_name_errors():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = run(Namespace(list=False, name="does_not_exist"))
    assert rc == 1
    assert "unknown snippet" in err.getvalue()
    assert "implement_and_tests" in err.getvalue()  # lists available


def test_dry_run_prints_without_typing():
    out = io.StringIO()
    args = Namespace(
        list=False, name="implement_and_tests",
        dry_run=True, port=8765, timeout=5.0,
    )
    with contextlib.redirect_stdout(out):
        rc = run(args)
    assert rc == 0
    text = out.getvalue()
    assert "implement the fixes" in text
    assert "print every file" in text


def main():
    tests = [
        ("snippets non-empty", test_snippets_non_empty),
        ("implement_and_tests present",
         test_implement_and_tests_present),
        ("register accepts name", test_register_accepts_name),
        ("register accepts list", test_register_accepts_list),
        ("list outputs names", test_list_outputs_names),
        ("missing name errors", test_missing_name_errors),
        ("unknown name errors", test_unknown_name_errors),
        ("dry run prints without typing",
         test_dry_run_prints_without_typing),
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
