# tests/recover/test_patch_bounds_html.py

def test_html_snap_prefers_innermost():
    from deeppeek.recover.patch_bounds import try_ast_snapped_boundaries
    base = (
        '<div class="field">\n'
        '  <label>Rotation</label>\n'
        '  <div style="display:flex;">\n'
        '    <input type="range">\n'
        '    <span>0\u00b0</span>\n'
        '  </div>\n'
        '</div>\n'
    )
    # The 3-line window around <input type="range"> is what the tool
    # would produce via region-replace. The snap must expand to the
    # enclosing <div class="field"> (7 lines), not to the outer wrapper
    # of every overlapping range.
    start, end = 2, 4
    snapped = try_ast_snapped_boundaries(base, "panel.html", start, end)
    assert snapped is not None
    s, e = snapped
    assert s == 0 and e == 7, (
        f"expected (0,7) — the enclosing <div class=\"field\">; got "
        f"({s},{e})")
