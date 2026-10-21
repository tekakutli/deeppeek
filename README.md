# deeppeek

Capture DeepSeek conversations from the browser and recover source files
from them.

Three moving parts, one CLI:

- **capture** — a local HTTP server that receives a conversation from a
  WebExtension you install into Zen; renders it as Markdown or JSON.
- **recover** — a nine-pass pipeline that pulls source files out of a
  Markdown dump (the last full print of each file, then any patches
  applied on top of it).
- **dig** — both, in one shot: open a chat, click the toolbar button,
  and the recovered tree appears in `./recovered/`.

A `deeppeek extension …` subcommand group installs and inspects the
WebExtension. A `deeppeek diagnose` subcommand prints environment status.

## Requirements

- **Python ≥ 3.10** (the pipeline uses `sys.stdlib_module_names`).
- **Zen browser** — needed only for `capture` and `dig`. If you only
  want to run `recover` on an existing Markdown dump, you don't need a
  browser at all.
- No JS toolchain. The extension ships pre-bundled inside the Python
  package.

Runtime dependencies are pulled in automatically: `markdown-it-py` is
the only one.

## Install

### Recommended — `pipx` (isolated, global, editable)

For a checkout you're actively developing or want on your `$PATH`
system-wide:

    pipx install --editable /path/to/deeppeek --force

    which deeppeek            # ~/.local/bin/deeppeek
    deeppeek --version        # deeppeek 0.1.0

`--editable` symlinks pipx's isolated venv at your source tree, so any
edit to `src/deeppeek/` is live immediately. If `~/.local/bin` isn't on
your `PATH`, add it once:

    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    exec bash

To update a non-editable install after a source change:

    pipx reinstall deeppeek

### Development — `.venv`

For running the test suite and linters:

    python -m venv .venv
    . .venv/bin/activate
    pip install -e '.[dev]'          # adds pytest, ruff, mypy

    pytest tests/recover/ -v         # 6 fixtures, all should pass

`.[dev]` installs `deeppeek` into the venv as well, so while the venv
is activated `which deeppeek` resolves to `.venv/bin/deeppeek` — that's
expected. The pipx shim takes over again as soon as you `deactivate`.

To keep `.venv` for dev tooling only and avoid two `deeppeek` shims
on disk:

    .venv/bin/pip uninstall -y deeppeek

### Updating after editing the source

    # pipx editable install — no action needed, edits are live
    # pipx non-editable install:
    pipx reinstall deeppeek
    # .venv install — no action needed

## First-time setup

### 1. Install the WebExtension into Zen

    deeppeek extension install --profile <profile-name>

`<profile-name>` is the directory name under `~/.zen/`. To list them:

    deeppeek diagnose
    # or:
    ls ~/.zen

If you only have one profile, `--profile` can be omitted. If you have
several, `deeppeek diagnose` and `deeppeek extension status` will tell
you — they refuse to guess:

    Multiple Zen profiles found:
      v8bamwd6.default       (~/.zen/v8bamwd6.default)
      3vkgg00f.Default (release)  (~/.zen/3vkgg00f.Default (release))
      Re-run with --profile <name>.

The install writes the signed XPI into the profile's `extensions/`
directory and registers it in `distribution/policies.json`. **Restart
Zen** afterwards.

### 2. Verify

    deeppeek extension status --profile <profile-name>
    # → Status: Up to date.

### 3. Where the toolbar button lives

Open `about:addons` in Zen. The extension appears as
`DeepSeek Chat Exporter (DSX)`. It adds a toolbar button on
`https://chat.deepseek.com/` — click it to send the current
conversation to whatever `deeppeek capture` / `deeppeek dig` process
is listening on `127.0.0.1:8765`.

## Use

### The three workflows

    # 1. Capture only — save a conversation to Markdown
    deeppeek capture -o chat.md
    # then click the toolbar button on the DeepSeek tab

    # 2. Recover only — pull files from an existing dump
    deeppeek recover chat.md --out ./recovered

    # 3. Both — the flagship workflow
    deeppeek dig --recover-to ./recovered
    # then click the toolbar button; when the message arrives, recover
    # runs automatically on the intermediate Markdown

### `deeppeek capture` options

    -o, --output PATH              output path (default: DeepSeek_<timestamp>.md)
    --port N                       listen port (default: 8765)
    --watch                        let the extension auto-trigger without a click
    --start-prompt TEXT            slice from the first user message containing TEXT
    --end-prompt TEXT              slice up to the matching user message (+ its reply)
    --start-index N / --end-index N
    --match-mode {substring,exact,regex}
    --context N                    include N surrounding messages on each side
    --no-thinking / --no-hints     drop <Thought Process> / search hints
    --latex                        convert \(…\) → $…$ and \[…\] → $$…$$
    --json                         emit structured JSON instead of Markdown
    --timeout SECONDS              give up if no message arrives

### `deeppeek recover` options

    --out DIR                      output directory (default: ./recovered)
    --layout {flat,header,passthrough}
    --remap OLD=NEW                rewrite a path prefix (passthrough layout only)
    --min-trust {HIGH,MEDIUM,LOW}  move below-threshold files to files_low_trust/
    --llm-mode {off,smart,always}  off is the default; see "LLM assistance" below
    --llm-url / --llm-model / --llm-timeout
    --escalate-below FLOAT         smart mode: escalate below this confidence
    --llm-max-calls N              hard cap on live LLM calls
    --trace                        print per-block fate at each pass

`recover` always writes:

    <out>/files/                 the recovered source files
    <out>/files_low_trust/       below --min-trust, when applicable
    <out>/warnings.txt           per-file warnings + graph problems
    <out>/MISSING_FILES.txt      files referenced but never recovered
    <out>/deleted.txt            files marked [DEL] in the transcript
    <out>/manifest.json          full audit trail
    <out>/cache/llm/             LLM response cache (when --llm-mode != off)

Exit codes:

    0   all files at trust >= --min-trust
    1   some files below --min-trust
    2   at least one file MISSING or PARTIAL_ONLY
    3   bad arguments / missing dependency
    4   import graph reports unresolved edges

### LLM assistance (optional)

`recover` ships with heuristics that handle well-structured dumps with
zero network calls. When the dump is ambiguous — headers missing,
prose fragments that could be patches, truncated blocks — it can ask a
local OpenAI-compatible endpoint.

    --llm-mode off        (default) heuristics only, no network
    --llm-mode smart      heuristics first; LLM only for genuinely
                          ambiguous blocks (usually 0–2 calls)
    --llm-mode always     LLM on every block classification

The endpoint defaults to `http://127.0.0.1:6006/v1/chat/completions`
(local llama.cpp / vLLM / any OpenAI-compatible server). Override with
`--llm-url` and `--llm-model`. If the endpoint isn't reachable at
startup, `recover` downgrades to `off` and continues.

### `deeppeek extension …`

    deeppeek extension install    [--profile P] [--unsigned]
    deeppeek extension update     [--profile P]
    deeppeek extension status     [--profile P] [--json]
    deeppeek extension build      [-o PATH]        # produce an .xpi, touch no profile
    deeppeek extension dev                         # print about:debugging instructions
    deeppeek extension uninstall  [--profile P]

`--unsigned` writes `xpinstall.signatures.required=false` into the
profile's `user.js`. Only useful on Firefox-derived builds that honor
the pref. For a personal Zen this is typically **not** required — the
default install is signed by the local XPI mechanism.

### `deeppeek diagnose`

Prints Python version, `markdown-it-py` availability, the resolved
extension-assets path, the manifest's version and ID, the Zen root,
the installed-extension status per profile, and whether `zen` is on
`PATH`. Use it first when something doesn't work.

## Extension dev loop

For iterating on the extension without reinstalling:

    deeppeek extension dev

Then in Zen:

1. open `about:debugging#/runtime/this-firefox`
2. click "Load Temporary Add-on…"
3. select `src/deeppeek/extension/assets/manifest.json`
4. reload the DeepSeek tab

Temporary installs survive no browser restart. When you're ready to
promote:

    deeppeek extension install --profile <profile-name>
    # restart Zen

`deeppeek extension status` will warn if the installed copy is stale
relative to the source — either `stale-version` (manifest version
changed) or `stale-source` (any shipped asset changed byte-for-byte).

## Troubleshooting

**`deeppeek diagnose` shows `status unavailable: Multiple Zen profiles found`**
— pass `--profile <name>`. `diagnose` deliberately refuses to pick
one for you. `ls ~/.zen` lists the candidates.

**`deeppeek capture` sits at "Listening on http://127.0.0.1:8765" forever**
— the extension never sent a message. Check:
1. Is Zen open on `https://chat.deepseek.com/`? The extension only
   activates on that origin.
2. Did the extension install and get activated after the restart?
   `about:addons` should list it.
3. Did you click the toolbar button? For `--watch` mode the extension
   polls `/poll` every 3 s; without `--watch` you must click.
4. Is another process on 8765? `ss -ltnp | grep 8765`.

**`deeppeek recover` says `import graph reports unresolved edges` (exit 4)**
— some recovered file imports a module that isn't in the recovered
set. The full list is in `<out>/warnings.txt`; the missing modules are
also listed in `<out>/MISSING_FILES.txt`. Either recover them from
another conversation, or ignore if they're framework/shared-library
imports your project resolves at runtime.

**pipx says `Note: deeppeek was already on your PATH at …/.venv/bin/deeppeek`**
— you ran `pipx install` inside an activated `.venv`. Both copies
exist and both work. `deactivate` and `which deeppeek` will resolve
to the pipx one. If you'd rather have exactly one:

    .venv/bin/pip uninstall -y deeppeek

**`markdown-it-py: MISSING` in `diagnose`**
— installed in a different Python than the one `deeppeek` is running
from. Re-run:

    pipx reinstall deeppeek
    # or, inside .venv:
    pip install markdown-it-py

## Repo layout

    src/deeppeek/
    ├── cli.py                 root argparse dispatcher
    ├── commands/              one file per subcommand (thin adapters)
    ├── capture/               local server, models, slicer, formatter
    ├── recover/               nine-pass pipeline + LLM client
    ├── extension/             XPI packaging, profile resolution, status
    │   └── assets/            manifest.json, background.js, content.js, injected.js
    └── shared/                verbosity-aware logging

    tests/recover/             six fixtures, one per recovery failure mode

## License

MIT
