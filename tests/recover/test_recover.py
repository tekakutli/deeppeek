#!/usr/bin/env python3
"""Fixture tests for recover.py.

Fixtures 01–06 exercise the standard `--out` path (self-contained
markdown, no pre-existing tree).

Fixtures 07, 08, and 09 exercise the `--apply-to` path: the fixture
markdown carries only patches, and the pre-existing source tree is
provided by `preexisting/<fixture>/`.

Fixture 09 tests that pair detection fires when the SECOND block's
prose is a `with:`-style connective, even if the FIRST block's prose
does not match any specific `_REPLACE_PROSE` phrase.  This is the
shape-driven detection rule that replaced the old prose-driven rule
after the pg_inpaint.py failure.

Fixture 10 tests the scoped-replace fallback: a single block whose
prose says "Replace the X `<details>` with:" and whose body contains
`<summary>Inpainting</summary>` is treated as a scoped replace
against the pre-existing baseline, even though no explicit scope
name appears in prose.

Fixture 11 tests fail-closed behaviour: a replace pair whose old
body does NOT match the pre-existing baseline causes the record to
be marked LOW trust and the file is NOT written to the target tree
under default `--min-trust MEDIUM`.  The attempt lands in
`deeppeek/low_trust/`; the target stays as-is.

Run:
    python tests/recover/test_recover.py

Set DEEPPEEK_TEST_KEEP=1 to preserve the temp directory on failure
for manual inspection.

Exits 0 on success, 1 on any failure.
"""

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


def run_recover_apply(fixture: Path, apply_to: Path,
                     min_trust: str = "LOW") -> dict:
    """Run recover in --apply-to mode and load the manifest.

    `min_trust` defaults to "LOW" to match the other --apply-to
    tests (which want to see everything including partial
    applications).  Fixture 11 overrides this to "MEDIUM" because
    it asserts the fail-closed default: under MEDIUM, a LOW-trust
    record is NOT written to the target tree.
    """
    cmd = [
        sys.executable, "-m", "deeppeek", "recover", str(fixture),
        "--apply-to", str(apply_to),
        "--llm-mode", "off",
        "--min-trust", min_trust,
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


def check_apply(fixture_name: str, extra=None, skip_file_compare=False,
                min_trust: str = "LOW"):
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

        run = run_recover_apply(fixture, target, min_trust=min_trust)

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


def test_10():
    """Scoped-replace fallback: a single partial block whose prose
    says "Replace the Inpainting `<details>` with:" and whose body
    carries `<summary>Inpainting</summary>` is treated as a scoped
    replace, even without an explicit name in prose.

    This is the fix for the pg_base.py failure: the old
    `_SCOPED_REPLACE_RE` required "for" / "named" between the thing
    and its name, so "Replace the Inpainting `<details>` with:" did
    not match.  Now the scope name is extracted from the block body
    directly via `_extract_scope_from_body`.

    The test uses semantic assertions rather than byte-compare
    because the exact indentation produced by `_reindent_body` is
    an implementation detail that would need to be kept in lockstep
    with the test.  The semantic checks are enough to prove the
    scope replacement worked without collateral damage.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("sample.html", {})
        assert rec, (
            f"sample.html missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("verification", {}).get("pairs_applied", 0) == 1, (
            f"expected 1 pair applied (scoped), got: {rec}"
        )
        assert not rec.get("verification", {}).get("pairs_incomplete"), (
            f"the scoped pair failed: {rec}"
        )
        assert not rec.get("verification", {}).get("sanity_rejected"), (
            f"sanity gate rejected the write: {rec}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}"
        )

        content = (target / "sample.html").read_text()

        # New content is present.
        assert "New label" in content, (
            f"new label missing from target:\n{content}"
        )
        assert "inpaintNewFeature" in content, (
            f"new input id missing from target:\n{content}"
        )

        # Old content is gone.
        assert "Old label" not in content, (
            f"old label still present in target:\n{content}"
        )
        assert "inpaintOldFeature" not in content, (
            f"old input id still present in target:\n{content}"
        )

        # The other `<details>` blocks survive untouched.
        assert "image content" in content, (
            f"Image details block lost:\n{content}"
        )
        assert "other content" in content, (
            f"Other details block lost:\n{content}"
        )
        assert content.count("<details") == 3, (
            f"expected 3 details blocks, got "
            f"{content.count('<details')}:\n{content}"
        )

    check_apply("10_scoped_details", extra, skip_file_compare=True)


def test_11():
    """Fail-closed on pair failure.

    Fixture 11 has a replace pair whose old body is
    `export const state = { value: 999, };` — but the pre-existing
    baseline has `value: 1`.  The pair cannot apply: `_try_replace_pair`
    finds no match and returns None.

    Under default `--min-trust MEDIUM` (the fail-closed target),
    the record is marked LOW trust, the file is NOT written to the
    target tree, and the attempt lands in `deeppeek/low_trust/`.
    The target `sample.js` stays exactly as it was pre-existing.

    This is the core robustness fix: without it, the file would be
    written with the pair half-applied (the old body would be
    treated as "not found" and the new body silently dropped),
    corrupting the target tree without a clear failure.
    """

    def extra(target, run):
        # Manifest-level assertions.
        decisions = run["manifest"].get("decisions", {})
        dec = decisions.get("sample.js", {})
        assert dec, (
            f"sample.js missing from manifest decisions; "
            f"decisions keys: {list(decisions)}"
        )
        assert dec.get("trust") == "LOW", (
            f"expected trust=LOW for sample.js, got "
            f"{dec.get('trust')}; verification: "
            f"{dec.get('verification')}"
        )
        assert dec.get("verification", {}).get("pairs_incomplete"), (
            f"expected pairs_incomplete=true, got "
            f"{dec.get('verification')}"
        )
        assert dec.get("written") is False, (
            f"expected written=False (fail-closed), got "
            f"{dec.get('written')}"
        )

        # target untouched.
        content = (target / "sample.js").read_text()
        assert "value: 1" in content, (
            f"expected original 'value: 1' in target, got:\n{content}"
        )
        assert "value: 999" not in content, (
            f"expected 'value: 999' NOT in target, got:\n{content}"
        )
        assert "extra:" not in content, (
            f"expected 'extra:' NOT in target, got:\n{content}"
        )
        assert "value: 2" not in content, (
            f"expected 'value: 2' NOT in target, got:\n{content}"
        )

        # Attempt landed in low_trust/ (the unchanged base content).
        low_trust = target / "deeppeek" / "low_trust" / "sample.js"
        assert low_trust.exists(), (
            f"expected {low_trust} to exist (LOW trust file)"
        )

    check_apply("11_fail_closed", extra, skip_file_compare=True,
                min_trust="MEDIUM")



def test_12():
    """Context insert (2-line snippet, anchor-last).

    Fixture 12 is the pg_state.py shape: a 2-line snippet where the
    second line (`inpaintPrompt: "",`) is a unique anchor in the
    base and the first line (`inpaintFullCanvas: false,`) is new.
    `_try_context_insert` inserts the new line BEFORE the anchor.

    Asserts on the natural outcome: the new field is present, the
    anchor field is still there, and the surrounding fields
    (`imageHash`, `running`) survive.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("state.js", {})
        assert rec, (
            f"state.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}"
        )
        content = (target / "state.js").read_text()
        assert "inpaintFullCanvas: false" in content, (
            f"new field missing:\n{content}"
        )
        assert 'inpaintPrompt: ""' in content, (
            f"anchor field gone:\n{content}"
        )
        assert "imageHash: null" in content, (
            f"pre-existing field (imageHash) gone:\n{content}"
        )
        assert "running: false" in content, (
            f"pre-existing field (running) gone:\n{content}"
        )

    check_apply("12_context_insert", extra)


def test_13():
    """Scoped-replace via add verb.

    Fixture 13 is the pg_view_inpaint.py shape: prose says "add X
    to Y", and the body is a full replacement of `Y`.  The pairing
    fallback fires (add verb + scope marker in body), so the record
    becomes a scoped-replace rather than a silently-dropped partial.

    Asserts the new line is added, the function structure survives,
    and the trailing content (ctx.fillRect) is preserved.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("view.js", {})
        assert rec, (
            f"view.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}"
        )
        assert rec.get("verification", {}).get("pairs_applied", 0) == 1, (
            f"expected 1 pair applied (scoped via add verb), got: {rec}"
        )
        content = (target / "view.js").read_text()
        assert "if (board.inpaintFullCanvas) return;" in content, (
            f"new early-out missing:\n{content}"
        )
        assert "if (!board.imageEl) return;" in content, (
            f"existing early-out lost:\n{content}"
        )
        assert "ctx.fillRect(0, 0, 100, 100);" in content, (
            f"function body lost:\n{content}"
        )

    check_apply("13_scoped_add_verb", extra)


def test_14():
    """Object-entry insert into named function.

    Fixture 14 is the pg_panel_fields.py shape: 2 lines of object
    entries and prose naming `_get()`.  No anchor is inside the block;
    the anchor is in prose.  `_try_object_entry_insert` finds `_get`
    in base, locates its `_els = { ... }` object literal, and inserts
    the entries before the closing `}`.

    Asserts the entries land inside the object, the surrounding
    fields and the `return _els;` survive, and the exported symbol
    still exists.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("panel.js", {})
        assert rec, (
            f"panel.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}"
        )
        content = (target / "panel.js").read_text()
        assert "inpaintFullCanvas" in content, (
            f"new entry missing:\n{content}"
        )
        assert "inpaintRectHint" in content, (
            f"new entry missing:\n{content}"
        )
        assert "imageInfo" in content, (
            f"pre-existing entry gone:\n{content}"
        )
        assert "maskCount" in content, (
            f"pre-existing entry gone:\n{content}"
        )
        assert "return _els;" in content, (
            f"return statement gone:\n{content}"
        )
        assert "export { _get };" in content, (
            f"export gone:\n{content}"
        )

    check_apply("14_object_entry_insert", extra)


def test_15():
    """Statement anchor insert.

    Fixture 15 is the pg_panel_fields block-42 shape: prose names an
    existing *statement* (`` `e.promptEl.disabled = busy;` ``) in
    backticks, and the block body should be inserted after that
    statement's line.  Tests `_try_insert_after_anchor`'s type-3
    pattern (backtick content >= 6 chars, unique in base).

    Asserts the new if-block is inserted at the correct indentation,
    the surrounding function survives, and the export is intact.
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("helper.js", {})
        assert rec, (
            f"helper.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}"
        )
        content = (target / "helper.js").read_text()
        assert "e.fullCanvasEl" in content, (
            f"new block missing:\n{content}"
        )
        assert "if (e.promptEl) e.promptEl.disabled = busy;" in content, (
            f"anchor line gone:\n{content}"
        )
        assert "return e;" in content, (
            f"return statement gone:\n{content}"
        )
        assert "export { _get };" in content, (
            f"export gone:\n{content}"
        )
        # Sanity: new if-block should appear AFTER the anchor.
        anchor_pos = content.index("e.promptEl.disabled = busy;")
        new_pos = content.index("e.fullCanvasEl")
        assert new_pos > anchor_pos, (
            f"new block inserted BEFORE the anchor (should be after):"
            f"\n{content}"
        )

    check_apply("15_statement_anchor", extra)


def test_16():
    """Silent drop now fails closed.

    Fixture 16 exercises the new policy: a `partial` block with code
    signal that has no anchor (in body or in prose) is attempted,
    fails all heuristics, and drives the record to LOW trust under
    default --min-trust MEDIUM.  The target file is preserved.

    Before 2026-11-01c, this block would have been silently dropped
    (`_is_real_patch` returned False because the prose had no
    add-family verb).  Now it's a patch candidate and fail-closed
    turns the silent drop into a visible failure.
    """

    def extra(target, run):
        decisions = run["manifest"].get("decisions", {})
        dec = decisions.get("helper.js", {})
        assert dec, (
            f"helper.js missing from manifest decisions; "
            f"decisions keys: {list(decisions)}"
        )
        assert dec.get("trust") == "LOW", (
            f"expected trust=LOW, got {dec.get('trust')}; "
            f"verification: {dec.get('verification')}"
        )
        assert dec.get("written") is False, (
            f"expected written=False (fail-closed), got "
            f"{dec.get('written')}"
        )
        # The block was *attempted*, not silently dropped.
        assert dec.get("patch_count", 0) == 1, (
            f"expected 1 patch attempted, got "
            f"{dec.get('patch_count')}; the block was silently dropped"
        )
        assert len(dec.get("patch_failures", [])) == 1, (
            f"expected 1 failed patch, got "
            f"{dec.get('patch_failures')}"
        )

        # Target unchanged.
        content = (target / "helper.js").read_text()
        assert "mysteriousHelper" not in content, (
            f"mysteriousHelper applied to target unexpectedly:\n"
            f"{content}"
        )
        assert "return 1;" in content, (
            f"original function body gone:\n{content}"
        )

        # Low-trust copy exists.
        low_trust = target / "deeppeek" / "low_trust" / "helper.js"
        assert low_trust.exists(), (
            f"expected {low_trust} to exist (LOW trust file)"
        )

    check_apply("16_silent_drop_fails_closed", extra, min_trust="MEDIUM")


def test_17():
    """Patch failure diagnostics name the block.

    Fixture 17 is an `export const` splice that cannot be applied
    (no matching base declaration).  Under default --min-trust
    MEDIUM, the record goes to LOW, the target is preserved, and the
    manifest records `patch_failure_reasons` with the block's first
    line and a specific reason string.

    Asserts the diagnostic chain:
      · decisions[helper.js].patch_failure_reasons is non-empty
      · its entry has idx, first_line, reason
      · warnings.txt contains [PATCH_FAILED] with the first line
    """

    def extra(target, run):
        decisions = run["manifest"].get("decisions", {})
        dec = decisions.get("helper.js", {})
        assert dec, (
            f"helper.js missing from manifest decisions; "
            f"decisions keys: {list(decisions)}"
        )
        assert dec.get("trust") == "LOW", (
            f"expected trust=LOW, got {dec.get('trust')}"
        )
        assert dec.get("written") is False, (
            f"expected written=False (fail-closed), got "
            f"{dec.get('written')}"
        )
        reasons = dec.get("patch_failure_reasons", [])
        assert len(reasons) == 1, (
            f"expected 1 patch_failure_reasons entry, got "
            f"{len(reasons)}: {reasons}"
        )
        entry = reasons[0]
        assert entry.get("first_line"), (
            f"patch_failure_reasons entry missing first_line: {entry}"
        )
        assert entry.get("reason"), (
            f"patch_failure_reasons entry missing reason: {entry}"
        )
        assert entry.get("idx") is not None, (
            f"patch_failure_reasons entry missing idx: {entry}"
        )
        # The reason should name the shape.
        reason = entry["reason"]
        assert "export const" in reason or "splice" in reason, (
            f"expected reason to name the export-const splice shape, "
            f"got: {reason!r}"
        )

        warnings_path = target / "deeppeek" / "warnings.txt"
        assert warnings_path.exists(), (
            f"warnings.txt not created at {warnings_path}"
        )
        warnings_text = warnings_path.read_text()
        assert "[PATCH_FAILED]" in warnings_text, (
            f"warnings.txt missing [PATCH_FAILED] line:\n"
            f"{warnings_text}"
        )

        content = (target / "helper.js").read_text()
        assert "totallyNew" not in content, (
            f"totallyNew applied to target unexpectedly:\n{content}"
        )
        assert "return 1;" in content, (
            f"original body gone:\n{content}"
        )

    check_apply("17_diagnostic_naming", extra, min_trust="MEDIUM")


def test_18():
    """Ellipsis-splice for a scope-replace block.

    Fixture 18 exercises the pg_view_inpaint.py shape: an AI block
    that shows the new prefix of a function, then `...`, then `}`.
    The old behavior refused this as truncation.  The new behavior
    splices: insert only genuinely-new lines (the `if (board.fullCanvas)`
    guard) at their position within the base scope, preserving the
    base's middle content unchanged.

    Asserts:
      · trust is HIGH (patch applied, not fail-closed)
      · the new guard is present, positioned before the existing guard
      · the base's middle content (ctx.fillRect) survives verbatim
      · the closing brace is intact
    """

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("view.js", {})
        assert rec, (
            f"view.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}, "
            f"warnings: {rec.get('warnings')}"
        )
        content = (target / "view.js").read_text()

        assert "if (board.fullCanvas) return;" in content, (
            f"new guard missing:\n{content}"
        )
        assert "if (!board.imageEl) return;" in content, (
            f"existing guard lost:\n{content}"
        )
        assert "ctx.fillRect(0, 0, 100, 100);" in content, (
            f"base body content lost (middle not preserved):\n{content}"
        )

        # New guard must come BEFORE the existing guard.
        new_pos = content.index("if (board.fullCanvas) return;")
        old_pos = content.index("if (!board.imageEl) return;")
        assert new_pos < old_pos, (
            f"new guard should precede existing guard (was "
            f"{new_pos} vs {old_pos}):\n{content}"
        )

        # No `...` in the result — the splice must have consumed it.
        for ln in content.splitlines():
            assert ln.strip() != "...", (
                f"raw ellipsis leaked into content:\n{content}"
            )

    check_apply("18_ellipsis_splice", extra)


def _ast_available():
    try:
        from deeppeek.recover.ast_anchor import is_available
        return is_available()
    except Exception:
        return False


def test_19():
    """AST anchor: statement inside enclosing function.

    Requires tree-sitter + tree-sitter-javascript (the `[ast]` extra).
    Skips silently if unavailable.  Asserts the block is inserted at
    the end of `syncPanelFields` (the enclosing function that declares
    `e`), preserving the base's other statements verbatim.
    """
    if not _ast_available():
        return  # skip

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("helper.js", {})
        assert rec, (
            f"helper.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}, "
            f"warnings: {rec.get('warnings')}"
        )
        content = (target / "helper.js").read_text()
        assert "if (e.inpaintFullCanvas" in content, (
            f"new if-block missing:\n{content}"
        )
        assert "if (e.imageInfo)" in content, (
            f"existing statement lost:\n{content}"
        )
        assert "if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;" in content, (
            f"trailing statement lost:\n{content}"
        )
        # New if-block must be inside the function, before the
        # closing brace, and AFTER the last existing statement.
        close_brace_pos = content.rfind("}")
        new_pos = content.index("if (e.inpaintFullCanvas")
        assert new_pos < close_brace_pos, (
            f"new block placed outside the function body:\n{content}"
        )
        last_existing = content.index(
            "if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;")
        assert new_pos > last_existing, (
            f"new block placed BEFORE the last statement:\n{content}"
        )

    check_apply("19_ast_enclosing_function", extra)


def test_20():
    """AST anchor: top-level declaration block.

    Requires tree-sitter + tree-sitter-javascript (the `[ast]` extra).
    Skips silently if unavailable.  Asserts the block is appended
    after the last top-level `if_statement` in base.
    """
    if not _ast_available():
        return  # skip

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("panel.js", {})
        assert rec, (
            f"panel.js missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}, "
            f"warnings: {rec.get('warnings')}"
        )
        content = (target / "panel.js").read_text()
        assert "inpaintFullCanvasEl" in content, (
            f"new declaration missing:\n{content}"
        )
        assert "inpaintPromptEl" in content, (
            f"existing declaration lost:\n{content}"
        )
        # New block must be after the existing pair.
        new_pos = content.index("inpaintFullCanvasEl")
        old_pos = content.index("inpaintPromptEl")
        assert new_pos > old_pos, (
            f"new block placed BEFORE existing block:\n{content}"
        )
        # No ellipsis leaked.
        for ln in content.splitlines():
            assert ln.strip() != "...", (
                f"raw ellipsis leaked:\n{content}"
            )

    check_apply("20_ast_top_level", extra)


def test_21():
    """AST anchor: JS embedded in a Python string literal.

    Requires tree-sitter + tree-sitter-javascript (the [ast] extra).
    Skips silently if unavailable.  Asserts the JS block is inserted
    inside the Python string, at the end of syncPanelFields, and that
    the surrounding Python structure (docstring, assignment, closing
    triple-quote) is preserved verbatim.
    """
    if not _ast_available():
        return  # skip

    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("helper.py", {})
        assert rec, (
            f"helper.py missing from manifest files; "
            f"manifest files: {list(run['manifest'].get('files', {}))}"
        )
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}, "
            f"warnings: {rec.get('warnings')}"
        )
        content = (target / "helper.py").read_text()
        assert "Fixture 21 helper" in content, (
            f"Python docstring at top lost:\n{content}"
        )
        assert "PANEL_FIELDS_JS = r" in content, (
            f"Python string assignment lost:\n{content}"
        )
        assert "if (e.inpaintFullCanvas" in content, (
            f"new JS if-block missing:\n{content}"
        )
        assert "if (e.imageInfo)" in content, (
            f"existing JS statement lost:\n{content}"
        )
        assert "if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;" in content, (
            f"trailing JS statement lost:\n{content}"
        )
        # New if-block must be INSIDE the string literal.
        q3 = '"' * 3
        first_open = content.index(q3)
        second_open = content.index(q3, first_open + 3)  # docstring close
        third_open = content.index(q3, second_open + 3)   # string open
        fourth_close = content.index(q3, third_open + 3)  # string close
        new_pos = content.index("if (e.inpaintFullCanvas")
        assert third_open < new_pos < fourth_close, (
            f"new block placed OUTSIDE the string literal:\n{content}"
        )
        last_existing = content.index(
            "if (e.inpaintPrompt) e.inpaintPrompt.disabled = busy;")
        assert new_pos > last_existing, (
            f"new block placed BEFORE the last JS statement:\n{content}"
        )

    check_apply("21_ast_embedded_js", extra)

def test_24():
    """Structural append: statement inside embedded JS.

    Reproduces the pg_panel_fields.py failure with NO AST dependency.
    The block is a bare `if (e.inpaintFullCanvas...)` guard; base
    has a `syncPanelFields` function inside a Python string.  The
    step-9 text fallback finds the last top-level function and
    inserts before its close brace.
    """
    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("helper.py", {})
        assert rec, "helper.py missing from manifest files"
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}, "
            f"warnings: {rec.get('warnings')}"
        )
        content = (target / "helper.py").read_text()
        assert "inpaintFullCanvas" in content, (
            f"new block missing:\n{content}"
        )
        assert "e.imageInfo" in content, (
            f"existing content lost:\n{content}"
        )
        assert "e.inpaintPrompt" in content, (
            f"trailing statement lost:\n{content}"
        )
        close = content.rfind("}")
        new_pos = content.index("if (e.inpaintFullCanvas")
        assert new_pos < close, (
            f"new block outside function body:\n{content}"
        )

    check_apply("24_structural_append_stmt", extra)


def test_25():
    """Structural append: declaration after existing pair.

    Reproduces the pg_panel_scrubs.py failure with NO AST dependency.
    The block is a `const X = ...; if (X) {...}` pair; base has
    an existing `const Y = document.getElementById(...); if (Y)
    {...}` block.  The RHS pattern matches (string literals
    abstracted) and the block is inserted after the existing pair.
    """
    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("scrubs.py", {})
        assert rec, "scrubs.py missing from manifest files"
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}; "
            f"verification: {rec.get('verification')}, "
            f"warnings: {rec.get('warnings')}"
        )
        content = (target / "scrubs.py").read_text()
        assert "inpaintFullCanvasEl" in content, (
            f"new block missing:\n{content}"
        )
        assert "inpaintPromptEl" in content, (
            f"existing block lost:\n{content}"
        )
        # New block must be after the existing if-block closes.
        existing_close = content.index("});\n}")
        new_start = content.index("inpaintFullCanvasEl")
        assert new_start > existing_close, (
            f"new block inserted inside existing block:\n{content}"
        )
        assert "export { };" in content, (
            f"trailing export lost:\n{content}"
        )

    check_apply("25_structural_append_decl", extra)


def test_26():
    """Unbalanced block must fail-closed.

    The block is a 3-line fragment with one open brace and zero
    close braces — exactly the pg_panel_fields.py shape.  Step-9
    structural append refuses to append it; pass8_verify's
    `validate_content` catches the resulting file anyway.  Target
    file preserved; block lands in `low_trust/`.
    """
    def extra(target, run):
        decisions = run["manifest"].get("decisions", {})
        dec = decisions.get("broken.js", {})
        assert dec, "broken.js missing from manifest decisions"
        assert dec.get("written") is False, (
            f"expected written=False (fail-closed), got "
            f"{dec.get('written')}"
        )
        assert dec.get("trust") == "LOW", (
            f"expected trust=LOW, got {dec.get('trust')}"
        )
        content = (target / "broken.js").read_text()
        assert "export function handler" in content, (
            f"base function gone:\n{content}"
        )
        assert "cond" not in content, (
            f"broken block leaked to target:\n{content}"
        )

    check_apply("26_unbalanced_fails_closed", extra, skip_file_compare=True, min_trust="MEDIUM")


def test_27():
    """Deleted-function regression must fail-closed.

    Base has two exported functions in an embedded JS string; a
    scoped-replace attempts to empty the `deleteMe` block.  The
    function-presence check in `validate_content` catches the
    missing declaration and routes the file to `low_trust/`.
    """
    def extra(target, run):
        decisions = run["manifest"].get("decisions", {})
        dec = decisions.get("deleted.py", {})
        assert dec, "deleted.py missing from manifest decisions"
        assert dec.get("written") is False, (
            f"expected written=False (fail-closed), got "
            f"{dec.get('written')}"
        )
        content = (target / "deleted.py").read_text()
        assert "keepMe" in content, (
            f"base function gone:\n{content}"
        )
        assert "deleteMe" in content, (
            f"base function gone:\n{content}"
        )

    check_apply("27_deleted_function_fails_closed", extra, skip_file_compare=True, min_trust="MEDIUM")


def test_22():
    """Fuzzy replace-pair: exact and normalized fail, superset
    similarity succeeds.  Reproduces the pg_dispatch.py shape."""
    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("target.js", {})
        assert rec, "target.js missing from manifest files"
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}")
        assert rec.get("verification", {}).get("pairs_applied", 0) == 1, (
            f"expected 1 pair applied, got {rec}")
        content = (target / "target.js").read_text()
        assert "Skipped when the rect is hidden" in content
        assert "board.inpaintEnabled && r" not in content
        assert "!board.inpaintFullCanvas" in content

    check_apply("22_fuzzy_replace_pair", extra)


def test_23():
    """Identifier-block anchor: const+if insertion after whole pair.
    Reproduces the pg_panel_scrubs.py shape."""
    def extra(target, run):
        rec = run["manifest"].get("files", {}).get("scrubs.js", {})
        assert rec, "scrubs.js missing from manifest files"
        assert rec.get("trust") == "HIGH", (
            f"expected trust=HIGH, got {rec.get('trust')}")
        content = (target / "scrubs.js").read_text()
        assert "inpaintFullCanvasEl" in content
        assert "inpaintPromptEl" in content
        existing_close = content.index("});\n}")
        new_start = content.index("inpaintFullCanvasEl")
        assert new_start > existing_close, (
            f"new block inside existing if-block:\n{content}")
        assert "export { };" in content

    check_apply("23_identifier_block_anchor", extra)


def main():
    tests = [
        ("01", test_01), ("02", test_02), ("03", test_03),
        ("04", test_04), ("05", test_05), ("06", test_06),
        ("07", test_07), ("08", test_08), ("09", test_09),
        ("10", test_10), ("11", test_11),
        ("12", test_12), ("13", test_13), ("14", test_14),
        ("15", test_15), ("16", test_16), ("17", test_17), ("19", test_19), ("20", test_20), ("21", test_21),
        ("18", test_18), ("22", test_22), ("23", test_23), ("24", test_24), ("25", test_25), ("26", test_26), ("27", test_27)
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
