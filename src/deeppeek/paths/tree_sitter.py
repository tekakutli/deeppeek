"""AST extraction using tree-sitter.

Grammars are loaded lazily; a missing grammar returns [] and the
orchestrator falls back to markdown-tier extraction for that block.
No regex over source text — the walkers navigate named nodes and
field names.
"""
from __future__ import annotations

from .models import PathMatch


def walk(source: bytes, lang_name: str, base_dir: str = "") -> list[PathMatch]:
    parser = _make_parser(lang_name)
    if parser is None:
        return []
    try:
        tree = parser.parse(source)
    except Exception:
        return []
    out: list[PathMatch] = []
    _WALKERS[lang_name](tree.root_node, source, base_dir, out)
    return out


def _make_parser(lang_name: str):
    try:
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        if lang_name == "python":
            import tree_sitter_python as m
        elif lang_name == "javascript":
            import tree_sitter_javascript as m
        elif lang_name == "html":
            import tree_sitter_html as m
        elif lang_name == "css":
            import tree_sitter_css as m
        else:
            return None
        ptr = m.language()
    except ImportError:
        return None
    try:
        return Parser(Language(ptr))
    except (TypeError, AttributeError):
        try:
            p = Parser()
            p.set_language(Language(ptr))
            return p
        except Exception:
            return None


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


_KNOWN_EXTS = frozenset({
    "py", "pyi", "js", "mjs", "cjs", "jsx", "ts", "tsx",
    "md", "json", "yaml", "yml", "toml", "sh", "bash", "zsh",
    "html", "htm", "css", "scss", "rs", "go", "rb", "php",
    "java", "kt", "swift", "c", "cc", "cpp", "h", "hpp",
    "sql", "xml", "svg", "csv", "txt", "log",
})


def _looks_like_path(value: str) -> bool:
    """Structure-aware check (not a prose regex).

    A string is a candidate path iff:
      · it is bounded (2..500 chars), contains no whitespace or
        prose punctuation, and is not a URL;
      · it ends in a known source extension, OR
      · it contains a `/` or `\\`.

    The check deliberately rejects prose-looking strings like
    `` "this is my file" `` without needing a regex over prose.
    """
    if not value:
        return False
    s = value.strip()
    if len(s) < 2 or len(s) > 500:
        return False
    if "://" in s:
        return False
    if any(c in s for c in "\n\r\t (){}[]<>,;\"'"):
        return False
    last = s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in last:
        if last.rsplit(".", 1)[1].lower() in _KNOWN_EXTS:
            return True
    if "/" in s or "\\" in s:
        return True
    return False


# ---------- Python ----------

def _walk_python(node, src: bytes, base_dir: str, out: list) -> None:
    t = node.type

    if t == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":
                _emit_python_module(_text(child, src), base_dir, out, "python-import")
            elif child.type == "aliased_import" and child.named_children:
                inner = child.named_children[0]
                if inner.type == "dotted_name":
                    _emit_python_module(_text(inner, src), base_dir, out, "python-import")

    elif t == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if module is None and node.named_children:
            module = node.named_children[0]
        if module is not None:
            _emit_python_module(_text(module, src), base_dir, out, "python-from-import")

    elif t == "string":
        value = _string_value(node, src)
        if _looks_like_path(value):
            out.append(PathMatch(
                path=value, kind="file", tier=3,
                source="python-string-literal",
                location=f"line {node.start_point[0] + 1}",
            ))

    elif t == "call":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and args is not None and args.named_children:
            fn_name = _text(fn, src)
            if fn_name in ("open", "Path", "read_text", "write_text", "read_bytes"):
                first = args.named_children[0]
                if first.type == "string":
                    value = _string_value(first, src)
                    if _looks_like_path(value):
                        out.append(PathMatch(
                            path=value, kind="file", tier=2,
                            source=f"python-call:{fn_name}",
                            location=f"line {node.start_point[0] + 1}",
                        ))

    for c in node.children:
        _walk_python(c, src, base_dir, out)


def _emit_python_module(mod: str, base_dir: str, out: list, source: str) -> None:
    if not mod:
        return
    if mod.startswith("."):
        n_dots = len(mod) - len(mod.lstrip("."))
        rel = mod[n_dots:]
        parts = base_dir.split("/") if base_dir else []
        if n_dots > 1 and (n_dots - 1) <= len(parts):
            parts = parts[:-(n_dots - 1)]
        full = "/".join(parts + rel.split(".")) if rel else "/".join(parts)
        if not full:
            return
    else:
        full = mod.replace(".", "/")

    out.append(PathMatch(path=full, kind="module", tier=2, source=source))
    out.append(PathMatch(path=full + ".py", kind="file", tier=3,
                         source=source + "-implied"))
    out.append(PathMatch(path=full + "/__init__.py", kind="file", tier=3,
                         source=source + "-implied"))


def _string_value(node, src: bytes) -> str:
    raw = _text(node, src)
    i = 0
    while i < len(raw) and raw[i] in "rRbBuUfF":
        i += 1
    raw = raw[i:]
    for q in ('"""', "'''", '"', "'"):
        if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
            return raw[len(q):-len(q)]
    return raw


# ---------- JavaScript ----------

def _walk_javascript(node, src: bytes, base_dir: str, out: list) -> None:
    t = node.type

    if t == "import_statement":
        source = node.child_by_field_name("source")
        if source is not None:
            _emit_js_path(_js_string_value(source, src), base_dir, out, "js-import")

    elif t == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and args is not None and args.named_children:
            fn_name = _text(fn, src)
            if fn_name in ("require", "import", "importScripts"):
                _emit_js_path(
                    _js_string_value(args.named_children[0], src),
                    base_dir, out, f"js-{fn_name}",
                )

    for c in node.children:
        _walk_javascript(c, src, base_dir, out)


def _emit_js_path(value: str, base_dir: str, out: list, source: str) -> None:
    if not value:
        return
    if value.startswith("./") or value.startswith("../"):
        parts = base_dir.split("/") if base_dir else []
        for seg in value.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        full = "/".join(parts)
    elif value.startswith("/"):
        full = value.lstrip("/")
    else:
        return  # bare npm specifier
    out.append(PathMatch(path=full, kind="module", tier=2, source=source))
    if not any(full.endswith("." + e) for e in ("js", "mjs", "cjs", "ts", "tsx", "jsx")):
        for ext in ("js", "mjs", "cjs", "ts", "tsx", "jsx"):
            out.append(PathMatch(path=f"{full}.{ext}", kind="file", tier=3,
                                 source=source + "-implied"))
        out.append(PathMatch(path=f"{full}/index.js", kind="file", tier=3,
                             source=source + "-implied"))


def _js_string_value(node, src: bytes) -> str:
    raw = _text(node, src)
    for q in ('"', "'", "`"):
        if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2:
            return raw[1:-1]
    return raw


# ---------- HTML ----------

def _walk_html(node, src: bytes, base_dir: str, out: list) -> None:
    if node.type == "attribute":
        name_node = node.child_by_field_name("attribute_name")
        value_node = node.child_by_field_name("attribute_value")
        if name_node is not None and value_node is not None:
            name = _text(name_node, src).lower()
            if name in ("src", "href", "action", "poster",
                        "data-src", "data-href"):
                value = _js_string_value(value_node, src).strip()
                if _looks_like_path(value):
                    out.append(PathMatch(
                        path=value, kind="file", tier=2,
                        source=f"html-{name}",
                        location=f"line {node.start_point[0] + 1}",
                    ))
    for c in node.children:
        _walk_html(c, src, base_dir, out)


# ---------- CSS ----------

def _walk_css(node, src: bytes, base_dir: str, out: list) -> None:
    if node.type == "import_statement":
        for c in node.named_children:
            value = _js_string_value(c, src)
            if _looks_like_path(value):
                out.append(PathMatch(path=value, kind="file", tier=2,
                                     source="css-import"))
    elif node.type == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and _text(fn, src).lower() == "url" and args is not None:
            for c in args.named_children:
                value = _js_string_value(c, src)
                if _looks_like_path(value):
                    out.append(PathMatch(
                        path=value, kind="file", tier=2,
                        source="css-url",
                        location=f"line {node.start_point[0] + 1}",
                    ))
    for c in node.children:
        _walk_css(c, src, base_dir, out)


_WALKERS = {
    "python":     _walk_python,
    "javascript": _walk_javascript,
    "html":       _walk_html,
    "css":        _walk_css,
}