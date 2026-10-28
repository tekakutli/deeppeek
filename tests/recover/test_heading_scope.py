"""Regression tests for H2 scope propagation.

Pass 1's extractor attaches prose to each fence.  When the prose
names no scope but the enclosing H2 does ("## Fix 3 — surface
per-command errors in `handle_inpaint`"), the scope is right there
in the section heading and must reach attribution.

Before the fix, block idx 634 in the 2026-10-28 run
(`inpaint_server.py`'s "And in every failure return:") orphaned
because its prose had no scope name and the enclosing H2's scope
was discarded after each fence.  `_try_scope_name_attribution`
searched for a scope, found none, and the block went to the
residual as `orphan_unattributed`.

These tests preserve:

  · `pass1_extract` sets `Block.scope_hint` from a non-file H1/H2
    whose text contains "in `X`" / "find the `X`" etc.
  · The H2-to-block propagation is a fallback — a block whose
    own prose names a different scope uses that.
  · Ambiguous scopes still fail closed.
  · H3+ subsections and file-headers do not carry a scope hint.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Extractor: scope hint from H1/H2
# ---------------------------------------------------------------------------

def test_h2_scope_hint_propagates_to_block():
    """An H2 containing "in `handle_inpaint`" populates
    `scope_hint` on subsequent orphan blocks.

    Failure mode caught: block 634's shape — the scope name lives
    only in the section heading, not in the fence's immediate prose.
    """
    from deeppeek.recover.extract import pass1_extract

    dump = (
        "## Fix 3 — surface per-command errors in `handle_inpaint`\n"
        "\n"
        "And in every failure return:\n"
        "\n"
        "```python\n"
        "    return {'success': False}\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 1
    assert blocks[0].scope_hint == "handle_inpaint", (
        f"expected scope_hint='handle_inpaint'; got "
        f"{blocks[0].scope_hint!r}. This is the block-634 failure "
        f"shape: prose is generic, scope is in the H2."
    )
    assert blocks[0].scope_name == "handle_inpaint"


def test_h2_with_file_header_does_not_set_scope_hint():
    """An H2 that parses as a file header (## File: `foo.py`) is
    a file-boundary heading, not a section heading; it does not
    contribute a scope hint."""
    from deeppeek.recover.extract import pass1_extract

    dump = (
        "## File: `inpaint_server.py`\n"
        "\n"
        "```python\n"
        "def handle_inpaint(payload):\n"
        "    return {}\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 1
    assert blocks[0].scope_hint == "", (
        f"file-header H2 should not set a scope hint; got "
        f"{blocks[0].scope_hint!r}"
    )
    assert blocks[0].header_path == "inpaint_server.py"


def test_h3_does_not_reset_scope_hint():
    """An H3 subsection inherits the H2's scope hint."""
    from deeppeek.recover.extract import pass1_extract

    dump = (
        "## Fix 3 — surface errors in `handle_inpaint`\n"
        "\n"
        "### (a) Failure path\n"
        "\n"
        "```python\n"
        "    return {'success': False}\n"
        "```\n"
    )
    blocks, _ = pass1_extract(dump)
    assert len(blocks) == 1
    assert blocks[0].scope_hint == "handle_inpaint", (
        f"H3 should preserve the H2 scope hint; got "
        f"{blocks[0].scope_hint!r}"
    )


# ---------------------------------------------------------------------------
# Attribution: falls back to scope_hint when prose is silent
# ---------------------------------------------------------------------------

def _make_scope_records(scope_body_map):
    """Build minimal records: one pre-existing file per (path, body)
    pair, with the given body text."""
    from deeppeek.recover.core import Block, FileRecord

    blocks = []
    records = {}
    for i, (path, body) in enumerate(scope_body_map.items()):
        b = Block(
            idx=i, header_path=path, modifier=None, fence_lang="py",
            body=body, body_line_count=body.count("\n") + 1,
            context_before="", line_start=0, line_end=0,
            is_orphan=False, is_code_file=True, kind="full",
            canonical_path=path, output_path=path,
            classified_by="preexisting",
        )
        blocks.append(b)
        rec = FileRecord(path=path, state="SINGLE_FULL")
        rec.source_blocks = [i]
        records[path] = rec
    return blocks, records, {b.idx: b for b in blocks}


def test_attribution_uses_h2_scope_hint():
    """The block-634 shape: prose is "And in every failure return:"
    (no scope name) but scope_hint is "handle_inpaint".  Attribution
    must find `handle_inpaint` in base and assign.

    Failure mode caught: the orphan going to the residual instead of
    into `inpaint_server.py`.
    """
    from deeppeek.recover.core import Block
    from deeppeek.recover.attribute import pass4_attribute_orphans

    orphan = Block(
        idx=0, header_path=None, modifier=None, fence_lang="py",
        body=(
            "    return {\n"
            "        'success': False,\n"
            "        'errors': captured['errors'],\n"
            "    }\n"
        ),
        body_line_count=4,
        context_before="And in every failure return:",
        line_start=0, line_end=4, is_orphan=True,
        is_code_file=True, kind="partial",
        scope_hint="handle_inpaint",
        scope_name="handle_inpaint",
    )

    blocks, records, blocks_by_idx = _make_scope_records({
        "inpaint_server.py":
            "def handle_inpaint(payload):\n    return {}\n",
        "other.py":
            "def handle_other(payload):\n    return {}\n",
    })
    # Re-key orphan into the blocks list.
    orphan.idx = 100
    blocks_by_idx[100] = orphan
    blocks = [orphan] + blocks

    pass4_attribute_orphans(blocks, blocks_by_idx, records, verbose=False)
    assert orphan.attributed_to == "inpaint_server.py", (
        f"expected inpaint_server.py; got "
        f"{orphan.attributed_to!r}. If the fallback to scope_hint is "
        f"missing, this orphan lands in the residual."
    )
    assert orphan.attributed_by == "scope_name"


def test_attribution_prefers_prose_over_scope_hint():
    """When both are set, prose wins.

    Failure mode caught: H2 says `handle_inpaint` but the fence's
    own prose says "In `handle_other`, add:".  Attribution must go to
    `handle_other`, not `handle_inpaint`.
    """
    from deeppeek.recover.core import Block
    from deeppeek.recover.attribute import pass4_attribute_orphans

    orphan = Block(
        idx=0, header_path=None, modifier=None, fence_lang="py",
        body="    extra = 1\n",
        body_line_count=1,
        context_before="In `handle_other`, add:",
        line_start=0, line_end=1, is_orphan=True,
        is_code_file=True, kind="partial",
        scope_hint="handle_inpaint",
        scope_name="handle_inpaint",
    )
    blocks, records, blocks_by_idx = _make_scope_records({
        "inpaint_server.py":
            "def handle_inpaint(payload):\n    return {}\n",
        "other.py":
            "def handle_other(payload):\n    return {}\n",
    })
    orphan.idx = 100
    blocks_by_idx[100] = orphan
    blocks = [orphan] + blocks

    pass4_attribute_orphans(blocks, blocks_by_idx, records, verbose=False)
    assert orphan.attributed_to == "other.py", (
        f"expected other.py (prose wins); got "
        f"{orphan.attributed_to!r}"
    )


def test_attribution_refuses_ambiguous_scope():
    """Two files declare `handle_inpaint`: even with scope_hint set,
    attribution must NOT commit.

    Failure mode caught: fallback makes attribution *over*-confident
    and picks a file, when fail-closed is the correct behaviour.
    """
    from deeppeek.recover.core import Block
    from deeppeek.recover.attribute import pass4_attribute_orphans

    orphan = Block(
        idx=0, header_path=None, modifier=None, fence_lang="py",
        body="    extra = 1\n",
        body_line_count=1,
        context_before="And in every failure return:",
        line_start=0, line_end=1, is_orphan=True,
        is_code_file=True, kind="partial",
        scope_hint="handle_inpaint",
        scope_name="handle_inpaint",
    )
    blocks, records, blocks_by_idx = _make_scope_records({
        "server_a.py":
            "def handle_inpaint(payload):\n    return {}\n",
        "server_b.py":
            "def handle_inpaint(payload):\n    return {}\n",
    })
    orphan.idx = 100
    blocks_by_idx[100] = orphan
    blocks = [orphan] + blocks

    pass4_attribute_orphans(blocks, blocks_by_idx, records, verbose=False)
    assert orphan.attributed_to is None, (
        f"ambiguous scope should not be attributed; got "
        f"{orphan.attributed_to!r}"
    )


def main():
    tests = [
        ("h2 scope hint propagates to block",
         test_h2_scope_hint_propagates_to_block),
        ("h2 with file header does not set scope hint",
         test_h2_with_file_header_does_not_set_scope_hint),
        ("h3 does not reset scope hint",
         test_h3_does_not_reset_scope_hint),
        ("attribution uses h2 scope hint",
         test_attribution_uses_h2_scope_hint),
        ("attribution prefers prose over scope hint",
         test_attribution_prefers_prose_over_scope_hint),
        ("attribution refuses ambiguous scope",
         test_attribution_refuses_ambiguous_scope),
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
        print(f"\n{failed}/{len(tests)} heading-scope test(s) failed")
        return 1
    print(f"\nAll {len(tests)} heading-scope tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
