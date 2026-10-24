#!/usr/bin/env python3
'''
test_recover.py — fixture tests for recover.py.

Fixtures 01–06 exercise the standard `--out` path (self-contained
markdown, no pre-existing tree).

Fixtures 07, 08, and 09 exercise the `--apply-to` path: the fixture
markdown carries only patches, and the pre-existing source tree is
provided by `preexisting/<fixture>/`.

Fixture 09 specifically tests that pair detection fires when the
SECOND block's prose is a `with:`-style connective, even if the
FIRST block's prose does not match any specific `_REPLACE_PROSE`
phrase.  This is the shape-driven detection rule that replaced the
old prose-driven rule after the pg_inpaint.py failure.

Run:
    python tests/recover/test_recover.py

Set DEEPPEEK_TEST_KEEP=1 to preserve the temp directory on failure
for manual inspection.

Exits 0 on success, 1 on any failure.
'''

import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
EXPECTED = HERE / "expected"
PREEXISTING = HERE / "preexisting"
DEEPPEEK = HERE.parents[1]

KEEP_TMP = os.environ.get("DEEPPEEK_TEST_KEEP") == "1"


def _run(cmd, cwd=None):
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode > 4:
        raise RuntimeError(
            f"command exited {result.returncode}\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def run_recover(fixture: Path, out: Path) -> dict:
    cmd = [
        sys.executable, "-m", "deeppeek", "recover", str(fixture),
        "--out", str(out),
        "--llm-mode", "off",
        "--min-trust", "LOW",
    ]
    result = _run(cmd)
    manifest_path = out / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "manifest": manifest,
    }


def run_recover_apply(fixture: Path, apply_to: Path) -> dict:
    """Run recover in --apply-to mode and load the manifest."""
    cmd = [
        sys.executable, "-m", "deeppeek", "recover", str(fixture),
        "--apply-to", str(apply_to),
        "--llm-mode", "off",
        "--min-trust", "LOW",
    ]
    result = _run(cmd)
    manifest_path = apply_to / "deeppeek" / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "manifest": manifest,
    }


def _show_diff(exp_file: Path, act_file: Path) -> str:
    exp_lines = exp_file.read_text().splitlines(keepends=True)
    act_lines = act_file.read_text().splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        exp_lines, act_lines,
        fromfile=str(exp_file.name),
        tofile=str(act_file.name),
        lineterm="",
    ))
    if not diff:
        return "(no line-level difference; possibly trailing newline only)"
    out = []
    for line in diff:
        out.append(repr(line))
    return "\n".join(out)


def check(fixture_name: str, extra=None, skip_file_compare=False):
    """Standard-mode test: `--out` with no pre-existing tree."""
    fixture = FIXTURES / f"{fixture_name}.md"
    expected_dir = EXPECTED / fixture_name
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        run = run_recover(fixture, out)

        if not skip_file_compare:
            for exp_file in sorted(expected_dir.iterdir()):
                if not exp_file.is_file():
                    continue
                actual = out / "files" / exp_file.name
                assert actual.exists(), (
                    f"{fixture_name}: expected output missing: "
                    f"{exp_file.name}"
                )
                exp_lines = exp_file.read_text().splitlines()
                act_lines = actual.read_text().splitlines()
                if exp_lines != act_lines:
                    raise AssertionError(
                        f"{fixture_name}: {exp_file.name} mismatch\n"
                        + _show_diff(exp_file, actual)
                    )

        if extra:
            extra(out, run)


def check_apply(fixture_name: str, extra=None, skip_file_compare=False):
    """`--apply-to` mode: pre-existing tree provided by
    `preexisting/<fixture_name>/`."""
    fixture = FIXTURES / f"{fixture_name}.md"
    preexisting_dir = PREEXISTING / fixture_name
    expected_dir = EXPECTED / fixture_name

    assert preexisting_dir.is_dir(), (
        f"{fixture_name}: expected preexisting directory at "
        f"{preexisting_dir}"
    )
    if not skip_file_compare:
        assert expected_dir.is_dir(), (
            f"{fixture_name}: expected directory at {expected_dir}"
        )

    if KEEP_TMP:
        tmp_dir = tempfile.mkdtemp(prefix=f"deeppeek_test_{fixture_name}_")
        tmp_ctx = None
    else:
        tmp_ctx = tempfile.TemporaryDirectory()
        tmp_dir = tmp_ctx.name

    try:
        target = Path(tmp_dir) / "target"
        target.mkdir()

        for src in sorted(preexisting_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(preexisting_dir)
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text())

        run = run_recover_apply(fixture, target)

        if not skip_file_compare:
            for exp_file in sorted(expected_dir.rglob("*")):
                if not exp_file.is_file():
                    continue
                rel = exp_file.relative_to(expected_dir)
                actual = target / rel
                assert actual.exists(), (
                    f"{fixture_name}: expected output missing: {rel}"
                )
                exp_lines = exp_file.read_text().splitlines()
                act_lines = actual.read_text().splitlines()
                if exp_lines != act_lines:
                    raise AssertionError(
                        f"{fixture_name}: {rel} mismatch\n"
                        + _show_diff(exp_file, actual)
                    )

        if extra:
            extra(target, run)

        if KEEP_TMP:
            print(f"\n[debug] tempdir kept: {tmp_dir}")
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def test_01():
    check("01_reprint_later")


def test_02():
    check("02_subsection_patches")


def test_03():
    check("03_multi_fence")


def test_04():
    check("04_prose_override")


def test_05():
    def extra(out, run):
        missing = out / "MISSING_FILES.txt"
        assert missing.exists(), "MISSING_FILES.txt not written"
        txt = missing.read_text()
        assert "core_helpers.py" in txt, (
            f"expected core_helpers.py in MISSING_FILES.txt:\n{txt}"
        )
        mf = run["manifest"].get("run", {}).get("missing_files", [])
        assert "core_helpers.py" in mf, (
            f"expected core_helpers.py in manifest missing_files"
        )
    check("05_missing_file", extra)


def test_06():
    check("06_superset_wins")


def test_07():
    """Deletion shapes, semantic assertions.

    The exact blank-line count left by `_delete_exact_block` depends
    on the pre-existing file's spacing around the deleted block —
    one blank above plus one blank below becomes two adjacent
    blanks, but a file with only one blank total becomes one.  Both
    are correct outputs; neither is a bug.  Asserting on the number
    of blank lines would fail on some valid inputs and pass on
    others for no good reason.

    So this test asserts on what deeppeek is actually FOR:
      · the panelState block is gone from the file
      · the surrounding functions survive
      · no patch reported `failed`
      · trust is HIGH (i.e., verify didn't demote)
    """

    def extra(target, run):
        manifest = run["manifest"]
        files = manifest.get("files", {})
        assert "sample.js" in files, (
            f"expected sample.js in manifest files, got: {list(files)}"
        )
        rec = files["sample.js"]

        assert rec["trust"] == "HIGH", (
            f"sample.js: expected trust=HIGH, got {rec['trust']}\n"
            f"warnings: {rec.get('verification', {})}"
        )
        assert rec.get("preexisting_base") is True, (
            f"sample.js: expected preexisting_base=True, got "
            f"{rec.get('preexisting_base')}"
        )

        patch_statuses = [p["applied_by"] for p in rec["patches"]]
        assert "failed" not in patch_statuses, (
            f"sample.js: expected no failed patches, got "
            f"{patch_statuses}"
        )
        assert any(s in ("deterministic",) for s in patch_statuses), (
            f"sample.js: expected a deterministic patch, got "
            f"{patch_statuses}"
        )

        content = (target / "sample.js").read_text()

        # The block the patches targeted MUST be gone.
        assert "panelState" not in content, (
            f"sample.js: panelState block still present after "
            f"patches applied.  Full content:\n{content}"
        )
        assert "hovered: false" not in content, (
            f"sample.js: 'hovered: false' still present.  "
            f"Full content:\n{content}"
        )
        assert "lastActivity: 0" not in content, (
            f"sample.js: 'lastActivity: 0' still present.  "
            f"Full content:\n{content}"
        )

        # The untouched neighbours MUST survive.
        assert "keepMe" in content, (
            f"sample.js: keepMe function removed unexpectedly.  "
            f"Full content:\n{content}"
        )
        assert "return 42" in content, (
            f"sample.js: return 42 (keepMe body) missing.  "
            f"Full content:\n{content}"
        )
        assert "alsoKeepMe" in content, (
            f"sample.js: alsoKeepMe function removed unexpectedly.  "
            f"Full content:\n{content}"
        )
        assert "return 99" in content, (
            f"sample.js: return 99 (alsoKeepMe body) missing.  "
            f"Full content:\n{content}"
        )

        # Reports live in deeppeek/.
        reports_dir = target / "deeppeek"
        assert reports_dir.is_dir(), (
            f"sample.js: expected {reports_dir} to be created"
        )
        for name in ("manifest.json", "warnings.txt",
                     "deleted.txt", "MISSING_FILES.txt"):
            assert (reports_dir / name).is_file(), (
                f"expected deeppeek/{name} to exist"
            )

    check_apply("07_deletion_shapes", extra, skip_file_compare=True)


def test_08():
    """Replace-pair edits on an existing file (--apply-to mode).

    The fixture carries two 5-line `partial` blocks — an OLD and a
    NEW half — plus a `### Dark theme` heading between the prose and
    the OLD block.  In standard `--out` mode neither half is `full`,
    so the record would fall back to `PARTIAL_ONLY` and the pair
    would never apply (no base to operate on).  `--apply-to` mode
    provides the base from `preexisting/08_replace_pairs/colors.js`,
    which is what a replace-pair edit operates on in the first
    place.

    The byte comparison of `expected/08_replace_pairs/colors.js`
    against the post-run target is done by `check_apply` itself.
    The `extra` hook adds semantic assertions the byte compare
    cannot express: that the pair is recorded as applied, that the
    sanity gate did not reject the write, that trust stayed at HIGH,
    and that the pre-existing baseline is what the pair was applied
    on top of.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("colors.js", {})
        assert rec, (
            f"colors.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("verification", {}).get("pairs_applied", 0) == 1, (
            f"expected 1 pair applied, got: {rec}"
        )
        assert not rec.get("verification", {}).get("sanity_rejected"), (
            f"sanity gate rejected the write: {rec}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}"
        )
        assert rec.get("preexisting_base") is True, (
            f"expected preexisting_base=True, got "
            f"{rec.get('preexisting_base')}"
        )

        content = (target / "colors.js").read_text()
        assert "#88c0d0" in content, (
            f"new value missing from target:\n{content}"
        )
        assert "#ff0000" not in content, (
            f"old value still present in target:\n{content}"
        )
        assert "paint" in content, (
            f"file truncated — paint() gone:\n{content}"
        )
        assert "el.style.background = colors.secondary;" in content, (
            f"paint() body gone:\n{content}"
        )

    check_apply("08_replace_pairs", extra)


def test_09():
    """Replace-pair detection must fire when the second block's
    prose is a `with:`-style connective, even if the first block's
    prose does not match any specific `_REPLACE_PROSE` phrase.

    Reproduces the pg_inpaint.py failure: "Replace the doc comment
    above X:" / "with:" was silently missed, so the old comment
    block was dropped and the new comment block was dropped too,
    and the file was written with only the second half of each edit.

    The shape being tested: two adjacent partials, where the second
    one's prose begins with "with:" (or "to:", "becomes:",
    "new code:", etc.).  That is the structural tell for a replace
    pair — the AI writing "with:" between two code blocks is
    describing "the block above is old, the block below is new",
    regardless of what verb introduced the old block.

    The fixture also exercises "And replace the function body:" as
    the first block's prose, which was another form the old
    prose-driven detection missed.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("paint.js", {})
        assert rec, (
            f"paint.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("verification", {}).get("pairs_applied", 0) == 2, (
            f"expected 2 pairs applied, got: {rec}"
        )
        assert not rec.get("verification", {}).get("pairs_incomplete"), (
            f"a pair failed to apply: {rec}"
        )
        assert not rec.get("verification", {}).get("sanity_rejected"), (
            f"sanity gate rejected the write: {rec}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}"
        )

        content = (target / "paint.js").read_text()
        assert "New comment: v2" in content, (
            f"new comment missing from target:\n{content}"
        )
        assert "Old comment: v1" not in content, (
            f"old comment still present in target:\n{content}"
        )
        assert "#88c0d0" in content, (
            f"new color missing from target:\n{content}"
        )
        assert "#ff0000" not in content, (
            f"old color still present in target:\n{content}"
        )

    check_apply("09_replace_pair_docstring", extra)


def main():
    tests = [
        ("01", test_01), ("02", test_02), ("03", test_03),
        ("04", test_04), ("05", test_05), ("06", test_06),
        ("07", test_07), ("08", test_08), ("09", test_09),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"test_{name}: PASS")
        except AssertionError as e:
            failed += 1
            print(f"test_{name}: FAIL\n  {e}")
        except RuntimeError as e:
            failed += 1
            print(f"test_{name}: ERROR\n  {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} test(s) failed")
        return 1
    print(f"\nAll {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
