"""Tests for H2-derived `scope_hint` propagation through pass 6.

Motivating case: block 634 in the 2026-10-28 run.  The fence's own
prose ("And in every failure return:") names no scope; the enclosing
H2 ("## Fix 3 — surface per-command errors in `handle_inpaint`") does.

Before the 2026-11-05 fix, `scope_hint` was attached to the Block by
pass 1's extractor, read by pass 4's scope-name attribution — but
lost by `pass6_apply_patches`, which built a patch dict containing
only `{idx, body, context_before, fence_lang}`.  `_try_insert_into_
named_scope` had nothing to insert into, the block orphaned, and it
went to the residual as `orphan_unattributed`.

The fix touches three places, all tested here:

  1. `pass6_apply_patches` copies `block.scope_hint` onto the patch
     dict it builds.
  2. `_try_deterministic_patch_inner` passes it through to
     `_try_insert_into_named_scope` (both call sites).
  3. `_try_insert_into_named_scope` accepts an optional `scope_hint`
     parameter and uses it when prose names no scope.  Prose continues
     to win when it names a scope (the fence's own context is more
     specific than the enclosing section's).
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord


def _pre_block(path, body, idx=0):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _orphan_block(body, idx, kind="partial", scope_hint=""):
    return Block(
        idx=idx, header_path=None, modifier=None, fence_lang="py",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=True, is_code_file=True, kind=kind,
        scope_hint=scope_hint,
        scope_name=scope_hint,
        attributed_to=None, attributed_by=None,
    )


# ---------------------------------------------------------------------------
# _try_insert_into_named_scope accepts scope_hint
# ---------------------------------------------------------------------------

def test_insert_into_named_scope_uses_prose():
    """Prose-named scope is used when present."""
    from deeppeek.recover.patch_scope_insert import (
        _try_insert_into_named_scope,
    )
    base = (
        "def handle_thing(payload):\n"
        "    name = payload.get('name', '')\n"
        "    return {'name': name}\n"
    )
    body = (
        "    try:\n"
        "        extra = float(payload.get('extra') or 0.0)\n"
        "    except (TypeError, ValueError):\n"
        "        extra = 0.0\n"
    )
    result = _try_insert_into_named_scope(
        base, body, "In `handle_thing`, add:",
        path="x.py",
    )
    assert result is not None, "prose-named scope should fire"
    assert "extra = float(" in result
    assert "def handle_thing" in result


def test_insert_into_named_scope_uses_scope_hint_when_prose_silent():
    """Block 634's shape: prose is scope-silent, but scope_hint from
    the enclosing H2 names the target."""
    from deeppeek.recover.patch_scope_insert import (
        _try_insert_into_named_scope,
    )
    base = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    return captured\n"
    )
    body = (
        "    try:\n"
        "        detail = payload['detail']\n"
        "    except KeyError:\n"
        "        detail = ''\n"
    )
    result = _try_insert_into_named_scope(
        base, body,
        "And in every failure return:",  # scope-silent prose
        path="x.py",
        scope_hint="handle_inpaint",
    )
    assert result is not None, (
        "scope_hint fallback should fire when prose is silent"
    )
    assert "detail = payload" in result
    assert "def handle_inpaint" in result


def test_insert_into_named_scope_prose_wins_over_scope_hint():
    """When both are present, prose wins — the fence's own context is
    more specific than the enclosing section's."""
    from deeppeek.recover.patch_scope_insert import (
        _try_insert_into_named_scope,
    )
    base = (
        "def handle_a(payload):\n"
        "    return 1\n"
        "\n"
        "def handle_b(payload):\n"
        "    return 2\n"
    )
    body = "    extra = 1\n"
    result = _try_insert_into_named_scope(
        base, body,
        "In `handle_b`, add:",
        path="x.py",
        scope_hint="handle_a",
    )
    assert result is not None
    # `extra = 1` should be inside handle_b, not handle_a.
    a_pos = result.index("def handle_a")
    b_pos = result.index("def handle_b")
    extra_pos = result.index("extra = 1")
    assert b_pos < extra_pos, (
        f"`extra = 1` should be in handle_b, not handle_a:\n{result}"
    )


def test_insert_into_named_scope_missing_hint_and_prose():
    """Neither prose nor scope_hint resolves the scope — return None
    (fail-closed, as before)."""
    from deeppeek.recover.patch_scope_insert import (
        _try_insert_into_named_scope,
    )
    base = "def handle():\n    pass\n"
    body = "    extra = 1\n"
    result = _try_insert_into_named_scope(
        base, body, "Add:",
        path="x.py",
    )
    assert result is None


# ---------------------------------------------------------------------------
# pass6_apply_patches copies scope_hint onto patch dict
# ---------------------------------------------------------------------------

def test_pass6_patch_dict_includes_scope_hint():
    """The patch dict built by `pass6_apply_patches` carries
    `scope_hint` from the source Block."""
    from deeppeek.recover.apply_patches import pass6_apply_patches
    base_body = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    return captured\n"
    )
    base_block = _pre_block("inpaint_server.py", base_body, idx=0)
    orphan = _orphan_block(
        body=(
            "    try:\n"
            "        detail = payload['detail']\n"
            "    except KeyError:\n"
            "        detail = ''\n"
        ),
        idx=1,
        kind="partial",
        scope_hint="handle_inpaint",
    )
    orphan.attributed_to = "inpaint_server.py"
    orphan.attributed_by = "prose"

    rec = FileRecord(path="inpaint_server.py", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.chosen_full_idx = 0
    rec.preexisting_base = True
    rec.source_blocks = [0, 1]
    rec.patches = [{
        "idx": 1,
        "body": orphan.body,
        "context_before": "And in every failure return:",
        "fence_lang": "py",
        "scope_hint": orphan.scope_hint,
    }]

    pass6_apply_patches(
        {"inpaint_server.py": rec},
        {0: base_block, 1: orphan},
        None,
    )
    # Patch should have applied successfully via scope_hint fallback.
    assert rec.patches[0]["applied_by"] == "deterministic", (
        f"expected deterministic application; got "
        f"{rec.patches[0].get('applied_by')!r}; "
        f"reason: {rec.patches[0].get('failure_reason')!r}"
    )
    assert "detail = payload" in rec.content


# ---------------------------------------------------------------------------
# End-to-end: block 634 shape via _try_deterministic_patch
# ---------------------------------------------------------------------------

def test_deterministic_patch_passes_scope_hint_through():
    """The dispatcher must pass `scope_hint` from the patch dict to
    `_try_insert_into_named_scope`.  Without this thread, block 634's
    shape silently fails."""
    from deeppeek.recover.patch_dispatch import _try_deterministic_patch
    base = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    return captured\n"
    )
    patch = {
        "idx": 1,
        "body": (
            "    try:\n"
            "        detail = payload['detail']\n"
            "    except KeyError:\n"
            "        detail = ''\n"
        ),
        "context_before": "And in every failure return:",
        "fence_lang": "py",
        "scope_hint": "handle_inpaint",
    }
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is not None, (
        "scope_hint must reach _try_insert_into_named_scope; "
        "without it, this shape fails closed"
    )
    assert "detail = payload" in result
    assert "def handle_inpaint" in result


def test_deterministic_patch_prefers_prose_over_scope_hint():
    """When prose names a scope, that scope wins even if scope_hint
    points elsewhere."""
    from deeppeek.recover.patch_dispatch import _try_deterministic_patch
    base = (
        "def handle_a(payload):\n"
        "    return 1\n"
        "\n"
        "def handle_b(payload):\n"
        "    return 2\n"
    )
    patch = {
        "idx": 1,
        "body": "    extra = 1\n",
        "context_before": "In `handle_b`, add:",
        "fence_lang": "py",
        "scope_hint": "handle_a",  # from the enclosing H2
    }
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is not None
    b_pos = result.index("def handle_b")
    extra_pos = result.index("extra = 1")
    assert extra_pos > b_pos, (
        f"expected `extra = 1` in handle_b; got:\n{result}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        ("insert into named scope uses prose",
         test_insert_into_named_scope_uses_prose),
        ("insert into named scope uses scope_hint when prose silent",
         test_insert_into_named_scope_uses_scope_hint_when_prose_silent),
        ("insert into named scope prose wins over scope_hint",
         test_insert_into_named_scope_prose_wins_over_scope_hint),
        ("insert into named scope missing hint and prose",
         test_insert_into_named_scope_missing_hint_and_prose),
        ("pass6 patch dict includes scope_hint",
         test_pass6_patch_dict_includes_scope_hint),
        ("deterministic patch passes scope_hint through",
         test_deterministic_patch_passes_scope_hint_through),
        ("deterministic patch prefers prose over scope_hint",
         test_deterministic_patch_prefers_prose_over_scope_hint),
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
        print(f"\n{failed}/{len(tests)} scope-hint propagation test(s) "
              f"failed")
        return 1
    print(f"\nAll {len(tests)} scope-hint propagation tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())