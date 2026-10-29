"""Tests for embedded-HTML snapping in `try_ast_snapped_boundaries`.

Motivating case (2026-10-29 pg_base.py run): a patch body that
replaces a 3-line `<input>` window inside a `<div class="field">`
with a 16-line new `<div class="field">` block was spliced into the
inner 3-line window, producing a nested `<div class="field">`
inside the outer one.  Valid Python (the whole thing is inside a
triple-quoted string), valid Python (ast.parse succeeds), and
semantically broken (two Rotation labels, two range inputs, one
orphaned value span).

The fix snaps the window to the innermost enclosing HTML element
whose opening tag matches the patch body's opening tag signature.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_pg_base_wrapper(inner_html):
    """Wrap `inner_html` in a PG_PANEL_BODY triple-quoted string."""
    Q = chr(34) * 3
    return f"PG_PANEL_BODY = r{Q}\n{inner_html}\n{Q}\n"


def test_embedded_html_snap_uses_body_signature():
    """The pg_base.py shape: body opens with `<div class="field"
    style="align-items:center;">`, base has the outer field that
    contains the window, snap expands to the outer field."""
    from deeppeek.recover.patch_bounds import try_ast_snapped_boundaries

    content = _make_pg_base_wrapper(
        '<div class="field" style="align-items:center;">\n'
        '  <label>Rotation</label>\n'
        '  <div style="display:flex;align-items:center;gap:8px;">\n'
        '    <input type="range" id="viewRotation"\n'
        '           min="0" max="360" step="1" value="0"\n'
        '           style="flex:1;min-width:0;">\n'
        '    <span id="viewRotationValue">0</span>\n'
        '  </div>\n'
        '</div>\n'
    )
    # Line indices (0-based).  The <input> starts at line 4 of the
    # file (line 0 is `PG_PANEL_BODY = r"""`, line 1 is the outer
    # div, line 4 is the input).  The window is lines 4-6 (the three
    # input lines).
    body = (
        '<div class="field" style="align-items:center;">\n'
        '  <label>Rotation</label>\n'
        '  <div style="display:flex;align-items:center;gap:8px;'
        'width:100%;">\n'
        '    <input type="range" id="viewRotation">\n'
        '    <input type="number" id="viewRotationInput">\n'
        '    <span>deg</span>\n'
        '  </div>\n'
        '</div>\n'
    )
    snapped = try_ast_snapped_boundaries(
        content, "pg_base.py", start=4, end=7, body=body)
    assert snapped is not None, (
        "expected snap to fire; got None")
    s, e = snapped
    # The outer field spans lines 1..9 (inclusive).  The snap should
    # return (1, 10) — the full outer field range (exclusive end).
    assert s == 1 and e == 10, (
        f"expected (1, 10) — outer field range; got ({s}, {e})")


def test_embedded_html_snap_falls_back_to_innermost():
    """No body signature match → snap to the innermost enclosing
    element (bounds the damage rather than cascading into the whole
    outer field)."""
    from deeppeek.recover.patch_bounds import try_ast_snapped_boundaries

    content = _make_pg_base_wrapper(
        '<div class="outer">\n'
        '  <div class="inner">\n'
        '    <input type="range">\n'
        '    <span>0</span>\n'
        '  </div>\n'
        '</div>\n'
    )
    # No opening tag in body → no signature match.  Window is the
    # inner <input>+<span> (lines 3-4, 0-based across the wrapper).
    body = 'replacement fragment'
    snapped = try_ast_snapped_boundaries(
        content, "x.py", start=3, end=5, body=body)
    assert snapped is not None
    s, e = snapped
    # Innermost enclosing = inner <div class="inner">, lines 2..5.
    assert s == 2 and e == 6, (
        f"expected (2, 6) — inner div range; got ({s}, {e})")


def test_standalone_html_snap_works():
    """Standalone .html files use the same range extraction without
    the wrapper."""
    from deeppeek.recover.patch_bounds import try_ast_snapped_boundaries
    content = (
        '<div class="outer">\n'
        '  <div class="inner">\n'
        '    <input>\n'
        '  </div>\n'
        '</div>\n'
    )
    snapped = try_ast_snapped_boundaries(
        content, "x.html", start=2, end=3, body='<input>')
    assert snapped is not None
    s, e = snapped
    # Innermost = inner div, lines 1..3.
    assert s == 1 and e == 4


def main():
    tests = [
        ("embedded html snap uses body signature",
         test_embedded_html_snap_uses_body_signature),
        ("embedded html snap falls back to innermost",
         test_embedded_html_snap_falls_back_to_innermost),
        ("standalone html snap works",
         test_standalone_html_snap_works),
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
        print(f"\n{failed}/{len(tests)} html-snap test(s) failed")
        return 1
    print(f"\nAll {len(tests)} html-snap tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())