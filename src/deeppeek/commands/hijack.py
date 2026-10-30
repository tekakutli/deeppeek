"""`deeppeek hijack` — redirect code-block downloads to cwd.

Opens an SSE connection to the extension, tells it to install a
click interceptor on every code-block "Download" button in the
active DeepSeek tab, and then writes every intercepted download to
the current working directory instead of the browser's Downloads
folder.

The extension side is `injected.js` Part 12; the interceptor is a
capture-phase click listener that fires before React's handler and
preventDefault()s the browser download. The file content is
POSTed back as base64 and written here.

    deeppeek hijack                  # write to cwd, flat
    deeppeek hijack --dir ./tmp      # write to ./tmp, flat
    deeppeek hijack --preserve-path  # keep declared subdirectories
    # Ctrl+C to stop (uninstalls the interceptor)

By default, only the basename of a declared path is kept: a block
whose prose says `utils/paths.py` writes to `<dir>/paths.py`. Pass
`--preserve-path` to keep the directory structure (creating
subdirectories as needed). Stripping is the safe default because a
declared path could otherwise land files in unexpected places; the
basename cannot escape `<dir>`.

Existing files at the target path are overwritten (last write wins).
No ` (1)` suffix is generated, so re-clicking a Download button is
idempotent. This mirrors `deeppeek recover --apply-to` semantics.

STRUCTURAL FILENAME DERIVATION (2026-11-05)
===========================================
When the extension's prose-based derivation fails (the surrounding
prose doesn't name a single source file), the extension sends
`source: 'lang-fallback'` with a timestamped placeholder filename
(`python_20261105_120000.py`) and includes the raw `lang` label.
This command then re-derives the filename *structurally* from the
code body itself:

  · For Python content: `ast.parse` + `ast.get_docstring` to
    extract the module docstring, then read the first non-empty
    line.  If it starts with a `module.name — <description>`
    pattern (a project convention), `write.py` is derived from
    `recover.write`.

  · For non-Python content: extract the leading `/* ... */` block
    comment or contiguous `// ...` line comments and apply the
    same module-path pattern.

Structural derivation is preferred over the extension's prose
lookback because the code body is *authoritative* — the AI wrote
`recover.write` explicitly as the module's identifier.  Prose
extraction is heuristic; body extraction is grounded.

The `source` field on the ack tells us which derivation the
extension used.  `lang-fallback` is the only one that triggers
re-derivation; anything else (`prose-header`, `prose-single`,
`prose-backtick`, `body-comment`) is trusted as-is because the
surrounding prose directly named the file.

Motivating case: a code block whose docstring says
`recover.write — Pass 9 (write files, manifest, warnings).` but
whose preceding prose is a long thought process mentioning multiple
`.py` filenames.  The extension's `_collectPrecedingProse` only
looks at the last 4 DOM siblings, which for this conversation are
the tail of the thought process (no filenames).  Prose derivation
fails; the CLI's structural pass picks `write.py` out of the
docstring.
"""
import argparse
import ast
import base64
import os
import queue
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

from ..capture.server import ExportServer


# Language label → canonical file extension.  Used when the
# structural derivation succeeds in finding a *stem* but the source
# code itself doesn't carry a definitive extension.  For Python
# specifically the extension is unambiguous (`ast.parse` succeeding
# implies `.py`); for other languages the label is the best hint.
_EXT_BY_LANG = {
    "python": ".py", "py": ".py", "python3": ".py",
    "javascript": ".js", "js": ".js", "jsx": ".jsx",
    "mjs": ".mjs", "cjs": ".cjs",
    "typescript": ".ts", "ts": ".ts", "tsx": ".tsx",
    "markdown": ".md", "md": ".md",
    "html": ".html", "htm": ".html",
    "css": ".css", "scss": ".scss", "sass": ".sass", "less": ".less",
    "json": ".json",
    "yaml": ".yaml", "yml": ".yaml",
    "toml": ".toml",
    "bash": ".sh", "sh": ".sh", "zsh": ".sh", "shell": ".sh",
    "rust": ".rs", "rs": ".rs",
    "go": ".go",
    "ruby": ".rb", "rb": ".rb",
    "java": ".java",
    "kotlin": ".kt", "kt": ".kt",
    "scala": ".scala",
    "groovy": ".groovy",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp",
    "cc": ".cc", "cxx": ".cxx",
    "h": ".h", "hpp": ".hpp",
    "php": ".php",
    "perl": ".pl", "pl": ".pl",
    "lua": ".lua",
    "swift": ".swift",
    "dart": ".dart",
    "sql": ".sql",
    "graphql": ".graphql", "gql": ".graphql",
    "proto": ".proto",
    "vue": ".vue",
    "svelte": ".svelte",
    "xml": ".xml", "svg": ".svg",
    "csv": ".csv",
    "txt": ".txt", "text": ".txt", "plaintext": ".txt",
    "log": ".log",
    "diff": ".diff", "patch": ".patch",
}


# Separators between a module reference and its description, in the
# project's conventional docstring shape:
#     recover.write — Pass 9 (write files, manifest, warnings).
# The first line is `package.module — one-line description`.
#
# We try the "typographic" separators first (em-dash, en-dash) — those
# are the canonical form in this codebase's docstrings.  A plain
# hyphen-space (`" - "`) is accepted too, but only after the fancier
# separators fail, because hyphens also appear inside compound
# identifiers in prose.
_MODULE_SEPARATORS = (" — ", " – ", " - ", ":")


def _derive_filename_from_body(source_text: str,
                               lang: str = "") -> Optional[str]:
    """Derive a filename from the code body's leading docstring or
    comment, using structural analysis.

    Returns the filename (e.g. `"write.py"`) when a module reference
    is found, else None.

    For Python content (or when `lang` says Python, or when nothing
    else can be inferred): parse with `ast.parse` — this is
    structurally equivalent to what tree-sitter-python would yield.
    Extract the module docstring via `ast.get_docstring`, then read
    its first non-empty line for a `package.module — <desc>` pattern.

    For other languages: extract the leading block / line comment
    and apply the same pattern, with the extension determined by
    `lang`.
    """
    if not source_text or not source_text.strip():
        return None

    lang_lower = (lang or "").lower().strip()

    # Try Python first — `ast.parse` is cheap, stdlib-only, and
    # succeeds only on genuinely-valid Python.  If it succeeds, the
    # content IS Python regardless of what `lang` said, and the
    # structural derivation from the docstring is authoritative.
    if lang_lower in ("python", "py", "python3", ""):
        derived = _derive_filename_from_python(source_text)
        if derived is not None:
            return derived

    # Non-Python: comment-block extraction.  Falls back to searching
    # the leading comments for a module-path pattern.
    return _derive_filename_from_leading_comment(source_text, lang_lower)


def _derive_filename_from_python(source_text: str) -> Optional[str]:
    """Extract the module docstring via `ast` and derive a filename
    from a `package.module — description` first line.

    Returns None when:
      · the content doesn't parse as Python,
      · there's no module-level docstring,
      · the first non-empty line doesn't match the module-path
        pattern.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return None

    docstring = ast.get_docstring(tree)
    if not docstring:
        return None

    first_line = next(
        (ln.strip() for ln in docstring.split("\n") if ln.strip()), "")
    if not first_line:
        return None

    stem = _stem_from_module_reference(first_line)
    if stem:
        return stem + ".py"
    return None


def _stem_from_module_reference(line: str) -> Optional[str]:
    """Given a candidate first line like `recover.write — Pass 9 ...`,
    return the *stem* of the leaf module (`"write"`).

    Returns None when the line doesn't follow the
    `dotted.ident — separator — rest` shape.

    The check is structural, not regex-based:

      1. Split the line at the first occurrence of any known
         separator.
      2. Left of the separator must be a dotted identifier chain
         (each part is a valid Python identifier).
      3. Right of the separator must be non-empty (we're not
         accepting bare `foo.bar` with no description, because that
         shape is too likely to be prose).

    The last component of the dotted chain becomes the filename
    stem.  `deeppeek.recover.write — ...` → `write`.  `recover.write
    — ...` → `write`.  `foo.py — ...` → `foo` (the leaf already
    carries an extension; the stem trims it).
    """
    for sep in _MODULE_SEPARATORS:
        if sep not in line:
            continue
        left, right = line.split(sep, 1)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            continue
        parts = _split_dotted_path(left)
        if parts is None:
            continue
        leaf = parts[-1]
        # Trailing ".py" on the leaf: strip it.  `foo.bar.py` is
        # ambiguous — could be a dotted module with extension; only
        # strip when the leaf has a *known* source extension.
        if "." in leaf:
            stem, ext = leaf.rsplit(".", 1)
            if ext.lower() in _EXT_BY_LANG.values() or ext.lower() in {
                "py", "js", "ts", "md", "json",
            }:
                return stem
            # Unknown trailing extension; not a filename.
            return None
        return leaf
    return None


def _split_dotted_path(text: str) -> Optional[list]:
    """Split a dotted-identifier chain into its components.

    Returns None when any component is not a valid identifier
    (which means `text` isn't a module path — it's prose that
    happens to contain a dot).

    `recover.write` → `["recover", "write"]`
    `deeppeek.recover.write` → `["deeppeek", "recover", "write"]`
    `foo bar` → None (contains a space)
    `1.2` → None (digits aren't identifiers)
    `self.foo` → `["self", "foo"]` (accepted — treat as hint)
    """
    if not text:
        return None
    # Path separators are also permitted: `commands/hijack.py` is a
    # legitimate reference in this project's docstrings.  Normalise
    # them to dots before splitting.
    normalised = text.replace("/", ".")
    parts = normalised.split(".")
    if not parts:
        return None
    for p in parts:
        # Every component must be an identifier or a known source
        # extension.  Empty components (`foo..bar`) are invalid.
        if not p:
            return None
        if p.isidentifier():
            continue
        # Trailing extension is allowed on the last component only —
        # and it must look like a filename extension, not prose.
        if p.lower() in {
            "py", "js", "mjs", "cjs", "jsx", "ts", "tsx",
            "md", "html", "css", "json", "yaml", "yml", "toml",
            "sh", "bash", "rs", "go", "rb", "php", "java",
            "kt", "swift", "dart", "sql",
        }:
            continue
        return None
    return parts


def _derive_filename_from_leading_comment(
        source_text: str, lang: str) -> Optional[str]:
    """Extract the leading block/line comment from non-Python
    content and derive a filename from a `package.module — ...` first
    line inside it.

    Returns None if there's no leading comment or the comment
    doesn't follow the module-reference pattern.
    """
    if not source_text:
        return None

    # Leading block comment: `/* ... */` at the very start (after
    # optional whitespace).  The `**`-style doc comments (JSDoc) are
    # also matched.
    m = re.match(r'\s*/\*{1,2}(.*?)\*/', source_text, re.DOTALL)
    if m is not None:
        body = m.group(1)
    else:
        # Leading line comments: one or more `// ...` lines at the
        # start.  Stop at the first non-`//` line.
        m = re.match(r'\s*((?://[^\n]*\n?)+)', source_text)
        if m is None:
            return None
        body = m.group(1)

    # Strip per-line comment markers (`*` for block comments,
    # `//` for line comments) and keep the text.
    lines = []
    for raw in body.split("\n"):
        s = raw.strip()
        if s.startswith("*"):
            s = s.lstrip("*").strip()
        elif s.startswith("//"):
            s = s[2:].strip()
        if s:
            lines.append(s)

    first_line = next((ln for ln in lines if ln), "")
    if not first_line:
        return None

    stem = _stem_from_module_reference(first_line)
    if stem is None:
        return None

    ext = _EXT_BY_LANG.get(lang, "")
    if not ext:
        return None
    return stem + ext


def _safe_relative_path(filename: str) -> Path:
    """Sanitize a declared filename into a relative path under out_dir.

    The recover pipeline's `header_path` may include subdirectories
    (`playgrounds/inpaint/pg_base.py`); we preserve them, matching
    the `--apply-to` behaviour. `..` components are refused so a
    malicious or confused filename cannot escape the output dir.
    """
    parts = []
    for part in (filename or "").replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(
                f"filename escapes output dir: {filename!r}")
        parts.append(part)
    if not parts:
        raise ValueError(
            f"empty filename after sanitization: {filename!r}")
    return Path(*parts)


def _dedup_target(target: Path) -> Path:
    """Return a non-colliding sibling path: name (1).ext, name (2).ext."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _send_and_wait(server, event: str, ack_name: str,
                   timeout: float) -> dict:
    batch_id = uuid.uuid4().hex[:12]
    server._broadcast(event, {"batch_id": batch_id})
    try:
        ack = server.wait_for_result(timeout=timeout)
    except queue.Empty:
        raise SystemExit(
            f"error: extension did not acknowledge {ack_name} within "
            f"{timeout:.0f}s")
    if ack.get("__ack") != ack_name:
        raise SystemExit(
            f"error: unexpected ack for {ack_name}: {ack!r}")
    if ack.get("batch_id") != batch_id:
        raise SystemExit(
            f"error: batch_id mismatch for {ack_name}: "
            f"{ack.get('batch_id')!r} != {batch_id!r}")
    return ack.get("result") or {}


def register(sub: argparse._SubParsersAction) -> None:
    """Wire the `hijack` subcommand into argparse.

    Registers the parser, all flags, and `set_defaults(func=run)`
    so `cli.main`'s dispatch map can find the handler.
    """
    p = sub.add_parser(
        "hijack",
        help="Intercept DeepSeek code-block download buttons; write "
             "files to the current directory instead of ~/Downloads",
    )
    p.add_argument(
        "--dir", type=Path, default=None, metavar="PATH",
        help="directory to write intercepted files into "
             "(default: current working directory)",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="how long to wait for the extension to connect (default: 30)",
    )
    p.add_argument(
        "--preserve-path", action="store_true",
        help="keep directory paths declared in the AI's prose. "
             "A block declared as `utils/paths.py` writes to "
             "<dir>/utils/paths.py (subdirectories created as "
             "needed). Default: flatten to basename, so the same "
             "block writes to <dir>/paths.py. Flattening is safe "
             "by construction: the basename has no path components, "
             "so a malicious or confused declaration cannot escape "
             "<dir>.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    out_dir = (args.dir if args.dir is not None else Path.cwd()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    preserve_path = bool(args.preserve_path)

    try:
        server = ExportServer(port=args.port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"error: port {args.port} is already in use — is "
                  f"another `deeppeek` command running?", file=sys.stderr)
            print("  stop it first (Ctrl+C the other command), then retry.",
                  file=sys.stderr)
            return 1
        raise

    installed = False
    try:
        print(f"Waiting for extension on http://127.0.0.1:{args.port}/events "
              f"(up to {args.timeout:.0f}s)...", file=sys.stderr)
        if not server.wait_for_extension(timeout=args.timeout):
            print("error: no extension connected within timeout.",
                  file=sys.stderr)
            print("  make sure Zen is open, the extension is loaded, and a "
                  "deepseek.com tab is active.", file=sys.stderr)
            return 1

        result = _send_and_wait(
            server, "hijack-install", "hijack-install", args.timeout)
        if not result.get("success"):
            reason = result.get("reason") or "(no reason)"
            print(f"error: hijack install failed: {reason}",
                  file=sys.stderr)
            return 2
        installed = True

        mode_desc = (
            "preserve-path" if preserve_path else "flat (basename only)"
        )
        mode_desc += ", overwrites existing files"
        print(f"\nHijacker installed.", file=sys.stderr)
        print(f"Mode: {mode_desc}", file=sys.stderr)
        print(f"Downloads will be written to: {out_dir}", file=sys.stderr)
        print(f"Click any code-block 'Download' button in the DeepSeek tab.",
              file=sys.stderr)
        print(f"Press Ctrl+C to stop and uninstall.\n", file=sys.stderr)

        n_written = 0
        while True:
            ack = server.wait_for_result(timeout=None)
            if ack.get("__ack") != "download":
                continue

            filename = ack.get("filename") or "download.bin"
            b64 = ack.get("base64") or ""
            source = ack.get("source") or "?"
            lang = ack.get("lang") or ""
            try:
                data = base64.b64decode(b64)
            except Exception as e:
                print(f"  [skip] {filename}: invalid base64 ({e})",
                      file=sys.stderr)
                continue

            # MODIFIED (2026-11-05): structural filename derivation.
            #
            # When the extension's prose-based derivation failed, the
            # extension sent a timestamped placeholder and marked the
            # source as 'lang-fallback'.  Try deriving the real name
            # from the code body's own docstring / leading comment
            # (structural parse, not regex over prose).  On success,
            # use the derived name and update `source` for the log.
            notes: list = []
            orig_filename = filename
            if source == "lang-fallback":
                try:
                    body_text = data.decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                if body_text:
                    derived = _derive_filename_from_body(body_text, lang)
                    if derived:
                        filename = derived
                        source = "structural-docstring"
                        notes.append(
                            f"renamed from {orig_filename} "
                            f"via structural analysis")
                        print(
                            f"  [derived] {derived} "
                            f"(structural, lang={lang or '?'})",
                            file=sys.stderr)

            if preserve_path:
                try:
                    rel = _safe_relative_path(filename)
                except ValueError as e:
                    print(f"  [skip] {filename}: {e}",
                          file=sys.stderr)
                    continue
            else:
                # Default: flatten to basename. Safe by construction
                # (basename has no path components), so a malicious
                # or confused declaration cannot escape out_dir.
                norm = (filename or "").replace("\\", "/")
                name = Path(norm).name
                if not name or name in (".", ".."):
                    name = "download.bin"
                rel = Path(name)

            # Overwrite semantics: same path replaces whatever was
            # there. No ` (1)` suffix, so re-clicking a Download
            # button is idempotent. Matches `deeppeek recover
            # --apply-to`, which also overwrites.
            target = out_dir / rel
            existed = target.exists()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as e:
                print(f"  [error] {filename}: write failed ({e})",
                      file=sys.stderr)
                continue

            n_written += 1
            try:
                display = target.relative_to(out_dir)
            except ValueError:
                display = target
            if not preserve_path and filename and (
                    "/" in filename or "\\" in filename):
                notes.append(f"declared {filename}")
            if existed:
                notes.append("overwrote")
            note = (", " + ", ".join(notes)) if notes else ""
            print(f"  [{n_written}] {display} "
                  f"({len(data):,} bytes, source={source}{note})",
                  file=sys.stderr)

    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
        return 0
    finally:
        if installed:
            try:
                _send_and_wait(
                    server, "hijack-uninstall", "hijack-uninstall", 3.0)
            except (SystemExit, queue.Empty):
                pass
        server.stop()
