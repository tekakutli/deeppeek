# tests/recover/test_diagnose_cumulative.py

def test_diagnose_uses_cumulative_context():
    """Patch 634's shape: adding `errors` to a return block that is
    only valid because an *earlier* patch (633) added the `errors`
    list. Diagnosing against the bare base reports a misleading
    'unresolved symbol'; diagnosing against cumulative state (633
    applied) says the patch is valid."""
    from deeppeek.recover.reconcile import _diagnose_exclusion
    from deeppeek.recover.core import FileRecord

    rec = FileRecord(path="inpaint_server.py", state="FULL_PLUS_PATCH")
    rec.patches = [
        {"idx": 633, "body": "...", "applied_by": "deterministic"},
        {
            "idx": 634,
            "body": (
                "return {\n"
                '    "success": False,\n'
                '    "message": detail,\n'
                '    "errors": captured["errors"],\n'
                '    "timing": round(elapsed, 3),\n'
                "}\n"
            ),
            "applied_by": "deterministic",
        },
    ]
    # cumulative_base for patch 634: base + patch 633 applied.
    cumulative = (
        "def handle_inpaint(payload):\n"
        "    captured = {'errors': []}\n"
        "    detail = 'no image'\n"
        "    elapsed = 0.0\n"
        "    return {\n"
        '        "success": False,\n'
        '        "message": detail,\n'
        '        "timing": round(elapsed, 3),\n'
        "    }\n"
    )
    diag = _diagnose_exclusion(
        ("patch", 634), rec, {},
        original_status="deterministic",
        cumulative_base=cumulative,
    )
    assert "applies cleanly" in diag, (
        f"diagnostic should identify the false-positive exclusion; got "
        f"{diag!r}")
