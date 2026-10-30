"""Tests for `_validate_resolution` and the basename-directory pruning
in `_resolve_one` (2026-10-29).

Motivating bug: header `src/deeppeek/recover/statement_sig.py`
resolved to `tests/recover/statement_sig.py` because the basename
matched a single candidate in tests/ and the `src/deeppeek/` prefix
was silently discarded.  The pruning closes the specific case;
`_validate_resolution` is defense-in-depth.

INVARIANT SEMANTICS (fixed 2026-10-29)
======================================
Three cases where a resolution is considered compatible:

  · Header has no directory at all.  Any output directory is fine —
    the resolver matched a bare basename and the header never named
    a directory to lose.

  · Both header and output have directories, and `output_dir` is a
    deeper suffix of `header_dir` (e.g. `recover/x.py` resolving to
    `src/deeppeek/recover/x.py`).  The deeper path is a valid
    extension of what the header asked for.

  · Symmetrically, `header_dir` is a deeper suffix of `output_dir`.

The case that is NOT compatible, and that the invariant rejects:

  · Header names a directory, output drops it entirely
    (`src/foo/x.py` -> `x.py`).

This last case is the same bug class as the motivating one — the
header carried evidence (`src/foo/`) that the resolution discarded.
Rejecting it is what prevents a root-level `x.py` from being
overwritten when the conversation wanted `src/foo/x.py`.
"""
import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from collections import defaultdict
from deeppeek.recover.core import Block, _basename
from deeppeek.recover.resolve import (
    _validate_resolution, _resolve_one,
)


def _pre_block(path, idx=0):
    body = "PLACEHOLDER\n"
    return Block(
        idx=idx, header_path=path, modifier=None, fence_lang="py",
        body=body, body_line_count=1, context_before="",
        line_start=0, line_end=0, is_orphan=False, is_code_file=True,
        kind="full", canonical_path=path, output_path=path,
        classified_by="preexisting",
    )


def _block(header_path, idx=1):
    return Block(
        idx=idx, header_path=header_path, modifier=None, fence_lang="py",
        body="x = 1", body_line_count=1, context_before="",
        line_start=0, line_end=0, is_orphan=False, is_code_file=True,
        kind="partial",
    )


def _build_index(paths):
    """Build (by_full, by_basename) index from a list of paths."""
    by_full = {}
    by_basename = defaultdict(list)
    for i, p in enumerate(paths):
        blk = _pre_block(p, idx=i)
        by_full[p] = blk
        by_basename[_basename(p)].append(p)
    return by_full, dict(by_basename)


# ---------------------------------------------------------------------------
# _validate_resolution invariants
# ---------------------------------------------------------------------------

def test_validate_resolution_matching_dirs_passes():
    err = _validate_resolution(
        "src/foo/x.py", "src/foo/x.py", "exact")
    assert err is None, f"expected None; got {err!r}"


def test_validate_resolution_suffix_match_passes():
    """Three compatible directory relationships pass:

      · header has no dir  → any output dir is fine
      · output dir is a deeper suffix of header dir
      · header dir is a deeper suffix of output dir
    """
    # Header has no dir; resolver matched by basename somewhere.
    err = _validate_resolution("x.py", "src/foo/x.py", "basename")
    assert err is None, (
        f"header with no dir should accept any output dir; got {err!r}")

    # Both have dirs; output_dir is a deeper suffix of header_dir.
    err = _validate_resolution(
        "recover/x.py", "src/deeppeek/recover/x.py", "suffix")
    assert err is None, (
        f"deeper output dir should be compatible; got {err!r}")

    # Both have dirs; header_dir is a deeper suffix of output_dir.
    err = _validate_resolution(
        "src/deeppeek/recover/x.py", "recover/x.py", "suffix")
    assert err is None, (
        f"header as deeper suffix should be compatible; got {err!r}")


def test_validate_resolution_header_dir_lost_fails():
    """Header names a directory; resolved path drops it entirely.

    This is the same bug class as the motivating one.  Discarding an
    explicit `src/foo/` from the header loses the evidence that
    located the file.  Rejecting it prevents a root-level `x.py`
    from being overwritten when the conversation wanted
    `src/foo/x.py`.
    """
    err = _validate_resolution(
        "src/foo/x.py", "x.py", "basename")
    assert err is not None, (
        "expected a warning for header-dir loss; got None — the "
        "invariant is too lenient and will let the "
        "src/deeppeek/recover -> tests/recover bug class recur")
    assert "incompatible" in err.lower(), (
        f"expected 'incompatible' in message; got {err!r}")


def test_validate_resolution_deeper_match_passes():
    """Output dir is deeper but the header's dir is a suffix of it."""
    err = _validate_resolution(
        "recover/x.py", "src/deeppeek/recover/x.py", "suffix")
    assert err is None, f"expected None; got {err!r}"


def test_validate_resolution_basename_mismatch_fails():
    err = _validate_resolution(
        "src/foo/x.py", "src/foo/y.py", "exact")
    assert err is not None, (
        "expected a basename-mismatch warning; got None")
    assert "basename" in err, (
        f"expected 'basename' in message; got {err!r}")


def test_validate_resolution_incompatible_dirs_fails():
    err = _validate_resolution(
        "src/deeppeek/recover/statement_sig.py",
        "tests/recover/statement_sig.py",
        "basename")
    assert err is not None, (
        "expected an incompatible-dirs warning; got None — the "
        "motivating bug class is no longer guarded")
    assert "incompatible" in err.lower(), (
        f"expected 'incompatible' in message; got {err!r}")


def test_validate_resolution_no_header_dir_passes():
    """A header with no directory has no directory invariant to check."""
    err = _validate_resolution("x.py", "anywhere/x.py", "basename")
    assert err is None, f"expected None; got {err!r}"


# ---------------------------------------------------------------------------
# _resolve_one pruning
# ---------------------------------------------------------------------------

def test_resolve_one_prunes_incompatible_basename():
    """Header `src/deeppeek/recover/statement_sig.py` with two
    basename candidates — one in src/, one in tests/.  Pruning keeps
    only the src/ one."""
    by_full, by_basename = _build_index([
        "src/deeppeek/recover/statement_sig.py",
        "tests/recover/statement_sig.py",
    ])
    blk = _block("src/deeppeek/recover/statement_sig.py")
    resolved, reason = _resolve_one(
        blk, by_full, by_basename, verbose=False)
    assert resolved == "src/deeppeek/recover/statement_sig.py", (
        f"expected src/ candidate; got {resolved!r} (reason={reason!r})")


def test_resolve_one_prunes_all_when_incompatible():
    """When NO basename candidate has a compatible directory, the
    resolver returns no match — pushing the caller to the literal-
    header fallback instead of silently using a wrong-directory
    match."""
    by_full, by_basename = _build_index([
        "tests/recover/statement_sig.py",
        "other/deep/dir/statement_sig.py",
    ])
    blk = _block("src/deeppeek/recover/statement_sig.py")
    resolved, reason = _resolve_one(
        blk, by_full, by_basename, verbose=False)
    assert resolved is None, (
        f"expected None (no compatible candidate); got "
        f"{resolved!r} (reason={reason!r})")
    assert "no match" in reason


def test_resolve_one_unique_basename_no_header_dir():
    """Header has no directory; basename match is legitimate."""
    by_full, by_basename = _build_index([
        "src/deeppeek/recover/unique.py",
    ])
    blk = _block("unique.py")
    resolved, reason = _resolve_one(
        blk, by_full, by_basename, verbose=False)
    assert resolved == "src/deeppeek/recover/unique.py", (
        f"expected src/ candidate; got {resolved!r}")
    assert reason == "basename", (
        f"expected reason='basename'; got {reason!r}")


def test_resolve_one_suffix_match_still_works():
    """Header is a suffix of a full path — suffix branch matches."""
    by_full, by_basename = _build_index([
        "src/deeppeek/recover/pipeline.py",
    ])
    blk = _block("recover/pipeline.py")
    resolved, reason = _resolve_one(
        blk, by_full, by_basename, verbose=False)
    assert resolved == "src/deeppeek/recover/pipeline.py", (
        f"expected src/ candidate; got {resolved!r}")
    assert reason == "suffix", (
        f"expected reason='suffix'; got {reason!r}")


def main():
    tests = [
        ("validate resolution matching dirs",
         test_validate_resolution_matching_dirs_passes),
        ("validate resolution suffix match",
         test_validate_resolution_suffix_match_passes),
        ("validate resolution header dir lost",
         test_validate_resolution_header_dir_lost_fails),
        ("validate resolution deeper match",
         test_validate_resolution_deeper_match_passes),
        ("validate resolution basename mismatch",
         test_validate_resolution_basename_mismatch_fails),
        ("validate resolution incompatible dirs",
         test_validate_resolution_incompatible_dirs_fails),
        ("validate resolution no header dir",
         test_validate_resolution_no_header_dir_passes),
        ("resolve one prunes incompatible basename",
         test_resolve_one_prunes_incompatible_basename),
        ("resolve one prunes all when incompatible",
         test_resolve_one_prunes_all_when_incompatible),
        ("resolve one unique basename no header dir",
         test_resolve_one_unique_basename_no_header_dir),
        ("resolve one suffix match still works",
         test_resolve_one_suffix_match_still_works),
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
        print(f"\n{failed}/{len(tests)} resolve-invariant test(s) failed")
        return 1
    print(f"\nAll {len(tests)} resolve-invariant tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())