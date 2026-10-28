"""Regression tests for the apply-heuristics fixes added alongside
reconciliation pass 8.7b.

  · Fix 1 (2026-10-28b): dict-key grammar widened to accept
    `--flag` / `-f` / `dotted.key` keys.

  · Fix 3 (2026-10-28b): duplicate-assignment pre-gate.

  · Fix 5 (2026-10-28b): scope-aware anchor selection.

  · Call-only-anchor fallthrough (2026-10-28c).

  · Prior-insertions scope context (2026-10-28e).

  · Prior-insertions safety gate (2026-10-28e): the prior-insertion
    fallback refuses anchors that open unclosed blocks.

  · Callee walk for scope disambiguation (2026-10-28e).

  · Scope-first ordering + expression-context refusal (2026-10-28f):
    the prior-insertion fallback tries scoped anchors first, and
    refuses unscoped anchors inside unclosed brackets in `.py` files.
    Motivating: comfyui.py idx=97 in production — the fallback
    tried an args-dict `"help": "..."},` closer (safe per
    `_anchor_line_is_safe`) and put the try/except inside
    `@command(args={...})`.

  · Already-present pre-check (2026-10-28f):
    `_try_deterministic_patch_inner` and `_apply_scoped_single` refuse
    patches whose body is substantially already in base — the fix for
    the four re-run-duplication bugs (docstring paragraph doubled in
    comfyui_krita.py, try/except doubled in inpaint_server.py,
    whitespace drift on the View `<details>` in pg_base.py, import
    line moved mid-file in pg_panel_scrubs.py).
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
    """`Find:` block already-present in 3 functions + `Add just after:`
    block placed into the correct scope by prior-line scope context.
    """
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
    """Sanity: without a scope in the Find block's prose (and no
    callee to walk), the prior-insertion fallback correctly refuses a
    repeated anchor.
    """
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
    """A prior line that opens an unclosed block must not be used as
    an anchor.
    """
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
    """When both a scoped anchor (the Find line) and unscoped anchors
    (args-dict entries from a previous patch) are in `prior_insertions`,
    the scoped anchor must be tried first.

    Motivating (comfyui.py idx=97, production): idx=95 landed an args
    entry inside `@command(args={...})`.  Without scope-first ordering,
    the fallback might pick the entry's `"help": "..."}` closer (safe
    per `_anchor_line_is_safe` but still inside the args dict) as the
    anchor.  With scope-first + expression-context refusal, the scoped
    `output_name` (recorded with `comfy_generate`) resolves via
    `_anchor_within_callee` to `_comfy_generate_impl`, which is the
    correct target.
    """
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

    # The scoped anchor (from the Find block's broader prose) and
    # the unscoped args-dict entry lines (from the prior patch).
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
    # Not inside the args dict
    args_start = result.find("args={")
    if args_start >= 0:
        args_end = result.find("        },", args_start)
        if args_end > args_start:
            inside = result[args_start:args_end]
            assert "output_rotation = float(" not in inside, (
                f"try block corrupted args dict:\n{result}"
            )


def test_prior_insertion_expression_context_refuses_closer():
    """An unscoped prior anchor whose position is inside an unclosed
    bracket group must be refused, even when the anchor line itself is
    "safe" (closes but doesn't open).

    Motivating: comfyui.py idx=97 in production — a `"help": "..."},`
    closer inside `@command(args={...})` is safe per
    `_anchor_line_is_safe` (opens=0, closes=1) but inserting after it
    lands inside the args dict.  The expression-context refusal in
    `_try_insert_after_anchor` catches this.
    """
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
    # Only unscoped candidates (no scope in prior_scopes).
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
        # If a placement was found, it must NOT be inside args={...}.
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
    """Re-applying a body that is already fully present in base
    returns base (no change) — the outer loop marks it already-present.

    Motivating: bugs 1-3 in the wild (docstring paragraph duplicated
    in comfyui_krita.py, try/except duplicated in inpaint_server.py,
    whitespace drift on View `<details>` in pg_base.py).
    """
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
    """The inpaint_server.py shape: `try: output_rotation = ...`
    re-applied on a base that already has it.
    """
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
    assert result is base, (
        f"expected unchanged base; got modified content:\n{result!r}"
    )


def test_whitespace_drift_scoped_replace_skipped():
    """The pg_base.py View `<details>` shape: a scoped-replace whose
    reindented body is token-equivalent to the existing scope.
    """
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
    # Body has 6-space indent vs base's 10-space — the reindented
    # body is token-equivalent to the base scope.
    body = (
        "    <details class=\"section\" open>\n"
        "      <summary>View</summary>\n"
        "      <div class=\"section-body\">\n"
        "        body content\n"
        "      </div>\n"
        "    </details>"
    )
    result = _apply_scoped(base, "View", body)
    assert result is base, (
        f"expected unchanged base (token-equivalent scope); "
        f"got modified:\n{result!r}"
    )


def test_scoped_replace_still_applies_when_scope_differs():
    """Sanity: a legitimate scoped-replace (real content change) still
    applies — the 0.99 token threshold isn't too aggressive.
    """
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
    """Short-body branch: a 1-line import already in base returns
    base (already-present), not a duplicate.

    Motivating (pg_panel_scrubs.py, production): the `import
    { board, view, _applyRotation } from "./pg_state.js"` line was
    re-applied mid-file.
    """
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
    assert result is base, (
        f"expected unchanged base (import already present); "
        f"got:\n{result!r}"
    )


# ---------------------------------------------------------------------------
# 2026-10-28e — callee walk
# ---------------------------------------------------------------------------

def test_prior_insertion_callee_walk_lands_in_delegate():
    """`Find:` block already-present twice + scope names the public
    method; the anchor lives in a helper the public method calls.
    """
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
    assert impl_pos < rotation_pos < wait_pos, (
        f"expected try/except inside _comfy_generate_impl, got:\n{content}"
    )
    assert content.count("output_rotation = float(") == 1
    assert content.count("output_name = params.get(") == 2


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
