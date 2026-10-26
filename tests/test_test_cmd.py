"""Tests for `deeppeek test -o PATH`.

Two properties:

  1. Argument parsing accepts -o/--output and stores it as a Path.

  2. End-to-end: `deeppeek test --filter <fast-file> -o <log>` writes
     the full transcript to <log> — runner's own `=== file ===`
     header AND the subprocess's stdout both appear in the file.
     This is the exact shape the -o flag exists to provide.

The integration test filters to test_last_thought (8 sub-millisecond
unit tests) so the whole file runs in well under a second.  If the
project layout doesn't ship test_last_thought.py, the test is a
no-op rather than a failure — same convention as other tests that
probe optional dependencies.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_register_has_output_flag():
    from deeppeek.commands.test import register
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)

    args = parser.parse_args(["test", "-o", "/tmp/x.log"])
    assert args.output == Path("/tmp/x.log")

    args = parser.parse_args(["test", "--output", "/tmp/x.log"])
    assert args.output == Path("/tmp/x.log")

    args = parser.parse_args(["test"])
    assert args.output is None


def test_output_flag_writes_transcript():
    """-o writes a durable transcript of the whole run."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "log.txt"
        r = subprocess.run(
            [sys.executable, "-m", "deeppeek", "test",
             "--filter", "test_last_thought",
             "-o", str(log)],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        # Filter matched nothing -> this checkout doesn't carry the
        # fixture file.  Not our concern; skip.
        if "no test file matched filter" in r.stderr:
            return
        if not log.is_file():
            raise AssertionError(
                f"log file not created at {log}\n"
                f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        content = log.read_text()
        assert "test_last_thought" in content, (
            f"runner message missing from transcript:\n{content}")
        assert "PASS" in content, (
            f"subprocess stdout missing from transcript:\n{content}")


def main():
    tests = [
        ("register has output flag", test_register_has_output_flag),
        ("output flag writes transcript",
         test_output_flag_writes_transcript),
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
