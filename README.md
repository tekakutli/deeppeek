# deeppeek

Capture DeepSeek conversations from the browser and recover source
files from them. The other direction works too: pipe local files back
into the chat input.

Five subcommands, each with one job:

- **`capture`** — extract the current conversation from the browser
  and recover source files from it.  `-o` also persists the markdown
  to disk; recovery runs either way.
- **`recover`** — recover source files from an existing markdown dump
  on disk.
- **`pipe`** — attach local files (images, documents, code) to the
  current chat input.
- **`extension`** — manage the bundled WebExtension.
- **`diagnose`** — environment check.

## Requirements

- **Python ≥ 3.10**
- **Zen browser** — only for `capture` and `pipe`. `recover` alone
  needs nothing but Python.
- No JS toolchain. The extension ships pre-built inside the Python
  package.

Runtime dependencies are pulled in automatically; `markdown-it-py` is
the only one.

## Install

    pipx install --editable /path/to/deeppeek --force

`--editable` symlinks pipx's isolated venv at the source tree, so
edits to `src/deeppeek/` are live immediately. If `~/.local/bin` isn't
on `$PATH`:

    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

For a frozen copy (no auto-pickup of edits), drop `--editable` and run
`pipx reinstall deeppeek` after each source change.

## Run the tests

`tests/recover/` holds fixture-based tests for the recovery
pipeline.  Each test spawns a real `deeppeek recover …` subprocess
and diffs the output tree line-by-line.

    python tests/recover/test_recover.py

or via pytest:

    python -m pytest tests/recover -v

### Install the runtime dependency first

The tests shell out to `deeppeek recover …` once per fixture.  The
subprocess is resolved in this order:

1. **`deeppeek` on `PATH`** — used if found.  `pipx install
   --editable …` puts it there, and the pipx venv already carries
   `markdown-it-py` (the project's only runtime dependency).
   Nothing extra to install.

2. **`python -m deeppeek`** — fallback when `deeppeek` is not on
   `PATH`.  Requires `markdown-it-py` in the Python that runs the
   tests.  Install it:

       python -m pip install --user markdown-it-py

   Or, for a project-local venv (isolated; preferred if you don't
   want to touch user site-packages):

       python -m venv .venv
       .venv/bin/pip install -e ".[dev]"
       .venv/bin/python tests/recover/test_recover.py

### Failure mode to recognise

A missing `markdown-it-py` does not surface as `ImportError` in
the test runner.  Every recovery subprocess dies at its own
import line, `out/files/` stays empty, and the test reports:

    test_01: FAIL
      01_reprint_later: expected output missing: bar.py
    test_02: FAIL
      02_subsection_patches: expected output missing: foo.js
    ...
    FileNotFoundError: …/manifest.json

Six `expected output missing` lines in a row is this.  Fix it by
either ensuring `deeppeek` is on `PATH`, or by running
`python -m pip install --user markdown-it-py`.

## Load the extension into Zen

Zen does not reliably read unsigned extensions placed on disk by a
policy file, and browsers do not let the CLI install one for you. Load
it manually. It's a five-second, per-session operation:

1. In Zen, open `about:debugging#/runtime/this-firefox`.
2. Click **"Load Temporary Add-on…"**.
3. Select `src/deeppeek/extension/assets/manifest.json`.
4. Pin the toolbar button if it isn't visible.

Temporary installs do not survive a browser restart — repeat after
each Zen restart.

## Use

Two examples covering the two directions.

**Recover the files from the last 3 turns of the current conversation:**

    deeppeek capture --last 3

Recovered files land in `./deeppeek_recovered/files/`. When `-o` is
given, the recovered directory is created next to the output file
instead (`-o /tmp/conv.md` → `/tmp/deeppeek_recovered/`). The
intermediate markdown stays in memory. To keep it on disk too, add
`-o`:

    deeppeek capture -o auto --last 3
    # writes DeepSeek_<title>_<timestamp>.md, then recovers from it

**Attach files to the current chat input:**

    deeppeek pipe screenshot.png notes.md

The files appear as chips in the input. Type your prompt and send —
`pipe` attaches but does not submit.

## Optional: AST anchoring

The `recover` command's text heuristics can place most AI-generated
edits.  For blocks with no unique text anchor (e.g. a bare `if`
statement referencing local variables, or a top-level `const X = ...`
declaration), the tool falls back to fail-closed by default.

To enable structural anchoring — which uses tree-sitter to find the
correct insertion point from the file's AST — install the `ast` extra:

    pipx inject deeppeek 'deeppeek[ast]'

This pulls in `tree-sitter` and `tree-sitter-javascript`.  Without it,
`recover` works exactly as before: text-only heuristics plus
fail-closed.

## Common flags

`capture`:

    --last N            last N user turns (each turn = prompt + reply)
    --last-regex PAT    last prompt matching a regex, to end of conversation
    -o PATH|auto        write the markdown; "auto" = timestamped default.
                        Recovery runs by default, with or without -o.
                        When PATH is given, the recovered directory
                        (deeppeek_recovered/) is created next to it.
    --no-recover        skip recovery; write the markdown file only
                        (requires -o)
    --recover           no-op; recovery is the default (kept for
                        backward compatibility)
    --apply-to DIR      write recovered files directly into an existing tree
                        (deletes [DEL]-marked files, reports land beside them)
    --watch             auto-fire when the extension connects

`pipe`:

    --serial            attach one file at a time (default: batch)

## Watchdog

The extension polls the local server every 500 ms to notice when a
CLI command starts. This gives sub-second reaction, at the cost of
~2–6 ms of background CPU per second while idle on the deepseek tab.
Disable if you prefer zero idle cost (first capture after each CLI
start then takes ~1 s):

    deeppeek extension watchdog off
    deeppeek extension watchdog on
    deeppeek extension watchdog status

The setting persists in `browser.storage.local.watchdog` across
extension reloads and Zen restarts.

## Troubleshooting

`deeppeek diagnose` reports Python, dependency, extension-assets,
Zen profile, and installed-extension state — start there.

- **`capture` hangs at "Listening on…"** — extension not loaded, or
  the deepseek tab needs a reload (F5).
- **`pipe` says no extension connected** — Zen is closed, or the
  extension isn't loaded in the current tab.
- **`extension watchdog …` says port 8765 in use** — another `deeppeek`
  command is running. Ctrl+C it, retry.
- **Tests all fail with `expected output missing`** — see
  [Run the tests](#run-the-tests); the Python running the tests
  can't import `markdown-it-py`.

## License

MIT
