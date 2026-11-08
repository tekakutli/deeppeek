import pytest

from deeppeek.paths import (
    HeaderEvent, PathEvent, extract_events, extract_paths,
)


B = chr(96) * 3


def _fence(lang, body):
    return f"{B}{lang}\n{body}\n{B}"


# ---- flat extraction (unchanged) -----------------------------------

def test_python_import_full_module():
    pytest.importorskip("tree_sitter_python")
    doc = _fence(
        "python",
        "from .foo import bar\n"
        "from deeppeek.commands import type",
    )
    paths = {m.path for m in extract_paths(doc)}
    assert "foo.py" in paths
    assert ("deeppeek/commands.py" in paths
            or "deeppeek/commands/__init__.py" in paths)


def test_python_string_literal_full_path():
    pytest.importorskip("tree_sitter_python")
    doc = _fence("python", 'p = "src/deeppeek/commands/type.py"')
    paths = {m.path for m in extract_paths(doc)}
    assert "src/deeppeek/commands/type.py" in paths


def test_js_import():
    pytest.importorskip("tree_sitter_javascript")
    doc = _fence(
        "js",
        'import { foo } from "./util/foo.js";\n'
        'const bar = require("../lib/bar");',
    )
    paths = {m.path for m in extract_paths(doc)}
    assert any(p.endswith("util/foo.js") or "util/foo" in p for p in paths)
    assert any("lib/bar" in p for p in paths)


def test_markdown_link_and_code_span():
    inline = chr(96)
    doc = f"See [the readme](./README.md) and {inline}src/foo.py{inline}."
    paths = {m.path for m in extract_paths(doc)}
    assert "./README.md" in paths or "README.md" in paths
    assert "src/foo.py" in paths


def test_nested_markdown_fence():
    inline = chr(96)
    doc = _fence(
        "markdown",
        f"See [x](./docs/x.md) and {inline}lib/y.py{inline}.",
    )
    paths = {m.path for m in extract_paths(doc)}
    assert "./docs/x.md" in paths or "docs/x.md" in paths
    assert "lib/y.py" in paths


def test_untagged_fence_skipped():
    doc = _fence("", "src/should_not_appear.py")
    paths = {m.path for m in extract_paths(doc)}
    assert "src/should_not_appear.py" not in paths


def test_no_prose_false_positives():
    doc = "This is my file. It has grown large. See os.path and self.foo.bar."
    paths = {m.path for m in extract_paths(doc)}
    assert "file" not in paths
    assert "os.path" not in paths
    assert "self.foo.bar" not in paths


# ---- structured extraction (new) -----------------------------------

def test_events_linear_order():
    inline = chr(96)
    doc = (
        "# Title\n\n"
        "## Section A\n\n"
        f"{inline}src/a.py{inline}\n\n"
        "## Section B\n\n"
        f"{inline}src/b.py{inline}\n"
    )
    events = extract_events(doc)
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "HeaderEvent",       # Title
        "HeaderEvent",       # Section A
        "PathEvent",         # src/a.py
        "HeaderEvent",       # Section B
        "PathEvent",         # src/b.py
    ]


def test_events_breadcrumb():
    inline = chr(96)
    doc = (
        "# Title\n\n"
        "## Section A\n\n"
        f"{inline}src/a.py{inline}\n"
    )
    events = extract_events(doc)
    paths = [e for e in events if isinstance(e, PathEvent)]
    assert len(paths) == 1
    assert paths[0].breadcrumb == ("Title", "Section A")


def test_events_nested_headers():
    inline = chr(96)
    doc = (
        "# Title\n\n"
        "## A\n\n"
        "### A.1\n\n"
        f"{inline}src/deep.py{inline}\n"
    )
    events = extract_events(doc)
    paths = [e for e in events if isinstance(e, PathEvent)]
    assert paths[0].breadcrumb == ("Title", "A", "A.1")


def test_events_sibling_headers_isolated():
    inline = chr(96)
    doc = (
        "# Title\n\n"
        "## A\n\n"
        f"{inline}src/a.py{inline}\n\n"
        "## B\n\n"
        f"{inline}src/b.py{inline}\n"
    )
    events = extract_events(doc)
    paths = [e for e in events if isinstance(e, PathEvent)]
    crumbs = [p.breadcrumb for p in paths]
    assert crumbs == [("Title", "A"), ("Title", "B")]


def test_events_path_before_any_header():
    inline = chr(96)
    doc = f"{inline}src/early.py{inline}\n\n# Title\n"
    events = extract_events(doc)
    paths = [e for e in events if isinstance(e, PathEvent)]
    assert paths[0].breadcrumb == ()
