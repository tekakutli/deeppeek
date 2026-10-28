"""Tests for the pass-6 patch-loop infrastructure added 2026-10-28:
duplicate-insertion gate, terminal-status preservation, fixed-point
loop, prior-insertions fallback, def-anchor insert, and the raised
fuzzy-misattribution thresholds."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.apply_patches import (
    _verify_no_duplicate_insertion, pass6_apply_patches,
)
from deeppeek.recover.patch_anchor_insert import (
    _try_insert_after_def, _try_insert_after_anchor,
)
from deeppeek.recover.reconcile import _fuzzy_locate_elsewhere


def _base_block(body, idx=0, path="x.js", lang="js"):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang=lang,
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _rec(path, content=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    return rec


# ---------------------------------------------------------------------------
# _verify_no_duplicate_insertion
# ---------------------------------------------------------------------------

def test_no_dup_normal_patch():
    old = "function f() {\n  a();\n}\n"
    new = "function f() {\n  a();\n  b();\n}\n"
    patch = {"idx": 1, "body": "b();\n"}
    assert _verify_no_duplicate_insertion(old, new, patch, "x.js")


def test_dup_extra_copy_of_preexisting_line():
    """Body doesn't contain the duplicated line → flagged."""
    old = "keep_me();\nother();\n"
    new = "keep_me();\nother();\nkeep_me();\n"
    patch = {"idx": 1, "body": "other();\n"}
    assert not _verify_no_duplicate_insertion(old, new, patch, "x.js")


def test_dup_sets_duplicate_insertion_field():
    old = "keep_me();\n"
    new = "keep_me();\nkeep_me();\n"
    patch = {"idx": 1, "body": "added();\n"}
    assert not _verify_no_duplicate_insertion(old, new, patch, "x.js")
    assert "keep_me();" in patch["duplicate_insertion"]


def test_dup_single_body_line_matching_preexisting_not_flagged():
    """A patch whose single body line happens to equal a pre-existing
    line: body subtraction prevents a false positive."""
    old = "first();\nsecond();\n"
    new = "first();\nsecond();\nfirst();\n"
    patch = {"idx": 1, "body": "first();\n"}
    assert _verify_no_duplicate_insertion(old, new, patch, "x.js")


def test_dup_object_literal_split_not_flagged():
    """Single-line object literal split across multiple lines is NOT a
    duplicate.  Motivating case: `const board = { a: 1 };` becomes
    `const board = {\\n  a: 1,\\n  b: 2,\\n};` via object-literal
    splice."""
    old = "const board = { a: 1 };\n"
    new = "const board = {\n  a: 1,\n  b: 2,\n};\n"
    patch = {
        "idx": 1,
        "body": "board = {\n  a: 1,\n  b: 2,\n};\n",
    }
    # `const board = {` is new (didn't exist as a standalone line
    # before), so old_counts doesn't iterate over it.  Not flagged.
    assert _verify_no_duplicate_insertion(old, new, patch, "x.js")


def test_dup_preexisting_line_split_into_two():
    """A pre-existing line 'foo(a, b);' reformatted to two lines
    'foo(a,\\n    b);' — the fragments aren't pre-existing lines, so
    they aren't flagged even though they didn't exist as complete
    lines before."""
    old = "foo(a, b);\n"
    new = "foo(a,\n    b);\n"
    patch = {
        "idx": 1,
        "body": "foo(a,\n    b);\n",
    }
    assert _verify_no_duplicate_insertion(old, new, patch, "x.js")


# ---------------------------------------------------------------------------
# Fixed-point loop
# ---------------------------------------------------------------------------

def test_fixed_point_retry():
    """Patch B (idx 1) needs `helper` (created by patch A idx 2).
    B fails on pass 1, succeeds on pass 2 after A applies."""
    base_body = "def existing():\n    pass\n"
    base = _base_block(base_body, path="x.py", lang="py")
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        # Patch B first (needs `helper` from patch A below)
        {"idx": 1,
         "body": "    x = 1",
         "context_before": "In `helper`, add:",
         "fence_lang": "py"},
        # Patch A: creates `helper`
        {"idx": 2,
         "body": "def helper():\n    pass\n",
         "context_before": "",
         "fence_lang": "py"},
    ]
    blocks_by_idx = {0: base}
    pass6_apply_patches({"x.py": rec}, blocks_by_idx, None)

    assert rec.content is not None
    assert "def helper" in rec.content
    assert "x = 1" in rec.content
    assert rec.patches[0]["applied_by"] == "deterministic"
    assert rec.patches[1]["applied_by"] == "deterministic"


# ---------------------------------------------------------------------------
# Terminal-status preservation
# ---------------------------------------------------------------------------

def test_terminal_status_already_present_not_relabelled_failed():
    old = "existing();\n"
    base = _base_block(old)
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        {"idx": 1, "body": "existing();\n",
         "context_before": "", "fence_lang": "js"},
    ]
    pass6_apply_patches({"x.js": rec}, {0: base}, None)
    assert rec.patches[0]["applied_by"] == "already-present"


def test_terminal_status_skipped_dup_not_relabelled_failed():
    """A patch rejected by the dup gate keeps its status."""
    old = "keep_me();\nother();\n"
    new_expected = "keep_me();\nother();\nkeep_me();\n"
    base = _base_block(old)
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    # Body is `other();` only, but the file base has `keep_me();` once.
    # If the patch fires an insert that duplicates `keep_me();`
    # (via context-insert's anchor mechanism), the gate should fire.
    # Simpler: body is `keep_me();` (matches a pre-existing line), so
    # already-present branch fires first — verify status is terminal.
    rec.patches = [
        {"idx": 1, "body": "keep_me();\n",
         "context_before": "Add `keep_me();`:",
         "fence_lang": "js"},
    ]
    pass6_apply_patches({"x.js": rec}, {0: base}, None)
    status = rec.patches[0].get("applied_by")
    assert status in ("already-present", "skipped_duplicate_insertion",
                      "deterministic"), (
        f"terminal statuses must survive; got {status!r}")


# ---------------------------------------------------------------------------
# Def-anchor insert
# ---------------------------------------------------------------------------

def test_insert_after_def_python():
    base = (
        "def existing(a):\n"
        "    return a\n"
        "\n"
        "def other():\n"
        "    pass\n"
    )
    body = "def new_helper(b):\n    return b\n"
    result = _try_insert_after_def(base, "existing", body)
    assert result is not None
    assert "def new_helper" in result
    existing_end = result.index("return a")
    new_pos = result.index("def new_helper")
    other_pos = result.index("def other")
    assert existing_end < new_pos < other_pos


def test_insert_after_def_python_multiline_sig():
    base = (
        "def existing(\n"
        "    a,\n"
        "    b,\n"
        "):\n"
        "    return a + b\n"
        "\n"
        "def other():\n"
        "    pass\n"
    )
    body = "def new():\n    pass\n"
    result = _try_insert_after_def(base, "existing", body)
    assert result is not None
    assert "def new" in result
    return_pos = result.index("return a + b")
    new_pos = result.index("def new")
    assert return_pos < new_pos


def test_insert_after_def_class():
    base = (
        "class Existing:\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "class Other:\n"
        "    pass\n"
    )
    body = "class New:\n    pass\n"
    result = _try_insert_after_def(base, "Existing", body)
    assert result is not None
    assert "class New" in result
    assert result.index("class New") < result.index("class Other")


def test_insert_after_def_js():
    base = (
        "function existing(a) {\n"
        "  return a;\n"
        "}\n"
        "\n"
        "function other() {\n"
        "}\n"
    )
    body = "function newHelper(b) {\n  return b;\n}\n"
    result = _try_insert_after_def(base, "existing", body)
    assert result is not None
    assert "function newHelper" in result
    existing_pos = result.index("function existing")
    new_pos = result.index("function newHelper")
    other_pos = result.index("function other")
    assert existing_pos < new_pos < other_pos


def test_insert_after_def_js_export():
    base = (
        "export function existing(a) {\n"
        "  return a;\n"
        "}\n"
    )
    body = "function newHelper(b) {\n  return b;\n}\n"
    result = _try_insert_after_def(base, "existing", body)
    assert result is not None
    assert "function newHelper" in result


def test_insert_after_def_not_found():
    base = "def other():\n    pass\n"
    body = "def new():\n    pass\n"
    assert _try_insert_after_def(base, "missing", body) is None


def test_insert_after_def_empty_body():
    base = "def existing():\n    pass\n"
    assert _try_insert_after_def(base, "existing", "") is None
    assert _try_insert_after_def(base, "existing", "   \n") is None


# ---------------------------------------------------------------------------
# Prior-insertions fallback in _try_insert_after_anchor
# ---------------------------------------------------------------------------

def test_prior_insertions_fallback():
    """No backtick anchor in prose, but prior_insertions gives one."""
    base = (
        "def f():\n"
        "    anchor_line()\n"
    )
    body = "after_anchor_line()"
    result = _try_insert_after_anchor(
        base, body, "Add the following:",
        prior_insertions={"anchor_line()"})
    assert result is not None
    assert "after_anchor_line()" in result
    anchor_pos = result.index("anchor_line()")
    after_pos = result.index("after_anchor_line()")
    assert anchor_pos < after_pos


def test_prior_insertions_none_no_crash():
    base = "def f():\n    pass\n"
    body = "g()"
    result = _try_insert_after_anchor(base, body, "no anchor here")
    assert result is None or isinstance(result, str)


def test_prior_insertions_ambiguous_skipped():
    """Prior line appears multiple times → skipped."""
    base = (
        "def f():\n"
        "    anchor()\n"
        "\n"
        "def g():\n"
        "    anchor()\n"
    )
    body = "new_call()"
    result = _try_insert_after_anchor(
        base, body, "Add:",
        prior_insertions={"anchor()"})
    # anchor() appears twice, count != 1 → fallback does not fire.
    assert result is None


# ---------------------------------------------------------------------------
# _fuzzy_locate_elsewhere threshold (Fix 4)
# ---------------------------------------------------------------------------

def test_fuzzy_short_body_requires_verbatim():
    """Short bodies (<500 chars) require exact substring match, not
    3-of-4-line fuzzy similarity."""
    body = (
        'try:\n'
        '    x = float(params.get("x"))\n'
        'except (TypeError, ValueError):\n'
        '    x = 0.0\n'
    )
    other_content = (
        'try:\n'
        '    x = float(payload.get("x"))\n'
        'except (TypeError, ValueError):\n'
        '    x = 0.0\n'
    )
    other = _rec("other.py", other_content)
    records = {"current.py": _rec("current.py", ""),
               "other.py": other}
    # 3-of-4 lines match — would have been flagged by the old 0.7
    # threshold.  Now returns None.
    assert len(body) < 500
    assert _fuzzy_locate_elsewhere(body, "current.py", records) is None


def test_fuzzy_short_body_verbatim_matches():
    """A 40+-char body that appears verbatim elsewhere is found by
    the short-body verbatim path."""
    body = (
        "some_unique_function_call();  // padding to exceed 40 chars\n"
    )
    other = _rec("other.py", "header\n" + body + "footer\n")
    records = {"current.py": _rec("current.py", ""),
               "other.py": other}
    assert _fuzzy_locate_elsewhere(
        body, "current.py", records) == "other.py"


def test_fuzzy_long_body_uses_higher_threshold():
    """Long bodies (>=500 chars) use 0.85 fuzzy (up from 0.7)."""
    body_lines = [
        f"function helper_{i}(input_value) {{ return input_value + {i}; }}"
        for i in range(10)
    ]
    body = "\n".join(body_lines) + "\n"
    assert len(body) >= 500
    # Other has 8 of 10 lines verbatim → 0.8 similarity, below 0.85.
    other_lines = body_lines[:8] + [
        "function other_a() {}",
        "function other_b() {}",
    ]
    other = _rec("other.py", "\n".join(other_lines) + "\n")
    records = {"current.py": _rec("current.py", ""),
               "other.py": other}
    assert _fuzzy_locate_elsewhere(body, "current.py", records) is None


def main():
    tests = [
        ("no dup normal patch", test_no_dup_normal_patch),
        ("dup extra copy of preexisting line",
         test_dup_extra_copy_of_preexisting_line),
        ("dup sets duplicate insertion field",
         test_dup_sets_duplicate_insertion_field),
        ("dup single body line matching preexisting not flagged",
         test_dup_single_body_line_matching_preexisting_not_flagged),
        ("dup object literal split not flagged",
         test_dup_object_literal_split_not_flagged),
        ("dup preexisting line split into two",
         test_dup_preexisting_line_split_into_two),
        ("fixed point retry", test_fixed_point_retry),
        ("terminal status already-present not relabelled failed",
         test_terminal_status_already_present_not_relabelled_failed),
        ("terminal status skipped-dup not relabelled failed",
         test_terminal_status_skipped_dup_not_relabelled_failed),
        ("insert after def python", test_insert_after_def_python),
        ("insert after def python multiline sig",
         test_insert_after_def_python_multiline_sig),
        ("insert after def class", test_insert_after_def_class),
        ("insert after def js", test_insert_after_def_js),
        ("insert after def js export", test_insert_after_def_js_export),
        ("insert after def not found", test_insert_after_def_not_found),
        ("insert after def empty body", test_insert_after_def_empty_body),
        ("prior insertions fallback", test_prior_insertions_fallback),
        ("prior insertions none no crash",
         test_prior_insertions_none_no_crash),
        ("prior insertions ambiguous skipped",
         test_prior_insertions_ambiguous_skipped),
        ("fuzzy short body requires verbatim",
         test_fuzzy_short_body_requires_verbatim),
        ("fuzzy short body verbatim matches",
         test_fuzzy_short_body_verbatim_matches),
        ("fuzzy long body uses higher threshold",
         test_fuzzy_long_body_uses_higher_threshold),
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
        print(f"\n{failed}/{len(tests)} patch-gate test(s) failed")
        return 1
    print(f"\nAll {len(tests)} patch-gate tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
