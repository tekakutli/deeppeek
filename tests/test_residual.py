"""Tests for `deeppeek residual` — residual.md file listing."""
import contextlib
import io
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from deeppeek.commands.residual import run
from deeppeek.recover.residual import parse_residual_entries


def _sample_residual():
    return """\
# Residual from deeppeek recovery

# Source: conversation.md
# 4 block(s) did not reach a successful placement against the
# target tree.

<!-- deeppeek: idx=10 outcome=failed note='missing anchor' -->
## File: `foo.py`

```py
def foo():
    pass
```

<!-- deeppeek: idx=20 outcome=not_written -->
## File: `bar.py`

```py
def bar():
    pass
```

<!-- deeppeek: idx=30 outcome=skipped_unplaceable -->
## File: `foo.py`

```py
def foo():
    return 1
```

<!-- deeppeek: idx=40 outcome=orphan_unattributed -->

```py
orphan()
```
"""


def test_parse_residual_basic():
    entries = parse_residual_entries(_sample_residual())
    assert entries == [
        ("foo.py", "failed"),
        ("bar.py", "not_written"),
        ("foo.py", "skipped_unplaceable"),
        (None, "orphan_unattributed"),
    ]


def test_parse_residual_empty():
    assert parse_residual_entries("") == []
    assert parse_residual_entries("# nothing here\n") == []


def test_parse_residual_header_inside_fence_ignored():
    """A `## File:` line inside the fence body (the recovered
    file's own content) must not be counted as a residual header."""
    text = """\
<!-- deeppeek: idx=0 outcome=failed -->
## File: `real.py`

```py
# ## File: `fake.py`  <- this is inside the body
def x():
    pass
```
"""
    entries = parse_residual_entries(text)
    assert entries == [("real.py", "failed")]


def test_run_missing_file():
    args = Namespace(path=Path("/tmp/deeppeek_residual_test_missing.md"))
    rc = run(args)
    assert rc == 1


def test_run_basic_output():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "residual.md"
        p.write_text(_sample_residual(), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run(Namespace(path=p))
        assert rc == 0
        text = out.getvalue()
        assert "blocks:        4" in text
        assert "with file:     3" in text
        assert "no file:       1" in text
        assert "unique files:  2" in text
        assert "foo.py" in text
        assert "bar.py" in text
        assert "<no file>" in text
        assert "orphan_unattributed" in text


def main():
    tests = [
        ("parse residual basic", test_parse_residual_basic),
        ("parse residual empty", test_parse_residual_empty),
        ("parse residual header inside fence ignored",
         test_parse_residual_header_inside_fence_ignored),
        ("run missing file", test_run_missing_file),
        ("run basic output", test_run_basic_output),
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
