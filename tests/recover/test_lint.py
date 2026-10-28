"""Unit tests for pass 8.4 (@command decorator arg lint).

Warn-only after the 2026-10-28 alignment: the pass emits a warning
when a patch introduces an `@command` arg the body never reads, but
does not change `rec.trust`.  The file ships; the warning names the
incomplete feature.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import FileRecord
from deeppeek.recover.lint import (
    _find_command_violations,
    _arg_introduced_by_patch,
    pass8_4_lint_command_args,
)


def _rec(path, content, patches=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    rec.patches = patches if patches is not None else []
    rec.trust = "HIGH"
    return rec


# ---------------------------------------------------------------------------
# _find_command_violations
# ---------------------------------------------------------------------------

def test_all_args_used():
    content = (
        'from x import command\n'
        '\n'
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '    "--bar": {"type": "int"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    a = params.get("foo")\n'
        '    b = params.get("bar")\n'
        '    return a, b\n'
    )
    assert _find_command_violations(content) == []


def test_one_arg_unused():
    content = (
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '    "--bar": {"type": "int"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    v = _find_command_violations(content)
    assert len(v) == 1
    assert v[0]["name"] == "handle"
    assert v[0]["unused"] == ["bar"]


def test_bracket_access_counts_as_use():
    content = (
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return params["foo"]\n'
    )
    assert _find_command_violations(content) == []


def test_get_with_default_counts_as_use():
    content = (
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return params.get("foo", "default")\n'
    )
    assert _find_command_violations(content) == []


def test_short_flag_recognized():
    content = (
        '@command(args={"-f": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return 1\n'
    )
    v = _find_command_violations(content)
    assert len(v) == 1
    assert v[0]["unused"] == ["f"]


def test_multiline_args_dict():
    content = (
        '@command(args={\n'
        '    "--foo": {\n'
        '        "type": "str",\n'
        '        "help": "some help",\n'
        '    },\n'
        '    "--bar": {\n'
        '        "type": "int",\n'
        '    },\n'
        '})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n'
    )
    v = _find_command_violations(content)
    assert len(v) == 1
    assert v[0]["unused"] == ["bar"]


def test_non_command_decorator_ignored():
    content = (
        '@dataclass\n'
        'class Foo:\n'
        '    x: int = 0\n'
    )
    assert _find_command_violations(content) == []


def test_command_with_no_args():
    content = (
        '@command(args={})\n'
        'def handle(self, params):\n'
        '    return None\n'
    )
    assert _find_command_violations(content) == []


def test_two_command_methods_checked_independently():
    content = (
        '@command(args={"--foo": {"type": "str"}})\n'
        'def one(self, params):\n'
        '    return params.get("foo")\n'
        '\n'
        '@command(args={"--bar": {"type": "int"}})\n'
        'def two(self, params):\n'
        '    return 1\n'
    )
    v = _find_command_violations(content)
    assert len(v) == 1
    assert v[0]["name"] == "two"
    assert v[0]["unused"] == ["bar"]


def test_delegated_reads_count_as_use():
    """A command that delegates to a helper which reads `params.get`
    counts as using those args.  Motivating case: comfyui.py
    `comfy_generate` delegates to `_comfy_generate_impl`; the arg
    `--output_rotation` is read in the helper, not in the method.
    """
    content = (
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return self._impl(params)\n'
        '\n'
        'def _impl(self, params):\n'
        '    return params.get("foo")\n'
    )
    assert _find_command_violations(content) == []


def test_delegated_reads_dont_fix_unrelated_args():
    """Delegation only counts args actually read in the callee."""
    content = (
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '    "--bar": {"type": "int"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return self._impl(params)\n'
        '\n'
        'def _impl(self, params):\n'
        '    return params.get("foo")\n'
    )
    v = _find_command_violations(content)
    assert len(v) == 1
    assert v[0]["unused"] == ["bar"]


# ---------------------------------------------------------------------------
# _arg_introduced_by_patch
# ---------------------------------------------------------------------------

def test_arg_introduced_by_declaration():
    rec = _rec("x.py", "", patches=[{
        "idx": 1,
        "body": '"--output_rotation": {"type": "float", "default": 0.0},',
    }])
    assert _arg_introduced_by_patch(rec, "output_rotation")


def test_arg_introduced_single_dash():
    rec = _rec("x.py", "", patches=[{
        "idx": 1,
        "body": '"-f": {"type": "str"},',
    }])
    assert _arg_introduced_by_patch(rec, "f")


def test_arg_usage_does_not_count_as_introduction():
    rec = _rec("x.py", "", patches=[{
        "idx": 1,
        "body": 'rotation = params.get("output_rotation") or 0.0\n',
    }])
    assert not _arg_introduced_by_patch(rec, "output_rotation")


def test_arg_substring_not_matched():
    rec = _rec("x.py", "", patches=[{
        "idx": 1,
        "body": '"--foo_bar": {"type": "str"},',
    }])
    assert not _arg_introduced_by_patch(rec, "foo")


# ---------------------------------------------------------------------------
# pass8_4_lint_command_args — warn-only semantics
# ---------------------------------------------------------------------------

def test_no_warning_when_all_used():
    rec = _rec("x.py",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n')
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0
    assert rec.trust == "HIGH"
    assert not rec.warnings


def test_warns_when_arg_introduced_by_patch():
    """Warn-only: an unused arg added by a patch is reported but does
    not change trust."""
    rec = _rec("x.py",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return 1\n',
        patches=[{
            "idx": 1,
            "body": '"--foo": {"type": "str"},',
            "applied_by": "deterministic",
        }])
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 1
    assert rec.trust == "HIGH", "pass8_4 is warn-only; trust unchanged"
    assert "unused_command_args" in rec.verification
    assert any("lint:" in w and "foo" in w for w in rec.warnings)


def test_no_warning_when_arg_preexisting():
    rec = _rec("x.py",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return 1\n',
        patches=[{
            "idx": 1,
            "body": "some_unrelated_addition = 1\n",
            "applied_by": "deterministic",
        }])
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0
    assert rec.trust == "HIGH"
    assert any("pre-existing" in w for w in rec.warnings)


def test_wiring_patch_does_not_trigger_warning():
    rec = _rec("x.py",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return params.get("foo")\n',
        patches=[{
            "idx": 1,
            "body": 'return params.get("foo")\n',
            "applied_by": "deterministic",
        }])
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0
    assert rec.trust == "HIGH"


def test_delegated_reads_prevent_warning():
    """The motivating comfyui.py shape: the arg is declared in the
    @command decorator and read in a helper the method delegates to.
    Lint must not fire."""
    rec = _rec("x.py",
        '@command(args={"--output_rotation": {"type": "float"}})\n'
        'def comfy_generate(self, params):\n'
        '    return self._comfy_generate_impl(params)\n'
        '\n'
        'def _comfy_generate_impl(self, params):\n'
        '    output_rotation = float(params.get("output_rotation") or 0.0)\n'
        '    return output_rotation\n',
        patches=[{
            "idx": 1,
            "body": '"--output_rotation": {"type": "float"},',
            "applied_by": "deterministic",
        }])
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0, (
        f"expected no warning (delegated read); "
        f"got unused={rec.verification.get('unused_command_args')}"
    )
    assert rec.trust == "HIGH"


def test_skips_non_python():
    rec = _rec("x.js",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return 1\n',
        patches=[{
            "idx": 1, "body": '"--foo": ...',
            "applied_by": "deterministic",
        }])
    records = {"x.js": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0
    assert rec.trust == "HIGH"


def test_processes_already_low_records():
    """LOW records still get lint warnings — the warning is
    informational, orthogonal to existing trust state."""
    rec = _rec("x.py",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return 1\n',
        patches=[{
            "idx": 1, "body": '"--foo": {"type": "str"},',
            "applied_by": "deterministic",
        }])
    rec.trust = "LOW"
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 1, "LOW records still get warnings"
    assert rec.trust == "LOW", "trust not touched"


def test_skips_none_content():
    rec = FileRecord(path="x.py", state="MISSING")
    rec.content = None
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0


def test_skips_deleted():
    rec = _rec("x.py",
        '@command(args={"--foo": {"type": "str"}})\n'
        'def handle(self, params):\n'
        '    return 1\n',
        patches=[{
            "idx": 1, "body": '"--foo": {"type": "str"},',
            "applied_by": "deterministic",
        }])
    rec.is_deleted = True
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n == 0


def test_crash_in_extractor_does_not_raise():
    import deeppeek.recover.lint as lint_mod
    original = lint_mod._find_command_violations

    def _boom(_):
        raise RuntimeError("simulated")

    try:
        lint_mod._find_command_violations = _boom
        rec = _rec(
            "x.py",
            '@command(args={"--foo": {}})\ndef h(self, params): pass\n')
        records = {"x.py": rec}
        n = pass8_4_lint_command_args(records)
        assert n == 0
        assert any("pass 8.4 lint raised" in w for w in rec.warnings)
    finally:
        lint_mod._find_command_violations = original


def test_one_warning_per_record():
    """Two unused args in the same record still emit one warning
    listing both.  Trust unchanged."""
    rec = _rec("x.py",
        '@command(args={\n'
        '    "--foo": {"type": "str"},\n'
        '    "--bar": {"type": "int"},\n'
        '})\n'
        'def handle(self, params):\n'
        '    return 1\n',
        patches=[{
            "idx": 1,
            "body": '"--foo": ..., "--bar": ...',
            "applied_by": "deterministic",
        }])
    records = {"x.py": rec}
    n = pass8_4_lint_command_args(records)
    assert n >= 1
    assert rec.trust == "HIGH"
    assert len(rec.verification["unused_command_args"]) == 1
    assert sorted(rec.verification["unused_command_args"][0]["unused"]) == \
        ["bar", "foo"]


def main():
    tests = [
        ("all args used", test_all_args_used),
        ("one arg unused", test_one_arg_unused),
        ("bracket access counts as use",
         test_bracket_access_counts_as_use),
        ("get with default counts as use",
         test_get_with_default_counts_as_use),
        ("short flag recognized", test_short_flag_recognized),
        ("multiline args dict", test_multiline_args_dict),
        ("non-command decorator ignored",
         test_non_command_decorator_ignored),
        ("command with no args", test_command_with_no_args),
        ("two command methods checked independently",
         test_two_command_methods_checked_independently),
        ("delegated reads count as use",
         test_delegated_reads_count_as_use),
        ("delegated reads dont fix unrelated args",
         test_delegated_reads_dont_fix_unrelated_args),
        ("arg introduced by declaration",
         test_arg_introduced_by_declaration),
        ("arg introduced single dash",
         test_arg_introduced_single_dash),
        ("arg usage does not count as introduction",
         test_arg_usage_does_not_count_as_introduction),
        ("arg substring not matched", test_arg_substring_not_matched),
        ("no warning when all used", test_no_warning_when_all_used),
        ("warns when arg introduced by patch",
         test_warns_when_arg_introduced_by_patch),
        ("no warning when arg preexisting",
         test_no_warning_when_arg_preexisting),
        ("wiring patch does not trigger warning",
         test_wiring_patch_does_not_trigger_warning),
        ("delegated reads prevent warning",
         test_delegated_reads_prevent_warning),
        ("skips non-python", test_skips_non_python),
        ("processes already low records",
         test_processes_already_low_records),
        ("skips none content", test_skips_none_content),
        ("skips deleted", test_skips_deleted),
        ("crash in extractor does not raise",
         test_crash_in_extractor_does_not_raise),
        ("one warning per record", test_one_warning_per_record),
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
        print(f"\n{failed}/{len(tests)} lint test(s) failed")
        return 1
    print(f"\nAll {len(tests)} lint tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
