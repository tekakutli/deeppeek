"""Helpers for fix scripts and self-verification.

`find_unique_anchor_in_class` is the class-scoped analogue of a
global `grep`: it searches only within one `class ...:` body, so an
anchor that happens to also exist in a different class is invisible.

`assert_dataclass_schema` compares a dataclass's declared fields
against an expected schema — a stronger check than `hasattr` because
it verifies the field is a *declared* field, not a runtime attribute
set after construction.
"""
from pathlib import Path
import dataclasses
import inspect


def find_unique_anchor_in_class(path, class_name, needle):
    """Return (zero-based line index, line text) for the unique line
    within `class class_name:` that contains `needle`.

    Raises ValueError if the class does not exist, if the needle is
    not found in the class body, or if it appears more than once.
    The class-scope boundary is determined by indentation: the class
    ends at the next line whose indent is at or below the class
    header's indent and which starts with `class `.
    """
    content = Path(path).read_text()
    lines = content.split("\n")

    start = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"class {class_name}"):
            start = i
            break
    if start is None:
        raise ValueError(f"class {class_name} not found in {path}")

    class_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if (line_indent <= class_indent
                and line.lstrip().startswith("class ")):
            end = i
            break

    matches = [i for i in range(start, end) if needle in lines[i]]
    if not matches:
        raise ValueError(
            f"anchor {needle!r} not found in class {class_name} "
            f"of {path}")
    if len(matches) > 1:
        raise ValueError(
            f"anchor {needle!r} appears {len(matches)}x in class "
            f"{class_name} at lines "
            f"{[m + 1 for m in matches]}")

    idx = matches[0]
    return idx, lines[idx]


def assert_dataclass_schema(cls, expected_fields):
    """Assert that `cls` is a dataclass whose declared fields exactly
    match `expected_fields` (a dict mapping name → expected default
    or `MISSING` for required fields).  Raises AssertionError on
    mismatch, naming the field and the discrepancy.
    """
    assert dataclasses.is_dataclass(cls), (
        f"{cls.__name__} is not a dataclass")
    actual = {f.name: f for f in dataclasses.fields(cls)}

    for name, expected_default in expected_fields.items():
        assert name in actual, (
            f"{cls.__name__}: expected field {name!r}, missing")
        f = actual[name]
        if expected_default is dataclasses.MISSING:
            assert (f.default is dataclasses.MISSING
                    and f.default_factory is dataclasses.MISSING), (
                f"{cls.__name__}.{name}: expected required field, "
                f"found default={f.default!r}")
        else:
            assert f.default == expected_default, (
                f"{cls.__name__}.{name}: expected default "
                f"{expected_default!r}, found {f.default!r}")

    for name in actual:
        if name not in expected_fields:
            raise AssertionError(
                f"{cls.__name__}: unexpected field {name!r} "
                f"(add it to the expected schema in the test, or "
                f"confirm it belongs on this dataclass)")


def assert_dataclass_init_accepts_fields(cls):
    """Assert that every field in `dataclasses.fields(cls)` is
    accepted as a kwarg by `cls.__init__`.  Catches fields declared
    but not wired into the generated __init__ (typo, custom __init__,
    frozen mismatch).
    """
    sig = inspect.signature(cls.__init__)
    params = set(sig.parameters.keys()) - {"self"}
    for f in dataclasses.fields(cls):
        assert f.name in params, (
            f"{cls.__name__}: field {f.name!r} declared but not "
            f"accepted by __init__.  Check the dataclass decorator "
            f"and whether a custom __init__ shadows the generated one.")


def assert_dataclass_init_only_declared(cls):
    """The reverse: every `__init__` parameter (except `self`) is a
    declared field.  Catches a param in __init__ that isn't in
    dataclasses.fields, which would be a manual __init__ overriding
    the generated one.
    """
    sig = inspect.signature(cls.__init__)
    params = set(sig.parameters.keys()) - {"self"}
    field_names = {f.name for f in dataclasses.fields(cls)}
    extra = params - field_names
    assert not extra, (
        f"{cls.__name__}: __init__ has params not in "
        f"dataclasses.fields: {sorted(extra)}")
