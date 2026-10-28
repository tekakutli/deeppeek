"""Unit tests for `pass8_7d_verify_already_present`.

The pass catches false-positive `already-present` claims — the
specific class of bug that silently dropped `comfyui.py`'s layer
recreation patch in the 2026-10-28 run.

NOTE ON TEST-FRAMEWORK: this repo uses a lightweight per-file
`main()` runner (see the bottom of every `test_*.py`), not pytest.
That means no `capsys` / `tmp_path` / `monkeypatch` fixtures.  Any
test that needs to inspect stderr must capture it itself, typically
with `contextlib.redirect_stderr(io.StringIO())`.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import Block, FileRecord
from deeppeek.recover.consistency import (
    _body_content_present,
    pass8_7d_verify_already_present,
)


def _rec(path="x.py", content="", patches=None, trust="HIGH"):
    rec = FileRecord(path=path, state="FULL_PLUS_PATCH")
    rec.content = content
    rec.patches = patches if patches is not None else []
    rec.trust = trust
    return rec


# ---------------------------------------------------------------------------
# _body_content_present
# ---------------------------------------------------------------------------

def test_body_content_present_exact():
    body = "def foo():\n    return 1\n"
    content = "prefix\ndef foo():\n    return 1\nsuffix\n"
    assert _body_content_present(body, content) is True


def test_body_content_present_with_whitespace_drift():
    body = "def foo():\n    return 1\n    return 2\n"
    content = "def foo():\n  return 1\n  return 2\n"
    # Line-stripped equivalence at 100%, above the 95% threshold.
    assert _body_content_present(body, content) is True


def test_body_content_absent():
    body = "def totally_new():\n    return 42\n"
    content = "def existing():\n    return 1\n"
    assert _body_content_present(body, content) is False


def test_body_content_partial_below_threshold():
    """The comfyui.py motivating shape: ~83% line match, below 95%."""
    body = "\n".join(f"line_{i}()" for i in range(6)) + "\nnew_stuff()\n"
    content = "\n".join(f"line_{i}()" for i in range(6)) + "\n"
    # 6/7 = 85.7%, under 95%.
    assert _body_content_present(body, content) is False


def test_body_content_empty_is_present():
    assert _body_content_present("", "anything") is True
    assert _body_content_present("   \n  ", "anything") is True


# ---------------------------------------------------------------------------
# pass8_7d_verify_already_present
# ---------------------------------------------------------------------------

def test_verifies_true_already_present_keeps_status():
    body = "def foo():\n    return 1\n"
    content = "prefix\ndef foo():\n    return 1\nsuffix\n"
    rec = _rec(
        path="a.py",
        content=content,
        patches=[{
            "idx": 1,
            "body": body,
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"a.py": rec})
    assert n == 0
    assert rec.patches[0]["applied_by"] == "already-present"
    assert rec.trust == "HIGH"


def test_flips_false_already_present_to_failed():
    """The specific bug: patch claims already-present but its bytes
    are not in base.  Pass flips to failed and demotes."""
    body = (
        "def comfy_set_layer_pixels(self, params):\n"
        "    import base64\n"
        "    new_stuff = 1\n"
        "    return {'success': True}\n"
    )
    # Base has some of the same lines but not the new body.
    content = (
        "def comfy_set_layer_pixels(self, params):\n"
        "    return {'success': False, 'message': 'old version'}\n"
    )
    rec = _rec(
        path="comfyui.py",
        content=content,
        patches=[{
            "idx": 652,
            "body": body,
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"comfyui.py": rec})
    assert n == 1
    assert rec.patches[0]["applied_by"] == "failed"
    assert rec.trust == "LOW"
    assert "652" in str(rec.verification.get("already_present_unverified"))
    assert any("contradicted by the file content" in w
               for w in rec.warnings)


def test_skips_diff_bodies():
    """Diff bodies aren't literally in base; the diff's own already-
    applied check (`_diff_already_applied`) covers them."""
    body = " unchanged()\n-removed()\n+added()\n"
    content = "unchanged()\nadded()\n"
    rec = _rec(
        content=content,
        patches=[{
            "idx": 1,
            "body": body,
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 0
    assert rec.patches[0]["applied_by"] == "already-present"


def test_skips_deletion_instructions():
    """Deletion bodies are *supposed* to be absent."""
    body = "def old_helper():\n    return 1\n"
    ctx = "Delete this block:"
    content = ""  # deletion succeeded
    rec = _rec(
        content=content,
        patches=[{
            "idx": 1,
            "body": body,
            "context_before": ctx,
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 0
    assert rec.patches[0]["applied_by"] == "already-present"


def test_skips_empty_bodies():
    rec = _rec(
        content="anything",
        patches=[{
            "idx": 1,
            "body": "   \n  ",
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 0


def test_skips_deterministic_status():
    """Only already-present is verified; deterministic is 8.7c's job."""
    body = "def foo():\n    return 1\n"
    rec = _rec(
        content="",  # body not present
        patches=[{
            "idx": 1,
            "body": body,
            "applied_by": "deterministic",
        }],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 0
    assert rec.patches[0]["applied_by"] == "deterministic"


def test_skips_deleted_record():
    rec = _rec(
        content="",
        patches=[{
            "idx": 1,
            "body": "def foo():\n    return 1\n",
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )
    rec.is_deleted = True
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 0


def test_skips_none_content():
    rec = FileRecord(path="x.py", state="PARTIAL_ONLY")
    rec.content = None
    rec.patches = [{"idx": 1, "body": "foo()",
                    "applied_by": "already-present"}]
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 0


def test_multiple_patches_flips_only_wrong_ones():
    """Two already-present patches: one real, one false."""
    good_body = "def keep_me():\n    return 1\n"
    bad_body = "def totally_new():\n    return 999\n"
    content = "def keep_me():\n    return 1\n"
    rec = _rec(
        content=content,
        patches=[
            {"idx": 1, "body": good_body, "applied_by": "already-present"},
            {"idx": 2, "body": bad_body, "applied_by": "already-present"},
        ],
        trust="HIGH",
    )
    n = pass8_7d_verify_already_present({"x.py": rec})
    assert n == 1
    assert rec.patches[0]["applied_by"] == "already-present"
    assert rec.patches[1]["applied_by"] == "failed"
    assert rec.trust == "LOW"


def test_verbose_mode_prints():
    """Sanity: verbose=True emits diagnostic lines to stderr.

    This repo uses a per-file `main()` runner, not pytest — no
    `capsys` fixture available.  Capture stderr manually.
    """
    import io
    from contextlib import redirect_stderr

    body = "def new():\n    pass\n"
    rec = _rec(
        content="",
        patches=[{
            "idx": 1,
            "body": body,
            "applied_by": "already-present",
        }],
        trust="HIGH",
    )

    buf = io.StringIO()
    with redirect_stderr(buf):
        pass8_7d_verify_already_present({"x.py": rec}, verbose=True)

    captured = buf.getvalue()
    assert "8.7d" in captured, (
        f"expected '8.7d' in stderr, got: {captured!r}"
    )
    assert "x.py" in captured, (
        f"expected record path in stderr, got: {captured!r}"
    )


def main():
    tests = [
        ("body content present exact", test_body_content_present_exact),
        ("body content present with whitespace drift",
         test_body_content_present_with_whitespace_drift),
        ("body content absent", test_body_content_absent),
        ("body content partial below threshold",
         test_body_content_partial_below_threshold),
        ("body content empty is present",
         test_body_content_empty_is_present),
        ("verifies true already-present keeps status",
         test_verifies_true_already_present_keeps_status),
        ("flips false already-present to failed",
         test_flips_false_already_present_to_failed),
        ("skips diff bodies", test_skips_diff_bodies),
        ("skips deletion instructions",
         test_skips_deletion_instructions),
        ("skips empty bodies", test_skips_empty_bodies),
        ("skips deterministic status", test_skips_deterministic_status),
        ("skips deleted record", test_skips_deleted_record),
        ("skips none content", test_skips_none_content),
        ("multiple patches flips only wrong ones",
         test_multiple_patches_flips_only_wrong_ones),
        ("verbose mode prints", test_verbose_mode_prints),
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
        print(f"\n{failed}/{len(tests)} consistency test(s) failed")
        return 1
    print(f"\nAll {len(tests)} consistency tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
