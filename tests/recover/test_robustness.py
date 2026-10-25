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



def main():
    tests = [
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
