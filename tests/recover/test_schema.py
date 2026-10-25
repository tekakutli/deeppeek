"""Schema regression guards for the core dataclasses.

Motivated by a real bug: `disposition` was added to `FileRecord`
when it needed to be on `Block`.  `hasattr` passed at runtime (the
attribute gets set dynamically by classify), so nothing failed —
until a test that constructed `Block(disposition=...)` hit a
TypeError.  These tests catch that class of bug directly by
asserting the declared schema against an expected shape.

If you intentionally change the schema, update BOTH the dataclass
and its expected-fields dict here.  The update is the check: you
have to look at both sides to keep them in sync.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
_SRC = _REPO_ROOT / "src"
# Both the repo root (for `from tests.helpers import ...`)
# and src/ (for `from deeppeek...`) must be on sys.path.
# Running `python tests/recover/test_schema.py` puts
# tests/recover/ on sys.path[0], not the repo root — so
# `from tests.helpers` fails without this.
for _p in (str(_REPO_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.helpers import (  # noqa: E402
    assert_dataclass_schema,
    assert_dataclass_init_accepts_fields,
    assert_dataclass_init_only_declared,
)
import dataclasses  # noqa: E402


def test_block_schema():
    from deeppeek.recover.core import Block
    MISSING = dataclasses.MISSING
    assert_dataclass_schema(Block, {
        # Required (no defaults)
        "idx": MISSING,
        "header_path": MISSING,
        "modifier": MISSING,
        "fence_lang": MISSING,
        "body": MISSING,
        "body_line_count": MISSING,
        "context_before": MISSING,
        "line_start": MISSING,
        "line_end": MISSING,
        # Optional (defaults)
        "is_orphan": False,
        "attributed_to": None,
        "attributed_by": None,
        "is_code_file": None,
        "kind": None,
        "canonical_path": None,
        "output_path": None,
        "confidence": None,
        "classified_by": "?",
        "escalation_reason": None,
        "heuristic_kind": None,
        "llm_kind": None,
        "override_reason": None,
        "llm_retries": 0,
        "intent": "unknown",
        "scope_hint": "",
        "scope_name": "",
        "paired_with": None,
        "disposition": None,
    })


def test_file_record_schema():
    from deeppeek.recover.core import FileRecord
    MISSING = dataclasses.MISSING
    expected = {
        "path": MISSING,
        "state": MISSING,
        "source_blocks": dataclasses.MISSING,  # default_factory
        "last_full_idx": None,
        "patches": dataclasses.MISSING,        # default_factory
        "is_new": False,
        "is_deleted": False,
        "trust": "SKIP",
        "content": None,
        "verification": dataclasses.MISSING,   # default_factory
        "warnings": dataclasses.MISSING,       # default_factory
        "all_full_idxs": dataclasses.MISSING,  # default_factory
        "chosen_full_idx": None,
        "dedup_reason": None,
        "import_problems": dataclasses.MISSING,  # default_factory
        "preexisting_base": False,
        "pairs": dataclasses.MISSING,          # default_factory
        "sanity_rejected": False,
        "repair_content": None,
    }
    # default_factory fields: validate presence but not default value
    actual = {f.name: f for f in dataclasses.fields(FileRecord)}
    for name, expected_default in expected.items():
        assert name in actual, (
            f"FileRecord: expected field {name!r}, missing")
        f = actual[name]
        if expected_default is dataclasses.MISSING and (
                f.default is not dataclasses.MISSING
                or f.default_factory is not dataclasses.MISSING):
            # Could be a required field or a default_factory field
            pass
        elif expected_default is not dataclasses.MISSING:
            assert f.default == expected_default, (
                f"FileRecord.{name}: expected default "
                f"{expected_default!r}, found {f.default!r}")


def test_block_disposition_not_on_file_record():
    """Regression: `disposition` is Block-only.  FileRecord must
    not declare it.  Set by pass2 classify on individual blocks."""
    from deeppeek.recover.core import Block, FileRecord
    block_fields = {f.name for f in dataclasses.fields(Block)}
    fr_fields = {f.name for f in dataclasses.fields(FileRecord)}
    assert "disposition" in block_fields, (
        "Block must declare `disposition`")
    assert "disposition" not in fr_fields, (
        "FileRecord must not declare `disposition` — it belongs on "
        "Block, set by classify/attribute stages")


def test_block_init_matches_fields():
    from deeppeek.recover.core import Block
    assert_dataclass_init_accepts_fields(Block)
    assert_dataclass_init_only_declared(Block)


def test_file_record_init_matches_fields():
    from deeppeek.recover.core import FileRecord
    assert_dataclass_init_accepts_fields(FileRecord)
    assert_dataclass_init_only_declared(FileRecord)


def test_block_constructs_with_every_optional_as_kwarg():
    """Construct Block with all required fields set, and every
    optional field given by its default value as a kwarg.  Catches
    a field declared but rejected by __init__."""
    from deeppeek.recover.core import Block
    kwargs = {
        "idx": 0, "header_path": None, "modifier": None,
        "fence_lang": "", "body": "", "body_line_count": 0,
        "context_before": "", "line_start": 0, "line_end": 0,
    }
    for f in dataclasses.fields(Block):
        if f.name in kwargs:
            continue
        kwargs[f.name] = f.default if f.default is not dataclasses.MISSING else None
    Block(**kwargs)
    print("  Block(**kwargs) with all fields -> OK")


def test_file_record_constructs_with_every_field():
    from deeppeek.recover.core import FileRecord
    kwargs = {"path": "x.js", "state": "MISSING"}
    for f in dataclasses.fields(FileRecord):
        if f.name in kwargs:
            continue
        if f.default is not dataclasses.MISSING:
            kwargs[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            try:
                kwargs[f.name] = f.default_factory()
            except TypeError:
                kwargs[f.name] = None
    FileRecord(**kwargs)
    print("  FileRecord(**kwargs) with all fields -> OK")


def main():
    tests = [
        ("block schema", test_block_schema),
        ("file record schema", test_file_record_schema),
        ("block disposition not on file record",
         test_block_disposition_not_on_file_record),
        ("block init matches fields",
         test_block_init_matches_fields),
        ("file record init matches fields",
         test_file_record_init_matches_fields),
        ("block constructs with every optional as kwarg",
         test_block_constructs_with_every_optional_as_kwarg),
        ("file record constructs with every field",
         test_file_record_constructs_with_every_field),
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
        print(f"\n{failed}/{len(tests)} schema test(s) failed")
        return 1
    print(f"\nAll {len(tests)} schema tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
