"""Markdown-it-py walker: link targets and inline code spans.

Fenced code blocks are not handled here — the orchestrator hands
each one to the tree-sitter walker.
"""
from __future__ import annotations

from markdown_it import MarkdownIt

from .models import PathMatch
from .tree_sitter import _looks_like_path


def walk(text: str) -> list[PathMatch]:
    md = MarkdownIt("commonmark")
    out: list[PathMatch] = []
    for token in md.parse(text):
        if token.type == "inline":
            _walk_inline(token, out)
        elif token.type == "link_open":
            _add_link(token, out)
        elif token.type == "code_inline":
            _add_code(token, out)
    return out


def _walk_inline(token, out: list) -> None:
    for child in token.children or []:
        if child.type == "link_open":
            _add_link(child, out)
        elif child.type == "code_inline":
            _add_code(child, out)


def _add_link(token, out: list) -> None:
    href = token.attrGet("href") or ""
    if not href or _is_nonpath(href):
        return
    if _looks_like_path(href):
        out.append(PathMatch(path=href, kind="file", tier=3,
                             source="markdown-link"))


def _add_code(token, out: list) -> None:
    content = (token.content or "").strip()
    if content and _looks_like_path(content):
        out.append(PathMatch(path=content, kind="file", tier=3,
                             source="markdown-code-span"))


def _is_nonpath(s: str) -> bool:
    return ("://" in s or s.startswith("mailto:")
            or s.startswith("#") or s.startswith("data:"))