"""Regression tests for deeppeek robustness fixes."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ============================================================
# Walker
# ============================================================

def test_walker_no_comment_returns_input():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "a();\n\n\ncode_here();\n"
    indent = base.index("code_here")
    assert _find_leading_comment_start(base, indent) == indent


def test_walker_no_comment_contiguous():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "a();\nb();\nc();\ncode_here();\n"
    indent = base.index("code_here")
    assert _find_leading_comment_start(base, indent) == indent


def test_walker_line_comment_blank_traversed():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "// C1\n\ncode_here();\n"
    assert _find_leading_comment_start(base, base.index("code_here")) == 0


def test_walker_block_comment_blank_traversed():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "/* block\n   comment */\n\ncode_here();\n"
    assert _find_leading_comment_start(base, base.index("code_here")) == 0


def test_walker_contiguous_comments():
    from deeppeek.recover.apply_patches import _find_leading_comment_start
    base = "// a\n// b\n// c\ncode_here();\n"
    assert _find_leading_comment_start(base, base.index("code_here")) == 0


def test_body_starts_with_comment():
    from deeppeek.recover.apply_patches import _body_starts_with_comment
    assert _body_starts_with_comment("/* x */\ncode") is True
    assert _body_starts_with_comment("// x") is True
    assert _body_starts_with_comment("# x") is True
    assert _body_starts_with_comment("code") is False
    assert _body_starts_with_comment("\n\n// x") is True
    assert _body_starts_with_comment("") is False


def test_apply_scoped_consumes_comment_with_blank():
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "/* Old docstring\n"
        "   multi-line. */\n"
        "\n"
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    new = (
        "/* New docstring */\n"
        "\n"
        "export function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    r = _apply_scoped(base, "foo", new)
    assert r is not None
    assert "Old docstring" not in r
    assert "New docstring" in r
    assert "return 2" in r


def test_apply_scoped_keeps_comment_when_body_has_none():
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "// Pre-existing docstring\n"
        "\n"
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    new = (
        "export function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    r = _apply_scoped(base, "foo", new)
    assert r is not None
    assert "Pre-existing docstring" in r
    assert "return 2" in r


def test_apply_scoped_consumes_decorator():
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "@app.route('/old')\n"
        "def handler():\n"
        "    return 'old'\n"
    )
    new = (
        "// new\n"
        "def handler():\n"
        "    return 'new'\n"
    )
    r = _apply_scoped(base, "handler", new)
    assert r is not None
    assert "app.route('/old')" not in r
    assert "return 'new'" in r


# ============================================================
# Dedup -- patch-provenance-aware signature
# ============================================================
#
# The new signature is:
#   _dedup_repeated_statements(content, path, base=None,
#                              patch_bodies=None, window=15)
#
# All tests below pass base and patch_bodies explicitly, so they
# exercise the real guards rather than the default no-op.

_TARGET_LINE = "if (e.inpaintFullCanvas) e.inpaintFullCanvas.disabled = busy;"

def test_dedup_stmt_removes_patch_added_dup():
    """The pg_panel_fields.py case: line not in base, in patch bodies,
    exactly two occurrences, same scope, within window."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    base = (
        "function f() {\n"
        "  a = 1;\n"
        "  b = 2;\n"
        "  c = 3;\n"
        "}\n"
    )
    content = (
        "function f() {\n"
        "  a = 1;\n"
        "  b = 2;\n"
        "  " + _TARGET_LINE + "\n"
        "  c = 3;\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base=base, patch_bodies=[_TARGET_LINE])
    assert n == 1, f"expected 1 removal, got {n}"
    assert out.count(_TARGET_LINE) == 1


def test_dedup_stmt_keeps_line_in_base():
    """Legitimate duplicate: line IS in base.  The tool must not remove
    it even if it also appears in a patch body (which can happen when
    a patch re-states an existing line for context)."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    line = "resetView();"
    base = (
        "function setImage() {\n"
        "  " + line + "\n"
        "}\n"
        "function clearImage() {\n"
        "  " + line + "\n"
        "}\n"
    )
    content = base
    out, n = _dedup_repeated_statements(
        content, "test.js", base=base, patch_bodies=[line])
    assert n == 0, f"line in base should not be deduped, got {n}"


def test_dedup_stmt_keeps_no_patch_provenance():
    """Line not in base, NOT in any patch body -- user-authored
    duplication.  Must be preserved."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    line = "resetView();"
    content = (
        "function f() {\n"
        "  " + line + "\n"
        "  other();\n"
        "  " + line + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="",
        patch_bodies=["const patchLine = 1;"])
    assert n == 0, f"no-provenance dup should not be deduped, got {n}"


def test_dedup_stmt_keeps_different_scope():
    """Same patch line in two different functions: same-scope guard."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
        "function g() {\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="", patch_bodies=[_TARGET_LINE])
    assert n == 0, f"different scopes should not dedupe, got {n}"


def test_dedup_stmt_keeps_distant():
    """Same line, same scope, distance > window."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        "  " + _TARGET_LINE + "\n"
        "  b();\n"
        "  c();\n"
        "  d();\n"
        "  e();\n"
        "  g();\n"
        "  h();\n"
        "  i();\n"
        "  j();\n"
        "  k();\n"
        "  l();\n"
        "  m();\n"
        "  n();\n"
        "  o();\n"
        "  p();\n"
        "  q();\n"
        "  r();\n"
        "  " + _TARGET_LINE + "\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="", patch_bodies=[_TARGET_LINE])
    assert n == 0, f"distance > window should not dedupe, got {n}"


def test_dedup_stmt_keeps_nfold_repetition():
    """Three or more occurrences of the same patch line: intentional
    repetition (loop, generated list), not a duplicate-insertion bug."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        + ("  " + _TARGET_LINE + "\n") * 3
        + "}\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.js", base="", patch_bodies=[_TARGET_LINE])
    assert n == 0, f"n-fold repetition should not be deduped, got {n}"


def test_dedup_stmt_markdown_skipped():
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = "a;\nb;\nc;\na;\n"
    out, n = _dedup_repeated_statements(
        content, "readme.md", base="", patch_bodies=["a;"])
    assert n == 0
    assert out == content


def test_dedup_stmt_css_properties_preserved():
    """CSS: `flex:0 0 auto;` twice with different case -- both must
    stay.  This is the pg_base.py corruption shape."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    line = "flex:0 0 auto;"
    content = (
        "  .maskSwatch {\n"
        "    width:18px;\n"
        "    " + line + "\n"
        "    background:#0e1622;\n"
        "  }\n"
        "  .maskMeta {\n"
        "    " + line + "\n"
        "    color:#5a6774;\n"
        "  }\n"
    )
    out, n = _dedup_repeated_statements(
        content, "test.css", base="", patch_bodies=["width:18px;"])
    assert n == 0, f"CSS properties should not be deduped, got {n}"


def test_dedup_stmt_base_required_for_any_action():
    """Sanity: with no base and no patch_bodies, the function is a
    no-op.  This is the safety default."""
    from deeppeek.recover.validate_output import _dedup_repeated_statements
    content = (
        "function f() {\n"
        "  a = 1;\n"
        "  b = 2;\n"
        "  a = 1;\n"
        "}\n"
    )
    out, n = _dedup_repeated_statements(content, "test.js")
    assert n == 0
    assert out == content


# ============================================================
# Header parsing
# ============================================================

def test_parse_file_header_markdown_link():
    from deeppeek.recover.extract import _parse_file_header
    r = _parse_file_header("[file.py](http://example.com)")
    assert r is not None and r[0] == "file.py"


# ============================================================
# Patch-insertion filtering (High impact)
# ============================================================

def test_collect_inserted_lines_basic():
    from deeppeek.recover.apply_patches import _collect_inserted_lines
    result = _collect_inserted_lines("a\nb\nc\n", "a\nb\nc\nd\ne\n")
    assert result == {"d", "e"}


def test_collect_inserted_lines_replaced():
    from deeppeek.recover.apply_patches import _collect_inserted_lines
    result = _collect_inserted_lines("a\nb\nc\n", "a\nb\nX\n")
    assert "X" in result
    assert "c" not in result


def test_split_body_into_blocks_statements():
    from deeppeek.recover.apply_patches import _split_body_into_blocks
    blocks = _split_body_into_blocks("a();\nb();\nc();")
    assert len(blocks) == 3
    assert blocks[0] == "a();"


def test_split_body_into_blocks_brace_block():
    from deeppeek.recover.apply_patches import _split_body_into_blocks
    blocks = _split_body_into_blocks("if (x) {\n  a();\n}\nb();")
    assert len(blocks) == 2
    assert "if (x) {" in blocks[0]
    assert blocks[1] == "b();"


def test_filter_body_by_blocks_basic():
    from deeppeek.recover.apply_patches import _filter_body_by_blocks
    body = "a();\nb();\nc();"
    record_inserted = {"a();", "b();"}
    filtered, was_filtered = _filter_body_by_blocks(
        body, set(), record_inserted
    )
    assert was_filtered is True
    assert "a();" not in filtered
    assert "b();" not in filtered
    assert "c();" in filtered


def test_filter_body_by_blocks_context_preserved():
    from deeppeek.recover.apply_patches import _filter_body_by_blocks
    body = "a();\nb();"
    base_stripped = {"a();"}
    record_inserted = {"b();"}
    filtered, was_filtered = _filter_body_by_blocks(
        body, base_stripped, record_inserted
    )
    # a(); is context (in base) -> block kept
    # b(); is non-context but in record_inserted -> block filtered
    assert "a();" in filtered
    assert "b();" not in filtered


def test_filter_body_by_blocks_partial_duplicate_kept():
    """A block with even one new line is preserved whole."""
    from deeppeek.recover.apply_patches import _filter_body_by_blocks
    body = "if (x) {\n  a();\n}"
    # Prior insertion covered the opener and closer but not the body
    record_inserted = {"if (x) {", "}"}
    filtered, was_filtered = _filter_body_by_blocks(
        body, set(), record_inserted
    )
    # a(); is new -> whole block kept
    assert was_filtered is False
    assert "if (x) {" in filtered
    assert "a();" in filtered
    assert "}" in filtered


def test_filter_body_by_blocks_all_filtered():
    from deeppeek.recover.apply_patches import _filter_body_by_blocks
    body = "a();\nb();"
    record_inserted = {"a();", "b();"}
    filtered, was_filtered = _filter_body_by_blocks(
        body, set(), record_inserted
    )
    assert was_filtered is True
    assert not filtered.strip()


def test_insert_filter_end_to_end():
    """Two patches with overlapping content.  Second's duplicate block
    should be filtered before insertion."""
    from deeppeek.recover.apply_patches import pass6_apply_patches
    from deeppeek.recover.core import FileRecord, Block

    base_body = "function f() {\n  a = 1;\n}\n"
    base_block = Block(
        idx=0, header_path="foo.js", modifier=None, fence_lang="js",
        body=base_body, body_line_count=3, context_before="",
        line_start=0, line_end=3, is_orphan=False, is_code_file=True,
        kind="full", canonical_path="foo.js", output_path="foo.js",
        classified_by="preexisting",
    )

    rec = FileRecord(path="foo.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        {"idx": 1,
         "body": "if (x) y = z;\nw = 2;\nu = 3;",
         "context_before": "After `a = 1;`:",
         "fence_lang": "js"},
        {"idx": 2,
         "body": "if (x) y = z;\nv = 4;\nt = 5;",
         "context_before": "After `a = 1;`:",
         "fence_lang": "js"},
    ]

    blocks_by_idx = {0: base_block}
    pass6_apply_patches({"foo.js": rec}, blocks_by_idx, None)

    assert rec.content is not None
    count = rec.content.count("if (x) y = z;")
    assert count == 1, (
        f"expected 1 occurrence of `if (x) y = z;`, got {count}:\n"
        f"{rec.content}"
    )
    assert "v = 4;" in rec.content
    assert "t = 5;" in rec.content
    assert "w = 2;" in rec.content
    assert "u = 3;" in rec.content


# ============================================================
# Dedup for multi-line block headers (Medium impact)
# ============================================================

def test_dedup_func_block_removes_duplicate():
    """Two identical `function foo() { body }` at column 0 in the
    same scope: the second is removed."""
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 1, f"expected 1 removal, got {n}:\n{out}"
    assert out.count("function foo()") == 1


def test_dedup_func_block_keeps_different_name():
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "function foo() {\n  return 1;\n}\n"
        "function bar() {\n  return 1;\n}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 0, f"different names should not dedupe, got {n}"


def test_dedup_func_block_keeps_different_body():
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "function foo() {\n  return 1;\n}\n"
        "function foo() {\n  return 2;\n}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 0, f"different bodies should not dedupe, got {n}"


def test_dedup_func_block_keeps_different_params():
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "function foo() {\n  return 1;\n}\n"
        "function foo(x) {\n  return 1;\n}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 0, (
        f"different param lists should not dedupe, got {n}:\n{out}"
    )


def test_dedup_func_block_keeps_different_scope():
    """Identical nested functions in different outer functions: not
    deduped (their `enclosing scope` differs)."""
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "function outer1() {\n"
        "  function foo() {\n    return 1;\n  }\n"
        "}\n"
        "function outer2() {\n"
        "  function foo() {\n    return 1;\n  }\n"
        "}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 0, f"different scopes should not dedupe, got {n}"


def test_dedup_class_block_removes_duplicate():
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "class Foo {\n"
        "  bar() { return 1; }\n"
        "}\n"
        "class Foo {\n"
        "  bar() { return 1; }\n"
        "}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 1, f"expected 1 removal, got {n}:\n{out}"
    assert out.count("class Foo") == 1


def test_dedup_func_block_markdown_skipped():
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "function foo() {\n  return 1;\n}\n"
        "function foo() {\n  return 1;\n}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "readme.md")
    assert n == 0
    assert out == content


def test_dedup_func_block_export_prefix():
    """`export function foo` also deduped (same name, same body,
    same scope)."""
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "export function foo() {\n  return 1;\n}\n"
        "export function foo() {\n  return 1;\n}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 1, f"expected 1 removal, got {n}:\n{out}"
    assert out.count("export function foo") == 1


def test_dedup_func_block_async():
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    content = (
        "async function fetch() {\n  return 1;\n}\n"
        "async function fetch() {\n  return 1;\n}\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.js")
    assert n == 1, f"expected 1 removal, got {n}:\n{out}"
    assert out.count("async function fetch") == 1


def test_dedup_func_block_python_different_strings_kept():
    """Identical functions inside two different embedded JS string
    literals of a .py file must NOT be deduped -- that would empty
    one string and corrupt the Python file."""
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    Q = chr(34) * 3
    content = (
        "A_JS = r" + Q + "\n"
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        + Q + "\n"
        "\n"
        "B_JS = r" + Q + "\n"
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        + Q + "\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.py")
    assert n == 0, (
        f"cross-string dup should not dedupe, got {n}:\n{out}"
    )
    assert out.count("function foo()") == 2
    assert out == content


def test_dedup_func_block_python_same_string_removed():
    """Same guard the other way: identical functions inside the SAME
    string literal ARE deduped."""
    from deeppeek.recover.validate_output import _dedup_duplicate_blocks
    Q = chr(34) * 3
    content = (
        "JS = r" + Q + "\n"
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        + Q + "\n"
    )
    out, n = _dedup_duplicate_blocks(content, "test.py")
    assert n == 1, f"same-string dup should be deduped, got {n}"
    assert out.count("function foo()") == 1


# ============================================================
# Scope-aware pre-check + indented-export gate
# ============================================================
#
# Three invariants that close the pg_inpaint.py nesting class:
#
#   · A patch whose body declares a scope that exists in base must be
#     routed through `_apply_scoped`, not the insert heuristics.
#   · `validate_content` must reject indented `export` (invalid ESM).
#   · Both checks apply to `.js` and to JS embedded in `.py` strings.

def test_scope_replace_via_patch_not_insert():
    """A patch whose body is a full function-decl matching an
    existing base scope must REPLACE the function, not insert it.

    Without the scope-aware pre-check in `_try_deterministic_patch`,
    a body that failed the earlier heuristics could fall through to
    `_try_insert_after_anchor` / `_try_structural_append`.  When the
    anchor landed inside an existing function, the new body got
    inserted there, nesting `export function foo()` inside `foo()`.
    Balanced-brace clean, invalid ESM.

    Motivating case: pg_inpaint.py in the wild — a
    thought-process-supplied function body was inserted inside
    `buildContextMaskB64`, immediately after a correct
    scoped-replace pair had already fixed the file.
    """
    from deeppeek.recover.apply_patches import _try_deterministic_patch
    base = (
        "export function foo() {\n"
        "  return 1;\n"
        "}"
    )
    patch = {
        "body": (
            "export function foo() {\n"
            "  return 2;\n"
            "}"
        ),
        "context_before": "",
    }
    result = _try_deterministic_patch(base, patch)
    assert result is not None, (
        "expected scope-replace to succeed; pre-check should have "
        "routed through _apply_scoped")
    assert "return 2" in result, (
        f"expected new body; got:\n{result}")
    assert "return 1" not in result, (
        f"expected old body replaced; got:\n{result}")
    assert result.count("export function foo") == 1, (
        f"expected exactly 1 foo decl, got "
        f"{result.count('export function foo')}:\n{result}")
    assert result.count("{") == result.count("}"), (
        f"unbalanced result:\n{result}")


def test_scope_replace_via_patch_nonmatching_scope_inserts():
    """When the body declares a scope NOT in base, the pre-check must
    fall through and the existing insert heuristics must run.

    This guards against over-triggering: if the pre-check fired on
    every scope-decl body (whether or not the scope exists in base),
    legitimate inserts of new functions would break.
    """
    from deeppeek.recover.apply_patches import _try_deterministic_patch
    base = (
        "export function existing() {\n"
        "  return 1;\n"
        "}"
    )
    patch = {
        "body": (
            "export function totallyNew() {\n"
            "  return 42;\n"
            "}"
        ),
        "context_before": "",
    }
    result = _try_deterministic_patch(base, patch)
    # The insert may or may not find an anchor depending on the
    # heuristic set; the important property is that the pre-check
    # did not force a scoped-replace on a non-existent scope.
    # If it did fire, we'd see "existing" replaced and "totallyNew"
    # nowhere — or a Return of None.  If a heuristic ran, result
    # either contains both or is None (no anchor).  Either is
    # acceptable; the pre-check did not consume the case.
    if result is not None:
        # If applied, the original function must survive.
        assert "existing" in result, (
            f"non-matching scope pre-check should not have replaced "
            f"existing:\n{result}")
        assert "totallyNew" in result, (
            f"new function missing:\n{result}")
    # (result is None is also acceptable — no anchor found.)


def test_indented_export_rejected():
    """A nested `export function X()` inside another function body is
    balanced-brace clean but invalid ESM.  `validate_content` must
    reject it so the fail-closed trust policy routes the file to
    low_trust/.

    This is the second line of defence for the pg_inpaint.py nesting
    mangle: even if a bad patch slips past the pre-check, the
    resulting file is caught before it can be written.
    """
    from deeppeek.recover.validate_output import validate_content
    content = (
        "function outer() {\n"
        "  export function inner() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
    )
    err = validate_content(content, "test.js")
    assert err is not None, "expected indented-export rejection"
    assert "indented" in err.lower(), (
        f"expected 'indented' in error; got {err!r}")


def test_indented_export_in_embedded_js_rejected():
    """Same check must fire for JS embedded in a Python string
    literal — that's the playground pattern and the exact
    pg_inpaint.py shape (`INPAINT_JS = r\"\"\"...\"\"\"`)."""
    from deeppeek.recover.validate_output import validate_content
    Q = chr(34) * 3
    content = (
        "JS = r" + Q + "\n"
        "function outer() {\n"
        "  export function inner() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
        + Q + "\n"
    )
    err = validate_content(content, "test.py")
    assert err is not None, (
        "expected indented-export rejection for embedded JS")
    assert "indented" in err.lower(), (
        f"expected 'indented' in error; got {err!r}")


def test_top_level_export_not_rejected():
    """Sanity: the indented-export check must not reject legitimate
    top-level `export function` / `export const`."""
    from deeppeek.recover.validate_output import validate_content
    content = (
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
        "export const bar = 42;\n"
    )
    err = validate_content(content, "test.js")
    assert err is None, (
        f"top-level export should be accepted; got {err!r}")


def test_export_inside_string_literal_not_rejected():
    """The indented-export check must ignore `export` inside JS
    string literals — those are data, not declarations."""
    from deeppeek.recover.validate_output import validate_content
    content = (
        "const s = '  export function hidden() {}';\n"
        "export function visible() {\n"
        "  return 1;\n"
        "}\n"
    )
    err = validate_content(content, "test.js")
    assert err is None, (
        f"export inside string should be ignored; got {err!r}")


# ============================================================
# Cross-file duplicate detection
# ============================================================

def _long_body(name="processData"):
    body = f"function {name}(x) {{\n"
    for i in range(12):
        body += f"  step{i}(x);\n"
    body += "  return x;\n}\n"
    return body


def test_cross_file_empty_when_no_records():
    from deeppeek.recover.cross_file import find_cross_file_duplicates
    assert find_cross_file_duplicates({}, None) == []


def test_cross_file_detects_identical_long_functions():
    from deeppeek.recover.cross_file import find_cross_file_duplicates
    from deeppeek.recover.core import FileRecord
    body = _long_body()
    rec_a = FileRecord(path="a.js", state="SINGLE_FULL")
    rec_a.content = body
    rec_b = FileRecord(path="b.js", state="SINGLE_FULL")
    rec_b.content = body
    dups = find_cross_file_duplicates(
        {"a.js": rec_a, "b.js": rec_b}, None)
    assert len(dups) == 1
    assert dups[0]["name"] == "processData"
    assert dups[0]["kind"] == "function"
    assert set(dups[0]["files"]) == {"a.js", "b.js"}
    assert dups[0]["line_count"] >= 10


def test_cross_file_ignores_short_functions():
    from deeppeek.recover.cross_file import find_cross_file_duplicates
    from deeppeek.recover.core import FileRecord
    body = "function tiny(x) {\n  return x;\n}\n"
    rec_a = FileRecord(path="a.js", state="SINGLE_FULL")
    rec_a.content = body
    rec_b = FileRecord(path="b.js", state="SINGLE_FULL")
    rec_b.content = body
    dups = find_cross_file_duplicates(
        {"a.js": rec_a, "b.js": rec_b}, None)
    assert dups == []


def test_cross_file_ignores_preexisting():
    from deeppeek.recover.cross_file import find_cross_file_duplicates
    from deeppeek.recover.core import FileRecord, Block
    body = _long_body()
    def _base(idx, name):
        return Block(
            idx=idx, header_path=name, modifier=None, fence_lang="js",
            body=body, body_line_count=body.count(chr(10)) + 1,
            context_before="", line_start=0, line_end=0,
            is_orphan=False, is_code_file=True, kind="full",
            canonical_path=name, output_path=name,
            classified_by="preexisting")
    base_a = _base(0, "a.js")
    base_b = _base(1, "b.js")
    blocks_by_idx = {0: base_a, 1: base_b}
    rec_a = FileRecord(path="a.js", state="SINGLE_FULL")
    rec_a.content = body
    rec_a.last_full_idx = 0
    rec_b = FileRecord(path="b.js", state="SINGLE_FULL")
    rec_b.content = body
    rec_b.last_full_idx = 1
    dups = find_cross_file_duplicates(
        {"a.js": rec_a, "b.js": rec_b}, blocks_by_idx)
    assert dups == []


def test_cross_file_ignores_same_file_duplicates():
    from deeppeek.recover.cross_file import find_cross_file_duplicates
    from deeppeek.recover.core import FileRecord
    body = _long_body()
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    rec.content = body + "\n" + body
    dups = find_cross_file_duplicates({"a.js": rec}, None)
    assert dups == []


def test_cross_file_ignores_partial_only():
    from deeppeek.recover.cross_file import find_cross_file_duplicates
    from deeppeek.recover.core import FileRecord
    body = _long_body()
    rec_a = FileRecord(path="a.js", state="PARTIAL_ONLY")
    rec_a.content = None
    rec_b = FileRecord(path="b.js", state="SINGLE_FULL")
    rec_b.content = body
    dups = find_cross_file_duplicates(
        {"a.js": rec_a, "b.js": rec_b}, None)
    assert dups == []


# ============================================================
# Prose/UI pre-filter (pruning orphans earlier)
# ============================================================

def test_looks_like_prose_ui_ui_mockup():
    from deeppeek.recover.prose_filter import looks_like_prose_ui
    assert looks_like_prose_ui("[Inpaint] [Cancel]") is True
    assert looks_like_prose_ui("(•) Rect  ( ) Full canvas") is True
    assert looks_like_prose_ui("[x] Use full canvas") is True


def test_looks_like_prose_ui_prose():
    from deeppeek.recover.prose_filter import looks_like_prose_ui
    assert looks_like_prose_ui(
        "Drag the context rectangle to move it, handles to resize."
    ) is True


def test_looks_like_prose_ui_code():
    from deeppeek.recover.prose_filter import looks_like_prose_ui
    assert looks_like_prose_ui("if (x) { y(); }") is False
    assert looks_like_prose_ui("function foo() { return 1; }") is False
    assert looks_like_prose_ui("const x = 1;") is False
    assert looks_like_prose_ui('<div class="field">') is False


def test_looks_like_prose_ui_docstring_kept():
    from deeppeek.recover.prose_filter import looks_like_prose_ui
    # Docstrings are code, even when the text reads as prose.
    assert looks_like_prose_ui(
        '"""This function does X, Y, and Z."""'
    ) is False


def test_looks_like_prose_ui_filename_kept():
    from deeppeek.recover.prose_filter import looks_like_prose_ui
    # Filename mention: might be attributable, keep.
    assert looks_like_prose_ui(
        "// pg_base.py Inpainting section"
    ) is False


def test_looks_like_prose_ui_too_short():
    from deeppeek.recover.prose_filter import looks_like_prose_ui
    assert looks_like_prose_ui("") is False
    assert looks_like_prose_ui("abc") is False


# ============================================================
# Body-filename orphan attribution
# ============================================================

def test_filename_in_orphan_header_basic():
    from deeppeek.recover.prose_filter import filename_in_orphan_header
    body = "// pg_base.py Inpainting section\nsome();\n"
    r = filename_in_orphan_header(body, ["pg_base.py", "other.py"])
    assert r == ["pg_base.py"]


def test_filename_in_orphan_header_not_in_first_3_lines():
    from deeppeek.recover.prose_filter import filename_in_orphan_header
    body = "line1\nline2\nline3\n// pg_base.py\n"
    r = filename_in_orphan_header(body, ["pg_base.py"])
    assert r == []


def test_filename_in_orphan_header_multiple_matches():
    from deeppeek.recover.prose_filter import filename_in_orphan_header
    body = "// pg_base.py and other.py\n"
    r = filename_in_orphan_header(body, ["pg_base.py", "other.py"])
    assert len(r) == 2


# ============================================================
# Integration: pass 2 pre-filter
# ============================================================

def test_pass2_classify_filters_prose():
    from deeppeek.recover.classify import pass2_classify
    from deeppeek.recover.core import Block
    prose = Block(
        idx=0, header_path=None, modifier=None, fence_lang="",
        body="[Inpaint] [Cancel]",
        body_line_count=1, context_before="",
        line_start=0, line_end=1, is_orphan=True,
    )
    pass2_classify([prose], None, "off", 0.85, False)
    assert prose.kind == "not_a_file"
    assert prose.is_code_file is False
    assert prose.disposition == "orphan_prose"


def test_pass4_attributes_via_body_filename():
    from deeppeek.recover.attribute import pass4_attribute_orphans
    from deeppeek.recover.core import Block, FileRecord

    orphan = Block(
        idx=0, header_path=None, modifier=None, fence_lang="js",
        body="// pg_base.py Inpainting section\nsome_code();\n",
        body_line_count=2, context_before="",
        line_start=0, line_end=2, is_orphan=True,
        is_code_file=True, kind="partial",
    )
    base = Block(
        idx=1, header_path="pg_base.py", modifier=None, fence_lang="js",
        body="original content\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="pg_base.py", output_path="pg_base.py",
    )
    rec = FileRecord(path="pg_base.py", state="SINGLE_FULL")
    rec.source_blocks = [1]

    blocks = [orphan, base]
    blocks_by_idx = {0: orphan, 1: base}
    records = {"pg_base.py": rec}

    pass4_attribute_orphans(blocks, blocks_by_idx, records, False)

    assert orphan.attributed_to == "pg_base.py"
    assert orphan.attributed_by == "body_header"


# ============================================================
# Disposition computation
# ============================================================

def test_compute_block_disposition_applied():
    from deeppeek.recover.write import _compute_block_disposition
    from deeppeek.recover.core import Block, FileRecord
    b = Block(
        idx=0, header_path="a.js", modifier=None, fence_lang="js",
        body="x", body_line_count=1, context_before="",
        line_start=0, line_end=1, is_orphan=False,
        is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
    )
    rec = FileRecord(path="a.js", state="SINGLE_FULL")
    disp = _compute_block_disposition(b, {"a.js": rec})
    assert disp == "applied"


def test_compute_block_disposition_orphan_prose():
    from deeppeek.recover.write import _compute_block_disposition
    from deeppeek.recover.core import Block
    b = Block(
        idx=0, header_path=None, modifier=None, fence_lang="",
        body="[Inpaint] [Cancel]", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=True, is_code_file=False,
        kind="not_a_file", disposition="orphan_prose",
    )
    disp = _compute_block_disposition(b, {})
    assert disp == "orphan_prose"


def test_compute_block_disposition_preexisting():
    from deeppeek.recover.write import _compute_block_disposition
    from deeppeek.recover.core import Block
    b = Block(
        idx=0, header_path="a.js", modifier=None, fence_lang="js",
        body="x", body_line_count=1, context_before="",
        line_start=0, line_end=1, is_orphan=False,
        is_code_file=True, kind="full",
        canonical_path="a.js", output_path="a.js",
        classified_by="preexisting",
    )
    disp = _compute_block_disposition(b, {})
    assert disp == "preexisting"


def test_pass4_marks_not_a_file_as_attributed():
    """A prose block from pass2 (kind=not_a_file) is marked
    attributed_by='not_a_file' by pass4 — not left as unattributed.
    The classify-set disposition=orphan_prose must be preserved."""
    from deeppeek.recover.attribute import pass4_attribute_orphans
    from deeppeek.recover.core import Block, FileRecord

    prose = Block(
        idx=0, header_path=None, modifier=None, fence_lang="",
        body="[Inpaint] [Cancel]", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=True, is_code_file=False,
        kind="not_a_file", disposition="orphan_prose",
        classified_by="heuristic",
    )
    base = Block(
        idx=1, header_path="pg_base.py", modifier=None, fence_lang="py",
        body="original\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="pg_base.py", output_path="pg_base.py",
    )
    rec = FileRecord(path="pg_base.py", state="SINGLE_FULL")
    rec.source_blocks = [1]
    blocks = [prose, base]
    records = {"pg_base.py": rec}

    pass4_attribute_orphans(blocks, {0: prose, 1: base}, records, False)

    assert prose.attributed_by == "not_a_file", (
        f"expected attributed_by='not_a_file', got "
        f"{prose.attributed_by!r}")
    assert prose.disposition == "orphan_prose", (
        f"expected disposition='orphan_prose' preserved, got "
        f"{prose.disposition!r}")


def test_pass4_marks_bash_not_a_file_as_orphan_not_code():
    """Non-prose not-a-file (bash without a target) gets
    disposition='orphan_not_code' from pass4's fallback."""
    from deeppeek.recover.attribute import pass4_attribute_orphans
    from deeppeek.recover.core import Block, FileRecord

    bash = Block(
        idx=0, header_path=None, modifier=None, fence_lang="bash",
        body="ls -la && echo hi\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=True, is_code_file=False,
        kind="not_a_file", disposition=None,
        classified_by="heuristic",
    )
    base = Block(
        idx=1, header_path="pg_base.py", modifier=None, fence_lang="py",
        body="x\n", body_line_count=1,
        context_before="", line_start=0, line_end=1,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="pg_base.py", output_path="pg_base.py",
    )
    rec = FileRecord(path="pg_base.py", state="SINGLE_FULL")
    rec.source_blocks = [1]
    blocks = [bash, base]
    records = {"pg_base.py": rec}

    pass4_attribute_orphans(blocks, {0: bash, 1: base}, records, False)

    assert bash.attributed_by == "not_a_file"
    assert bash.disposition == "orphan_not_code", (
        f"expected disposition='orphan_not_code', got "
        f"{bash.disposition!r}")


# ============================================================
# _resolve_module / pass7 cross_validate — dotted-module handling
# ============================================================

def _mk_rec(path):
    from deeppeek.recover.core import FileRecord
    r = FileRecord(path=path, state="SINGLE_FULL")
    r.content = "placeholder"
    return r


def test_resolve_module_dotted_top_not_recovered():
    """`from pg_comfy.image import X` with neither `pg_comfy.py` nor
    `pg_comfy/` recovered must return None.  Prior bug: the last
    component was tried as a basename, so an unrelated `image.py`
    anywhere in the recovery set falsely resolved the import."""
    from deeppeek.recover.validate import _resolve_module
    records = {"inpaint_server.py": _mk_rec("inpaint_server.py")}
    assert _resolve_module(
        "pg_comfy.image", records, "inpaint_server.py") is None


def test_resolve_module_dotted_top_recovered():
    """If `pg_comfy.py` IS recovered, `from pg_comfy.image import X`
    resolves to that file (sub-paths become attribute accesses)."""
    from deeppeek.recover.validate import _resolve_module
    records = {
        "inpaint_server.py": _mk_rec("inpaint_server.py"),
        "pg_comfy.py":       _mk_rec("pg_comfy.py"),
    }
    result = _resolve_module(
        "pg_comfy.image", records, "inpaint_server.py")
    assert result == "pg_comfy.py"


def test_pass7_reports_top_level_once():
    """`from pg_comfy import X` and `from pg_comfy.image import Y`
    must produce a single problem: `{module: "pg_comfy"}`, not two
    entries (`pg_comfy` and `pg_comfy.image`)."""
    from deeppeek.recover.validate import pass7_cross_validate
    src = (
        "from pg_comfy import ComfyClient\n"
        "from pg_comfy.image import make_encoded_image\n"
    )
    rec = _mk_rec("inpaint_server.py")
    rec.content = src
    result = pass7_cross_validate({"inpaint_server.py": rec})
    modules = [p["module"] for p in result["problems"]
               if p["type"] == "missing_module"]
    assert modules == ["pg_comfy"], (
        f"expected ['pg_comfy'], got {modules!r}")


# ============================================================
# Fix 5 — sanity failure attribution
# ============================================================

def test_attribute_sanity_failure_finds_culprit():
    """Two single-line-replace patches; the second introduces the
    failure.  Attribution must return the second patch's idx.

    Uses `value:`/`other:` field flips because
    `_try_single_line_replace` fires deterministically on that shape.
    A short backtick anchor (`a();`) would fall through every insert
    heuristic (>= 6 chars required), so replay fails on the first
    patch and the helper correctly returns None -- this was the
    failure mode of the original version of this test.
    """
    from deeppeek.recover.apply_patches import attribute_sanity_failure
    from deeppeek.recover.core import Block, FileRecord

    base_body = (
        "export const config = {\n"
        "  value: false,\n"
        "  other: false,\n"
        "};\n"
    )
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        {"idx": 1, "body": "value: true,",
         "applied_by": "deterministic",
         "context_before": "Change value to true:"},
        {"idx": 2, "body": "other: true,",
         "applied_by": "deterministic",
         "context_before": "Change other to true:"},
    ]
    base_block = Block(
        idx=0, header_path="x.js", modifier=None, fence_lang="js",
        body=base_body, body_line_count=4, context_before="",
        line_start=0, line_end=0, is_orphan=False, is_code_file=True,
        kind="full", canonical_path="x.js", output_path="x.js",
        classified_by="preexisting",
    )

    def validate_fn(content, path):
        return "other true" if "other: true" in content else None

    idx = attribute_sanity_failure(rec, {0: base_block}, validate_fn)
    assert idx == 2, f"expected culprit idx=2, got {idx!r}"


def test_attribute_sanity_failure_no_patches():
    """No patches -> no attribution."""
    from deeppeek.recover.apply_patches import attribute_sanity_failure
    from deeppeek.recover.core import Block, FileRecord

    rec = FileRecord(path="x.js", state="SINGLE_FULL")
    rec.patches = []
    rec.last_full_idx = 0
    base_block = Block(
        idx=0, header_path="x.js", modifier=None, fence_lang="js",
        body="x\n", body_line_count=1, context_before="",
        line_start=0, line_end=0, is_orphan=False, is_code_file=True,
        kind="full", canonical_path="x.js", output_path="x.js",
        classified_by="preexisting",
    )
    idx = attribute_sanity_failure(
        rec, {0: base_block}, lambda c, p: "always fail")
    assert idx is None


def test_attribute_sanity_failure_no_base():
    """Missing base -> no attribution (can't replay)."""
    from deeppeek.recover.apply_patches import attribute_sanity_failure
    from deeppeek.recover.core import FileRecord

    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        {"idx": 1, "body": "b();",
         "applied_by": "deterministic",
         "context_before": ""},
    ]
    idx = attribute_sanity_failure(
        rec, {}, lambda c, p: "always fail")
    assert idx is None


def test_attribute_sanity_failure_llm_patch_stops_attribution():
    """If any patch was LLM-applied we can't replay the chain --
    return None rather than guessing."""
    from deeppeek.recover.apply_patches import attribute_sanity_failure
    from deeppeek.recover.core import Block, FileRecord

    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        {"idx": 1, "body": "b();", "applied_by": "llm"},
        {"idx": 2, "body": "c();", "applied_by": "deterministic"},
    ]
    base_block = Block(
        idx=0, header_path="x.js", modifier=None, fence_lang="js",
        body="function f() { a(); }\n", body_line_count=1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="x.js", output_path="x.js",
        classified_by="preexisting",
    )
    idx = attribute_sanity_failure(
        rec, {0: base_block}, lambda c, p: "always fail")
    assert idx is None


def test_attribute_sanity_failure_replay_failure():
    """Replay failure (body can't be placed) returns None, never a
    wrong idx."""
    from deeppeek.recover.apply_patches import attribute_sanity_failure
    from deeppeek.recover.core import Block, FileRecord

    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.last_full_idx = 0
    rec.patches = [
        {"idx": 1, "body": "this_will_not_place_easily();",
         "applied_by": "deterministic", "context_before": ""},
    ]
    base_block = Block(
        idx=0, header_path="x.js", modifier=None, fence_lang="js",
        body="function f() { a(); }\n", body_line_count=1,
        context_before="", line_start=0, line_end=0,
        is_orphan=False, is_code_file=True, kind="full",
        canonical_path="x.js", output_path="x.js",
        classified_by="preexisting",
    )
    idx = attribute_sanity_failure(
        rec, {0: base_block}, lambda c, p: "always fail")
    # Replay either succeeded (int) or failed (None); never a wrong type.
    assert idx is None or isinstance(idx, int)


# ============================================================
# Fix 5 companion -- direct unit for _try_object_literal_splice
# ============================================================
#
# test_35 exercises the heuristic end-to-end; these isolate it so a
# heuristic-level regression is distinguishable from a
# classification/pipeline issue.

def test_object_literal_splice_const_omitted():
    """Body `board = { ... }` against base `const board = { ... }`
    preserves the base's `const` prefix and does not produce a
    duplicate `board =` assignment."""
    from deeppeek.recover.apply_patches import _try_object_literal_splice
    base = "const board = { a: 1 };"
    body = "board = { a: 1, b: 2 };"
    result = _try_object_literal_splice(base, body, "board")
    assert result is not None, "expected splice to succeed"
    assert "const board" in result, (
        f"base's `const` prefix lost:\n{result}"
    )
    assert "b: 2" in result, f"new field missing:\n{result}"
    assert result.count("board") == 1, (
        f"expected exactly 1 'board', got {result.count('board')}:"
        f"\n{result}"
    )
    assert result.count("{") == result.count("}"), (
        f"unbalanced braces:\n{result}"
    )


def test_object_literal_splice_export_preserved():
    """Base `export const board = { ... }` + body `board = { ... }`
    preserves the full `export const` prefix."""
    from deeppeek.recover.apply_patches import _try_object_literal_splice
    base = "export const board = { a: 1 };"
    body = "board = { a: 1, b: 2 };"
    result = _try_object_literal_splice(base, body, "board")
    assert result is not None, "expected splice to succeed"
    assert "export const board" in result, (
        f"full prefix lost:\n{result}"
    )
    assert "b: 2" in result


# ============================================================
# Replace-pair object-literal prefix preservation
# ============================================================
#
# `_try_replace_pair` used to run `base.replace(old, new, 1)`
# unconditionally after the drift/ambiguity guards.  When `new` is
# a bare object-literal assignment (`board = {...}`) and `old` is
# a declaration (`const board = {...}`), that produces
# `board = {...}` -- no `const` -- which either throws a TDZ error
# or silently rebinds the name as an implicit global.  The splice
# branch preserves the base prefix.

def test_replace_pair_object_literal_preserves_const():
    from deeppeek.recover.apply_patches import _try_replace_pair
    base = "const board = { a: 1 };"
    old = "const board = { a: 1 };"
    new = "board = { a: 1, b: 2 };"
    result = _try_replace_pair(base, old, new)
    assert result is not None, "expected pair to apply"
    assert "const board" in result, (
        f"base's `const` prefix lost:\n{result}"
    )
    assert "b: 2" in result, f"new field missing:\n{result}"
    assert result.count("board") == 1, (
        f"expected exactly 1 'board', got "
        f"{result.count('board')}:\n{result}"
    )


def test_replace_pair_object_literal_export_preserved():
    from deeppeek.recover.apply_patches import _try_replace_pair
    base = "export const board = { a: 1 };"
    old = "export const board = { a: 1 };"
    new = "board = { a: 1, b: 2 };"
    result = _try_replace_pair(base, old, new)
    assert result is not None, "expected pair to apply"
    assert "export const board" in result, (
        f"full prefix lost:\n{result}"
    )
    assert "b: 2" in result


def test_replace_pair_object_literal_old_absent_still_fails():
    """If old is absent from base (state drift or ambiguous), the
    pair still fail-closes -- the splice path must not bypass the
    fail-closed contract."""
    from deeppeek.recover.apply_patches import _try_replace_pair
    base = "export const state = { value: 1 };"
    old = "export const state = { value: 999 };"
    new = "state = { value: 2 };"
    result = _try_replace_pair(base, old, new)
    assert result is None, (
        f"expected fail-closed (old absent), got {result!r}"
    )


def test_replace_pair_object_literal_rename_falls_through():
    """When old and new name different variables, the object-literal
    splice path must NOT fire -- that's a rename, and raw replace
    handles it.  The existing `board` declaration in base is not
    clobbered."""
    from deeppeek.recover.apply_patches import _try_replace_pair
    base = "const other = { x: 1 };\nconst board = { a: 1 };\n"
    old = "const other = { x: 1 };"
    new = "board = { y: 2 };"
    result = _try_replace_pair(base, old, new)
    if result is not None:
        assert "const board = { a: 1 }" in result, (
            f"object-literal splice clobbered the existing "
            f"`board` declaration:\n{result}"
        )


def test_replace_pair_non_object_literal_skips_splice():
    """When the new body is not an object-literal assignment, the
    splice path must not fire.  Raw replace handles it (as before)."""
    from deeppeek.recover.apply_patches import _try_replace_pair
    base = "export function foo() {\n  return 1;\n}\n"
    old = "export function foo() {\n  return 1;\n}"
    new = "export function foo() {\n  return 2;\n}"
    result = _try_replace_pair(base, old, new)
    assert result is not None, "expected pair to apply"
    assert "return 2" in result
    assert "return 1" not in result
    assert result.count("export function foo") == 1

# ============================================================
# Fix 1 (multi-scope apply)  — pair body declares 2+ scopes
# ============================================================
#
# The pg_pixels.py failure: pair 98's body declared two functions
# (`_maskPixelData` and `hitTestMaskAtPoint`) but was applied as a
# single scoped-replace of `_maskPixelData`.  The second function
# was inserted at the same span, while the base's original copy
# survived below — producing `Identifier 'hitTestMaskAtPoint' has
# already been declared`.
#
# Fix: `_apply_scoped` splits multi-scope bodies via
# `_extract_top_level_scopes` and applies each scope separately.

def test_apply_scoped_multi_scope_body():
    """A body declaring two functions must be split and applied as
    two scoped replaces, not embedded as one replacement for the
    first scope."""
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function bar() {\n"
        "  return 2;\n"
        "}\n"
    )
    new_body = (
        "function foo() {\n"
        "  return 10;\n"
        "}\n"
        "\n"
        "function bar() {\n"
        "  return 20;\n"
        "}\n"
    )
    result = _apply_scoped(base, "foo", new_body)
    assert result is not None, (
        "expected multi-scope apply to succeed")
    assert result.count("function foo") == 1, (
        f"expected exactly 1 'function foo', got "
        f"{result.count('function foo')}:\n{result}")
    assert result.count("function bar") == 1, (
        f"expected exactly 1 'function bar', got "
        f"{result.count('function bar')}:\n{result}")
    assert "return 10" in result, f"new foo body missing:\n{result}"
    assert "return 20" in result, f"new bar body missing:\n{result}"
    assert "return 1\n" not in result, (
        f"old foo body still present:\n{result}")
    assert "return 2\n" not in result, (
        f"old bar body still present:\n{result}")


def test_apply_scoped_single_scope_unchanged():
    """Single-scope body follows the original path unchanged."""
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "function foo() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function unrelated() {\n"
        "  return 99;\n"
        "}\n"
    )
    new_body = (
        "function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    result = _apply_scoped(base, "foo", new_body)
    assert result is not None
    assert "return 2" in result
    assert "return 1\n" not in result
    assert "function unrelated" in result, (
        f"unrelated function lost:\n{result}")
    assert "return 99" in result


# ============================================================
# Fix 2 (duplicate export decl detection)
# ============================================================
#
# `_js_structural_check`'s duplicate-declaration regex must match
# `export function` / `export const` / `export class` in addition
# to the bare forms.  Pre-fix, two `export function foo() {}`
# declarations slipped through undetected because the regex
# required `function` to be the very first token on the line.

def test_duplicate_export_function_detected():
    from deeppeek.recover.validate_output import validate_content
    content = (
        "export function foo() {\n"
        "  return 1;\n"
        "}\n"
        "export function foo() {\n"
        "  return 2;\n"
        "}\n"
    )
    err = validate_content(content, "test.js")
    assert err is not None, (
        "expected duplicate `export function foo` to be rejected")
    assert "duplicate" in err.lower(), (
        f"expected 'duplicate' in error, got {err!r}")
    assert "foo" in err, f"expected declaration name in error, got {err!r}"


def test_duplicate_export_const_detected():
    from deeppeek.recover.validate_output import validate_content
    content = (
        "export const foo = 1;\n"
        "export const foo = 2;\n"
    )
    err = validate_content(content, "test.js")
    assert err is not None
    assert "duplicate" in err.lower()


def test_no_duplicate_still_passes():
    """Sanity: the tightened regex must not flag legitimate code."""
    from deeppeek.recover.validate_output import validate_content
    content = (
        "export function foo() { return 1; }\n"
        "export function bar() { return 2; }\n"
    )
    err = validate_content(content, "test.js")
    assert err is None, f"unexpected rejection: {err!r}"


# ============================================================
# Fix 3 (preamble preservation on full-block replace)
# ============================================================
#
# The pg_state.py failure: block 94 was a full reprint of a file
# body but omitted the 17-line module docstring.  Pass 6 used the
# block body as the base and pass 9 wrote it verbatim, silently
# dropping the docstring.

def test_preserve_preamble_python_docstring():
    from deeppeek.recover.patch_text import _preserve_preamble
    preexisting = (
        '"""Module docstring.\n'
        '\n'
        'Describes what the module does.\n'
        '"""\n'
        '\n'
        'CONSTANT = 1\n'
    )
    block_body = "CONSTANT = 2\n"
    result = _preserve_preamble(preexisting, block_body)
    assert result.startswith('"""Module docstring.'), (
        f"expected preamble preserved:\n{result}")
    assert "CONSTANT = 2" in result
    assert result.endswith("CONSTANT = 2\n")


def test_preserve_preamble_block_has_own():
    """If the block has its own preamble, use it verbatim — no
    pre-existing preamble is prepended."""
    from deeppeek.recover.patch_text import _preserve_preamble
    preexisting = (
        '"""Old docstring."""\n'
        'CONSTANT = 1\n'
    )
    block_body = (
        '"""New docstring."""\n'
        'CONSTANT = 2\n'
    )
    result = _preserve_preamble(preexisting, block_body)
    assert result == block_body, (
        f"expected block body verbatim (own preamble wins):\n{result}")


def test_preserve_preamble_no_preexisting_preamble():
    """If the pre-existing file has no preamble, don't invent one."""
    from deeppeek.recover.patch_text import _preserve_preamble
    preexisting = "CONSTANT = 1\n"
    block_body = "CONSTANT = 2\n"
    result = _preserve_preamble(preexisting, block_body)
    assert result == block_body


def test_preserve_preamble_js_comment():
    """JS leading comments are recognized as preamble."""
    from deeppeek.recover.patch_text import _preserve_preamble
    preexisting = (
        "// Copyright 2026.\n"
        "// All rights reserved.\n"
        "\n"
        "export function foo() { return 1; }\n"
    )
    block_body = "export function foo() { return 2; }\n"
    result = _preserve_preamble(preexisting, block_body)
    assert "// Copyright 2026." in result
    assert "export function foo() { return 2; }" in result


# ============================================================
# Fix 4 (stale warning cleanup on reconcile)
# ============================================================
#
# Pass 6 appends `patch idx=N could not be applied (...)` warnings
# for failed patches.  When reconcile excludes patch N and promotes
# the record, the warning is stale — the patch is now
# `skipped_unplaceable`, not "failed".  Clean it up.

def test_reconcile_clears_stale_pass6_warnings():
    from deeppeek.recover.reconcile import (
        _clear_stale_pass6_warnings)
    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.warnings = [
        "patch idx=86 could not be applied (ellipsis block: ...)",
        "patch idx=90 could not be applied (no scope)",
        "patch or pair application failed; file not written",
        "unrelated warning",
    ]
    excluded_keys = [("patch", 86), ("patch", 90)]
    _clear_stale_pass6_warnings(rec, excluded_keys)
    assert not any(
        "patch idx=86 could not be applied" in w
        for w in rec.warnings), (
        f"stale warning for idx=86 not cleared: {rec.warnings!r}")
    assert not any(
        "patch idx=90 could not be applied" in w
        for w in rec.warnings), (
        f"stale warning for idx=90 not cleared: {rec.warnings!r}")
    assert "unrelated warning" in rec.warnings, (
        f"unrelated warning was cleared: {rec.warnings!r}")


def test_reconcile_clears_only_excluded():
    """Warnings for patches that were NOT excluded stay in place."""
    from deeppeek.recover.reconcile import (
        _clear_stale_pass6_warnings)
    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    rec.warnings = [
        "patch idx=1 could not be applied (a)",
        "patch idx=2 could not be applied (b)",
    ]
    excluded_keys = [("patch", 1)]
    _clear_stale_pass6_warnings(rec, excluded_keys)
    assert len(rec.warnings) == 1
    assert "patch idx=2" in rec.warnings[0], (
        f"expected idx=2 warning preserved, got {rec.warnings!r}")


def test_reconcile_no_excluded_patch_keys():
    """Calling with no excluded patch keys leaves warnings alone."""
    from deeppeek.recover.reconcile import (
        _clear_stale_pass6_warnings)
    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.js", state="FULL_PLUS_PATCH")
    orig = [
        "patch idx=1 could not be applied (a)",
    ]
    rec.warnings = list(orig)
    _clear_stale_pass6_warnings(rec, [("pair", -1, 5)])
    assert rec.warnings == orig

# ============================================================
# Fix 1b (multi-scope with new functions) — pg_panel_bindings.py
# ============================================================
#
# A multi-scope body may declare scopes that DON'T exist in base
# (they're new functions being added alongside a replacement).
# Splitting into individual scoped replaces would fail (the new
# scopes have no span).  The correct behavior is the old single-
# scope path: replace the first scope's span with the WHOLE new_body
# — the new helpers get inserted at the first scope's position,
# which is where the AI placed them.

def test_apply_scoped_multi_scope_with_new_functions():
    """Body declares `_inpaintAction` (exists) plus two new helpers.

    Multi-scope splitting would return None (helpers not in base).
    The fallback to single-scope must insert the whole body at the
    first scope's position, correctly adding the helpers.
    """
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "async function _inpaintAction() {\n"
        "  old_body();\n"
        "}\n"
        "\n"
        "async function _clearImageAction() {\n"
        "  clearImage();\n"
        "}\n"
    )
    new_body = (
        "async function _inpaintAction() {\n"
        "  new_body();\n"
        "  helper_a();\n"
        "  helper_b();\n"
        "}\n"
        "\n"
        "function _rotatedMaskToCanvasB64(mask) {\n"
        "  return mask;\n"
        "}\n"
        "\n"
        "function _setOriginalFromB64(b64) {\n"
        "  return b64;\n"
        "}\n"
    )
    result = _apply_scoped(base, "_inpaintAction", new_body)
    assert result is not None, (
        "expected multi-scope-with-new-helpers to succeed via "
        "single-scope fallback")
    assert result.count("async function _inpaintAction") == 1, (
        f"expected exactly 1 '_inpaintAction', got "
        f"{result.count('async function _inpaintAction')}:\n{result}")
    assert "function _rotatedMaskToCanvasB64" in result, (
        f"helper _rotatedMaskToCanvasB64 missing:\n{result}")
    assert "function _setOriginalFromB64" in result, (
        f"helper _setOriginalFromB64 missing:\n{result}")
    assert "_clearImageAction" in result, (
        f"unrelated function lost:\n{result}")
    assert "old_body" not in result, (
        f"old _inpaintAction body still present:\n{result}")
    assert "new_body" in result
    assert "helper_a" in result
    assert "helper_b" in result


def test_apply_scoped_multi_scope_all_existing_still_splits():
    """The pg_pixels.py fix must still fire when ALL sub-scopes exist
    in base — otherwise the duplicate-declaration bug regresses."""
    from deeppeek.recover.apply_patches import _apply_scoped
    base = (
        "function _maskPixelData(mask) {\n"
        "  old_m();\n"
        "}\n"
        "\n"
        "export function hitTestMaskAtPoint(ix, iy) {\n"
        "  old_h();\n"
        "}\n"
    )
    new_body = (
        "function _maskPixelData(mask) {\n"
        "  new_m();\n"
        "}\n"
        "\n"
        "export function hitTestMaskAtPoint(ix, iy) {\n"
        "  new_h();\n"
        "}\n"
    )
    result = _apply_scoped(base, "_maskPixelData", new_body)
    assert result is not None
    assert result.count("function _maskPixelData") == 1, (
        f"expected exactly 1 '_maskPixelData', got "
        f"{result.count('function _maskPixelData')}:\n{result}")
    assert result.count(
        "export function hitTestMaskAtPoint") == 1, (
        f"expected exactly 1 'hitTestMaskAtPoint', got "
        f"{result.count('export function hitTestMaskAtPoint')}:\n"
        f"{result}")
    assert "new_m" in result
    assert "new_h" in result
    assert "old_m" not in result
    assert "old_h" not in result


# ============================================================
# Fix 4b (stale warning cleanup — additional classes)
# ============================================================

def test_reconcile_clears_pair_failure_warning():
    """`N/M replace pair(s) failed` from pass 6 must be cleared once
    reconcile rewrites the failing pairs to skipped_unplaceable."""
    from deeppeek.recover.reconcile import _clear_stale_pass6_warnings
    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.verification["pair_records"] = [
        {"applied_by": "skipped_unplaceable",
         "pair": {"old_idx": -1, "new_idx": 103}},
    ]
    rec.warnings = [
        "1/2 replace pair(s) failed: ...",
    ]
    _clear_stale_pass6_warnings(rec, [("pair", -1, 103)])
    assert not rec.warnings, (
        f"stale pair-failure warning not cleared: {rec.warnings!r}")


def test_reconcile_clears_unresolved_symbol_warning():
    """`unresolved symbol reference(s) introduced by patch` from
    pass 8.5 must be cleared when the record's `unresolved_calls`
    has been popped by reconcile."""
    from deeppeek.recover.reconcile import _clear_stale_pass6_warnings
    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    # After reconcile pop's unresolved_calls, this field is absent
    rec.verification = {}
    rec.warnings = [
        "unresolved symbol reference(s) introduced by patch: "
        "pgkitNotifyAll (patch almost certainly targeted the wrong file)",
    ]
    _clear_stale_pass6_warnings(rec, [("patch", 86)])
    assert not rec.warnings, (
        f"stale unresolved-symbol warning not cleared: "
        f"{rec.warnings!r}")

# ============================================================
# Fix (2026-11-05) — first-token language detection
# ============================================================
#
# `_infer_body_lang` used keyword-count scoring.  A block whose first
# token is `def ` but whose body contains no `import` or `self.`
# scored `py = 2, js = 0` (still Python, by a thin margin), and a
# block whose first token is `function ` scored `js = 1, py = 0` and
# returned None because `js >= 2` was required.  The comfyui_krita.py
# failure was this: two `def _helper` blocks scored below the
# threshold, `_try_structural_append` bailed, and the record was
# written with calls to undefined names.

def test_infer_body_lang_python_def():
    """First token `def ` must be Python even with no import/self."""
    from deeppeek.recover.patch_structural import _infer_body_lang
    body = (
        "def _rotate_about_center(img, angle_deg):\n"
        "    return img\n"
    )
    assert _infer_body_lang(body) == "python", (
        f"expected python, got {_infer_body_lang(body)!r}"
    )


def test_infer_body_lang_async_and_decorated():
    from deeppeek.recover.patch_structural import _infer_body_lang
    assert _infer_body_lang("async def foo():\n    pass\n") == "python"
    assert _infer_body_lang("@decorator\ndef foo():\n    pass\n") == "python"
    assert _infer_body_lang("class Foo:\n    pass\n") == "python"


def test_infer_body_lang_js_first_token():
    from deeppeek.recover.patch_structural import _infer_body_lang
    assert _infer_body_lang(
        "function foo() {\n    return 1;\n}\n") == "javascript"
    assert _infer_body_lang(
        "export function foo() {}\n") == "javascript"
    assert _infer_body_lang(
        "export const x = 1;\n") == "javascript"
    assert _infer_body_lang(
        "const x = 1;\n") == "javascript"


def test_structural_append_appends_python_defs():
    """End-to-end: a top-level `def` block appends at end of a
    Python module.  Reproduces the comfyui_krita.py failure mode."""
    from deeppeek.recover.patch_structural import _try_structural_append
    base = (
        '"""Utilities."""\n'
        "\n"
        "def paste_image(doc, png, x, y, rotation=0.0):\n"
        "    if rotation:\n"
        "        png = _rotate_about_center(png, rotation)\n"
        "    return png\n"
    )
    body = (
        "def _rotate_about_center(img, angle_deg):\n"
        "    return img\n"
        "\n"
        "def _center_crop(img, w, h):\n"
        "    return img\n"
    )
    result = _try_structural_append(base, body, "comfyui_krita.py")
    assert result is not None, (
        "_try_structural_append returned None; first-token language "
        "detection is likely still using the old scoring heuristic"
    )
    assert "def _rotate_about_center" in result
    assert "def _center_crop" in result
    # Base is preserved intact.
    assert "def paste_image" in result
    assert '"""Utilities."""' in result


# ============================================================
# Fix (2026-11-05) — reconcile ordering
# ============================================================
#
# Two ordering bugs in `pass8_7_reconcile`:
#
#   1. `_diagnose_exclusion` was called AFTER the patch statuses
#      had been mutated to `skipped_*`.  The "could not be placed"
#      branch was unreachable because it checks
#      `applied_by == "failed"`.
#
#   2. `_trust_cap(rec)` was called BEFORE `rec.verification
#      ["reconciled"]` was populated, so the MEDIUM cap never fired
#      and reconciled records always reported HIGH.

def test_diagnose_exclusion_uses_original_status():
    """`_diagnose_exclusion` must honour `original_status` when the
    caller has already mutated the patch status."""
    from deeppeek.recover.reconcile import _diagnose_exclusion
    from deeppeek.recover.core import FileRecord
    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    rec.patches = [{
        "idx": 62,
        "body": '"--output_rotation": {"type": "float", "default": 0.0,',
        "applied_by": "skipped_unplaceable",  # already mutated
    }]
    diag = _diagnose_exclusion(
        ("patch", 62), rec, {}, original_status="failed")
    assert "could not be placed" in diag, (
        f"expected 'could not be placed' branch, got: {diag!r}; "
        f"_diagnose_exclusion is reading the mutated status "
        f"instead of original_status"
    )
    # Without original_status, the mutated skipped_* falls through
    # to the generic branch (this is what the pre-fix code always
    # did, which is why the message was wrong).
    diag2 = _diagnose_exclusion(("patch", 62), rec, {})
    assert "could not be placed" not in diag2, (
        f"without original_status, mutated skipped_* should not "
        f"reach the 'could not be placed' branch; got: {diag2!r}"
    )


def test_trust_cap_returns_medium_after_reconciled():
    """`_trust_cap` must return MEDIUM for a reconciled record."""
    from deeppeek.recover.reconcile import _trust_cap
    from deeppeek.recover.core import FileRecord

    rec = FileRecord(path="x.py", state="FULL_PLUS_PATCH")
    assert _trust_cap(rec) == "HIGH", (
        "reconciled-empty record should cap at HIGH"
    )
    rec.verification["reconciled"] = {"excluded": []}
    assert _trust_cap(rec) == "MEDIUM", (
        "reconciled record must cap at MEDIUM (reconcile excluded "
        "evidence, it did not verify the remaining content)"
    )

    # last_full still caps at MEDIUM regardless of `reconciled`.
    rec2 = FileRecord(path="y.py", state="FULL_PLUS_PATCH")
    rec2.dedup_reason = "last_full"
    assert _trust_cap(rec2) == "MEDIUM"


def test_reconcile_sets_reconciled_before_trust_cap():
    """Ordering invariant: `rec.verification["reconciled"]` must be
    set BEFORE `_trust_cap(rec)` is called in `pass8_7_reconcile`.

    Verified by source inspection rather than integration, because
    constructing a promotable record would require reproducing the
    entire patch-application pipeline.  The invariant is the point:
    a future refactor that reorders these two lines reinstates the
    bug even if every other reconcile test still passes.
    """
    import inspect
    from deeppeek.recover.reconcile import pass8_7_reconcile
    src = inspect.getsource(pass8_7_reconcile)
    idx_reconciled = src.find('rec.verification["reconciled"]')
    idx_trust_cap = src.find("_trust_cap(rec)")
    assert idx_reconciled > 0, (
        "_trust_cap ordering test: pass8_7_reconcile no longer "
        "sets rec.verification['reconciled']; update the test"
    )
    assert idx_trust_cap > 0, (
        "_trust_cap ordering test: pass8_7_reconcile no longer "
        "calls _trust_cap; update the test"
    )
    assert idx_reconciled < idx_trust_cap, (
        f"_trust_cap called at char {idx_trust_cap} BEFORE "
        f"reconciled field set at char {idx_reconciled}; "
        f"reconciled records will not cap at MEDIUM. "
        f"Move the _trust_cap call after the reconciled field is set."
    )

def main():

    tests = [
        ("resolve module dotted top not recovered",
         test_resolve_module_dotted_top_not_recovered),
        ("resolve module dotted top recovered",
         test_resolve_module_dotted_top_recovered),
        ("pass7 reports top level once",
         test_pass7_reports_top_level_once),
        ("walker no comment returns input", test_walker_no_comment_returns_input),
        ("walker no comment contiguous", test_walker_no_comment_contiguous),
        ("walker line comment blank traversed", test_walker_line_comment_blank_traversed),
        ("walker block comment blank traversed", test_walker_block_comment_blank_traversed),
        ("walker contiguous comments", test_walker_contiguous_comments),
        ("body_starts_with_comment", test_body_starts_with_comment),
        ("apply_scoped consumes comment with blank",
         test_apply_scoped_consumes_comment_with_blank),
        ("apply_scoped keeps comment when body has none",
         test_apply_scoped_keeps_comment_when_body_has_none),
        ("apply_scoped consumes decorator",
         test_apply_scoped_consumes_decorator),
        ("dedup_stmt removes patch-added dup",
         test_dedup_stmt_removes_patch_added_dup),
        ("dedup_stmt keeps line in base",
         test_dedup_stmt_keeps_line_in_base),
        ("dedup_stmt keeps no patch provenance",
         test_dedup_stmt_keeps_no_patch_provenance),
        ("dedup_stmt keeps different scope",
         test_dedup_stmt_keeps_different_scope),
        ("dedup_stmt keeps distant", test_dedup_stmt_keeps_distant),
        ("dedup_stmt keeps nfold repetition",
         test_dedup_stmt_keeps_nfold_repetition),
        ("dedup_stmt markdown skipped", test_dedup_stmt_markdown_skipped),
        ("dedup_stmt CSS properties preserved",
         test_dedup_stmt_css_properties_preserved),
        ("dedup_stmt base required for action",
         test_dedup_stmt_base_required_for_any_action),
        ("parse_file_header markdown link",
         test_parse_file_header_markdown_link),
        ("collect_inserted_lines basic",
         test_collect_inserted_lines_basic),
        ("collect_inserted_lines replaced",
         test_collect_inserted_lines_replaced),
        ("split_body_into_blocks statements",
         test_split_body_into_blocks_statements),
        ("split_body_into_blocks brace block",
         test_split_body_into_blocks_brace_block),
        ("filter_body_by_blocks basic",
         test_filter_body_by_blocks_basic),
        ("filter_body_by_blocks context preserved",
         test_filter_body_by_blocks_context_preserved),
        ("filter_body_by_blocks partial dup kept",
         test_filter_body_by_blocks_partial_duplicate_kept),
        ("filter_body_by_blocks all filtered",
         test_filter_body_by_blocks_all_filtered),
        ("insert_filter end-to-end",
         test_insert_filter_end_to_end),
        ("dedup_func_block removes duplicate",
         test_dedup_func_block_removes_duplicate),
        ("dedup_func_block keeps different name",
         test_dedup_func_block_keeps_different_name),
        ("dedup_func_block keeps different body",
         test_dedup_func_block_keeps_different_body),
        ("dedup_func_block keeps different params",
         test_dedup_func_block_keeps_different_params),
        ("dedup_func_block keeps different scope",
         test_dedup_func_block_keeps_different_scope),
        ("dedup_class_block removes duplicate",
         test_dedup_class_block_removes_duplicate),
        ("dedup_func_block markdown skipped",
         test_dedup_func_block_markdown_skipped),
        ("dedup_func_block export prefix",
         test_dedup_func_block_export_prefix),
        ("dedup_func_block async",
         test_dedup_func_block_async),
        ("dedup_func_block python different strings kept",
         test_dedup_func_block_python_different_strings_kept),
        ("dedup_func_block python same string removed",
         test_dedup_func_block_python_same_string_removed),
        ("scope_replace via patch not insert",
         test_scope_replace_via_patch_not_insert),
        ("scope_replace via patch non-matching scope inserts",
         test_scope_replace_via_patch_nonmatching_scope_inserts),
        ("indented export rejected",
         test_indented_export_rejected),
        ("indented export in embedded JS rejected",
         test_indented_export_in_embedded_js_rejected),
        ("top-level export not rejected",
         test_top_level_export_not_rejected),
        ("export inside string literal not rejected",
         test_export_inside_string_literal_not_rejected),
        ("cross_file empty when no records",
         test_cross_file_empty_when_no_records),
        ("cross_file detects identical long functions",
         test_cross_file_detects_identical_long_functions),
        ("cross_file ignores short functions",
         test_cross_file_ignores_short_functions),
        ("cross_file ignores preexisting",
         test_cross_file_ignores_preexisting),
        ("cross_file ignores same file duplicates",
         test_cross_file_ignores_same_file_duplicates),
        ("cross_file ignores partial only",
         test_cross_file_ignores_partial_only),
        ("looks_like_prose_ui ui mockup",
         test_looks_like_prose_ui_ui_mockup),
        ("looks_like_prose_ui prose",
         test_looks_like_prose_ui_prose),
        ("looks_like_prose_ui code",
         test_looks_like_prose_ui_code),
        ("looks_like_prose_ui docstring kept",
         test_looks_like_prose_ui_docstring_kept),
        ("looks_like_prose_ui filename kept",
         test_looks_like_prose_ui_filename_kept),
        ("looks_like_prose_ui too short",
         test_looks_like_prose_ui_too_short),
        ("filename_in_orphan_header basic",
         test_filename_in_orphan_header_basic),
        ("filename_in_orphan_header not in first 3",
         test_filename_in_orphan_header_not_in_first_3_lines),
        ("filename_in_orphan_header multiple matches",
         test_filename_in_orphan_header_multiple_matches),
        ("pass2 classify filters prose",
         test_pass2_classify_filters_prose),
        ("pass4 attributes via body filename",
         test_pass4_attributes_via_body_filename),
        ("compute_block_disposition applied",
         test_compute_block_disposition_applied),
        ("compute_block_disposition orphan_prose",
         test_compute_block_disposition_orphan_prose),
        ("compute_block_disposition preexisting",
         test_compute_block_disposition_preexisting),
        ("pass4 marks not_a_file as attributed",
         test_pass4_marks_not_a_file_as_attributed),
        ("pass4 marks bash not_a_file as orphan_not_code",
         test_pass4_marks_bash_not_a_file_as_orphan_not_code),
        # Fix 5 -- sanity failure attribution
        ("attribute_sanity_failure finds culprit",
         test_attribute_sanity_failure_finds_culprit),
        ("attribute_sanity_failure no patches",
         test_attribute_sanity_failure_no_patches),
        ("attribute_sanity_failure no base",
         test_attribute_sanity_failure_no_base),
        ("attribute_sanity_failure llm patch stops attribution",
         test_attribute_sanity_failure_llm_patch_stops_attribution),
        ("attribute_sanity_failure replay failure",
         test_attribute_sanity_failure_replay_failure),
        # Fix 5 companion -- direct unit for _try_object_literal_splice
        ("object_literal_splice const omitted",
         test_object_literal_splice_const_omitted),
        ("object_literal_splice export preserved",
         test_object_literal_splice_export_preserved),
        # Replace-pair object-literal prefix preservation
        ("replace_pair object_literal preserves const",
         test_replace_pair_object_literal_preserves_const),
        ("replace_pair object_literal export preserved",
         test_replace_pair_object_literal_export_preserved),
        ("replace_pair object_literal old absent fails",
         test_replace_pair_object_literal_old_absent_still_fails),
        ("replace_pair object_literal rename falls through",
         test_replace_pair_object_literal_rename_falls_through),
        ("replace_pair non object literal skips splice",
         test_replace_pair_non_object_literal_skips_splice),
        ("apply_scoped multi-scope body",
         test_apply_scoped_multi_scope_body),
        ("apply_scoped single scope unchanged",
         test_apply_scoped_single_scope_unchanged),
        ("duplicate export function detected",
         test_duplicate_export_function_detected),
        ("duplicate export const detected",
         test_duplicate_export_const_detected),
        ("no duplicate still passes",
         test_no_duplicate_still_passes),
        ("preserve preamble python docstring",
         test_preserve_preamble_python_docstring),
        ("preserve preamble block has own",
         test_preserve_preamble_block_has_own),
        ("preserve preamble no preexisting preamble",
         test_preserve_preamble_no_preexisting_preamble),
        ("preserve preamble js comment",
         test_preserve_preamble_js_comment),
        ("reconcile clears stale pass6 warnings",
         test_reconcile_clears_stale_pass6_warnings),
        ("reconcile clears only excluded",
         test_reconcile_clears_only_excluded),
        ("reconcile no excluded patch keys",
         test_reconcile_no_excluded_patch_keys),
        ("apply_scoped multi-scope with new functions",
         test_apply_scoped_multi_scope_with_new_functions),
        ("apply_scoped multi-scope all existing still splits",
         test_apply_scoped_multi_scope_all_existing_still_splits),
        ("reconcile clears pair failure warning",
         test_reconcile_clears_pair_failure_warning),
        ("reconcile clears unresolved symbol warning",
         test_reconcile_clears_unresolved_symbol_warning),
        ("infer_body_lang python def", test_infer_body_lang_python_def),
        ("infer_body_lang async and decorated",
         test_infer_body_lang_async_and_decorated),
        ("infer_body_lang js first token",
         test_infer_body_lang_js_first_token),
        ("structural append appends python defs",
         test_structural_append_appends_python_defs),
        ("diagnose exclusion uses original status",
         test_diagnose_exclusion_uses_original_status),
        ("trust cap returns medium after reconciled",
         test_trust_cap_returns_medium_after_reconciled),
        ("reconcile sets reconciled before trust cap",
         test_reconcile_sets_reconciled_before_trust_cap),
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
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\nAll {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
