"""Extract filenames and paths from a markdown document.

Two public entry points:

    extract_paths(source)    — flat, deduped list of PathMatch
    extract_events(source)   — linear sequence of HeaderEvent /
                               PathEvent in document order

Extraction tiers (lower = more reliable):

    1  tree-sitter AST, per fenced code block
    2  markdown-it-py — link targets, inline code spans
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union

from markdown_it import MarkdownIt

from . import markdown as _md
from . import tree_sitter as _ts
from .models import PathMatch


_FENCE_TO_LANG = {
    "py": "python", "python": "python", "python3": "python",
    "js": "javascript", "javascript": "javascript", "jsx": "javascript",
    "mjs": "javascript", "cjs": "javascript",
    "ts": "javascript", "typescript": "javascript", "tsx": "javascript",
    "html": "html", "htm": "html",
    "css": "css", "scss": "css",
}


@dataclass
class HeaderEvent:
    breadcrumb: tuple[str, ...]   # includes this header at the end
    level: int                     # 1 for '#', 2 for '##', …


@dataclass
class PathEvent:
    breadcrumb: tuple[str, ...]   # current header stack
    match: PathMatch


Event = Union[HeaderEvent, PathEvent]


def extract_paths(source: str | Path) -> list[PathMatch]:
    events = extract_events(source)
    return _dedupe(ev.match for ev in events if isinstance(ev, PathEvent))


def extract_events(source: str | Path) -> list[Event]:
    text = (source.read_text(encoding="utf-8", errors="replace")
            if isinstance(source, Path) else source)
    tokens = MarkdownIt("commonmark").parse(text)

    events: list[Event] = []
    stack: list[tuple[int, str]] = []

    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1])
            heading_text = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                heading_text = tokens[i + 1].content
            stack = [(l, t) for l, t in stack if l < level]
            stack.append((level, heading_text))
            events.append(HeaderEvent(
                breadcrumb=tuple(t for _, t in stack),
                level=level,
            ))
        elif token.type == "inline":
            bc = tuple(t for _, t in stack)
            for m in _inline_matches(token):
                events.append(PathEvent(breadcrumb=bc, match=m))
        elif token.type == "fence":
            bc = tuple(t for _, t in stack)
            lang = (token.info or "").strip().split(None, 1)[0].lower()
            for m in _fence_matches(token.content, lang):
                events.append(PathEvent(breadcrumb=bc, match=m))

    return events


def _inline_matches(token) -> list[PathMatch]:
    out: list[PathMatch] = []
    for child in token.children or []:
        if child.type == "link_open":
            href = child.attrGet("href") or ""
            if href and not _is_nonpath(href) and _ts._looks_like_path(href):
                out.append(PathMatch(path=href, kind="file", tier=2,
                                     source="markdown-link"))
        elif child.type == "code_inline":
            content = (child.content or "").strip()
            if content and _ts._looks_like_path(content):
                out.append(PathMatch(path=content, kind="file", tier=2,
                                     source="markdown-code-span"))
    return out


def _fence_matches(content: str, lang: str) -> list[PathMatch]:
    out: list[PathMatch] = []
    if lang in ("markdown", "md"):
        out.extend(_md.walk(content))
        for tok in MarkdownIt("commonmark").parse(content):
            if tok.type == "fence":
                inner_lang = (tok.info or "").strip().split(None, 1)[0].lower()
                out.extend(_fence_matches(tok.content, inner_lang))
        return out
    target = _FENCE_TO_LANG.get(lang)
    if target is None:
        return out
    out.extend(_ts.walk(content.encode(), target))
    return out


def _is_nonpath(s: str) -> bool:
    return ("://" in s or s.startswith("mailto:")
            or s.startswith("#") or s.startswith("data:"))


def _dedupe(matches) -> list[PathMatch]:
    best: dict[tuple[str, str], PathMatch] = {}
    for m in matches:
        key = (m.normalized(), m.kind)
        existing = best.get(key)
        if existing is None or m.tier < existing.tier:
            best[key] = m
    return sorted(best.values(),
                  key=lambda m: (m.tier, m.normalized(), m.kind))
