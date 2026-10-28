"""Regression tests for the apply-heuristics fixes added alongside
reconciliation pass 8.7b:

  · Fix 1 (2026-10-28b): dict-key grammar widened to accept
    `--flag` / `-f` / `dotted.key` keys.  Enables `--output_rotation`
    placement inside a `@command(args={...})` decorator.

  · Fix 3 (2026-10-28b): duplicate-assignment pre-gate.  A single-
    line assignment that already exists verbatim is `already-present`,
    not re-inserted by the anchor-after heuristic.

  · Fix 5 (2026-10-28b): scope-aware anchor selection.  When the
    backticked statement anchor appears multiple times, the heuristic
    picks the unique occurrence inside the scope named by the prose.

  · Call-only-anchor fallthrough (2026-10-28c): a bare `func()` in
    backticks used to be skipped by the statement branch (the call-
    arg branch requires a string literal).  It now fires via the
    unique-anchor or scope-aware path.

Each test targets a shape that the pre-fix code silently mishandled.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.patch_misc_insert import (
    _DICT_ENTRY_START_RE, _ENTRY_OUTER_KEY_RE,
    _try_named_dict_insert,
)
from deeppeek.recover.apply_patches import (
    _is_duplicate_assignment_insert, pass6_apply_patches,
)
from deeppeek.recover.patch_anchor_insert import _try_insert_after_anchor


# ---------------------------------------------------------------------------
# Fix 1 — dict-key grammar
# ---------------------------------------------------------------------------
#
# The pre-fix regex required a letter to start the key, so `"--flag"`
# and `"-f"` were rejected.  These are exactly the shapes that appear
# in `@command(args={...})` decorator dictionaries.

def test_dict_entry_start_regex_accepts_dashed_keys():
    """Direct regex: entry-start must match leading-dash keys."""
    for line in (
        '"--output_rotation": {',
        '"-f": {"type": "str"},',
        '"--key":',
        "'--k':",
        'plain_key: value',
        'dotted.key: value',
        'board.inpaintContext: {',
    ):
        assert _DICT_ENTRY_START_RE.match(line), (
            f"entry-start regex refused {line!r}"
        )


def test_entry_outer_key_regex_accepts_dashed_keys():
    """Direct regex: outer-key must extract the full dashed name."""
    cases = [
        ('"--output_rotation": {', "--output_rotation"),
        ('"-f": {"type": "str"}', "-f"),
        ('plain: value', "plain"),
        ('dotted.key: {', "dotted.key"),
    ]
    for line, expected in cases:
        m = _ENTRY_OUTER_KEY_RE.match(line)
        assert m is not None, f"outer-key regex refused {line!r}"
        assert m.group(1) == expected, (
            f"extracted {m.group(1)!r}, expected {expected!r}"
        )


def test_dict_entry_with_leading_dashes_placed():
    """A `--`-prefixed entry must be placed inside the args dict."""
    base = (
        '    @command(\n'
        '        args={\n'
        '            "--timeout": {"type": "float"},\n'
        '        },\n'
        '    )\n'
        '    def handle(self, params):\n'
        '        pass\n'
    )
    body = (
        '"--output_rotation": {"type": "float", "default": 0.0,\n'
        '                      "help": "Degrees ..."},\n'
    )
    result = _try_named_dict_insert(base, body, "Add to the args dict:")
    assert result is not None, (
        "expected placement; the dict-key grammar is rejecting the "
        "`--` prefix"
    )
    assert '"--output_rotation"' in result
    assert '"--timeout"' in result
    # Insert must be inside the args dict, before the closing `},`.
    assert (result.index('"--output_rotation"')
            < result.index('        },'))


def test_dict_entry_single_dash_placed():
    """A `-f`-style entry is placed the same as a `--flag` entry."""
    base = (
        '    @command(\n'
        '        args={\n'
        '            "--verbose": {"type": "bool"},\n'
        '        },\n'
        '    )\n'
        '    def handle(self, params):\n'
        '        pass\n'
    )
    body = '"-f": {"type": "str"},\n'
    result = _try_named_dict_insert(base, body, "Add to the args dict:")
    assert result is not None
    assert '"-f"' in result
    assert '"--verbose"' in result


def test_dict_entry_dotted_key_placed():
    """A dotted key is legal; the grammar widens to include `.`."""
    base = (
        '    @command(\n'
        '        args={\n'
        '            "existing": {"type": "str"},\n'
        '        },\n'
        '    )\n'
        '    def handle(self, params):\n'
        '        pass\n'
    )
    body = 'dotted.key: "value",\n'
    result = _try_named_dict_insert(base, body, "Add to the args dict:")
    assert result is not None
    assert 'dotted.key' in result


def test_dict_entry_dashed_key_skipped_when_already_present():
    """A `--`-prefixed entry that already exists in the dict must NOT
    be inserted again.  Pre-fix, `--output_rotation` wasn't extracted
    as an existing key (regex rejected it) — the entry was inserted
    twice."""
    base = (
        '    @command(\n'
        '        args={\n'
        '            "--output_rotation": {"type": "float"},\n'
        '        },\n'
        '    )\n'
        '    def handle(self, params):\n'
        '        pass\n'
    )
    body = '"--output_rotation": {"type": "float", "default": 0.0},\n'
    result = _try_named_dict_insert(base, body, "Add to the args dict:")
    assert result is None, (
        f"expected None (entry already present), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Fix 3 — duplicate-assignment pre-gate
# ---------------------------------------------------------------------------
#
# The motivating shape: `output_name = params.get("output_name") or
# None`.  The anchor-after heuristic finds a "safe" statement boundary
# (after a `finally:` block) and inserts the assignment there, where
# it is unreachable dead code.  The post-gate
# `_verify_no_duplicate_insertion` misses it because `new_counts -
# body_counts == old_counts` when the patch body IS the duplicate.

def test_duplicate_assignment_gate_catches_single_line():
    """A single-line assignment that already exists → gate fires."""
    old_content = (
        'def handle(self, params):\n'
        '    output_name = params.get("output_name") or None\n'
        '    return output_name\n'
    )
    patch = {
        "idx": 62,
        "body": 'output_name = params.get("output_name") or None',
    }
    assert _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_allows_multi_line():
    """A multi-line patch never triggers the gate — context lines are
    legitimate even if some duplicate existing lines."""
    old_content = "output_name = params.get('output_name') or None\n"
    patch = {
        "idx": 1,
        "body": 'output_name = params.get("output_name") or None\n'
                'return 0\n',
    }
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_skips_continuation():
    """A line ending in `{` is a continuation, not a complete
    statement; a prefix-coincidence in old_content must not fire."""
    old_content = "obj = {\n  'a': 1,\n}\n"
    patch = {"idx": 1, "body": "obj = {"}
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_skips_calls():
    """A single-line call isn't an assignment; the gate does not
    apply (calls can legitimately repeat)."""
    old_content = "do_thing();\n"
    patch = {"idx": 1, "body": "do_thing();"}
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_skips_equality():
    """`x == y` is not an assignment; the gate does not fire."""
    old_content = "if x == y:\n    pass\n"
    patch = {"idx": 1, "body": "x == y"}
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_none_body():
    """Defensive: body=None must not raise.

    Regression for the None-handling bug: `patch.get("body", "")`
    returns None when the key exists with value None (the default
    only fires on missing keys).
    """
    assert not _is_duplicate_assignment_insert(
        "x = 1\n", {"idx": 1, "body": None}
    )


def test_duplicate_assignment_gate_empty_body():
    """An empty body (not None) has no lines to duplicate; the gate
    is a no-op."""
    assert not _is_duplicate_assignment_insert(
        "x = 1\n", {"idx": 1, "body": ""}
    )


def test_duplicate_assignment_gate_missing_body_key():
    """A patch dict without a body key falls back to empty; not a
    duplicate insert."""
    assert not _is_duplicate_assignment_insert("x = 1\n", {"idx": 1})


def test_duplicate_assignment_gate_end_to_end():
    """Full pipeline: a duplicate single-line assignment is
    `already-present` and does NOT insert a second copy."""
    base_body = (
        'def handle(self, params):\n'
        '    output_name = params.get("output_name") or None\n'
        '    return output_name\n'
        '\n'
        '\n'
        'def other():\n'
        '    pass\n'
    )
    base_block = Block(
        idx=0, header_path="x.py", modifier=None, fence_lang="py",
        body=base_body, body_line_count=base_body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="x.py", output_path="x.py",
        classified_by="preexisting",
    )
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [{
        "idx": 1,
        "body": 'output_name = params.get("output_name") or None',
        "context_before": "Add after the finally block:",
        "fence_lang": "py",
    }]
    pass6_apply_patches({"x.py": rec}, {0: base_block}, None)
    assert rec.patches[0]["applied_by"] == "already-present", (
        f"expected already-present, got "
        f"{rec.patches[0]['applied_by']!r}"
    )
    assert rec.patches[0].get("duplicate_assignment") is True
    count = rec.content.count(
        'output_name = params.get("output_name")')
    assert count == 1, (
        f"expected 1 occurrence, got {count}:\n{rec.content}"
    )


# ---------------------------------------------------------------------------
# Fix 5 — scope-aware anchor selection
# ---------------------------------------------------------------------------
#
# When the backticked statement anchor appears multiple times in the
# file, one occurrence lives in the scope named by the prose.  The
# heuristic picks that occurrence rather than failing on ambiguity.

def test_anchor_within_named_scope_resolves_unique():
    """Anchor appears in two functions; prose names one of them;
    insert lands in the named one."""
    base = (
        'def handle_a():\n'
        '    do_thing()\n'
        '    return 1\n'
        '\n'
        'def handle_b():\n'
        '    do_thing()\n'
        '    return 2\n'
    )
    body = 'do_after()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body,
        "In `handle_a`, after `do_thing()`:",
    )
    assert result is not None, (
        "scope-aware anchor selection did not fire"
    )
    a_pos = result.index("def handle_a")
    b_pos = result.index("def handle_b")
    insert_pos = result.index("do_after()")
    assert a_pos < insert_pos < b_pos, (
        f"expected insert in handle_a (between {a_pos} and {b_pos}), "
        f"got position {insert_pos}:\n{result}"
    )


def test_anchor_ambiguous_within_named_scope_fails_closed():
    """Anchor appears twice inside the named scope → refuse rather
    than guess."""
    base = (
        'def handle_a():\n'
        '    do_thing()\n'
        '    do_thing()\n'
        '    return 1\n'
    )
    body = 'do_after()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body,
        "In `handle_a`, after `do_thing()`:",
    )
    # No heuristic fires; the branch requires a unique occurrence
    # within the named scope and refuses ambiguity.
    assert result is None


def test_anchor_no_named_scope_stays_unique_only():
    """Prose names no scope (no backticked "in <name>" phrase);
    unique-anchor behavior is preserved exactly (this is the
    pre-fix path)."""
    base = (
        'def handle():\n'
        '    e.promptEl.disabled = busy\n'
        '    return e\n'
    )
    body = 'guard()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body,
        "Add after `e.promptEl.disabled = busy`:",
    )
    assert result is not None
    assert (result.index("guard()")
            > result.index("e.promptEl.disabled = busy"))


def test_anchor_named_scope_not_in_base_falls_through():
    """Prose names a scope that does not exist; the heuristic falls
    back to the unique-anchor path (the anchor itself is unique in
    base) and inserts cleanly."""
    base = (
        'def other_scope():\n'
        '    do_thing()\n'
        '    return 1\n'
    )
    body = 'do_after()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body,
        "In `handle_missing`, after `do_thing()`:",
    )
    # `do_thing()` is unique in base so the unique-anchor path fires.
    # The scope name did not need to resolve.
    assert result is not None, (
        "unique-anchor fallback did not fire when the named scope "
        "is missing"
    )
    assert "do_after()" in result


def test_anchor_call_with_no_args_uses_statement_branch():
    """`func()` (call without args) is handled by the statement
    branch, not skipped as call-only.  The call-arg branch needs a
    string literal; this shape must still place."""
    base = (
        'def f():\n'
        '    do_thing()\n'
        '    return 1\n'
    )
    body = 'after_call()\ncleanup()\n'
    result = _try_insert_after_anchor(
        base, body,
        "After `do_thing()`:",
    )
    assert result is not None, (
        "call-without-args anchor was not placed; the statement "
        "branch is likely still skipping _CALL_ONLY_RE shapes"
    )
    assert "after_call()" in result
    assert result.index("after_call()") > result.index("do_thing()")

# Add these to the existing file, alongside the other tests.
# Also add the names to the `tests` list in `main()`.

def test_dict_var_names_dashed_key_fallback():
    """A body whose first key starts with `-` adds `args` as a
    fallback var name, even when prose names only a non-dict var.

    Motivating case (2026-10-28d, comfyui.py block idx=62): prose
    named `comfy_generate` (a method) but not `args`; the entry
    couldn't be placed.  With the fallback, `args` is tried and the
    dict is found.
    """
    from deeppeek.recover.patch_misc_insert import _extract_dict_var_names
    names = _extract_dict_var_names(
        "In handlers/comfyui.py, find the `comfy_generate` command "
        "decorator. Add:",
        '"--output_rotation": {"type": "float", "default": 0.0,',
    )
    assert "args" in names, (
        f"expected `args` fallback for dashed-key body, got {names!r}"
    )


def test_dict_var_names_no_fallback_for_plain_key():
    """A body whose first key is a plain identifier does NOT add
    `args` — the fallback only fires on dashed keys."""
    from deeppeek.recover.patch_misc_insert import _extract_dict_var_names
    names = _extract_dict_var_names(
        "Add to the config:",
        '"existing_key": "value",',
    )
    assert "args" not in names, (
        f"did not expect `args` fallback for plain-key body, "
        f"got {names!r}"
    )


def test_dict_entry_with_dashed_key_placed_via_body_fallback():
    """End-to-end: a `--output_rotation` entry with prose that names
    only the method (not `args`) places into the args dict, because
    the body's dashed key adds `args` as a fallback var name."""
    from deeppeek.recover.patch_misc_insert import _try_named_dict_insert
    base = (
        '    @command(\n'
        '        args= {\n'
        '            "--timeout": {"type": "float", "default": 55.0},\n'
        '        },\n'
        '    )\n'
        '    def handle(self, params):\n'
        '        pass\n'
    )
    body = (
        '"--output_rotation": {"type": "float", "default": 0.0,\n'
        '                      "help": "Degrees to rotate ..."},\n'
    )
    context = (
        "In handlers/comfyui.py, find the `comfy_generate` command "
        "decorator. Add:"
    )
    result = _try_named_dict_insert(base, body, context)
    assert result is not None, (
        "expected placement via dashed-key fallback; the fix for "
        "block idx=62 is not working"
    )
    assert '"--output_rotation"' in result
    assert '"--timeout"' in result

def test_signature_replace_indented_method():
    """The pre-fix `_try_signature_replace` called `body.strip()`,
    discarding the method's leading indent.  `base_indent` was then
    always "", and the signature search only matched top-level defs.
    Motivating case: comfyui.py block idx=99's signature-only patch
    for the indented `_paste_result` method never matched.
    """
    from deeppeek.recover.patch_replace import _try_signature_replace
    base = (
        "class Foo:\n"
        "    def bar(self, a):\n"
        "        return a\n"
        "\n"
        "def toplevel(a):\n"
        "    return a\n"
    )
    body = "    def bar(self, a, b=0):\n"
    result = _try_signature_replace(base, body)
    assert result is not None, (
        "expected signature replace to match the indented method; "
        "the indentation is likely being stripped")
    assert "    def bar(self, a, b=0):" in result, (
        f"signature replacement not applied:\n{result}")
    assert "    def bar(self, a):" not in result, (
        f"old signature still present:\n{result}")
    assert "def toplevel(a):" in result, (
        f"unrelated top-level function was clobbered:\n{result}")


def test_placeholder_marker_merged_into_docstring():
    """The comfyui_krita.py shape: base has a function with a real
    docstring; the new body has a placeholder inside its docstring
    plus an appended paragraph.  The merge splices the base's
    docstring content into the placeholder.
    """
    from deeppeek.recover.patch_scope import _try_merge_placeholder_in_scope
    base = (
        "def paste_image(doc, png, x, y):\n"
        '    """Decode a PNG and write it as a new layer.\n'
        "\n"
        "    The PNG comes straight from ComfyUI.  QImage decodes it\n"
        "    to Format_ARGB32.\n"
        '    """\n'
        "    return doc\n"
    )
    new_body = (
        "def paste_image(doc, png, x, y, rotation=0.0):\n"
        '    """... (docstring unchanged) ...\n'
        "\n"
        "    `rotation` is degrees, positive = clockwise on screen.\n"
        '    """\n'
        "    return doc\n"
    )
    merged = _try_merge_placeholder_in_scope(base, "paste_image",
                                              new_body)
    assert merged is not None, (
        "expected merge to succeed; the placeholder should be "
        "replaceable with the base docstring")
    assert "... (docstring unchanged) ..." not in merged, (
        f"placeholder survived the merge:\n{merged}")
    assert "Decode a PNG and write it as a new layer." in merged, (
        f"base docstring content not spliced in:\n{merged}")
    assert "`rotation` is degrees" in merged, (
        f"new paragraph lost in merge:\n{merged}")
    assert "rotation=0.0" in merged, (
        f"signature change lost:\n{merged}")

def main():
    tests = [
        # Fix 1 — dict-key grammar
        ("dict entry-start regex accepts dashed keys",
         test_dict_entry_start_regex_accepts_dashed_keys),
        ("entry outer-key regex accepts dashed keys",
         test_entry_outer_key_regex_accepts_dashed_keys),
        ("dict entry with leading dashes placed",
         test_dict_entry_with_leading_dashes_placed),
        ("dict entry single dash placed",
         test_dict_entry_single_dash_placed),
        ("dict entry dotted key placed",
         test_dict_entry_dotted_key_placed),
        ("dict entry dashed key skipped when already present",
         test_dict_entry_dashed_key_skipped_when_already_present),
        # Fix 3 — duplicate-assignment pre-gate
        ("duplicate assignment gate catches single line",
         test_duplicate_assignment_gate_catches_single_line),
        ("duplicate assignment gate allows multi line",
         test_duplicate_assignment_gate_allows_multi_line),
        ("duplicate assignment gate skips continuation",
         test_duplicate_assignment_gate_skips_continuation),
        ("duplicate assignment gate skips calls",
         test_duplicate_assignment_gate_skips_calls),
        ("duplicate assignment gate skips equality",
         test_duplicate_assignment_gate_skips_equality),
        ("duplicate assignment gate none body",
         test_duplicate_assignment_gate_none_body),
        ("duplicate assignment gate empty body",
         test_duplicate_assignment_gate_empty_body),
        ("duplicate assignment gate missing body key",
         test_duplicate_assignment_gate_missing_body_key),
        ("duplicate assignment gate end to end",
         test_duplicate_assignment_gate_end_to_end),
        # Fix 5 — scope-aware anchor
        ("anchor within named scope resolves unique",
         test_anchor_within_named_scope_resolves_unique),
        ("anchor ambiguous within named scope fails closed",
         test_anchor_ambiguous_within_named_scope_fails_closed),
        ("anchor no named scope stays unique only",
         test_anchor_no_named_scope_stays_unique_only),
        ("anchor named scope not in base falls through",
         test_anchor_named_scope_not_in_base_falls_through),
        ("anchor call with no args uses statement branch",
         test_anchor_call_with_no_args_uses_statement_branch),
        ("dict var names dashed key fallback",
         test_dict_var_names_dashed_key_fallback),
        ("dict var names no fallback for plain key",
         test_dict_var_names_no_fallback_for_plain_key),
        ("dict entry with dashed key placed via body fallback",
         test_dict_entry_with_dashed_key_placed_via_body_fallback),
        # 2026-10-28 — signature replace indentation & placeholder merge
        ("signature replace indented method",
         test_signature_replace_indented_method),
        ("placeholder marker merged into docstring",
         test_placeholder_marker_merged_into_docstring),
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
        print(f"\n{failed}/{len(tests)} apply-gate extra test(s) failed")
        return 1
    print(f"\nAll {len(tests)} apply-gate extra tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
