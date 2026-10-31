"""`deeppeek prompt attachments` — download files attached to messages.

Files land in the browser's configured download directory.
The CLI never writes them — the page-world fetcher triggers a
browser download via `<a download>` + Blob URL, exactly the same
mechanism as clicking a file chip in the UI.

Two primary workflows, plus supporting modes:

  --focus (default)
      Attachments currently in the composer — whether it holds a
      new draft or is editing a previous message.  Detects edit-
      mode via the textarea's React fiber: composer messageId
      -2 means new draft, +N means editing message N.

  --prompt-regex PATTERN
      Find the most recent user prompt whose content matches
      PATTERN (Python regex, case-insensitive by default) that
      had attachments, and download those.  Scans backward from
      the conversation end — first matching prompt with files
      wins, mirroring `capture --last-regex` semantics.  Use
      --all-matches to download attachments from every matching
      prompt.

Supporting modes for completeness:

  --first              attachments from the literal first user
                       message of the conversation.  Returns empty
                       when the seed prompt had no files; that is
                       not an error.  Used by `deeppeek prompt
                       resume`.
  --message-id ID      attachments from one sent message
  --last N             attachments from the last N sent messages
                       that had files
  --all                every attachment in the conversation
  --filename PATTERN   attachments whose filename contains
                       PATTERN (case-insensitive substring match
                       by default; --case-sensitive overrides).
                       Scans every message in the conversation,
                       or active-path only without
                       --include-forked.  Useful for "give me
                       the file X" when you don't remember which
                       message it was attached to.

MECHANISM
=========
Every attachment carries a `signedPath` (draft) or `signed_path`
(sent) — a relative URL of the form:

    /file?file_id=<uuid>&state=<signed-token>

The real URL is:

    https://files.deepseeksvc.com/api + signedPath + "&ty=r"

Verified by console POCs:

  · `state` is the sole authenticator; `file_id` is decorative
  · `ty=r` is required by CORS
  · `credentials: 'omit'` is correct
  · `state` tokens do not expire within the conversation's
    lifetime
  · every fetched byte count is checked against metadata
    `file_size`; mismatch is reported as an error and no
    download is triggered

WHAT CAN'T BE RECOVERED
=======================
Drafts that were cleared before sending — the bytes never
reached DeepSeek's servers.

DOWNLOAD BEHAVIOUR
==================
Downloads are triggered one at a time with a short gap between
them.  Firefox's default behaviour is to save to its configured
download directory without prompting.  If "Always ask you where
to save files" is enabled in Firefox settings, you will get one
"Save As" dialog per file — for `--all` on a long conversation
that is a lot of dialogs.  Use `--list-only` to preview the
count before committing.

--fetch-to DIR (base64 mode)
============================
Instead of triggering browser downloads, fetch each attachment's
bytes as base64 inside the extension and write them to DIR on
the local filesystem.  This is what `deeppeek prompt resume`
uses: it needs the seed prompt's files on disk so it can pipe
them into a fresh tab along with the conversation markdown.
The extension's `_fetchAndEncode` code path (already exercised
by `prompt retry`) is reused, so there is exactly one base64
fetch implementation in the page world.

Mutually exclusive with --list-only — listing does not fetch,
fetching needs somewhere to write.

Exit codes:
    0  all requested downloads (or base64 writes) succeeded
    1  extension connection failure
    2  at least one file failed (fetch error, size mismatch, or
       download/write failure)
    3  no attachments matched the selection
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from typing import Optional

from ..commands._composer import broadcast_and_wait, composer_session


DEFAULT_TIMEOUT = 600.0  # generous: N files x ~400ms fetch + ~200ms gap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _human_size(n: Optional[int]) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def _resolve_mode(args: argparse.Namespace) -> str:
    if args.first:
        return "first"
    if args.prompt_regex is not None:
        return "prompt-regex"
    if args.filename is not None:
        return "filename"
    if args.message_id is not None:
        return "message-id"
    if args.last is not None:
        return "last-N"
    if args.all:
        return "all"
    return "focus"


def _build_opts(args: argparse.Namespace, mode: str) -> dict:
    opts: dict = {
        "mode": mode,
        "list_only": bool(args.list_only),
        "include_forked": bool(args.include_forked),
        "return_base64": bool(args.fetch_to),
    }
    if mode == "prompt-regex":
        opts["pattern"] = args.prompt_regex
        opts["flags"] = "" if args.case_sensitive else "i"
        opts["all_matches"] = bool(args.all_matches)
    elif mode == "filename":
        opts["pattern"] = args.filename
        opts["case_sensitive"] = bool(args.case_sensitive)
    elif mode == "message-id":
        opts["message_id"] = int(args.message_id)
    elif mode == "last-N":
        opts["last"] = int(args.last)
    return opts


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    mode = _resolve_mode(args)

    if args.all_matches and mode != "prompt-regex":
        print("error: --all-matches is only meaningful with "
              "--prompt-regex", file=sys.stderr)
        return 3
    if args.case_sensitive and mode not in ("prompt-regex", "filename"):
        print("error: --case-sensitive is only meaningful with "
              "--prompt-regex or --filename", file=sys.stderr)
        return 3
    if args.fetch_to and args.list_only:
        print("error: --fetch-to and --list-only are mutually "
              "exclusive (list does not fetch, fetch needs "
              "somewhere to write)", file=sys.stderr)
        return 3

    opts = _build_opts(args, mode)

    print(f"[connect] waiting for extension on port {args.port}...",
          file=sys.stderr)

    with composer_session(port=args.port, timeout=args.timeout) as server:
        if server is None:
            return 1

        print(f"[extract] mode={mode}"
              + (f"  pattern={args.prompt_regex!r}" if mode == "prompt-regex" else "")
              + (f"  filename={args.filename!r}" if mode == "filename" else "")
              + (f"  message_id={args.message_id}" if mode == "message-id" else "")
              + (f"  last={args.last}" if mode == "last-N" else "")
              + ("  (list only)" if args.list_only else "")
              + (f"  (fetch-to {args.fetch_to})" if args.fetch_to else ""),
              file=sys.stderr)

        rc, result = broadcast_and_wait(
            server, "extract-attachments", {"opts": opts},
            timeout=args.timeout, ack_name="extract-attachments",
        )
        if rc != 0:
            reason = (result.get("reason") or result.get("error")
                      or "(no reason)")
            print(f"error: extraction failed: {reason}",
                  file=sys.stderr)
            return rc

    files = result.get("files") or []
    title = result.get("title") or "(untitled)"
    composer_id = result.get("composer_message_id")

    if mode == "focus" and composer_id is not None:
        if composer_id == -2:
            print(f"[focus] new draft (composer_message_id=-2)",
                  file=sys.stderr)
        else:
            print(f"[focus] editing message {composer_id}",
                  file=sys.stderr)

    matched_ids = result.get("matched_message_ids") or []
    if matched_ids and mode != "focus":
        preview = ", ".join(str(x) for x in matched_ids[:5])
        tail = f" +{len(matched_ids)-5} more" if len(matched_ids) > 5 else ""
        print(f"[matched] {len(matched_ids)} message(s): {preview}{tail}",
              file=sys.stderr)

    if not files:
        reason = result.get("reason") or "no attachments matched the selection"
        print(f"\n{reason}", file=sys.stderr)
        # --first returning empty is a valid, non-error outcome:
        # the seed prompt simply had no attachments.  Return 0 so
        # callers (resume) can distinguish "no files" from "error".
        if mode == "first":
            print(f"[first] seed prompt had no attachments — "
                  f"nothing to fetch", file=sys.stderr)
            return 0
        return 3

    print(f"\n[found] {len(files)} attachment(s) in {title!r}",
          file=sys.stderr)

    # ---- fetch-to: write base64 payloads to disk ---------------------
    if args.fetch_to is not None:
        out_dir = Path(args.fetch_to).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        failed = 0
        for f in files:
            name = f.get("file_name") or "attachment.bin"
            # Flatten: strip any path components the extension or
            # the model may have embedded in the name.  Basename
            # cannot escape out_dir.
            name = Path(name).name or "attachment.bin"
            if f.get("error"):
                print(f"  [fail] {name}: {f['error']}",
                      file=sys.stderr)
                failed += 1
                continue
            b64 = f.get("base64")
            if not b64:
                print(f"  [fail] {name}: no base64 in ack",
                      file=sys.stderr)
                failed += 1
                continue
            # Collision-safe naming: name, name (1), name (2), ...
            # Mirrors how `deeppeek hijack --preserve-path` off-mode
            # would behave if the same file were downloaded twice.
            target = out_dir / name
            n = 1
            while target.exists():
                target = out_dir / (
                    f"{Path(name).stem} ({n}){Path(name).suffix}")
                n += 1
            try:
                data = base64.b64decode(b64)
            except Exception as e:
                print(f"  [fail] {name}: b64decode threw ({e})",
                      file=sys.stderr)
                failed += 1
                continue
            try:
                target.write_bytes(data)
            except OSError as e:
                print(f"  [fail] {name}: write failed ({e})",
                      file=sys.stderr)
                failed += 1
                continue
            written += 1
            print(f"  [ok]   {target.name}  ({len(data):,} bytes)",
                  file=sys.stderr)

        print(f"\n[fetch-to] {written}/{len(files)} file(s) → "
              f"{out_dir}", file=sys.stderr)
        if failed:
            return 2
        return 0

    # ---- default: browser download / list-only reporting -------------
    ok_count = 0
    err_count = 0
    total_bytes = 0
    for f in files:
        name = f.get("file_name", "(unnamed)")
        size = int(f.get("file_size") or 0)
        mid = f.get("message_id")
        err = f.get("error")
        downloaded = f.get("downloaded")

        if err:
            err_count += 1
            print(f"  msg={mid}  {name}  ({_human_size(size)})  "
                  f"ERR: {err}", file=sys.stderr)
            continue

        if args.list_only:
            ok_count += 1
            print(f"  msg={mid}  {name}  ({_human_size(size)})",
                  file=sys.stderr)
            continue

        if downloaded:
            ok_count += 1
            total_bytes += size
            print(f"  msg={mid}  {name}  ({_human_size(size)})  "
                  f"downloaded", file=sys.stderr)
        else:
            err_count += 1
            print(f"  msg={mid}  {name}  ({_human_size(size)})  "
                  f"NOT downloaded", file=sys.stderr)

    if args.list_only:
        print(f"\n[list-only] {len(files)} file(s), "
              f"{_human_size(total_bytes) if total_bytes else 'unknown total'}"
              f".  No downloads triggered.", file=sys.stderr)
        return 0 if ok_count else 2

    print(f"\n[done] triggered {ok_count}/{len(files)} download(s), "
          f"{_human_size(total_bytes)} total, "
          f"to your browser's download directory", file=sys.stderr)

    if err_count:
        print(f"  {err_count} file(s) failed — see above.",
              file=sys.stderr)
        return 2

    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "attachments",
        help="Download files attached to the current conversation "
             "to your browser's download directory "
             "(composer draft/edit, first prompt, prompt regex, "
             "message id, last N, filename, or all)",
    )

    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument(
        "--focus", action="store_true",
        help="(default) download attachments from the current "
             "composer — whether it holds a new draft or is "
             "editing a previous message.  Detects edit-mode via "
             "textarea messageId: -2 = new draft, +N = editing "
             "message N.",
    )
    g.add_argument(
        "--first", action="store_true",
        help="attachments from the literal first user message of "
             "the conversation.  Returns empty when the seed prompt "
             "had no files — that is not an error.  Used by "
             "`deeppeek prompt resume` to rehydrate the seed "
             "context.",
    )
    g.add_argument(
        "--prompt-regex", metavar="PATTERN",
        help="find the most recent user prompt whose content "
             "matches PATTERN (Python regex) AND that had "
             "attachments; download those.  Backward scan from "
             "the conversation end — most recent matching prompt "
             "wins.  Case-insensitive by default "
             "(--case-sensitive to override).  Use --all-matches "
             "to download from every matching prompt.",
    )
    g.add_argument(
        "--message-id", type=int, metavar="ID",
        help="download attachments from the sent message with "
             "this ID.",
    )
    g.add_argument(
        "--last", type=int, metavar="N",
        help="download attachments from the last N sent messages "
             "that had files.",
    )
    g.add_argument(
        "--all", action="store_true",
        help="download attachments from every message in the "
             "conversation.",
    )
    g.add_argument(
        "--filename", metavar="PATTERN",
        help="download attachments whose filename contains PATTERN "
             "(case-insensitive substring match; --case-sensitive "
             "to override).  Scans every message in the "
             "conversation, or active-path only without "
             "--include-forked.  Use --list-only to preview "
             "matches before downloading.",
    )

    p.add_argument(
        "--all-matches", action="store_true",
        help="with --prompt-regex: download from all matching "
             "prompts (default: only the most recent match).",
    )
    p.add_argument(
        "--case-sensitive", action="store_true",
        help="with --prompt-regex or --filename: match "
             "case-sensitively (default: case-insensitive).",
    )
    p.add_argument(
        "--include-forked", action="store_true",
        help="include attachments from forked-away branches "
             "(edited or regenerated messages hidden from the UI).  "
             "Only meaningful with --prompt-regex, --filename, "
             "--first, --message-id, --last, or --all.",
    )
    p.add_argument(
        "--list-only", action="store_true",
        help="enumerate matching files without triggering any "
             "downloads.  Fast, useful for checking counts before "
             "committing.  Mutually exclusive with --fetch-to.",
    )
    p.add_argument(
        "--fetch-to", metavar="DIR", default=None,
        help="fetch files as base64 and write them to DIR instead "
             "of triggering browser downloads.  Implies "
             "return_base64 in the extension, which reuses the "
             "same _fetchAndEncode code path as `prompt retry`.  "
             "Mutually exclusive with --list-only.  Used by "
             "`deeppeek prompt resume`.",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"connection + per-file timeout in seconds "
             f"(default: {DEFAULT_TIMEOUT:.0f})",
    )

    p.set_defaults(func=run)
