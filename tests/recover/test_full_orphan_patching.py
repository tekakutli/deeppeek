"""Tests for `full`-orphan reclassification and patching.

Pass 2's classification falls back to
`_looks_like_full_file(body, "unknown_N")` when no header path is
present.  The non-`.py` fallback returns `True`, so any orphan whose
body is JS/HTML/CSS on multiple lines is classified `full`.  Pass 5
previously excluded `full`-kind orphans from the patch pipeline on
the theory that a full print IS the base.

The 2026-10-28 run showed the failure: three playground fixes
(`pg_base.py`, `pg_panel_scrubs.py`, `pg_panel_bindings.py`) all
silently dropped because the block's `full` label was unreliable.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.dedup import pass5_deduplicate


def _pre_block(path, body, idx=0):
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="",
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _orphan(path, body, idx, kind="full", lang="js"):
    return Block(
        idx=idx, header_path=None, modifier=None, fence_lang=lang,
        body=body, body_line_count=body.count("\n") + 1,
        context_before="", line_start=0, line_end=0,
        is_orphan=True, is_code_file=True, kind=kind,
        attributed_to=path, attributed_by="prose",
        output_path=path,
        in_blockquote=False,
    )


def test_full_orphan_attributed_becomes_patch():
    """A `full`-kind orphan attributed to a pre-existing .js file
    becomes a patch against the base."""
    base_body = (
        "export function existing() {\n"
        "  return 1;\n"
        "}\n"
    )
    base = _pre_block("panel.js", base_body, idx=0)

    new_fn = (
        "export function added() {\n"
        "  return 42;\n"
        "}\n"
    )
    orphan = _orphan("panel.js", new_fn, idx=1, kind="full", lang="js")

    rec = FileRecord(path="panel.js", state="SINGLE_FULL")
    rec.preexisting_base = True
    rec.source_blocks = [0]
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0

    records = {"panel.js": rec}
    blocks_by_idx = {0: base, 1: orphan}

    pass5_deduplicate(records, blocks_by_idx, verbose=False)

    # The orphan must be in the record's patches (kind now means
    # nothing — it's a candidate).
    patch_idxs = [p["idx"] for p in rec.patches]
    assert 1 in patch_idxs, (
        f"full-orphan not admitted to patches: {patch_idxs}")


def test_full_orphan_embedded_js_reclassified():
    """A `full`-kind orphan with a `js` fence attributed to a
    pre-existing `.py` file is reclassified to `partial`."""
    base_body = (
        '"""Playground."""\n'
        "\n"
        "PANEL_JS = r\"\"\"\n"
        "export function existing() {}\n"
        "\"\"\"\n"
    )
    base = _pre_block("pg_panel_scrubs.py", base_body, idx=0)

    fragment = (
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  syncPanelFields();\n"
        "}\n"
    )
    orphan = _orphan("pg_panel_scrubs.py", fragment, idx=1,
                      kind="full", lang="js")

    rec = FileRecord(path="pg_panel_scrubs.py", state="SINGLE_FULL")
    rec.preexisting_base = True
    rec.source_blocks = [0]
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0

    records = {"pg_panel_scrubs.py": rec}
    blocks_by_idx = {0: base, 1: orphan}

    pass5_deduplicate(records, blocks_by_idx, verbose=False)

    assert orphan.kind == "partial", (
        f"expected reclassification to `partial`, got "
        f"{orphan.kind!r}")


def test_unattributed_full_orphan_unchanged():
    """An unattributed full orphan keeps its kind (write.py handles
    the warning; no reclassification without a target)."""
    base_body = "export function existing() {}\n"
    base = _pre_block("panel.js", base_body, idx=0)

    orphan = Block(
        idx=1, header_path=None, modifier=None, fence_lang="js",
        body="export function new() {}\n",
        body_line_count=1, context_before="",
        line_start=0, line_end=0, is_orphan=True, is_code_file=True,
        kind="full",
    )

    rec = FileRecord(path="panel.js", state="SINGLE_FULL")
    rec.preexisting_base = True
    rec.source_blocks = [0]
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0

    records = {"panel.js": rec}
    blocks_by_idx = {0: base, 1: orphan}

    pass5_deduplicate(records, blocks_by_idx, verbose=False)

    # No attribution target → reclassification skipped.
    assert orphan.kind == "full"


def test_html_full_orphan_not_reclassified():
    """An `html`-kind `full` orphan attributed to a `.html` file
    is NOT reclassified (no language mismatch)."""
    base_body = "<html><body>existing</body></html>\n"
    base = _pre_block("sample.html", base_body, idx=0)

    orphan = _orphan(
        "sample.html",
        "<div class=\"new\">added</div>\n",
        idx=1, kind="full", lang="html",
    )

    rec = FileRecord(path="sample.html", state="SINGLE_FULL")
    rec.preexisting_base = True
    rec.source_blocks = [0]
    rec.chosen_full_idx = 0
    rec.last_full_idx = 0

    records = {"sample.html": rec}
    blocks_by_idx = {0: base, 1: orphan}

    pass5_deduplicate(records, blocks_by_idx, verbose=False)

    # Same-language `.html` file: `full` is not reclassified as
    # embedded.  The patch becomes a candidate via the dedup change,
    # but the label stays.
    assert orphan.kind == "full"


def main():
    tests = [
        ("full orphan attributed becomes patch",
         test_full_orphan_attributed_becomes_patch),
        ("full orphan embedded js reclassified",
         test_full_orphan_embedded_js_reclassified),
        ("unattributed full orphan unchanged",
         test_unattributed_full_orphan_unchanged),
        ("html full orphan not reclassified",
         test_html_full_orphan_not_reclassified),
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
        print(f"\n{failed}/{len(tests)} full-orphan test(s) failed")
        return 1
    print(f"\nAll {len(tests)} full-orphan tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
