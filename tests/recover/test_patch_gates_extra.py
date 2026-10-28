"""Regression tests for the apply-heuristics fixes added alongside
reconciliation pass 8.7b.

See the pre-fix module docstring for the full inventory.  The
2026-10-28g addition is a fix for `_try_object_literal_splice`'s
spurious space between the base prefix and the body object
(`args= {` when base had `args={`).

The other 2026-10-28 additions are covered by their respective
tests below.

2026-10-28g — three more fixes, each with its own test:

  · H2 that doesn't name a file resets `current_header` (header
    leakage across sections — the bug that made Fix 5's fences
    inherit `pg_state.py` from Fix 4's H2).  Test:
    `test_h2_without_filename_resets_header` and
    `test_h3_preserves_header`.

  · Multi-statement apply: `captured = {...}` followed by
    `def on_progress(...)` no longer silently drops the def.  Test:
    `test_multi_statement_apply_object_literal_plus_def`.

  · Indented def/class replace: a column-0 `def name(...)` whose
    body lives *indented* in base is replaced in place, not
    duplicated.  Test: `test_indented_def_replace_alone`.

2026-10-28h — object-literal trailing-`;` acceptance:

  · The initial trailing-content refusal (2026-10-28g) treated a bare
    trailing `;` as "trailing content" and refused the splice.  That
    regressed `tests/recover/fixtures/02_subsection_patches.md`,
    `04_prose_override.md`, and `35_object_literal_replace.md`, all
    of which use `export const NAME = {...};` or `NAME = {...};`
    bodies.  The refined check strips a single leading `;` from the
    tail before checking for non-comment content.  Test:
    `test_object_literal_splice_accepts_trailing_semicolon` and
    `test_object_literal_splice_accepts_trailing_semicolon_plus_comment`.
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
from deeppeek.recover.patch_replace import _try_object_literal_splice
from deeppeek.recover.apply_patches import (
    _is_duplicate_assignment_insert, pass6_apply_patches,
)
from deeppeek.recover.patch_anchor_insert import _try_insert_after_anchor
from deeppeek.recover.patch_dispatch import _try_deterministic_patch


# ---------------------------------------------------------------------------
# Fix 1 — dict-key grammar
# ---------------------------------------------------------------------------

def test_dict_entry_start_regex_accepts_dashed_keys():
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
            f"entry-start regex refused {line!r}")


def test_entry_outer_key_regex_accepts_dashed_keys():
    cases = [
        ('"--output_rotation": {', "--output_rotation"),
        ('"-f": {"type": "str"}', "-f"),
        ('plain: value', "plain"),
        ('dotted.key: {', "dotted.key"),
    ]
    for line, expected in cases:
        m = _ENTRY_OUTER_KEY_RE.match(line)
        assert m is not None, f"outer-key regex refused {line!r}"
        assert m.group(1) == expected


def test_dict_entry_with_leading_dashes_placed():
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
    assert result is not None
    assert '"--output_rotation"' in result
    assert '"--timeout"' in result
    assert (result.index('"--output_rotation"')
            < result.index('        },'))


def test_dict_entry_single_dash_placed():
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
    assert result is None


def test_dict_entry_scopes_to_prose_function():
    base = (
        'class Handler:\n'
        '    @command(\n'
        '        args={\n'
        '            "--host": {"type": "str"},\n'
        '        },\n'
        '    )\n'
        '    def comfy_connect(self, params):\n'
        '        return params.get("host")\n'
        '\n'
        '    @command(\n'
        '        args={\n'
        '            "--timeout": {"type": "float", "default": 55.0},\n'
        '        },\n'
        '    )\n'
        '    def comfy_generate(self, params):\n'
        '        return params.get("timeout")\n'
    )
    body = '            "--output_rotation": {"type": "float"},\n'
    context = (
        "In handlers/comfyui.py, find the `comfy_generate` command "
        "decorator. Add:"
    )
    result = _try_named_dict_insert(base, body, context)
    assert result is not None, "expected placement"
    connect_pos = result.index("def comfy_connect")
    generate_pos = result.index("def comfy_generate")
    rot_pos = result.index('"--output_rotation"')
    assert connect_pos < rot_pos < generate_pos, (
        f"--output_rotation not in comfy_generate's decorator:\n{result}"
    )
    connect_args_start = result.index('"--host"')
    connect_args_end = connect_pos
    connect_args = result[connect_args_start:connect_args_end]
    assert '"--output_rotation"' not in connect_args, (
        f"--output_rotation leaked into comfy_connect's args:\n{result}"
    )


def test_dict_entry_scoped_already_present_returns_base():
    base = (
        'class Handler:\n'
        '    @command(\n'
        '        args={\n'
        '            "--host": {"type": "str"},\n'
        '        },\n'
        '    )\n'
        '    def comfy_connect(self, params):\n'
        '        return params.get("host")\n'
        '\n'
        '    @command(\n'
        '        args={\n'
        '            "--timeout": {"type": "float", "default": 55.0},\n'
        '            "--output_rotation": {"type": "float"},\n'
        '        },\n'
        '    )\n'
        '    def comfy_generate(self, params):\n'
        '        return params.get("timeout")\n'
    )
    body = '            "--output_rotation": {"type": "float"},\n'
    context = (
        "In handlers/comfyui.py, find the `comfy_generate` command "
        "decorator. Add:"
    )
    result = _try_named_dict_insert(base, body, context)
    assert result is base


# ---------------------------------------------------------------------------
# 2026-10-28g — _try_object_literal_splice whitespace preservation
# ---------------------------------------------------------------------------

def test_object_literal_splice_preserves_no_space_before_brace():
    """`args={` (no space) must remain `args={` after splice; the
    old code produced `args= {` (spurious space).

    Motivating case (comfyui.py, in the wild): the daemon's
    `comfy_generate` `@command(args={...})` decorator gained
    `--output_rotation`; the whole args dict was re-spliced by
    `_try_object_literal_splice`, which inserted a space that wasn't
    in the source.
    """
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
        'args={\n'
        '    "--timeout": {"type": "float"},\n'
        '    "--count": {"type": "int", "default": 1},\n'
        '}'
    )
    result = _try_object_literal_splice(base, body, "args")
    assert result is not None, "expected splice to succeed"
    assert "args={" in result, (
        f"expected `args={{` (no space); got:\n{result}"
    )
    assert "args= {" not in result, (
        f"spurious `args= {{` (space) introduced:\n{result}"
    )


def test_object_literal_splice_preserves_space_before_brace():
    """`const board = {` (with space) must remain with the space."""
    base = (
        "const board = {\n"
        "  a: 1,\n"
        "};\n"
    )
    body = (
        "board = {\n"
        "  a: 1,\n"
        "  b: 2,\n"
        "};"
    )
    result = _try_object_literal_splice(base, body, "board")
    assert result is not None, "expected splice to succeed"
    assert "const board = {" in result, (
        f"expected `const board = {{` (with space); got:\n{result}"
    )
    assert "const board={" not in result, (
        f"space before `{{` removed (broke style):\n{result}"
    )


# ---------------------------------------------------------------------------
# 2026-10-28h — object-literal trailing-`;` acceptance
# ---------------------------------------------------------------------------

def test_object_literal_splice_accepts_trailing_semicolon():
    """A bare trailing `;` on the body must NOT be treated as
    "trailing content" by the object-literal refusal.

    Motivating regressions: fixtures 02 (`02_subsection_patches`),
    04 (`04_prose_override`), and 35 (`35_object_literal_replace`)
    all use `export const NAME = {...};` or `NAME = {...};` bodies.
    The initial version of the trailing-content refusal (2026-10-28g)
    saw the `;` and refused the splice, so `c: 3` (or `b: 2`) was
    silently dropped from the resulting file.
    """
    base = (
        "// foo.js — full print.\n"
        "\n"
        "export const foo = {\n"
        "  a: 1,\n"
        "  b: 2,\n"
        "};\n"
    )
    body = (
        "export const foo = {\n"
        "  a: 1,\n"
        "  b: 2,\n"
        "  c: 3,\n"
        "};\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "js"}
    result = _try_deterministic_patch(base, patch, path="foo.js")
    assert result is not None, "expected splice to fire"
    assert "c: 3" in result, (
        f"`c: 3` missing (trailing `;` refusal bug):\n{result}"
    )
    assert result.count("export const foo = {") == 1, (
        f"duplicate declaration introduced:\n{result}"
    )


def test_object_literal_splice_accepts_trailing_semicolon_plus_comment():
    """A trailing `;` followed by a comment still passes the refusal
    check — the comment is not "trailing content"."""
    base = (
        "const board = { a: 1 };\n"
    )
    body = (
        "board = {\n"
        "  a: 1,\n"
        "  b: 2,\n"
        "};  // updated\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "js"}
    result = _try_deterministic_patch(base, patch, path="board.js")
    assert result is not None, "expected splice to fire"
    assert "b: 2" in result
    assert result.count("const board") == 1


def test_object_literal_refuses_trailing_semicolon_plus_statement():
    """A trailing `;` followed by *code* (not a comment) still triggers
    the refusal — the code after `;` is real trailing content."""
    from deeppeek.recover.patch_dispatch import (
        _try_deterministic_patch,
    )
    base = (
        "def handle():\n"
        "    captured = {'msg': ''}\n"
        "    return captured\n"
    )
    body = (
        "captured = {'msg': '', 'errors': []};\n"
        "def on_progress(idx, res):\n"
        "    pass\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(base, patch, path="x.py")
    # Either multi-statement apply succeeded (both statements land)
    # or the whole attempt failed — but the object-literal splice on
    # its own must NOT have fired and silently dropped the def.
    if result is not None:
        assert "def on_progress" in result, (
            f"object-literal splice dropped the def:\n{result}"
        )
        assert "captured = " in result


# ---------------------------------------------------------------------------
# Fix 3 — duplicate-assignment pre-gate
# ---------------------------------------------------------------------------

def test_duplicate_assignment_gate_catches_single_line():
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
    old_content = "output_name = params.get('output_name') or None\n"
    patch = {
        "idx": 1,
        "body": 'output_name = params.get("output_name") or None\n'
                'return 0\n',
    }
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_skips_continuation():
    old_content = "obj = {\n  'a': 1,\n}\n"
    patch = {"idx": 1, "body": "obj = {"}
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_skips_calls():
    old_content = "do_thing();\n"
    patch = {"idx": 1, "body": "do_thing();"}
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_skips_equality():
    old_content = "if x == y:\n    pass\n"
    patch = {"idx": 1, "body": "x == y"}
    assert not _is_duplicate_assignment_insert(old_content, patch)


def test_duplicate_assignment_gate_none_body():
    assert not _is_duplicate_assignment_insert(
        "x = 1\n", {"idx": 1, "body": None})


def test_duplicate_assignment_gate_empty_body():
    assert not _is_duplicate_assignment_insert(
        "x = 1\n", {"idx": 1, "body": ""})


def test_duplicate_assignment_gate_missing_body_key():
    assert not _is_duplicate_assignment_insert("x = 1\n", {"idx": 1})


def test_duplicate_assignment_gate_end_to_end():
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
    assert rec.patches[0]["applied_by"] == "already-present"
    assert rec.patches[0].get("duplicate_assignment") is True
    count = rec.content.count('output_name = params.get("output_name")')
    assert count == 1


# ---------------------------------------------------------------------------
# Fix 5 — scope-aware anchor selection
# ---------------------------------------------------------------------------

def test_anchor_within_named_scope_resolves_unique():
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
        base, body, "In `handle_a`, after `do_thing()`:")
    assert result is not None
    a_pos = result.index("def handle_a")
    b_pos = result.index("def handle_b")
    insert_pos = result.index("do_after()")
    assert a_pos < insert_pos < b_pos


def test_anchor_ambiguous_within_named_scope_fails_closed():
    base = (
        'def handle_a():\n'
        '    do_thing()\n'
        '    do_thing()\n'
        '    return 1\n'
    )
    body = 'do_after()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body, "In `handle_a`, after `do_thing()`:")
    assert result is None


def test_anchor_no_named_scope_stays_unique_only():
    base = (
        'def handle():\n'
        '    e.promptEl.disabled = busy\n'
        '    return e\n'
    )
    body = 'guard()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body, "Add after `e.promptEl.disabled = busy`:")
    assert result is not None
    assert result.index("guard()") > result.index(
        "e.promptEl.disabled = busy")


def test_anchor_named_scope_not_in_base_falls_through():
    base = (
        'def other_scope():\n'
        '    do_thing()\n'
        '    return 1\n'
    )
    body = 'do_after()\nreturn result()\n'
    result = _try_insert_after_anchor(
        base, body, "In `handle_missing`, after `do_thing()`:")
    assert result is not None
    assert "do_after()" in result


def test_anchor_call_with_no_args_uses_statement_branch():
    base = (
        'def f():\n'
        '    do_thing()\n'
        '    return 1\n'
    )
    body = 'after_call()\ncleanup()\n'
    result = _try_insert_after_anchor(
        base, body, "After `do_thing()`:")
    assert result is not None
    assert "after_call()" in result
    assert result.index("after_call()") > result.index("do_thing()")


def test_dict_var_names_dashed_key_fallback():
    from deeppeek.recover.patch_misc_insert import _extract_dict_var_names
    names = _extract_dict_var_names(
        "In handlers/comfyui.py, find the `comfy_generate` command "
        "decorator. Add:",
        '"--output_rotation": {"type": "float", "default": 0.0,',
    )
    assert "args" in names


def test_dict_var_names_no_fallback_for_plain_key():
    from deeppeek.recover.patch_misc_insert import _extract_dict_var_names
    names = _extract_dict_var_names(
        "Add to the config:",
        '"existing_key": "value",',
    )
    assert "args" not in names


def test_dict_entry_with_dashed_key_placed_via_body_fallback():
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
    assert result is not None
    assert '"--output_rotation"' in result
    assert '"--timeout"' in result


def test_signature_replace_indented_method():
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
    assert result is not None
    assert "    def bar(self, a, b=0):" in result
    assert "    def bar(self, a):" not in result
    assert "def toplevel(a):" in result


def test_placeholder_marker_merged_into_docstring():
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
    merged = _try_merge_placeholder_in_scope(
        base, "paste_image", new_body)
    assert merged is not None
    assert "... (docstring unchanged) ..." not in merged
    assert "Decode a PNG and write it as a new layer." in merged
    assert "`rotation` is degrees" in merged
    assert "rotation=0.0" in merged


# ---------------------------------------------------------------------------
# 2026-10-28e — prior-insertions scope context
# ---------------------------------------------------------------------------

def test_prior_insertion_with_scope_context():
    base_body = (
        'def _comfy_generate_impl(self, params):\n'
        '    output_name = params.get("output_name") or None\n'
        '    return output_name\n'
        '\n'
        '\n'
        'def comfy_wait(self, params):\n'
        '    output_name = params.get("output_name") or None\n'
        '    return output_name\n'
        '\n'
        '\n'
        'def comfy_paste_last(self, params):\n'
        '    output_name = params.get("output_name") or None\n'
        '    return output_name\n'
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
    rec.patches = [
        {
            "idx": 1,
            "body": 'output_name = params.get("output_name") or None',
            "context_before":
                'In `_comfy_generate_impl`, extract it and pass it '
                'through. Find:',
            "fence_lang": "py",
        },
        {
            "idx": 2,
            "body": (
                'try:\n'
                '    output_rotation = float(\n'
                '        params.get("output_rotation") or 0.0)\n'
                'except (TypeError, ValueError):\n'
                '    output_rotation = 0.0'
            ),
            "context_before": "Add just after:",
            "fence_lang": "py",
        },
    ]
    blocks_by_idx = {0: base_block}
    pass6_apply_patches({"x.py": rec}, blocks_by_idx, None)

    assert rec.patches[0]["applied_by"] == "already-present"
    assert rec.patches[1]["applied_by"] == "deterministic", (
        f"idx=2 failed: {rec.patches[1].get('failure_reason')!r}")

    content = rec.content or ""
    assert "output_rotation = float(" in content
    target_fn = content.index("def _comfy_generate_impl")
    other_fn = content.index("def comfy_wait")
    rotation_pos = content.index("output_rotation = float(")
    assert target_fn < rotation_pos < other_fn
    assert content.count("output_rotation = float(") == 1


def test_prior_insertion_without_scope_still_refuses():
    base_body = (
        'def a():\n'
        '    target_line()\n'
        '    return 1\n'
        '\n'
        'def b():\n'
        '    target_line()\n'
        '    return 2\n'
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
    rec.patches = [
        {
            "idx": 1,
            "body": "target_line()",
            "context_before": "Find:",
            "fence_lang": "py",
        },
        {
            "idx": 2,
            "body": "do_after()\nreturn result()\n",
            "context_before": "Add just after:",
            "fence_lang": "py",
        },
    ]
    blocks_by_idx = {0: base_block}
    pass6_apply_patches({"x.py": rec}, blocks_by_idx, None)

    assert rec.patches[0]["applied_by"] == "already-present"
    assert rec.patches[1]["applied_by"] != "deterministic"
    assert rec.trust == "LOW"


# ---------------------------------------------------------------------------
# 2026-10-28e — prior-insertion safety gate (opener)
# ---------------------------------------------------------------------------

def test_prior_insertion_safety_rejects_opener_anchor():
    base_body = (
        'class Handler:\n'
        '\n'
        '    @command(\n'
        '        args={\n'
        '            "--timeout": {"type": "float"},\n'
        '        },\n'
        '    )\n'
        '    def handle(self, params):\n'
        '        output_name = params.get("output_name") or None\n'
        '        return output_name\n'
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
    rec.patches = [
        {
            "idx": 1,
            "body": '"--output_rotation": {"type": "float", "default": 0.0},\n',
            "context_before": "Add to the args dict:",
            "fence_lang": "py",
        },
        {
            "idx": 2,
            "body": (
                '        try:\n'
                '            output_rotation = float(\n'
                '                params.get("output_rotation") or 0.0)\n'
                '        except (TypeError, ValueError):\n'
                '            output_rotation = 0.0'
            ),
            "context_before": "Add just after:",
            "fence_lang": "py",
        },
    ]
    blocks_by_idx = {0: base_block}
    pass6_apply_patches({"x.py": rec}, blocks_by_idx, None)

    assert rec.patches[0]["applied_by"] == "deterministic"

    content = rec.content or ""
    args_start = content.find("args={")
    args_close = content.find("        },", args_start) if args_start >= 0 else -1
    if args_close > args_start:
        inside_args = content[args_start:args_close]
        assert "output_rotation = float(" not in inside_args, (
            f"try/except corrupted the args literal:\n{content}"
        )

    if rec.patches[1].get("applied_by") == "deterministic":
        import ast
        ast.parse(content)


# ---------------------------------------------------------------------------
# 2026-10-28f — prior-insertion scope-first ordering
# ---------------------------------------------------------------------------

def test_prior_insertion_scope_first_over_args_dict():
    base = (
        "class Handler:\n"
        "\n"
        "    @command(\n"
        "        args={\n"
        '            "--timeout": {"type": "float", "default": 55.0},\n'
        '            "--output_rotation": {"type": "float", "default": 0.0,\n'
        '                                  "help": "Degrees to rotate."},\n'
        "        },\n"
        "    )\n"
        "    def comfy_generate(self, params):\n"
        "        return self._comfy_generate_impl(params)\n"
        "\n"
        "    def _comfy_generate_impl(self, params):\n"
        '        output_name = params.get("output_name") or None\n'
        "        return output_name\n"
    )
    body = (
        "try:\n"
        '    output_rotation = float(\n'
        '        params.get("output_rotation") or 0.0)\n'
        "except (TypeError, ValueError):\n"
        "    output_rotation = 0.0"
    )
    prior_insertions = {
        'output_name = params.get("output_name") or None',
        '"--output_rotation": {"type": "float", "default": 0.0,',
        '"help": "Degrees to rotate."},',
    }
    prior_scopes = {
        'output_name = params.get("output_name") or None': "comfy_generate",
    }

    result = _try_insert_after_anchor(
        base, body, "Add just after:",
        prior_insertions=prior_insertions,
        prior_scopes=prior_scopes,
        path="x.py",
    )
    assert result is not None, "expected placement via scope-first ordering"
    impl_pos = result.find("def _comfy_generate_impl")
    try_pos = result.find("output_rotation = float(")
    assert impl_pos >= 0 and impl_pos < try_pos, (
        f"try block not placed inside _comfy_generate_impl:\n{result}"
    )
    args_start = result.find("args={")
    if args_start >= 0:
        args_end = result.find("        },", args_start)
        if args_end > args_start:
            inside = result[args_start:args_end]
            assert "output_rotation = float(" not in inside, (
                f"try block corrupted args dict:\n{result}"
            )


def test_prior_insertion_expression_context_refuses_closer():
    base = (
        "class Handler:\n"
        "\n"
        "    @command(\n"
        "        args={\n"
        '            "--timeout": {"type": "float", "default": 55.0},\n'
        '            "--output_rotation": {"type": "float", "default": 0.0,\n'
        '                                  "help": "Degrees to rotate."},\n'
        "        },\n"
        "    )\n"
        "    def handle(self, params):\n"
        '        return params.get("output_name")\n'
    )
    body = (
        "try:\n"
        '    output_rotation = float(\n'
        '        params.get("output_rotation") or 0.0)\n'
        "except (TypeError, ValueError):\n"
        "    output_rotation = 0.0"
    )
    prior_insertions = {
        '"--output_rotation": {"type": "float", "default": 0.0,',
        '"help": "Degrees to rotate."},',
    }
    prior_scopes: dict = {}

    result = _try_insert_after_anchor(
        base, body, "Add just after:",
        prior_insertions=prior_insertions,
        prior_scopes=prior_scopes,
        path="x.py",
    )
    if result is not None:
        args_start = result.find("args={")
        if args_start >= 0:
            args_end = result.find("        },", args_start)
            if args_end > args_start:
                inside = result[args_start:args_end]
                assert "output_rotation = float(" not in inside, (
                    f"try block landed inside args dict:\n{result}"
                )


# ---------------------------------------------------------------------------
# 2026-10-28f — already-present pre-check (duplicate-application guard)
# ---------------------------------------------------------------------------

def test_reprint_of_existing_block_skipped():
    base = (
        "def paste_image(doc, png, x, y):\n"
        '    """Decode a PNG and write it as a new layer.\n'
        "\n"
        "    `rotation` is degrees, positive = clockwise on screen.\n"
        "    Sub-pixel rounding is handled by _center_crop.\n"
        '    """\n'
        "    return doc\n"
    )
    body = (
        "def paste_image(doc, png, x, y):\n"
        '    """Decode a PNG and write it as a new layer.\n'
        "\n"
        "    `rotation` is degrees, positive = clockwise on screen.\n"
        "    Sub-pixel rounding is handled by _center_crop.\n"
        '    """\n'
        "    return doc\n"
    )
    patch = {
        "idx": 1,
        "body": body,
        "context_before": "",
        "fence_lang": "py",
    }
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is base, (
        f"expected unchanged base (already-present); got modified content")


def test_try_except_block_reprint_skipped():
    base = (
        "def handle_inpaint(payload):\n"
        "    try:\n"
        '        output_rotation = float(payload.get("output_rotation") or 0.0)\n'
        "    except (TypeError, ValueError):\n"
        "        output_rotation = 0.0\n"
        "    return output_rotation\n"
    )
    body = (
        "try:\n"
        '    output_rotation = float(payload.get("output_rotation") or 0.0)\n'
        "except (TypeError, ValueError):\n"
        "    output_rotation = 0.0"
    )
    patch = {
        "idx": 1,
        "body": body,
        "context_before": "",
        "fence_lang": "py",
    }
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is base


def test_whitespace_drift_scoped_replace_skipped():
    from deeppeek.recover.patch_scope import _apply_scoped
    base = (
        "<html>\n"
        "<body>\n"
        "    <details class=\"section\" open>\n"
        "          <summary>View</summary>\n"
        "          <div class=\"section-body\">\n"
        "            body content\n"
        "          </div>\n"
        "        </details>\n"
        "</body>\n"
        "</html>\n"
    )
    body = (
        "    <details class=\"section\" open>\n"
        "      <summary>View</summary>\n"
        "      <div class=\"section-body\">\n"
        "        body content\n"
        "      </div>\n"
        "    </details>"
    )
    result = _apply_scoped(base, "View", body)
    assert result is base


def test_scoped_replace_still_applies_when_scope_differs():
    from deeppeek.recover.patch_scope import _apply_scoped
    base = (
        "<details class=\"section\" open>\n"
        "    <summary>Inpainting</summary>\n"
        "    <label>Old label</label>\n"
        "</details>\n"
    )
    body = (
        "<details class=\"section\" open>\n"
        "    <summary>Inpainting</summary>\n"
        "    <label>New label</label>\n"
        "</details>"
    )
    result = _apply_scoped(base, "Inpainting", body)
    assert result is not None
    assert "New label" in result
    assert "Old label" not in result


def test_short_import_already_present_returns_base():
    base = (
        '"""Module."""\n'
        "import { board } from \"./pg_state.js\";\n"
        "const x = 1;\n"
    )
    body = 'import { board } from "./pg_state.js";'
    patch = {
        "idx": 1,
        "body": body,
        "context_before": "",
        "fence_lang": "js",
    }
    result = _try_deterministic_patch(base, patch, path="x.js")
    assert result is base


# ---------------------------------------------------------------------------
# 2026-10-28e — callee walk
# ---------------------------------------------------------------------------

def test_prior_insertion_callee_walk_lands_in_delegate():
    base_body = (
        'class Handler:\n'
        '    def comfy_generate(self, params):\n'
        '        return self._comfy_generate_impl(params)\n'
        '\n'
        '    def _comfy_generate_impl(self, params):\n'
        '        output_name = params.get("output_name") or None\n'
        '        return output_name\n'
        '\n'
        '    def comfy_wait(self, params):\n'
        '        output_name = params.get("output_name") or None\n'
        '        return output_name\n'
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
    rec.patches = [
        {
            "idx": 1,
            "body": 'output_name = params.get("output_name") or None',
            "context_before":
                "In `handlers/comfyui.py`, find the `comfy_generate` "
                "command decorator. Add:\n\nFind:",
            "fence_lang": "py",
        },
        {
            "idx": 2,
            "body": (
                'try:\n'
                '    output_rotation = float(\n'
                '        params.get("output_rotation") or 0.0)\n'
                'except (TypeError, ValueError):\n'
                '    output_rotation = 0.0'
            ),
            "context_before": "Add just after:",
            "fence_lang": "py",
        },
    ]
    blocks_by_idx = {0: base_block}
    pass6_apply_patches({"x.py": rec}, blocks_by_idx, None)

    assert rec.patches[0]["applied_by"] == "already-present"
    assert rec.patches[1]["applied_by"] == "deterministic", (
        f"callee walk failed: {rec.patches[1].get('failure_reason')!r}")

    content = rec.content or ""
    impl_pos = content.index("def _comfy_generate_impl")
    wait_pos = content.index("def comfy_wait")
    rotation_pos = content.index("output_rotation = float(")
    assert impl_pos < rotation_pos < wait_pos
    assert content.count("output_rotation = float(") == 1
    assert content.count("output_name = params.get(") == 2


# ---------------------------------------------------------------------------
# 2026-10-28g — H2 header reset on non-file heading
# ---------------------------------------------------------------------------

def test_h2_without_filename_resets_header():
    """An H2 that doesn't parse as a file header resets
    `current_header`, so subsequent fences don't inherit the previous
    section's file.  Motivating shape: `## Fix 4 — … (pg_state.py)`
    followed by `## Fix 5 — text input for rotation` (no file named),
    whose three fences actually target three different files
    (pg_base.py, pg_panel_scrubs.py, pg_panel_bindings.py).
    """
    from deeppeek.recover.extract import pass1_extract
    dump = (
        "## Fix 1 — mask encoding (`foo.py`)\n"
        "\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "\n"
        "## Fix 2 — some unrelated section\n"
        "\n"
        "```python\n"
        "y = 2\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 2
    assert blocks[0].header_path == "foo.py"
    assert blocks[0].is_orphan is False
    # Second H2 has no filename → header reset, block is orphan
    assert blocks[1].header_path is None
    assert blocks[1].is_orphan is True


def test_h3_preserves_header():
    """H3 subsections under a file H2 must keep the header."""
    from deeppeek.recover.extract import pass1_extract
    dump = (
        "## File: `foo.py`\n"
        "\n"
        "```python\n"
        "a = 1\n"
        "```\n"
        "\n"
        "### (a) Add a field\n"
        "\n"
        "```python\n"
        "b = 2\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 2
    assert all(b.header_path == "foo.py" for b in blocks)


# ---------------------------------------------------------------------------
# 2026-10-28g — multi-statement apply
# ---------------------------------------------------------------------------

def test_multi_statement_apply_object_literal_plus_def():
    """Body: `captured = {...}` + `def on_progress(...)`.

    Must apply both statements — object-literal splice for the
    assignment, indented-def replace for the nested function.  The
    pre-fix bug was that the object-literal splice silently dropped
    the def, and the whole patch then failed with "no matching base
    declaration" (Fix 3's exact shape).
    """
    base = (
        "def handle(payload):\n"
        "    batch_id = 'abc'\n"
        "    captured = {'msg': ''}\n"
        "\n"
        "    def on_progress(idx, res):\n"
        "        captured['msg'] = res.get('message', '')\n"
        "\n"
        "    return captured\n"
    )
    body = (
        "captured = {'msg': '', 'errors': []}\n"
        "\n"
        "def on_progress(idx, res):\n"
        "    msg = res.get('message', '')\n"
        "    if msg:\n"
        "        captured['errors'].append(msg)\n"
        "    captured['msg'] = msg\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is not None, "multi-statement apply should fire"
    assert "'errors': []" in result
    assert "captured['errors'].append(msg)" in result
    # Original `on_progress` body must be gone (not nested)
    assert result.count("def on_progress") == 1


def test_object_literal_refuses_trailing_def():
    """`x = {...}` followed by `def foo(...)` must not be spliced.

    The multi-statement hook should fire first and either apply both
    statements correctly, or bail entirely.  What must NOT happen is
    the object-literal splice firing on its own and silently dropping
    the trailing def.
    """
    base = (
        "def handle():\n"
        "    captured = {'msg': ''}\n"
        "    return captured\n"
    )
    body = (
        "captured = {'msg': '', 'errors': []}\n"
        "\n"
        "def on_progress(idx, res):\n"
        "    pass\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(base, patch, path="x.py")
    # Either multi-statement apply succeeded (both statements land)
    # or the whole attempt failed — but the object-literal splice on
    # its own must NOT have fired and silently dropped the def.
    if result is not None:
        assert "def on_progress" in result
        assert "captured = " in result


# ---------------------------------------------------------------------------
# 2026-10-28g — indented def/class replace
# ---------------------------------------------------------------------------

def test_indented_def_replace_alone():
    """Body that's just a nested `def name(...)` — replace in place.

    The pre-fix behaviour: scoped-replace declines (no top-level
    declaration with that name); AST anchor would then insert the
    new def at the end of the enclosing function — producing TWO
    copies of `on_progress`.
    """
    base = (
        "def outer():\n"
        "    def helper(a):\n"
        "        return a + 1\n"
        "    return helper(10)\n"
    )
    body = (
        "def helper(a):\n"
        "    return a + 100\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is not None
    assert "return a + 100" in result
    assert "return a + 1\n" not in result
    # Still nested inside `outer`
    assert "def outer" in result
    lines = result.split("\n")
    outer_idx = next(i for i, l in enumerate(lines)
                     if l.startswith("def outer"))
    helper_idx = next(i for i, l in enumerate(lines)
                      if "def helper" in l)
    assert helper_idx > outer_idx
    # helper is indented
    assert lines[helper_idx].startswith("    ")


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
        ("dict entry scopes to prose function",
         test_dict_entry_scopes_to_prose_function),
        ("dict entry scoped already present returns base",
         test_dict_entry_scoped_already_present_returns_base),
        # 2026-10-28g — object-literal splice whitespace preservation
        ("object_literal_splice preserves no space before brace",
         test_object_literal_splice_preserves_no_space_before_brace),
        ("object_literal_splice preserves space before brace",
         test_object_literal_splice_preserves_space_before_brace),
        # 2026-10-28h — object-literal trailing-`;` acceptance
        ("object_literal_splice accepts trailing semicolon",
         test_object_literal_splice_accepts_trailing_semicolon),
        ("object_literal_splice accepts trailing semicolon plus comment",
         test_object_literal_splice_accepts_trailing_semicolon_plus_comment),
        ("object_literal refuses trailing semicolon plus statement",
         test_object_literal_refuses_trailing_semicolon_plus_statement),
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
        # 2026-10-28e — prior-insertion scope context
        ("prior insertion with scope context",
         test_prior_insertion_with_scope_context),
        ("prior insertion without scope still refuses",
         test_prior_insertion_without_scope_still_refuses),
        # 2026-10-28e — prior-insertion safety gate
        ("prior insertion safety rejects opener anchor",
         test_prior_insertion_safety_rejects_opener_anchor),
        # 2026-10-28e — callee walk
        ("prior insertion callee walk lands in delegate",
         test_prior_insertion_callee_walk_lands_in_delegate),
        # 2026-10-28f — scope-first ordering + expression-context refusal
        ("prior insertion scope first over args dict",
         test_prior_insertion_scope_first_over_args_dict),
        ("prior insertion expression context refuses closer",
         test_prior_insertion_expression_context_refuses_closer),
        # 2026-10-28f — already-present pre-check
        ("reprint of existing block skipped",
         test_reprint_of_existing_block_skipped),
        ("try except block reprint skipped",
         test_try_except_block_reprint_skipped),
        ("whitespace drift scoped replace skipped",
         test_whitespace_drift_scoped_replace_skipped),
        ("scoped replace still applies when scope differs",
         test_scoped_replace_still_applies_when_scope_differs),
        ("short import already present returns base",
         test_short_import_already_present_returns_base),
        # 2026-10-28g — H2 header reset
        ("h2 without filename resets header",
         test_h2_without_filename_resets_header),
        ("h3 preserves header",
         test_h3_preserves_header),
        # 2026-10-28g — multi-statement apply
        ("multi statement apply object literal plus def",
         test_multi_statement_apply_object_literal_plus_def),
        ("object literal refuses trailing def",
         test_object_literal_refuses_trailing_def),
        # 2026-10-28g — indented def/class replace
        ("indented def replace alone",
         test_indented_def_replace_alone),
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
