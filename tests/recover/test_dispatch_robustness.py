"""End-to-end dispatch robustness tests.

Three motivating cases:

  · Indented def/class replace with a Python comment containing an
    apostrophe (`# ComfyUI's ...`).  `_try_indented_def_replace`
    previously bailed on `_js_balance`'s unclosed-string detection.

  · Multi-statement body indented at 4 spaces on every line.
    `body.strip()` only removed the first line's indent, leaving
    subsequent statements un-splittable by
    `_split_python_statements`.

  · Decorated method (`@command\ndef new_cmd`) into a class.
    Multi-statement apply split the decorator from the def, then
    fell through to context-insert, which placed the method at
    method-body indent (nested inside a sibling method).  Fixed by
    merging decorators in `_split_python_statements` and routing
    decorated bodies to `_try_insert_into_named_scope` first.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.patch_dispatch import (
    _try_deterministic_patch,
    _split_python_statements,
)


def test_indented_def_with_apostrophe_comment_succeeds():
    base = (
        "class Handler:\n"
        "    def comfy_set_layer_pixels(self, params):\n"
        "        return {'success': False, 'message': 'old'}\n"
        "\n"
        "    def other(self, params):\n"
        "        return {'ok': True}\n"
    )
    body = (
        "    def comfy_set_layer_pixels(self, params):\n"
        "        # It at encode time so ComfyUI's 1 - alpha/255\n"
        "        # ComfyUI's mask reconstruction needs this.\n"
        "        return {'success': True, 'message': 'new'}\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(
        base, patch, path="kritomatic_daemon/handlers/comfyui.py")
    assert result is not None
    assert "return {'success': True, 'message': 'new'}" in result
    assert "old" not in result
    assert "def other" in result
    assert result.count("def comfy_set_layer_pixels") == 1
    for line in result.split("\n"):
        if "def comfy_set_layer_pixels" in line:
            assert line.startswith("    ")
            break


def test_multistatement_indented_body_applies_both():
    base = (
        "def handle_inpaint(payload):\n"
        "    batch_id = 'abc'\n"
        "    captured = {'image_b64': None, 'message': ''}\n"
        "\n"
        "    def on_progress(index, result):\n"
        "        if result.get('status') != 'success':\n"
        "            captured['message'] = result.get('message', '')\n"
        "\n"
        "    return captured\n"
    )
    body = (
        "    captured = {'image_b64': None, 'message': '', 'errors': []}\n"
        "\n"
        "    def on_progress(index, result):\n"
        "        cmd = result.get('command')\n"
        "        if result.get('status') != 'success':\n"
        "            captured['errors'].append(f'{cmd}: {result}')\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(
        base, patch, path="playgrounds/inpaint/inpaint_server.py")
    assert result is not None
    assert "'errors': []" in result
    assert "captured['errors'].append" in result
    assert result.count("def on_progress") == 1
    assert result.count("captured = {") == 1
    assert "batch_id = 'abc'" in result
    assert "return captured" in result


def test_multistatement_body_one_failure_bails_whole():
    base = (
        "def handle():\n"
        "    captured = {'msg': ''}\n"
        "    return captured\n"
    )
    body = (
        "    captured = {'msg': '', 'errors': []}\n"
        "\n"
        "    def totally_new():\n"
        "        nonexistent_call()\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(base, patch, path="x.py")
    if result is not None:
        assert (
            "totally_new" not in result
            or "'errors': []" in result
        ), (
            "multi-statement path partially mutated the base: "
            f"'errors' landed but 'totally_new' did not.\n{result}"
        )


def test_structural_append_accepts_python_with_apostrophe():
    base = (
        '"""Utilities."""\n'
        "\n"
        "def paste_image(doc, png, x, y, rotation=0.0):\n"
        "    return png\n"
    )
    body = (
        "def _rotate_about_center(img, angle_deg):\n"
        "    # ComfyUI's mask uses alpha, not RGB.\n"
        "    return img\n"
    )
    patch = {"body": body, "context_before": "", "fence_lang": "py"}
    result = _try_deterministic_patch(
        base, patch, path="comfyui_krita.py")
    assert result is not None
    assert "def _rotate_about_center" in result
    assert "# ComfyUI's mask" in result
    assert "def paste_image" in result


def test_decorated_method_inserted_into_class():
    """The exact test_40 shape: a `@command`-decorated method must
    be inserted INTO the prose-named class at class-body indent."""
    base = (
        "def handle(payload):\n"
        "    return payload\n"
        "\n"
        "\n"
        "class Handler:\n"
        "    @decorator(name=\"existing\")\n"
        "    def existing(self, params):\n"
        "        return 1\n"
    )
    body = (
        "    @decorator(name=\"new\")\n"
        "    def new_method(self, params):\n"
        "        return 2\n"
    )
    context = "Add the following method to `Handler`:"
    patch = {"body": body, "context_before": context,
             "fence_lang": "py"}
    result = _try_deterministic_patch(base, patch, path="x.py")
    assert result is not None, (
        "expected decorated-method insertion to succeed")

    for line in result.split("\n"):
        if "def new_method" in line:
            indent = len(line) - len(line.lstrip())
            assert indent == 4, (
                f"expected def new_method at 4 spaces, got "
                f"{indent}: {line!r}")
            break
    else:
        assert False, f"def new_method not found in result:\n{result}"

    assert result.count("class Handler") == 1
    assert "def existing" in result
    import ast
    ast.parse(result)


def test_split_doesnt_separate_decorator_from_def():
    """The decorator-merge fix: a `@decorator` line must remain
    attached to its def in `_split_python_statements`."""
    body = "@decorator\ndef foo():\n    return 1\n"
    stmts = _split_python_statements(body)
    assert len(stmts) == 1, (
        f"decorator+def should be 1 stmt; got {stmts!r}")


def test_split_merges_stacked_decorators():
    """Stacked decorators must also merge with their def."""
    body = (
        "@decorator_a\n"
        "@decorator_b\n"
        "def foo():\n"
        "    return 1\n"
    )
    stmts = _split_python_statements(body)
    assert len(stmts) == 1, (
        f"stacked decorators + def should be 1 stmt; got {stmts!r}")


def test_scope_ast_check_correct_insertion():
    """The AST check passes a correct insertion (new method as a
    direct child of the target scope)."""
    from deeppeek.recover.patch_scope_insert import _verify_scope_insertion
    base = (
        "class Foo:\n"
        "    def existing(self):\n"
        "        return 1\n"
    )
    correct = (
        "class Foo:\n"
        "    def existing(self):\n"
        "        return 1\n"
        "    def new_method(self):\n"
        "        return 2\n"
    )
    inserted_body = (
        "    def new_method(self):\n"
        "        return 2\n"
    )
    assert _verify_scope_insertion(
        base, correct, "Foo", inserted_body, "x.py") is True


def test_scope_ast_check_rejects_nesting():
    """The AST check rejects a buggy insertion where the new method
    is nested inside a sibling method."""
    from deeppeek.recover.patch_scope_insert import _verify_scope_insertion
    base = (
        "class Foo:\n"
        "    def existing(self):\n"
        "        return 1\n"
    )
    buggy = (
        "class Foo:\n"
        "    def existing(self):\n"
        "        return 1\n"
        "        def new_method(self):\n"
        "            return 2\n"
    )
    inserted_body = (
        "    def new_method(self):\n"
        "        return 2\n"
    )
    assert _verify_scope_insertion(
        base, buggy, "Foo", inserted_body, "x.py") is False


def test_scope_ast_check_non_decl_insertion_passes():
    """Try/except insertion into a function body: no decls in body,
    so the AST check passes on parse alone."""
    from deeppeek.recover.patch_scope_insert import _verify_scope_insertion
    base = (
        "def handle():\n"
        "    a = 1\n"
        "    return a\n"
    )
    result = (
        "def handle():\n"
        "    a = 1\n"
        "    try:\n"
        "        b = 2\n"
        "    except:\n"
        "        b = 0\n"
        "    return a\n"
    )
    inserted_body = (
        "    try:\n"
        "        b = 2\n"
        "    except:\n"
        "        b = 0\n"
    )
    assert _verify_scope_insertion(
        base, result, "handle", inserted_body, "x.py") is True


def main():
    tests = [
        ("indented def with apostrophe comment succeeds",
         test_indented_def_with_apostrophe_comment_succeeds),
        ("multistatement indented body applies both",
         test_multistatement_indented_body_applies_both),
        ("multistatement body one failure bails whole",
         test_multistatement_body_one_failure_bails_whole),
        ("structural append accepts python with apostrophe",
         test_structural_append_accepts_python_with_apostrophe),
        ("decorated method inserted into class",
         test_decorated_method_inserted_into_class),
        ("split doesnt separate decorator from def",
         test_split_doesnt_separate_decorator_from_def),
        ("split merges stacked decorators",
         test_split_merges_stacked_decorators),
        ("scope ast check correct insertion",
         test_scope_ast_check_correct_insertion),
        ("scope ast check rejects nesting",
         test_scope_ast_check_rejects_nesting),
        ("scope ast check non decl insertion passes",
         test_scope_ast_check_non_decl_insertion_passes),
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
        print(f"\n{failed}/{len(tests)} dispatch robustness test(s) "
              f"failed")
        return 1
    print(f"\nAll {len(tests)} dispatch robustness tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
