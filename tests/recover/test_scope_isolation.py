# tests/recover/test_scope_isolation.py

def test_scope_isolation_rejects_sibling_change():
    from deeppeek.recover.patch_scope import _verify_scope_isolation
    base = (
        "function _pickMaskAction() {\n"
        "  input.addEventListener('change', onMask);\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "}\n"
    )
    # `_clearImageAction` replaced, but `_pickMaskAction` lost the
    # addEventListener. Bug 2.
    bad_result = (
        "function _pickMaskAction() {\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  clearRotation();\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, bad_result, "_clearImageAction", "x.js") is False

    # Only the target changed; siblings byte-identical.
    good_result = (
        "function _pickMaskAction() {\n"
        "  input.addEventListener('change', onMask);\n"
        "}\n"
        "function _clearImageAction() {\n"
        "  clearImage();\n"
        "  clearRotation();\n"
        "}\n"
    )
    assert _verify_scope_isolation(
        base, good_result, "_clearImageAction", "x.js") is True
