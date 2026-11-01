"""`deeppeek prompt resume` — capture → seed → open → continue.

Stitches together three primitives:

    deeppeek conversation -o /tmp/resume_<title>_<ts>.md
    deeppeek prompt attachments --first --fetch-to <tmpdir>
    (in-process) open-tab → wait → attach(conv + seed files) → type

The intent: restart from the seed context of a conversation that
was interrupted mid-thought, in a fresh tab, with the full old
conversation attached so the AI can read where it was cut off.

Two decisions the earlier draft got wrong, corrected here:

1. **Filename (`someUsefulNameForID.md`).**
   The captured markdown is named `/tmp/resume_<slug>_<ts>.md`
   where `<slug>` is the sanitized conversation title (the
   `# [Title](url)` header that `capture` writes), truncated to
   40 chars, fallback to the first 40 chars of the first user
   prompt, fallback to `conversation`.  `[a-zA-Z0-9_]` only.
   Example: `/tmp/resume_KritaInpaint_20261030_143022.md`.  The
   timestamp prevents collisions across multiple `resume` runs;
   the title makes the file self-identifying in `ls /tmp/`.
   Capture is written to `/tmp/_resume_capture_<ts>.md` first
   (we don't know the title until we read the file), then
   renamed in place after derivation.  `--name` overrides.

2. **The `{guidance}` slot in the resume text.**
   This is a real substitution, not a literal.  The code
   extracts the tail of the last assistant message — the last
   paragraph of its main content (after any `### Thought
   Process` block), falling back through: tail of the thought
   process, the bolded hint line, the last user prompt.  The
   paragraph is flattened to a single line and truncated to the
   last `--guidance-words N` words (default 40), with a
   500-char safety cap for pathological single-token cases.
   Embedded in the type text:

       you were thinking about it, but was interrupted, keep
       going where you left it: "<extracted guidance>"; dont
       give me any code yet, simply design it

   So the AI sees the actual cut-off point, not a template.
   `--text` overrides verbatim (no formatting applied),
   `--guidance TEXT` overrides just the substituted string, and
   `--guidance-words N` overrides just the word limit.

CONTAINER TABS
==============
`--container NAME` opens the fresh tab inside the Firefox/Zen
Container Tab whose display name matches NAME (case-insensitive;
exact match preferred).  `--container-id COOKIE_STORE_ID` takes a
raw cookieStoreId (e.g. `firefox-container-3`) and skips the
name lookup.  Both are threaded into the same `open-tab` event
that `deeppeek prompt new` uses, so the extension resolves the
name the same way — via `browser.contextualIdentities.query`,
which requires the `contextualIdentities` permission.

`resume` always needs the extension (it can't attach files or
type without it), so unlike `prompt new` there is no
direct-launch fallback that could silently open in the default
container.  If the extension is unreachable, the run fails — the
container flag never has to be second-guessed.  If the container
name doesn't resolve, the extension acks with `success: false`,
the error names the reason, and resume exits with code 2 (same
path as any other open-tab failure).

PLACEHOLDER DEFENCE
===================
A recurring failure mode: this file's own docstring (or a test
fixture, or the README) contains an illustrative placeholder like
`<ALL-CAPS TOKEN>`, and the conversation being resumed happens to
quote it back.  `_clean_guidance()` strips `<UPPER CASE>` angle-
bracket tokens from the extracted tail before substitution, so a
placeholder that leaked through capture never reaches the typed
prompt.  The regex is deliberately narrow — it matches
`<[A-Z][A-Z0-9 _-]{2,}>`, so real code like `<input>` or `<div>`
(lowercase) is untouched.

MULTI-GENERATION RESUME
=======================
A second or later resume finds the previous resume file as one of
the seed attachments (the previous run attached `resume_A.md` plus
A's seed files; the current conversation's first prompt carries
them, and `--first` fetches them).  `resume` now detects these:

  · merge content      — `MERGED FILE 1/N:` in the first 2KB
  · plain capture      — filename starts with `resume_` or
                         `merged_resume_`

and merges them with the current plain capture into a single file:

  /tmp/merged_<conv_path.stem>_<ts>.md

which is attached in place of the individual prior resumes.  If a
prior attachment is itself a merged file (third-or-later resume),
its conversations are extracted first, recursively, so the merged
output is always flat (A + B + C, not (A + B) + C).  Seed files
that are not resumes stay in the attachment list unchanged — they
are the seed context, not history, and the AI still needs them.

The merge falls back to the pre-merge behaviour (attach everything
separately) if `scripts/merge_md.py` is missing, times out, or
exits non-zero; the failure reason is printed to stderr.

`--no-merge` disables the merge step entirely, for the rare case
where a seed attachment happens to share the resume naming
convention and you don't want it treated as history.

Note on `--name`: content-detection handles merged files
regardless of filename, so a `--name custom.md` output that later
gets merged is picked up correctly.  But a *plain* capture named
`custom.md` (never merged) is only detected by the filename rule —
so if you use `--name`, prefer a `resume_*` prefix to keep the
chain detectable.  The default (title-derived) naming already
does this.

The whole emit stage (open-tab, attach, type) runs in ONE
composer_session so port 8765 is bound once.  attach is retried
up to three times, because the new tab's content script has to
load before browser.tabs.sendMessage succeeds.  The initial
`--tab-wait` (default 1.0 s) only has to cover the warm-cache
case — the retry loop absorbs cold-cache loads.
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ..commands._composer import (
    broadcast_and_wait, broadcast_many, composer_session,
)


_PROJECT_ROOT    = Path(__file__).resolve().parents[3]
_MERGE_MD_SCRIPT = _PROJECT_ROOT / "scripts" / "merge_md.py"

# The exact wording the user asked for, with a `{guidance}` slot
# where the extracted tail goes.  No str.format on the whole
# thing — we substitute one piece explicitly so user-supplied
# text can never inject format directives.
DEFAULT_RESUME_TEMPLATE = (
    'you were thinking about it, but was interrupted, keep going '
    'where you left it: "{guidance}"; '
    'dont give me any code yet, simply design it'
)
DEFAULT_URL = "https://chat.deepseek.com/"
DEFAULT_GUIDANCE_WORDS = 40
_GUIDANCE_MAX_CHARS = 500  # safety net for pathological single tokens

# `<ALL-CAPS TOKEN>` — placeholder illustration, not real prose.
# Matches `<NOTE>`, `<THE ACTUAL TAIL>`, `<FILL_ME_IN>`, etc., but
# not `<input>`, `<div>`, or `<last 40 words>` (lowercase).
_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9 _-]{2,}>")

# merge_md.py's per-file boundary is a run of `+` whose width is
# `max(70, max_plus_run_in_content + 1)`.  The `\1` backreference
# ensures opener and closer use the same width — no false positives
# from `+` runs inside the fenced content.
_MERGE_HEADER_RE = re.compile(
    r"^(\+{70,})\n"
    r"MERGED FILE (\d+)/(\d+): (.+?)\n"
    r"\1\n",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Subprocess plumbing (same shape as every other prompt module)
# ---------------------------------------------------------------------------

def _deeppeek_cmd() -> list[str]:
    if shutil.which("deeppeek"):
        return ["deeppeek"]
    return [sys.executable, "-m", "deeppeek"]


def _run_cmd(cmd: list[str], *, timeout=None,
             allow_failure=False) -> tuple[int, str, str]:
    print(f"    $ {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        msg = f"`{cmd[0]}` not on PATH"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        if allow_failure:
            return -1, "", msg
        raise SystemExit(f"error: {msg}")
    if proc.returncode != 0:
        tag = "(allowed)" if allow_failure else ""
        print(f"    exit {proc.returncode} {tag}".rstrip(),
              file=sys.stderr)
        for line in (proc.stderr or "").splitlines()[:5]:
            print(f"      {line}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# Filename derivation
# ---------------------------------------------------------------------------

def _sanitize(s: str, max_len: int = 40) -> str:
    """Reduce `s` to [a-zA-Z0-9_]+, stripped, truncated to max_len.

    Returns "conversation" when nothing survives (e.g. s was
    entirely punctuation).
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")
    return s[:max_len] or "conversation"


def _derive_filename_slug(text: str) -> str:
    """Pick a useful filename component from the conversation.

    Preference: the title header (`# [Title](url)` or `# Title`),
    which `capture` always writes first.  Fallback: the first 40
    chars of the first user prompt.  Fallback: "conversation".

    Only scans the first ~40 lines for the header — a `# ` line
    deep in a message body should not be mistaken for the title.
    """
    for line in text.split("\n", 40):
        if line.startswith("# ["):
            m = re.match(r"# \[(.+?)\]\(", line)
            if m:
                return _sanitize(m.group(1))
            # malformed markdown-link header — fall through and
            # try the plain `# ` branch below.
            break
        if line.startswith("# "):
            return _sanitize(line[2:].strip())
        # A non-blank, non-header line means we've left the header
        # region without finding a title.
        if line.strip() and not line.startswith("```"):
            break
    m = re.search(r"## User\n\n(.*?)(?:\n\n---|\Z)",
                  text, re.DOTALL)
    if m:
        return _sanitize(m.group(1).strip()[:40])
    return "conversation"


# ---------------------------------------------------------------------------
# Last-relevant-string extraction
# ---------------------------------------------------------------------------

def _clean_guidance(s: str) -> str:
    """Strip `<ALL-CAPS TOKEN>` placeholders from extracted prose.

    Real prose and code rarely use angle brackets with
    all-caps-and-spaces; if one appears in the extracted tail,
    it's almost certainly a template illustration that leaked
    back through conversation capture (e.g. this file's own
    docstring, a test fixture, the README).

    Lowercase tags (`<input>`, `<div>`) and sentence-form
    parentheticals (`<last 40 words>`) are not matched — the
    regex requires the first character after `<` to be `[A-Z]`.
    """
    return _PLACEHOLDER_RE.sub("", s).strip()


def _tail_words(s: str, max_words: int,
                max_chars: int = _GUIDANCE_MAX_CHARS) -> str:
    """Flatten whitespace and keep only the last `max_words` words.

    Flattening matters because the result is embedded inside a
    quoted sentence in the composer text — a newline in the quote
    would split the sentence and look broken in the UI.  The
    final char cap is a safety net: a single "word" can be a
    2000-char URL or base64 blob, and 40 of those would dwarf the
    rest of the prompt.
    """
    s = " ".join(s.split())
    if not s:
        return ""
    words = s.split(" ")
    if len(words) > max_words:
        s = " ".join(words[-max_words:])
    if len(s) > max_chars:
        s = s[-max_chars:].lstrip()
    return s


def _extract_last_relevant_string(
    conv_path: Path,
    max_words: int = DEFAULT_GUIDANCE_WORDS,
    max_chars: int = _GUIDANCE_MAX_CHARS,
) -> str:
    """Return the tail of the last assistant message, for use as
    the `{guidance}` slot in the resume text.

    Priority:
      1. Last paragraph of the main content (after any
         `### Thought Process` block).
      2. Tail of the thought process, when the message was cut
         off mid-thought and there is no main content yet.
      3. The bolded `**hint**` line (rare; when the assistant
         produced only a hint).
      4. The last user prompt (when the conversation ends on a
         user turn — i.e. the assistant never replied).

    The chosen chunk is flattened, stripped of `<UPPER CASE>`
    placeholders, and trimmed to the last `max_words` words.
    Returns "" when none of the above produce text.
    """
    text = conv_path.read_text(encoding="utf-8")
    # The capture formatter separates messages with `\n\n---\n\n`.
    blocks = re.split(r"\n\n---\n\n", text)

    def _trim(s: str) -> str:
        return _clean_guidance(_tail_words(s, max_words, max_chars))

    # 1/2/3 — last assistant message
    for block in reversed(blocks):
        m = re.match(r"## Assistant\n\n(.*)", block, re.DOTALL)
        if not m:
            continue
        body = m.group(1).strip()
        if not body:
            continue

        # Separate thought process from main content.
        thought_text = ""
        main = body
        if body.startswith("### Thought Process"):
            lines = body.split("\n")
            i = 1
            while i < len(lines) and not lines[i].startswith("> "):
                i += 1
            quote_start = i
            while i < len(lines) and lines[i].startswith("> "):
                i += 1
            thought_text = "\n".join(
                ln[2:] for ln in lines[quote_start:i])
            main = "\n".join(lines[i:]).strip()

        # Strip a leading `**hint**` paragraph from main.
        hint_text = ""
        m_hint = re.match(r"\*\*([^\n]+?)\*\*\n*", main)
        if m_hint:
            hint_text = m_hint.group(1).strip()
            main = main[m_hint.end():].strip()

        if main:
            paras = [p.strip() for p in main.split("\n\n")
                     if p.strip()]
            if paras:
                return _trim(paras[-1])
        if thought_text.strip():
            paras = [p.strip() for p in thought_text.split("\n\n")
                     if p.strip()]
            if paras:
                return _trim(paras[-1])
        if hint_text:
            return _trim(hint_text)

    # 4 — last user prompt
    for block in reversed(blocks):
        m = re.match(r"## User\n\n(.*?)\s*$", block, re.DOTALL)
        if m:
            body = m.group(1).strip()
            if body:
                paras = [p.strip() for p in body.split("\n\n")
                         if p.strip()]
                if paras:
                    return _trim(paras[-1])
    return ""


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _is_merge_content(text: str) -> bool:
    """True iff `text` looks like merge_md.py output.

    The `MERGED FILE 1/` marker is unique to merge_md.py; it never
    appears inside a plain conversation.md (whose headers are `## User`
    / `## Assistant`).  Checking only the head is enough and cheap.
    """
    return "MERGED FILE 1/" in text[:2048]


def _extract_from_merge(
    text: str, depth: int = 0,
) -> list[tuple[str, str]]:
    """Return [(filename, content), ...] flattened from a merge_md.py file.

    Recurses when a chunk's inner content is itself a merge, so a
    merge-of-merges yields the original leaf conversations in order,
    not a nested structure.  `depth` guards against a self-referential
    file.
    """
    if depth > 20:
        print("warning: merge nesting too deep; stopping recursion",
              file=sys.stderr)
        return [("unknown.md", text)]

    headers = list(_MERGE_HEADER_RE.finditer(text))
    if not headers:
        return [("unknown.md", text)]

    result: list[tuple[str, str]] = []
    for i, m in enumerate(headers):
        name = m.group(4)
        content_start = m.end()
        content_end = (
            headers[i + 1].start()
            if i + 1 < len(headers)
            else len(text)
        )
        chunk = text[content_start:content_end]

        # Chunk shape (from merge_md._emit_block):
        #   ```<lang?>\n<content>\n```\n\n[optional group header]
        opener = re.match(r"(`{3,})[^\n]*\n", chunk)
        if not opener:
            inner = chunk.rstrip("\n")
        else:
            n = len(opener.group(1))
            closer_pat = "\n" + "`" * n + "\n"
            last = chunk.rfind(closer_pat)
            if last < 0:
                # Defensive: closer without trailing newline.
                fallback = "\n" + "`" * n
                last = chunk.rfind(fallback)
                inner = (
                    chunk[opener.end():].rstrip("\n")
                    if last < 0
                    else chunk[opener.end():last]
                )
            else:
                inner = chunk[opener.end():last]

        if _is_merge_content(inner):
            result.extend(_extract_from_merge(inner, depth + 1))
        else:
            result.append((name, inner))

    return result


def _is_resume_or_merge_attachment(path: Path) -> bool:
    """True iff `path` should be treated as a prior resume.

    Merge-marker detection runs first (exact, cannot false-positive
    on a normal conversation).  Filename fallback catches plain
    captures — `resume_*.md` from `_stage_capture`, `merged_resume_*`
    from this function's output on a later resume.
    """
    if not path.is_file():
        return False
    try:
        head = path.read_text(encoding="utf-8")[:2048]
    except (OSError, UnicodeDecodeError):
        return False
    if _is_merge_content(head):
        return True
    name = path.name
    return (name.startswith("resume_")
            or name.startswith("merged_resume_"))


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _stage_capture(args: argparse.Namespace) -> Path:
    """Capture to `/tmp/_resume_capture_<ts>.md`, derive a title
    slug from the content, rename to `/tmp/resume_<slug>_<ts>.md`.

    Returns the final path.  Renaming in place is safe — same
    filesystem, POSIX atomic.
    """
    ts = _timestamp()
    tmp = Path(f"/tmp/_resume_capture_{ts}.md")
    print(f"[capture] conversation → {tmp}", file=sys.stderr)
    cmd = _deeppeek_cmd() + ["conversation", "-o", str(tmp)]
    rc, _, _ = _run_cmd(cmd, timeout=args.capture_timeout,
                        allow_failure=False)
    if rc != 0:
        raise SystemExit(f"error: conversation capture exited {rc}")
    if not tmp.is_file():
        raise SystemExit(
            f"error: conversation not written to {tmp}")

    try:
        text = tmp.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"error: cannot read {tmp}: {e}")

    slug = _derive_filename_slug(text)
    final = Path(f"/tmp/resume_{slug}_{ts}.md")
    if final != tmp:
        try:
            tmp.rename(final)
        except OSError as e:
            # Rename failed — fall back to the temp path.  Don't
            # abort; the workflow still works with the ugly name.
            print(f"  warning: rename to {final} failed ({e}); "
                  f"using {tmp}", file=sys.stderr)
            return tmp
    print(f"[capture] → {final}", file=sys.stderr)
    return final


def _stage_get_attachments(attach_dir: Path,
                           args: argparse.Namespace) -> list[Path]:
    """Fetch the first prompt's attachments as base64, write to
    attach_dir.  Returns the written paths.

    Exit 2 from attachments is treated as "some files failed" and
    the successful subset is returned.  Exit 0 with an empty
    directory means the seed prompt had no attachments — that is
    a valid outcome for `resume`; the conversation itself is
    still attached.
    """
    print(f"[attachments] first prompt's files → {attach_dir}",
          file=sys.stderr)
    cmd = _deeppeek_cmd() + [
        "prompt", "attachments",
        "--first",
        "--fetch-to", str(attach_dir),
    ]
    rc, _, _ = _run_cmd(cmd, timeout=args.attach_timeout,
                        allow_failure=True)
    files = sorted(p for p in attach_dir.iterdir() if p.is_file())
    if rc not in (0, 2):
        # 1 = extension connection failure, 3 = no match (should
        # not happen with --first, which returns 0 for empty),
        # anything else = unexpected.  Bail loud: the user needs
        # to know that the seed files were not retrieved.
        print(f"error: attachments step failed (exit {rc}); "
              f"resume will proceed without seed files",
              file=sys.stderr)
        return files
    print(f"[attachments] {len(files)} file(s) retrieved",
          file=sys.stderr)
    for f in files:
        print(f"  {f.name}  ({f.stat().st_size:,} bytes)",
              file=sys.stderr)
    return files


def _stage_merge_history(
    resume_files: list[Path],
    current_conversation: Path,
    args: argparse.Namespace,
) -> Path | None:
    """Merge prior resumes with the current conversation into one file.

    Returns the merged path, or None when merge is not possible
    (merge_md.py missing / subprocess fails / times out / produces
    nothing).  None means: fall back to attaching everything
    separately — the caller keeps the pre-merge behaviour, and the
    user sees a warning naming the reason.

    Chronological order: each prior resume contributes the
    conversation(s) it carries (in file order), and the current
    capture comes last, so a reader of the merged file sees
    A → B → C.
    """
    if not _MERGE_MD_SCRIPT.is_file():
        print(f"warning: merge_md.py not found at {_MERGE_MD_SCRIPT}; "
              f"attaching resumes separately (not flat)",
              file=sys.stderr)
        return None

    conversation_parts: list[tuple[str, str]] = []
    for r in resume_files:
        try:
            text = r.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"warning: cannot read {r}: {e}; skipping",
                  file=sys.stderr)
            continue
        if _is_merge_content(text):
            parts = _extract_from_merge(text)
            if not parts:
                print(f"warning: no conversations extracted from "
                      f"{r}; treating content as a single file",
                      file=sys.stderr)
                parts = [(r.name, text)]
            conversation_parts.extend(parts)
        else:
            conversation_parts.append((r.name, text))

    if not conversation_parts:
        print("warning: no prior conversations to merge; "
              "attaching current only", file=sys.stderr)
        return None

    try:
        current_text = current_conversation.read_text(encoding="utf-8")
    except OSError as e:
        print(f"warning: cannot read current conversation "
              f"{current_conversation}: {e}; skipping merge",
              file=sys.stderr)
        return None
    conversation_parts.append(
        (current_conversation.name, current_text))

    ts   = _timestamp()
    stem = current_conversation.stem or "conversation"
    merged_path = current_conversation.parent / f"merged_{stem}_{ts}.md"

    parts_dir = Path(tempfile.mkdtemp(prefix="deeppeek_resume_parts_"))
    try:
        part_paths: list[Path] = []
        for i, (name, body) in enumerate(conversation_parts):
            safe = Path(name).name or "conversation.md"
            p = parts_dir / f"{i:03d}_{safe}"
            p.write_text(body, encoding="utf-8")
            part_paths.append(p)

        cmd = [
            sys.executable, str(_MERGE_MD_SCRIPT),
            "-o", str(merged_path),
            "-t", "Resume history (all conversations)",
            *[str(p) for p in part_paths],
        ]
        print(f"[merge] {len(part_paths)} conversation(s) → "
              f"{merged_path}", file=sys.stderr)
        for p, (name, _body) in zip(part_paths, conversation_parts):
            print(f"  {name}", file=sys.stderr)

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=args.merge_timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"warning: merge_md.py timed out after "
                  f"{args.merge_timeout}s; attaching resumes separately",
                  file=sys.stderr)
            return None

        if proc.returncode != 0:
            print(f"warning: merge_md.py exited {proc.returncode}; "
                  f"attaching resumes separately", file=sys.stderr)
            for line in (proc.stderr or "").splitlines()[:5]:
                print(f"  {line}", file=sys.stderr)
            return None
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)

    if not merged_path.is_file():
        print(f"warning: merge_md.py did not produce {merged_path}; "
              f"attaching resumes separately", file=sys.stderr)
        return None

    print(f"[merge] wrote {merged_path} "
          f"({merged_path.stat().st_size:,} bytes)", file=sys.stderr)
    return merged_path


def _stage_emit(files: list[Path], text: str,
                args: argparse.Namespace) -> int:
    timeout = max(args.pipe_timeout, args.type_timeout)
    with composer_session(port=args.port, timeout=timeout) as server:
        if server is None:
            return 1

        # ---- open new tab ----
        #
        # Container args are threaded into the same `open-tab` payload
        # that `deeppeek prompt new` uses, so the extension resolves
        # the name via contextualIdentities.query and passes the
        # resulting cookieStoreId to browser.tabs.create.
        open_tab_payload: dict = {"url": args.url}
        if getattr(args, "container", None) is not None:
            open_tab_payload["container"] = args.container
        if getattr(args, "container_id", None) is not None:
            open_tab_payload["container_id"] = args.container_id

        target_desc = args.url
        if getattr(args, "container", None):
            target_desc += f"  (container: {args.container})"
        elif getattr(args, "container_id", None):
            target_desc += f"  (container-id: {args.container_id})"
        print(f"[new-tab] opening {target_desc}", file=sys.stderr)

        rc, result = broadcast_and_wait(
            server, "open-tab", open_tab_payload,
            timeout=args.tab_timeout, ack_name="open-tab",
        )
        if rc != 0:
            reason = result.get("reason") or "(no reason)"
            print(f"error: open-tab failed: {reason}",
                  file=sys.stderr)
            return rc
        cs_id = result.get("cookie_store_id")
        detail = f"tab {result.get('tab_id')}"
        if cs_id:
            detail += f", container={cs_id}"
        print(f"  OK - {detail}", file=sys.stderr)

        # ---- wait for content script ----
        print(f"[wait] {args.tab_wait:.1f}s for tab load...",
              file=sys.stderr)
        time.sleep(args.tab_wait)

        # ---- build payload ----
        payload_files = []
        for f in files:
            if not f.is_file():
                print(f"  skipped (not a regular file): {f}",
                      file=sys.stderr)
                continue
            data = f.read_bytes()
            mime = (mimetypes.guess_type(f.name)[0]
                    or "application/octet-stream")
            payload_files.append({
                "name": f.name,
                "mime": mime,
                "base64": base64.b64encode(data).decode("ascii"),
            })

        # ---- attach with retry ----
        if payload_files:
            attach_spec = {
                "tag": "pipe",
                "event": "attach",
                "payload": {
                    "files": payload_files,
                    "opts": {"delay_ms": 0},
                },
                "ack_name": "attach",
            }
            attach_ok = False
            for attempt in range(3):
                if attempt > 0:
                    print(f"  attach retry {attempt}/3...",
                          file=sys.stderr)
                    time.sleep(2)
                res = broadcast_many(
                    server, [attach_spec], timeout=args.pipe_timeout,
                )
                if res["pipe"]["exit_code"] == 0:
                    attach_ok = True
                    print(f"  OK - pipe "
                          f"({len(payload_files)} file(s))",
                          file=sys.stderr)
                    break
                reason = res["pipe"]["result"].get(
                    "reason", "(no reason)")
                print(f"  attach attempt {attempt+1} failed: "
                      f"{reason}", file=sys.stderr)
            if not attach_ok:
                print("error: could not attach files after 3 "
                      "attempts", file=sys.stderr)
                return 1
        else:
            print("  (no files to attach)", file=sys.stderr)

        # ---- type ----
        print(f"[type] {len(text):,} chars", file=sys.stderr)
        type_spec = {
            "tag": "type",
            "event": "type",
            "payload": {"text": text},
            "ack_name": "type",
        }
        res = broadcast_many(
            server, [type_spec], timeout=args.type_timeout,
        )
        if res["type"]["exit_code"] != 0:
            reason = res["type"]["result"].get(
                "reason", "(no reason)")
            print(f"error: type failed: {reason}", file=sys.stderr)
            return res["type"]["exit_code"]
        print("  OK - type", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# Composer-text assembly
# ---------------------------------------------------------------------------

def _build_text(args: argparse.Namespace,
                conv_path: Path) -> tuple[str, str]:
    """Return (text, source_tag) for the composer.

    `--text STR` wins outright (no substitution).  Otherwise the
    template is filled with the extracted last-relevant string,
    which `--guidance TEXT` can override.
    """
    if args.text is not None:
        return args.text, "--text override"

    if args.guidance is not None:
        guidance = args.guidance
        source = "--guidance override"
    else:
        guidance = _extract_last_relevant_string(
            conv_path,
            max_words=args.guidance_words,
        )
        source = (f"extracted last relevant string "
                  f"(last {args.guidance_words} words)")
        if not guidance:
            print("warning: could not extract a last relevant "
                  "string from the conversation; the guidance "
                  "quote will be empty.  Use --guidance to "
                  "supply one explicitly.", file=sys.stderr)

    text = DEFAULT_RESUME_TEMPLATE.format(guidance=guidance)
    return text, source


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    # ---- argument validation (before any resources are created) ----
    if args.container is not None and args.container_id is not None:
        print("error: --container and --container-id are mutually "
              "exclusive", file=sys.stderr)
        return 3
    if args.container is not None and not args.container.strip():
        print("error: --container is empty (whitespace-only)",
              file=sys.stderr)
        return 3
    if args.container_id is not None and not args.container_id.strip():
        print("error: --container-id is empty (whitespace-only)",
              file=sys.stderr)
        return 3

    attach_dir = Path(tempfile.mkdtemp(
        prefix="deeppeek_resume_attach_"))
    try:
        # 1. Conversation: use --conversation if given, else
        #    capture + rename.
        if args.conversation is not None:
            conv_path = args.conversation.resolve()
            if not conv_path.is_file():
                raise SystemExit(
                    f"error: --conversation not found: {conv_path}")
            print(f"[capture] using {conv_path}", file=sys.stderr)
        elif args.name is not None:
            # Explicit name: capture directly there, skip
            # title-derived rename.  A bare filename is treated
            # as /tmp-relative, matching the original ask
            # (`/tmp/someUsefulNameForID.md`).
            name = args.name
            conv_path = Path(name)
            if not conv_path.is_absolute():
                conv_path = Path("/tmp") / conv_path
            print(f"[capture] conversation → {conv_path}",
                  file=sys.stderr)
            cmd = _deeppeek_cmd() + ["conversation", "-o",
                                     str(conv_path)]
            rc, _, _ = _run_cmd(cmd,
                                timeout=args.capture_timeout,
                                allow_failure=False)
            if rc != 0:
                raise SystemExit(
                    f"error: conversation capture exited {rc}")
        else:
            conv_path = _stage_capture(args)

        # 2. First prompt's files (base64 → attach_dir).
        attached = _stage_get_attachments(attach_dir, args)

        # 2b. Collapse prior resumes.
        #     A second-or-later resume finds the previous resume file
        #     (or a merge file from an even earlier resume) as one of
        #     the seed attachments.  Left alone, the AI would see the
        #     same history twice — once as `resume_A.md`, once as
        #     `resume_B.md` — with no signal that A precedes B.
        #     Instead we merge them into one file and attach only
        #     that.  Non-resume attachments (images, docs, code) are
        #     unaffected: they were the seed of A, re-attached by the
        #     first resume, and they stay in the attachment list.
        if attached and not args.no_merge:
            resume_atts = [f for f in attached
                           if _is_resume_or_merge_attachment(f)]
            other_atts  = [f for f in attached
                           if not _is_resume_or_merge_attachment(f)]
            if resume_atts:
                merged = _stage_merge_history(resume_atts, conv_path, args)
                if merged is not None:
                    all_files = [merged] + other_atts
                else:
                    all_files = [conv_path] + attached
            else:
                all_files = [conv_path] + attached
        else:
            all_files = [conv_path] + attached

        # 3. Composer text.
        text, source_tag = _build_text(args, conv_path)
        print(f"[build] composer text: {len(text):,} chars "
              f"({source_tag})", file=sys.stderr)

        # 4. Dry-run or emit.
        if args.dry_run:
            target = args.url
            if args.container:
                target += f"  (container: {args.container})"
            elif args.container_id:
                target += f"  (container-id: {args.container_id})"
            print(f"\n[dry-run] would open: {target}",
                  file=sys.stderr)
            for f in all_files:
                try:
                    size = f"{f.stat().st_size:,} bytes"
                except OSError:
                    size = "?"
                print(f"[dry-run]   attach: {f}  ({size})",
                      file=sys.stderr)
            print(f"[dry-run] would type: {text!r}",
                  file=sys.stderr)
            return 0

        return _stage_emit(all_files, text, args)
    finally:
        shutil.rmtree(attach_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "resume",
        help="Capture the current conversation, open a new tab, "
             "attach the conversation + the first prompt's files, "
             "and type a 'keep going' message with the last "
             "relevant string from the interrupted thought",
    )
    p.add_argument(
        "--conversation", type=Path, default=None, metavar="PATH",
        help="use this existing conversation.md instead of running "
             "`deeppeek conversation -o …`.  Useful for replaying "
             "a previously-captured conversation.",
    )
    p.add_argument(
        "--name", default=None, metavar="FILENAME",
        help="filename for the captured conversation.  A bare "
             "filename is resolved relative to /tmp/.  When "
             "omitted, the name is derived from the conversation "
             "title: /tmp/resume_<slug>_<YYYYMMDD_HHMMSS>.md.",
    )
    p.add_argument(
        "--text", default=None, metavar="STR",
        help="override the composer text verbatim.  No substitution "
             "is applied — STR is typed exactly as given.  Use "
             "this when you want a custom prompt.  Ignores "
             "--guidance and --guidance-words.",
    )
    p.add_argument(
        "--guidance", default=None, metavar="STR",
        help="override just the guidance slot in the default "
             "template.  Ignored when --text is given.  By "
             "default, the last relevant string is extracted from "
             "the conversation (tail of the last assistant "
             "message, or of its thought process, or of the last "
             "user prompt).",
    )
    p.add_argument(
        "--guidance-words", type=int,
        default=DEFAULT_GUIDANCE_WORDS, metavar="N",
        help=f"trim the extracted guidance string to its last N "
             f"words (default: {DEFAULT_GUIDANCE_WORDS}).  Only "
             f"affects the automatic extraction; --guidance and "
             f"--text bypass it.  A 500-char cap is applied on "
             f"top as a safety net for pathological single-token "
             f"cases (giant URLs, base64 blobs).",
    )
    p.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"URL for the new tab (default: {DEFAULT_URL})",
    )
    p.add_argument(
        "--container", default=None, metavar="NAME",
        help="open the new tab inside the Firefox/Zen Container Tab "
             "whose display name matches NAME (case-insensitive; "
             "exact match preferred).  Mutually exclusive with "
             "--container-id.",
    )
    p.add_argument(
        "--container-id", default=None, metavar="COOKIE_STORE_ID",
        help="open the new tab inside the container with this raw "
             "cookieStoreId (e.g. 'firefox-container-3').  Skips "
             "the name lookup — useful when multiple containers "
             "share a display name, or for scripting.  Mutually "
             "exclusive with --container.",
    )
    p.add_argument("--port", type=int, default=8765,
                   help="extension SSE port (default: 8765)")
    p.add_argument(
        "--tab-wait", type=float, default=1.0, metavar="SECONDS",
        help="sleep after opening the new tab, before attaching "
             "(default: 1.0).  Only needs to cover the warm-cache "
             "load; the attach step is retried up to 3 times, so "
             "cold-cache loads are absorbed by the retry loop.  "
             "Bump this if you see retry messages on every run.",
    )
    p.add_argument(
        "--tab-timeout", type=float, default=30.0,
        help="timeout for the open-tab ack (default: 30)",
    )
    p.add_argument(
        "--capture-timeout", type=float, default=600.0,
        help="timeout for `deeppeek conversation` (default: 600)",
    )
    p.add_argument(
        "--attach-timeout", type=float, default=120.0,
        help="timeout for `deeppeek prompt attachments` "
             "(default: 120)",
    )
    p.add_argument("--pipe-timeout", type=float, default=180.0,
                   help="timeout per attach attempt (default: 180)")
    p.add_argument("--type-timeout", type=float, default=300.0,
                   help="timeout for the type ack (default: 300)")
    p.add_argument(
        "--no-merge", action="store_true",
        help="do not merge prior resume attachments with the current "
             "conversation; attach them all separately (pre-merge "
             "behaviour).  Useful if a seed attachment happens to "
             "share the resume naming convention and you don't want "
             "it treated as history.",
    )
    p.add_argument(
        "--merge-timeout", type=float, default=120.0,
        help="timeout for scripts/merge_md.py (default: 120)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="run capture + attachments, print what would be "
             "attached/typed, then stop — do not open a tab, "
             "do not attach, do not type",
    )
    p.set_defaults(func=run)
