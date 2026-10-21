# deeppeek

Capture DeepSeek conversations from the browser and recover source
files from them. The other direction works too: pipe local files back
into the chat input.

Five subcommands, each with one job:

- **`capture`** — extract the current conversation from the browser,
  recover source files from it (in memory by default, or persist the
  markdown with `-o`).
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

Recovered files land in `./recovered/files/`. The intermediate
markdown stays in memory. To keep it on disk too, add `-o`:

    deeppeek capture -o auto --recover --last 3
    # writes DeepSeek_<title>_<timestamp>.md, then recovers from it

**Attach files to the current chat input:**

    deeppeek pipe screenshot.png notes.md

The files appear as chips in the input. Type your prompt and send —
`pipe` attaches but does not submit.

## Common flags

`capture`:

    --last N            last N user turns (each turn = prompt + reply)
    --last-regex PAT    last prompt matching a regex, to end of conversation
    -o PATH|auto        write the markdown; "auto" = timestamped default
    --recover           run recovery even when -o writes a file
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

## License

MIT
