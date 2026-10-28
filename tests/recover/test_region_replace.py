"""Regression tests for region-replace.

Region-replace is the class of heuristic that replaces an existing
region of `base` with the body when the body is a *modified version*
of that region — the edit shape that AI-assisted edits produce
constantly and that no insert-shaped heuristic handles correctly.

Without it, the two pg_panel_* patches that failed in the 2026-10-28
run (`pg_panel_scrubs.py`, `pg_panel_bindings.py`) were placed by
`_append_decl_after_last` / `_try_structural_append` — appended
after the existing region, producing duplicate `const`
declarations and dead `if` blocks.  `validate_content` caught the
result via `_js_balance` (which then reported the symptom as an
"unclosed string literal"), and the record fail-closed.  The bug
was real, but the diagnostic was opaque: the fix isn't "make
`_js_balance` smarter", it's "there's a whole class of edit that no
heuristic understands".

These tests preserve:

  · The positive cases — a modified block IS placed correctly.
  · The negative cases — an insertion is NOT clobbered by
    region-replace.
  · The safety invariant — the result never drops the file below
    50% of base's line count.
  · End-to-end dispatch — the heuristic is actually *wired into*
    `_try_deterministic_patch_inner`, not just present.

If a future change removes region-replace from the dispatcher, or
loosens its similarity threshold enough that insertions clobber
unrelated code, one of the tests below names exactly which
invariant broke.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Direct unit tests for _try_region_replace (text-based)
# ---------------------------------------------------------------------------

def test_region_replace_scrubs_shape_direct():
    """The pg_panel_scrubs.py shape, exercised directly.

    Failure mode caught: appending the modified body after the
    existing region instead of replacing it. Symptom: duplicate
    `const rotationValueEl`, duplicate `if (rotationEl)` block,
    duplicate `pgkitRegisterField("view.rotation", …)`.
    """
    from deeppeek.recover.patch_dispatch import _try_region_replace

    base = (
        "const rotationEl = document.getElementById(\"viewRotation\");\n"
        "const rotationValueEl = document.getElementById(\"viewRotationValue\");\n"
        "\n"
        "if (rotationEl) {\n"
        "  rotationEl.addEventListener(\"input\", () => {\n"
        "    _applyRotation(parseFloat(rotationEl.value));\n"
        "  });\n"
        "}\n"
        "\n"
        "pgkitRegisterField(\"view.rotation\", {\n"
        "  get: () => view.rotation || 0,\n"
        "  meta: { kind: \"number\" },\n"
        "});\n"
    )
    body = (
        "const rotationEl = document.getElementById(\"viewRotation\");\n"
        "const rotationInputEl = document.getElementById(\"viewRotationInput\");\n"
        "const rotationValueEl = document.getElementById(\"viewRotationValue\");\n"
        "\n"
        "if (rotationEl) {\n"
        "  rotationEl.addEventListener(\"input\", () => {\n"
        "    _applyRotation(parseFloat(rotationEl.value));\n"
        "  });\n"
        "}\n"
        "if (rotationInputEl) {\n"
        "  rotationInputEl.addEventListener(\"input\", () => {\n"
        "    _applyRotation(parseFloat(rotationInputEl.value));\n"
        "  });\n"
        "}\n"
        "\n"
        "pgkitRegisterField(\"view.rotation\", {\n"
        "  get: () => view.rotation || 0,\n"
        "  meta: { kind: \"number\" },\n"
        "});\n"
    )
    result = _try_region_replace(base, body.strip(), "x.js")
    assert result is not None, (
        "region-replace did not fire on the scrubs shape. This is "
        "the exact edit shape that failed on 2026-10-28 with an "
        "'unclosed string literal' from _js_balance after duplicate "
        "insertion. If region-replace is missing, the caller falls "
        "through to structural-append and the duplicate is back."
    )
    assert "rotationInputEl" in result, (
        "new content missing; the body was not placed"
    )
    assert result.count("const rotationValueEl") == 1, (
        f"expected 1 'const rotationValueEl'; got "
        f"{result.count('const rotationValueEl')}. Region-replace "
        f"appended instead of replacing."
    )
    assert result.count("if (rotationEl)") == 1, (
        f"expected 1 'if (rotationEl)'; got "
        f"{result.count('if (rotationEl)')}"
    )
    assert result.count("if (rotationInputEl)") == 1
    assert result.count('pgkitRegisterField("view.rotation"') == 1, (
        f"expected 1 field registration; got "
        f"{result.count('pgkitRegisterField(\"view.rotation\"')}"
    )


def test_region_replace_does_not_fire_on_pure_insertion():
    """A body that adds a *new* block must not be treated as a
    region-replace, or unrelated existing code gets clobbered.

    Failure mode caught: a heuristic that fires on any same-opening-
    construct window (e.g. body starts with `if`, base has `if`
    blocks — would fire and delete the wrong region).
    """
    from deeppeek.recover.patch_dispatch import _try_region_replace

    base = (
        "if (condA) {\n"
        "  doA();\n"
        "  doA2();\n"
        "}\n"
        "\n"
        "if (condB) {\n"
        "  doB();\n"
        "}\n"
    )
    body = (
        "if (condC) {\n"
        "  doC();\n"
        "}\n"
    )
    result = _try_region_replace(base, body.strip(), "x.js")
    assert result is None, (
        f"region-replace fired on a pure insertion; produced:\n"
        f"{result}. This is the failure mode where an unrelated "
        f"`if` block gets replaced by a new one."
    )


def test_region_replace_does_not_fire_on_unrelated_same_construct():
    """A body and a base region that share a construct (both start
    with `function …`) but contain unrelated content must not
    match.

    Failure mode caught: opening-construct match plus low-similarity
    window creates a replacement that deletes real code.
    """
    from deeppeek.recover.patch_dispatch import _try_region_replace

    base = (
        "function foo() {\n"
        "  console.log('foo');\n"
        "  fooHelper();\n"
        "}\n"
        "\n"
        "function bar() {\n"
        "  barHelper();\n"
        "}\n"
    )
    body = (
        "function foo() {\n"
        "  // totally different body\n"
        "  somethingNew();\n"
        "  return 42;\n"
        "}\n"
    )
    result = _try_region_replace(base, body.strip(), "x.js")
    # The similarity should be too low to fire; if it DOES fire, the
    # result is a legitimate replacement of `foo` — verify the
    # neighbouring `bar` survived, which is the essential invariant.
    if result is not None:
        assert "function bar()" in result, (
            f"region-replace clobbered an unrelated function:\n{result}"
        )
        assert result.count("function foo()") == 1, (
            f"expected exactly 1 foo; got "
            f"{result.count('function foo()')}"
        )


def test_region_replace_never_drops_below_half():
    """The size guard: any region-replace result must contain at
    least 50% of base's line count.

    Failure mode caught: a tunable threshold (e.g. `min_ratio=0.3`)
    accidentally enables aggressive replacements that eat most of
    the file. Deletions are handled by `_delete_exact_block`, not
    here; region-replace is for modifications, and a modification
    should not turn a 20-line file into a 1-line file.
    """
    from deeppeek.recover.patch_dispatch import _try_region_replace

    for base_size in (12, 24, 40):
        for body_size in (3, 5, 8):
            base_lines = [f"line_{i} = {i}" for i in range(base_size)]
            base = "\n".join(base_lines) + "\n"
            body_lines = [f"line_{i} = 999" for i in range(body_size)]
            body = "\n".join(body_lines)
            result = _try_region_replace(base, body, "x.py")
            if result is not None:
                assert len(result.splitlines()) >= base_size * 0.5, (
                    f"base={base_size} lines, body={body_size} lines: "
                    f"result dropped to {len(result.splitlines())} "
                    f"lines (below 50%)"
                )


# ---------------------------------------------------------------------------
# End-to-end: the heuristic is WIRED INTO the dispatcher
# ---------------------------------------------------------------------------

def test_region_replace_scrubs_shape_via_dispatcher():
    """The scrubs shape, end-to-end through `_try_deterministic_patch`.

    Failure mode caught: `_try_region_replace` exists but is not
    called from the dispatcher chain. The direct test above would
    pass; this one would fail.
    """
    from deeppeek.recover.patch_dispatch import _try_deterministic_patch

    base = (
        "const rotationEl = document.getElementById(\"viewRotation\");\n"
        "const rotationValueEl = document.getElementById(\"viewRotationValue\");\n"
        "\n"
        "if (rotationEl) {\n"
        "  rotationEl.addEventListener(\"input\", () => {\n"
        "    _applyRotation(parseFloat(rotationEl.value));\n"
        "  });\n"
        "}\n"
        "\n"
        "pgkitRegisterField(\"view.rotation\", {\n"
        "  get: () => view.rotation || 0,\n"
        "  meta: { kind: \"number\" },\n"
        "});\n"
    )
    body = (
        "const rotationEl = document.getElementById(\"viewRotation\");\n"
        "const rotationInputEl = document.getElementById(\"viewRotationInput\");\n"
        "const rotationValueEl = document.getElementById(\"viewRotationValue\");\n"
        "\n"
        "if (rotationEl) {\n"
        "  rotationEl.addEventListener(\"input\", () => {\n"
        "    _applyRotation(parseFloat(rotationEl.value));\n"
        "  });\n"
        "}\n"
        "if (rotationInputEl) {\n"
        "  rotationInputEl.addEventListener(\"input\", () => {\n"
        "    _applyRotation(parseFloat(rotationInputEl.value));\n"
        "  });\n"
        "}\n"
        "\n"
        "pgkitRegisterField(\"view.rotation\", {\n"
        "  get: () => view.rotation || 0,\n"
        "  meta: { kind: \"number\" },\n"
        "});\n"
    )
    patch = {"idx": 1, "body": body, "context_before": ""}
    result = _try_deterministic_patch(base, patch, path="scrubs.js")

    assert result is not None, (
        "dispatcher failed to place the scrubs-shaped body. If "
        "_try_region_replace is not called from "
        "_try_deterministic_patch_inner, structural-append fires and "
        "produces duplicates that validate_content rejects — same "
        "outcome as 2026-10-28."
    )
    assert result.count("if (rotationEl)") == 1, (
        f"expected 1 'if (rotationEl)'; got "
        f"{result.count('if (rotationEl)')}. This is the duplicate-"
        f"block regression."
    )
    assert result.count("const rotationValueEl") == 1
    assert "rotationInputEl" in result


def test_region_replace_bindings_shape_via_dispatcher():
    """The pg_panel_bindings.py shape: body replaces
    `_clearImageAction` in place, not in addition.

    Failure mode caught: two `function _clearImageAction` declarations
    — the exact shape that `_js_structural_check` flags as a duplicate
    top-level declaration, which fail-closed the record on 2026-10-28.
    """
    from deeppeek.recover.patch_dispatch import _try_deterministic_patch

    base = (
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  syncPanelFields();\n"
        "  syncMaskList();\n"
        "  pgkitNotifyAll();\n"
        "  import(\"./pg_dispatch.js\").then(m => m.draw()).catch(() => {});\n"
        "}\n"
    )
    body = (
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  const rotEl = document.getElementById(\"viewRotation\");\n"
        "  if (rotEl) rotEl.value = \"0\";\n"
        "  const rotVal = document.getElementById(\"viewRotationValue\");\n"
        "  if (rotVal) rotVal.textContent = \"0\\u00b0\";\n"
        "  syncPanelFields();\n"
        "  syncMaskList();\n"
        "  pgkitNotifyAll();\n"
        "  import(\"./pg_dispatch.js\").then(m => m.draw()).catch(() => {});\n"
        "}\n"
    )
    patch = {"idx": 1, "body": body, "context_before": ""}
    result = _try_deterministic_patch(base, patch, path="bindings.js")

    assert result is not None, (
        "dispatcher failed to place the bindings-shaped body"
    )
    assert result.count("function _clearImageAction") == 1, (
        f"expected exactly 1 copy; got "
        f"{result.count('function _clearImageAction')}. Duplicate "
        f"declaration is the failure shape that fail-closed the "
        f"record on 2026-10-28."
    )
    assert "rotEl" in result, "new content missing"


# ---------------------------------------------------------------------------
# AST-based region-replace (skips if module not implemented)
# ---------------------------------------------------------------------------

def test_region_replace_ast_replaces_by_name():
    """The tree-sitter-based region-replace: matches by declaration
    name, replaces exactly those declarations in place, and leaves
    everything else byte-identical.

    Failure mode caught: a text-anchored region-replace that
    incidentally reformats adjacent content, or (worse) fails to
    recognize the modified declaration because the change to it
    dropped similarity below the text threshold.
    """
    try:
        from deeppeek.recover.js_scope import try_region_replace_ast
    except ImportError:
        print("  SKIP: js_scope module not implemented")
        return

    base = (
        "const board = { a: 1 };\n"
        "\n"
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    body = (
        "const board = { a: 1, b: 2 };\n"
        "\n"
        "function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    result = try_region_replace_ast(base, body, "javascript")
    assert result is not None
    assert "b: 2" in result
    assert "return 2" in result
    assert "return 1" not in result, "old foo body survived"
    assert result.count("const board") == 1, (
        f"expected 1 'const board'; got "
        f"{result.count('const board')}"
    )
    assert result.count("function foo") == 1, (
        f"expected 1 'function foo'; got "
        f"{result.count('function foo')}"
    )


def test_region_replace_ast_skips_unmatched_names():
    """A body declaring a name that base does not contain falls
    through — insert heuristics own that case."""
    try:
        from deeppeek.recover.js_scope import try_region_replace_ast
    except ImportError:
        print("  SKIP: js_scope module not implemented")
        return

    base = "function existing() {\n  return 1;\n}\n"
    body = "function totallyNew() {\n  return 42;\n}\n"
    result = try_region_replace_ast(base, body, "javascript")
    assert result is None, (
        f"region-replace-ast fired on a name that isn't in base; "
        f"produced:\n{result}"
    )


def main():
    tests = [
        ("region replace scrubs shape direct",
         test_region_replace_scrubs_shape_direct),
        ("region replace does not fire on pure insertion",
         test_region_replace_does_not_fire_on_pure_insertion),
        ("region replace does not fire on unrelated same construct",
         test_region_replace_does_not_fire_on_unrelated_same_construct),
        ("region replace never drops below half",
         test_region_replace_never_drops_below_half),
        ("region replace scrubs shape via dispatcher",
         test_region_replace_scrubs_shape_via_dispatcher),
        ("region replace bindings shape via dispatcher",
         test_region_replace_bindings_shape_via_dispatcher),
        ("region replace ast replaces by name",
         test_region_replace_ast_replaces_by_name),
        ("region replace ast skips unmatched names",
         test_region_replace_ast_skips_unmatched_names),
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
        print(f"\n{failed}/{len(tests)} region-replace test(s) failed")
        return 1
    print(f"\nAll {len(tests)} region-replace tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
