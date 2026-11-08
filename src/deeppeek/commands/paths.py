"""`deeppeek paths` — extract filenames and paths from a document."""
import argparse
import json
import sys
from pathlib import Path

from ..paths import (
    HeaderEvent, PathEvent, extract_events, extract_paths,
)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "paths",
        help="Extract filenames and paths from a markdown document",
    )
    p.add_argument("document", type=Path, help="markdown document to scan")
    p.add_argument(
        "-o", "--output", type=Path, default=None, metavar="PATH",
        help="write to PATH instead of stdout.  '-' is stdout (default).",
    )
    p.add_argument("--format", choices=["plain", "json"], default="plain")
    p.add_argument(
        "--min-tier", type=int, default=2, metavar="N",
        help="only emit matches with tier <= N (1 = AST, 2 = all; "
             "default: 2)",
    )
    p.add_argument(
        "--kind", choices=["file", "dir", "module", "all"], default="all",
    )
    p.add_argument(
        "--structure", action="store_true",
        help="annotate paths with the document's header structure: "
             "headers appear as '//'-prefixed comments, paths are "
             "indented under their section.  Preserves the input "
             "markdown's heading hierarchy.  Sections with no paths "
             "in their subtree are pruned.",
    )
    p.add_argument(
        "--structure-full", action="store_true",
        help="with --structure, keep every header, even ones with no "
             "paths.  Implies --structure.",
    )
    p.add_argument(
        "--resolve-against", type=Path, default=None, metavar="DIR",
        help="attempt to resolve each relative path against DIR; "
             "marks resolved vs missing.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if not args.document.is_file():
        print(f"error: not a file: {args.document}", file=sys.stderr)
        return 1

    if args.structure_full:
        args.structure = True

    if args.structure:
        return _run_structured(args)

    matches = extract_paths(args.document)
    matches = [m for m in matches if m.tier <= args.min_tier]
    if args.kind != "all":
        matches = [m for m in matches if m.kind == args.kind]

    root = _resolve_root(args)
    if root is None:
        return 1
    if root is not False:  # sentinel: --resolve-against was given and valid
        for m in matches:
            m.resolved = _try_resolve(m.path, root)

    if args.format == "json":
        body = json.dumps({
            "document": str(args.document),
            "matches": [m.to_dict() for m in matches],
        }, indent=2) + "\n"
    else:
        lines: list[str] = []
        for m in matches:
            if args.resolve_against is not None:
                mark = "OK  " if m.resolved else "MISS"
                lines.append(f"{mark}  {m.path}")
            else:
                lines.append(m.path)
        body = "".join(line + "\n" for line in lines)

    _write_output(body, args, len(matches))
    return 0


def _run_structured(args: argparse.Namespace) -> int:
    events = extract_events(args.document)

    filtered: list = []
    for ev in events:
        if isinstance(ev, HeaderEvent):
            filtered.append(ev)
        else:
            if ev.match.tier > args.min_tier:
                continue
            if args.kind != "all" and ev.match.kind != args.kind:
                continue
            filtered.append(ev)

    root = _resolve_root(args)
    if root is None:
        return 1
    if root is not False:
        for ev in filtered:
            if isinstance(ev, PathEvent):
                ev.match.resolved = _try_resolve(ev.match.path, root)

    if not args.structure_full:
        filtered = _prune_empty_sections(filtered)

    n_paths = sum(1 for ev in filtered if isinstance(ev, PathEvent))

    if args.format == "json":
        tree = _events_to_tree(filtered)
        body = json.dumps({
            "document": str(args.document),
            "root_paths": tree["paths"],
            "structure": tree["children"],
        }, indent=2) + "\n"
    else:
        body = _format_structure(
            filtered, with_marks=args.resolve_against is not None,
        )

    _write_output(body, args, n_paths)
    return 0


# ---- helpers ------------------------------------------------------


def _resolve_root(args):
    """Return the resolved root Path, False if --resolve-against was
    not given, or None on error (already reported)."""
    if args.resolve_against is None:
        return False
    root = args.resolve_against.resolve()
    if not root.is_dir():
        print(f"error: --resolve-against is not a directory: {root}",
              file=sys.stderr)
        return None
    return root


def _prune_empty_sections(events):
    """Drop headers whose subtree contains no paths.

    A header survives iff it is an ancestor of at least one
    PathEvent.  Paths are always kept.  This turns a full outline
    into the minimal outline that still shows where each path sits.
    """
    have_paths: set[tuple[str, ...]] = set()
    for ev in events:
        if isinstance(ev, PathEvent):
            bc = ev.breadcrumb
            for i in range(1, len(bc) + 1):
                have_paths.add(bc[:i])

    kept: list = []
    for ev in events:
        if isinstance(ev, HeaderEvent):
            if ev.breadcrumb in have_paths:
                kept.append(ev)
        else:
            kept.append(ev)
    return kept


def _events_to_tree(events):
    root = {"type": "root", "children": [], "paths": []}
    stack = [root]
    for ev in events:
        if isinstance(ev, HeaderEvent):
            while len(stack) > ev.level:
                stack.pop()
            node = {
                "type": "header", "level": ev.level,
                "text": ev.breadcrumb[-1],
                "children": [], "paths": [],
            }
            stack[-1]["children"].append(node)
            stack.append(node)
        else:
            stack[-1]["paths"].append(ev.match.to_dict())
    return root


def _format_structure(events, *, with_marks: bool) -> str:
    lines: list[str] = []
    seen: dict[tuple[str, ...], set[str]] = {}

    for ev in events:
        if isinstance(ev, HeaderEvent):
            level = ev.level
            prefix = "// " + "  " * (level - 1) + "#" * level + " "
            lines.append(prefix + ev.breadcrumb[-1])
            seen[ev.breadcrumb] = set()
            continue

        sec = ev.breadcrumb
        norm = ev.match.normalized()
        if sec in seen and norm in seen[sec]:
            continue
        if sec in seen:
            seen[sec].add(norm)

        level = len(ev.breadcrumb)
        prefix = "" if level == 0 else " " * (3 * level + 4)

        if with_marks:
            mark = "OK  " if ev.match.resolved else "MISS"
            lines.append(f"{prefix}{mark}  {ev.match.path}")
        else:
            lines.append(prefix + ev.match.path)

    return "\n".join(lines) + "\n" if lines else ""


def _write_output(body: str, args: argparse.Namespace, count: int) -> None:
    if args.output is None or str(args.output) == "-":
        sys.stdout.write(body)
        return
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    except OSError as e:
        print(f"error: cannot write {args.output}: {e}", file=sys.stderr)
        return
    print(f"Wrote {count} match(es) to {args.output}", file=sys.stderr)


def _try_resolve(path: str, root: Path) -> bool:
    p = Path(path)
    candidate = p if p.is_absolute() else root / p
    if candidate.exists():
        return True
    if not candidate.suffix:
        for ext in (".py", ".js", ".ts", ".md", ".json"):
            if candidate.with_suffix(ext).exists():
                return True
        if (candidate / "__init__.py").exists():
            return True
        if (candidate / "index.js").exists():
            return True
    return False
