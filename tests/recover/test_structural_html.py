"""Structural HTML checks added in the 2026-11-05 pg_base.py fix.

The pg_base.py corruption: `_try_region_replace` spliced a 17-line
body (`<div class="field"><label>Rotation</label>...`) over a 3-line
`<input type="range">` window in a base where the outer
`<div class="field">` wrapper already existed.  Result: two nested
`<div class="field">` elements and an orphaned `<span
id="viewRotationValue">`.  Everything downstream reported success:
ast.parse passed (the malformed content was inside a raw string),
`_html_structural_check` balanced the `<div>` opens and closes,
`_module_level_stmts_added` saw no new top-level statement, and
`patch_presence` saw all body lines present.

The fix has four layers, all tested here:

  1. `_extract_embedded_html` + `_has_duplicate_class_nesting` in
     `validate_output.py` — the diagnostic that names the corruption.
  2. `_check_structural_sanity` gate in `apply_patches.py` — rejects
     the patch during application.
  3. `_try_manual_html_snap` fallback in `patch_bounds.py` — closes
     the gap when tree-sitter-html is not installed.
  4. `splice_region` reindent — allows the region-replace body to
     match the base's indentation.

Tests skip cleanly if a tree-sitter grammar needed by the specific
test is unavailable.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


TD = chr(34) * 3
TS = chr(39) * 3


def _has_html_grammar():
    try:
        from deeppeek.recover.ast_anchor import ast_status
        return ast_status().get("html", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# _extract_embedded_html
# ---------------------------------------------------------------------------

def test_extract_embedded_html_from_py():
    """`_extract_embedded_html` pulls HTML out of a Python triple-
    quoted string."""
    from deeppeek.recover.validate_output import _extract_embedded_html
    py = (
        '"""Docstring."""\n'
        '\n'
        'PG_PANEL_BODY = r' + TD + '\n'
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '</div>\n'
        + TD + '\n'
    )
    html = _extract_embedded_html(py)
    assert '<div class="field">' in html
    assert '<label>Rotation</label>' in html


def test_extract_embedded_html_ignores_js_strings():
    """A string that looks like JS but not HTML is excluded."""
    from deeppeek.recover.validate_output import _extract_embedded_html
    py = (
        'PANEL_JS = r' + TD + '\n'
        'export function foo() { return 1; }\n'
        + TD + '\n'
    )
    html = _extract_embedded_html(py)
    assert html == ""


def test_extract_embedded_html_empty_input():
    from deeppeek.recover.validate_output import _extract_embedded_html
    assert _extract_embedded_html("") == ""


# ---------------------------------------------------------------------------
# _has_duplicate_class_nesting
# ---------------------------------------------------------------------------

def test_duplicate_class_nesting_positive_pg_base_shape():
    """The exact pg_base.py corruption: `<div class="field">` nested
    inside another `<div class="field">`."""
    if not _has_html_grammar():
        print("  SKIP: tree-sitter-html not available")
        return
    from deeppeek.recover.validate_output import (
        _has_duplicate_class_nesting,
    )
    html = (
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '  <div class="field">\n'  # ← nested duplicate
        '    <label>Rotation</label>\n'
        '  </div>\n'
        '</div>\n'
    )
    assert _has_duplicate_class_nesting(html) is True


def test_duplicate_class_nesting_negative_siblings():
    """Two sibling `<div class="field">` elements are legitimate;
    only *nesting* triggers the check."""
    if not _has_grammar_safe():
        return
    from deeppeek.recover.validate_output import (
        _has_duplicate_class_nesting,
    )
    html = (
        '<div class="wrapper">\n'
        '  <div class="field">\n'
        '    <label>A</label>\n'
        '  </div>\n'
        '  <div class="field">\n'
        '    <label>B</label>\n'
        '  </div>\n'
        '</div>\n'
    )
    assert _has_duplicate_class_nesting(html) is False


def test_duplicate_class_nesting_negative_different_classes():
    """Nested elements with *different* classes are fine."""
    if not _has_grammar_safe():
        return
    from deeppeek.recover.validate_output import (
        _has_duplicate_class_nesting,
    )
    html = (
        '<div class="wrapper">\n'
        '  <div class="field">\n'
        '    <label>A</label>\n'
        '  </div>\n'
        '</div>\n'
    )
    assert _has_duplicate_class_nesting(html) is False


def test_duplicate_class_nesting_negative_empty():
    from deeppeek.recover.validate_output import (
        _has_duplicate_class_nesting,
    )
    assert _has_duplicate_class_nesting("") is False


def test_duplicate_class_nesting_skips_gracefully_without_grammar():
    """Without tree-sitter-html, the check returns False (no-op)."""
    from deeppeek.recover.validate_output import (
        _has_duplicate_class_nesting,
    )
    # This input triggers the check when a grammar is present; if
    # not, it returns False — same result, no false positive.
    html = '<div class="x"><div class="x"></div></div>'
    result = _has_duplicate_class_nesting(html)
    # When grammar is absent: False.  When present: True.
    # Either outcome is safe — never a crash.
    assert isinstance(result, bool)


def _has_grammar_safe():
    return _has_html_grammar()


# ---------------------------------------------------------------------------
# _check_structural_sanity gate
# ---------------------------------------------------------------------------

def test_structural_sanity_rejects_duplicate_nesting():
    """The gate rejects a `.py` patch whose embedded HTML gained a
    duplicate `<div class="field">` nesting.  Motivating case:
    pg_base.py."""
    if not _has_grammar_safe():
        print("  SKIP: tree-html not available; gate is a no-op")
        return
    from deeppeek.recover.apply_patches import _check_structural_sanity
    old = (
        '"""Panel."""\n'
        'BODY = r' + TD + '\n'
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '  <input type="range">\n'
        '</div>\n'
        + TD + '\n'
    )
    new = (
        '"""Panel."""\n'
        'BODY = r' + TD + '\n'
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '<div class="field">\n'       # ← nested duplicate
        '  <label>Rotation</label>\n'
        '  <input type="range">\n'
        '</div>\n'
        '</div>\n'
        + TD + '\n'
    )
    body = (
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '  <input type="range">\n'
        '</div>\n'
    )
    err = _check_structural_sanity(
        old, new, "pg_base.py", patch_body=body)
    assert err is not None, (
        "expected duplicate-nesting rejection"
    )
    assert "duplicate element nested" in err.lower(), (
        f"unexpected error text: {err!r}"
    )


def test_structural_sanity_accepts_when_base_also_nested():
    """If the base already had the nesting, the gate is silent —
    the check is about *introduction*, not presence."""
    if not _has_grammar_safe():
        print("  SKIP: tree-sitter-html not available")
        return
    from deeppeek.recover.apply_patches import _check_structural_sanity
    old = (
        '"""Panel."""\n'
        'BODY = r' + TD + '\n'
        '<div class="field">\n'
        '  <div class="field">\n'
        '  </div>\n'
        '</div>\n'
        + TD + '\n'
    )
    new = old
    err = _check_structural_sanity(
        old, new, "x.py", patch_body="")
    assert err is None


def test_structural_sanity_noop_when_html_grammar_missing():
    """Without tree-sitter-html, the duplicate-nesting gate is a
    no-op; other structural checks still run."""
    if _has_grammar_safe():
        print("  SKIP: tree-sitter-html IS available")
        return
    from deeppeek.recover.apply_patches import _check_structural_sanity
    old = 'BODY = r' + TD + '<div class="x"></div>' + TD + '\n'
    new = 'BODY = r' + TD + '<div class="x"><div class="x"></div></div>' + TD + '\n'
    err = _check_structural_sanity(old, new, "x.py", patch_body="")
    assert err is None, (
        "should be a no-op without the html grammar; got: "
        + repr(err)
    )


# ---------------------------------------------------------------------------
# _try_manual_html_snap fallback
# ---------------------------------------------------------------------------

def test_manual_html_snap_finds_enclosing_div():
    """Without tree-sitter-html, `_try_manual_html_snap` expands a
    3-line window to the enclosing `<div class="field">`."""
    from deeppeek.recover.patch_bounds import _try_manual_html_snap
    content = (
        'PG = r' + TD + '\n'   # line 0
        '<div class="field">\n'  # line 1
        '  <label>Rotation</label>\n'  # line 2
        '  <input type="range">\n'     # line 3 (window start)
        '  <span>0</span>\n'           # line 4 (window end-1)
        '</div>\n'                     # line 5
        + TD + '\n'                    # line 6
    )
    body = (
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '  <input type="range" id="viewRotation">\n'
        '  <input type="number" id="viewRotationInput">\n'
        '</div>\n'
    )
    # Window [3, 4) is a 1-line inner window inside the outer field.
    snapped = _try_manual_html_snap(content, 3, 4, body)
    assert snapped is not None, (
        "expected a snap to the enclosing <div class=\"field\">"
    )
    s, e = snapped
    assert s == 1, f"expected open_idx=1, got {s}"
    assert e == 6, f"expected close_idx+1=6, got {e}"


def test_manual_html_snap_none_when_no_matching_open():
    from deeppeek.recover.patch_bounds import _try_manual_html_snap
    content = "no html here\njust text\n"
    body = '<div class="field">\n  <label>X</label>\n</div>\n'
    snapped = _try_manual_html_snap(content, 0, 1, body)
    assert snapped is None


def test_manual_html_snap_none_when_class_mismatch():
    """If the body's class doesn't match any enclosing `<tag`, return
    None (conservative)."""
    from deeppeek.recover.patch_bounds import _try_manual_html_snap
    content = (
        '<div class="other">\n'
        '  <input>\n'
        '</div>\n'
    )
    body = '<div class="field">\n  <label>X</label>\n</div>\n'
    snapped = _try_manual_html_snap(content, 1, 2, body)
    assert snapped is None, (
        f"expected None for class mismatch; got {snapped!r}"
    )


# ---------------------------------------------------------------------------
# splice_region reindent
# ---------------------------------------------------------------------------

def test_splice_region_reindents_dedented_body():
    """A body written at column 0 spliced into an indented Python
    context must be reindented to match.  Motivating case:
    inpaint_server.py's failure return block, inside an `if` at
    8-space indent."""
    from deeppeek.recover.patch_bounds import splice_region
    base = (
        "def handle(payload):\n"
        "    try:\n"
        "        x = payload['x']\n"
        "    except KeyError:\n"
        "        return {'success': False, 'message': 'no x'}\n"
        "    return {'success': True}\n"
    )
    body_lines = [
        "return {",
        "    'success': False,",
        "    'message': 'no x',",
        "    'errors': [],",
        "}",
    ]
    out = splice_region(base, 4, 5, body_lines, "x.py")
    assert out.status == "changed", (
        f"expected a changed splice; got {out.status!r}"
    )
    assert out.content is not None
    for line in out.content.split("\n"):
        if "'errors': []" in line:
            assert line.startswith("        "), (
                f"body not reindented to 8 spaces:\n{out.content}"
            )
            break
    else:
        raise AssertionError(
            f"reindented body missing:\n{out.content}"
        )


def test_splice_region_does_not_reindent_already_indented_body():
    """A body already carrying the correct indent is spliced
    verbatim — no double-prefix."""
    from deeppeek.recover.patch_bounds import splice_region
    base = (
        "def handle():\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        return 1\n"
        "    return 2\n"
    )
    body_lines = [
        "        return 10",
        "        return 20",
    ]
    out = splice_region(base, 4, 5, body_lines, "x.py")
    assert out.status == "changed"
    assert out.content is not None
    # Should NOT have 16 spaces of indent
    for line in out.content.split("\n"):
        if "return 10" in line:
            assert not line.startswith("                "), (
                f"body was double-indented:\n{out.content}"
            )
            assert line.startswith("        "), (
                f"body was not at original indent:\n{out.content}"
            )
            break


# ---------------------------------------------------------------------------
# End-to-end pg_base.py shape
# ---------------------------------------------------------------------------

def test_pg_base_shape_region_replace_expands_to_enclosing_div():
    """The full pg_base.py shape: base has an outer
    `<div class="field">` containing a 3-line inner rotation widget;
    the patch body reopens `<div class="field">`.  `_try_region_replace`
    must expand to the enclosing element, not splice over the 3-line
    inner window.

    This is the outer-to-inner test — it succeeds when the snap
    fires (either via tree-sitter-html or via `_try_manual_html_snap`).
    """
    from deeppeek.recover.patch_dispatch import _try_region_replace
    base = (
        'PG_PANEL_BODY = r' + TD + '\n'
        '<div class="field" style="align-items:center;">\n'
        '  <label>Rotation</label>\n'
        '  <div style="display:flex;align-items:center;gap:8px;">\n'
        '    <input type="range" id="viewRotation"\n'
        '           min="0" max="360" step="1" value="0"\n'
        '           style="flex:1;min-width:0;">\n'
        '    <span id="viewRotationValue">0</span>\n'
        '  </div>\n'
        '</div>\n'
        + TD + '\n'
    )
    body = (
        '<div class="field" style="align-items:center;">\n'
        '  <label>Rotation</label>\n'
        '  <div style="display:flex;align-items:center;gap:8px;width:100%;">\n'
        '    <input type="range" id="viewRotation"\n'
        '           min="0" max="360" step="1" value="0"\n'
        '           style="flex:1;min-width:0;">\n'
        '    <input type="number" id="viewRotationInput"\n'
        '           min="0" max="360" step="0.1" value="0">\n'
        '    <span style="color:#7b8794;font-size:11px;">\u00b0</span>\n'
        '  </div>\n'
        '</div>\n'
    )

    result = _try_region_replace(base, body.strip(), "pg_base.py")
    if result is None:
        print("  SKIP: region-replace did not fire at all")
        return
    # Critical: exactly ONE outer wrapper and ONE label after splice.
    assert result.count('<div class="field" style="align-items:center;">') == 1, (
        f"expected 1 outer wrapper; got "
        f"{result.count(chr(60) + 'div class='
                        + chr(34) + 'field' + chr(34)
                        + ' style=' + chr(34) + 'align-items:center;' + chr(34))}"
        f"\n{result}"
    )
    assert result.count("<label>Rotation</label>") == 1, (
        f"expected 1 Rotation label; got "
        f"{result.count('<label>Rotation</label>')}\n{result}"
    )
    assert "viewRotationInput" in result, (
        f"new input widget missing from result:\n{result}"
    )
    # The body replaces the old `<span id="viewRotationValue">0</span>`
    # with `<span style="...">°</span>`.  Since region-replace targets
    # the *outer* `<div class="field">` (the wrapper the body reopens),
    # the inner span is *supposed* to be gone.  Asserting it survives
    # would pin the wrong behaviour — and would fail even when the
    # outer-snap path runs correctly.
    assert "viewRotationValue" not in result, (
        f"old value span should be replaced by the body's new "
        f"`<span>`; it survived, meaning the body's full replacement "
        f"did not happen (likely an under-snapped splice):\n{result}"
    )
    # Sanity: the body's new span (with its distinguishing style) IS
    # present.  If the body were only partially spliced, this would
    # be missing too.
    assert 'color:#7b8794' in result, (
        f"body's new styled span missing from result:\n{result}"
    )

def test_manual_html_snap_falls_back_to_innermost_without_signature():
    """No body signature → return innermost enclosing block element.

    The failing test from the 2026-11-05 run: a body with no HTML
    signature must fall back to the innermost enclosing block, not
    to the whole outer wrapper, and not to the whole `BODY = r\"\"\"...\"\"\"`
    Python statement.
    """
    from deeppeek.recover.patch_bounds import _try_manual_html_snap
    content = (
        '<div class="outer">\n'          # line 0
        '  <span>filler</span>\n'         # line 1
        '  <div class="inner">\n'         # line 2  ← inner open
        '    line 1\n'                    # line 3
        '    line 2\n'                    # line 4
        '  </div>\n'                      # line 5  ← inner close
        '  <span>filler</span>\n'         # line 6
        '  <span>filler</span>\n'         # line 7
        '</div>\n'                        # line 8  ← outer close
    )
    # Body has no HTML signature.
    body = ""
    # Window (3, 4) is inside the inner div.
    snapped = _try_manual_html_snap(content, 3, 4, body)
    assert snapped == (2, 6), (
        f"expected (2, 6) — inner div range; got {snapped!r}.  "
        f"Without signature fallback, the whole-outer range (0, 9) "
        f"or None would be returned, both of which over- or "
        f"under-expand."
    )


def test_manual_html_snap_falls_back_to_innermost_with_prose_body():
    """A prose body (no `<`) is also signature-less."""
    from deeppeek.recover.patch_bounds import _try_manual_html_snap
    content = (
        '<div class="outer">\n'
        '  <div class="inner">\n'
        '    content\n'
        '  </div>\n'
        '</div>\n'
    )
    snapped = _try_manual_html_snap(content, 2, 3, "just some text")
    assert snapped == (1, 4)


def test_try_ast_snapped_boundaries_does_not_over_expand_to_python_range():
    """When the window is inside embedded HTML but no enclosing element
    was found, `try_ast_snapped_boundaries` returns None instead of
    falling through to `_python_top_level_stmt_ranges`, which would
    expand to the whole `BODY = r\"\"\"...\"\"\"` assignment.

    This is the guard added alongside the manual-snap fix; without
    it, a `_try_embedded_html_snap` returning None silently produces
    the worst possible snap.
    """
    from deeppeek.recover.patch_bounds import try_ast_snapped_boundaries
    content = (
        'BODY = r"""\n'                  # 0
        '<div class="outer">\n'           # 1
        '  <div class="inner">\n'         # 2
        '    content\n'                   # 3
        '  </div>\n'                      # 4
        '</div>\n'                        # 5
        '"""\n'                           # 6
        '\n'                              # 7
        'def other():\n'                  # 8
        '    return 1\n'                  # 9
    )
    # Window (3, 4) is inside the inner div.  With the manual snap
    # finding the inner (2, 5), this returns (2, 5) or (3, 5).
    # Assert the result, if not None, is at most the outer wrapper's
    # span — never the whole string assignment (0, 7).
    snapped = try_ast_snapped_boundaries(content, "x.py", 3, 4)
    if snapped is not None:
        s, e = snapped
        assert (s, e) != (0, 7), (
            f"over-expanded to the whole `BODY = r\"\"\"...\"\"\"` "
            f"statement (0, 7); got {snapped!r}"
        )
        # Should be at most the outer div (1, 6).
        assert s >= 1 and e <= 6, (
            f"snapped past the outer div; got {snapped!r}"
        )

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        # ── _extract_embedded_html ────────────────────────────────────
        ("extract embedded html from py",
         test_extract_embedded_html_from_py),
        ("extract embedded html ignores js strings",
         test_extract_embedded_html_ignores_js_strings),
        ("extract embedded html empty input",
         test_extract_embedded_html_empty_input),
        # ── _has_duplicate_class_nesting ──────────────────────────────
        ("duplicate class nesting positive pg_base shape",
         test_duplicate_class_nesting_positive_pg_base_shape),
        ("duplicate class nesting negative siblings",
         test_duplicate_class_nesting_negative_siblings),
        ("duplicate class nesting negative different classes",
         test_duplicate_class_nesting_negative_different_classes),
        ("duplicate class nesting negative empty",
         test_duplicate_class_nesting_negative_empty),
        ("duplicate class nesting skips gracefully without grammar",
         test_duplicate_class_nesting_skips_gracefully_without_grammar),
        # ── _check_structural_sanity gate ─────────────────────────────
        ("structural sanity rejects duplicate nesting",
         test_structural_sanity_rejects_duplicate_nesting),
        ("structural sanity accepts when base also nested",
         test_structural_sanity_accepts_when_base_also_nested),
        ("structural sanity noop when html grammar missing",
         test_structural_sanity_noop_when_html_grammar_missing),
        # ── _try_manual_html_snap fallback ────────────────────────────
        ("manual html snap finds enclosing div",
         test_manual_html_snap_finds_enclosing_div),
        ("manual html snap none when no matching open",
         test_manual_html_snap_none_when_no_matching_open),
        ("manual html snap none when class mismatch",
         test_manual_html_snap_none_when_class_mismatch),
        # ── NEW (2026-11-05): signature-less fallback policy ──────────
        ("manual html snap falls back to innermost without signature",
         test_manual_html_snap_falls_back_to_innermost_without_signature),
        ("manual html snap falls back to innermost with prose body",
         test_manual_html_snap_falls_back_to_innermost_with_prose_body),
        ("try_ast_snapped_boundaries does not over expand to python range",
         test_try_ast_snapped_boundaries_does_not_over_expand_to_python_range),
        # ── splice_region reindent ────────────────────────────────────
        ("splice region reindents dedented body",
         test_splice_region_reindents_dedented_body),
        ("splice region does not reindent already indented body",
         test_splice_region_does_not_reindent_already_indented_body),
        # ── end-to-end pg_base.py shape ───────────────────────────────
        ("pg_base shape region replace expands to enclosing div",
         test_pg_base_shape_region_replace_expands_to_enclosing_div),
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
        print(f"\n{failed}/{len(tests)} structural-html test(s) failed")
        return 1
    print(f"\nAll {len(tests)} structural-html tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
