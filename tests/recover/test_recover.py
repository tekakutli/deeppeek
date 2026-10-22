#!/usr/bin/env python3
'''
test_recover.py — fixture tests for recover.py.

Run:
    python tests/test_recover.py

Exits 0 on success, 1 on any failure.
'''

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
EXPECTED = HERE / "expected"
DEEPPEEK = HERE.parents[1]


def run_recover(fixture: Path, out: Path) -> dict:
    cmd = [
        sys.executable, "-m", "deeppeek", "recover", str(fixture),
        "--out", str(out),
        "--llm-mode", "off",
        "--min-trust", "LOW",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode > 4:
        raise RuntimeError(
            f"recover exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
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


def check(fixture_name: str, extra=None):
    fixture = FIXTURES / f"{fixture_name}.md"
    expected_dir = EXPECTED / fixture_name
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        run = run_recover(fixture, out)
        for exp_file in sorted(expected_dir.iterdir()):
            if not exp_file.is_file():
                continue
            actual = out / "files" / exp_file.name
            assert actual.exists(), (
                f"{fixture_name}: expected output missing: {exp_file.name}"
            )
            exp_lines = exp_file.read_text().splitlines()
            act_lines = actual.read_text().splitlines()
            assert exp_lines == act_lines, (
                f"{fixture_name}: {exp_file.name} mismatch\n"
                f"EXPECTED:\n{chr(10).join(exp_lines)}\n"
                f"ACTUAL:\n{chr(10).join(act_lines)}"
            )
        if extra:
            extra(out, run)


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


def main():
    tests = [
        ("01", test_01), ("02", test_02), ("03", test_03),
        ("04", test_04), ("05", test_05), ("06", test_06),
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
