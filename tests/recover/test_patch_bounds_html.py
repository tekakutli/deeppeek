# tests/recover/test_patch_bounds_html.py

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_html_snap_prefers_outer_signature_match():
    """When the body's opening tag matches an enclosing element's
    opening tag, prefer that element over the innermost.

    Motivating case (pg_base.py, 2026-10-29): the patch body reopens
    `<div class="field" style="align-items:center;">`; the base has
    that same outer div wrapping a 3-line `<input>...</input>` window.
    Without the signature match, the snap returns the *inner* flex
    div (2, 6), and the splice produces `<div class="field">` nested
    inside the outer `<div class="field">`.  With the signature, the
    snap expands to (0, 7) — the enclosing outer div — so the body
    fully replaces it.

    The no-signature case is covered by
    `tests/test_patch_bounds_html.py::test_embedded_html_snap_falls_back_to_innermost`.
    This test exists to pin the *signature-match* path, which is what
    actually closes the pg_base.py corruption.
    """
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
    # Body's opening tag matches the outer `<div class="field">`.
    body = '<div class="field">\n  <label>Rotation</label>\n</div>\n'
    start, end = 2, 4
    snapped = try_ast_snapped_boundaries(
        base, "panel.html", start, end, body=body)
    assert snapped is not None
    s, e = snapped
    assert s == 0 and e == 7, (
        f"expected (0, 7) — the outer <div class=\"field\"> matched "
        f"by body signature; got ({s}, {e}).  If the implementation "
        f"falls back to innermost despite a matching signature, the "
        f"pg_base.py nested-div corruption returns."
    )


def main():
    tests = [
        ("html snap prefers outer signature match",
         test_html_snap_prefers_outer_signature_match),
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
