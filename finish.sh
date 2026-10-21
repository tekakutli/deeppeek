#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
DEST=$(pwd)

# ---------- package markers ----------
: > src/deeppeek/commands/__init__.py
: > src/deeppeek/shared/__init__.py
: > src/deeppeek/extension/assets/__init__.py

# ---------- src/deeppeek/__init__.py ----------
cat > src/deeppeek/__init__.py <<'PY'
"""deeppeek — capture DeepSeek conversations and recover source files."""
__version__ = "0.1.0"
PY

# ---------- src/deeppeek/__main__.py ----------
cat > src/deeppeek/__main__.py <<'PY'
import sys
from .cli import main
if __name__ == "__main__":
    sys.exit(main())
PY

# ---------- src/deeppeek/cli.py ----------
cat > src/deeppeek/cli.py <<'PY'
"""Top-level CLI dispatcher.  Subcommand bodies live in commands/<name>.py."""
import argparse
import sys

from . import __version__
from .commands import (
    capture as capture_cmd,
    recover as recover_cmd,
    dig as dig_cmd,
    extension as extension_cmd,
    diagnose as diagnose_cmd,
)


def _build_root_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deeppeek",
        description="Capture DeepSeek conversations and recover files from them.",
    )
    p.add_argument("--version", action="version", version=f"deeppeek {__version__}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose diagnostics to stderr")
    sub = p.add_subparsers(dest="command", required=True)
    capture_cmd.register(sub)
    recover_cmd.register(sub)
    dig_cmd.register(sub)
    extension_cmd.register(sub)
    diagnose_cmd.register(sub)
    return p


_DISPATCH = {
    "capture": capture_cmd.run,
    "recover": recover_cmd.run,
    "dig": dig_cmd.run,
    "extension": extension_cmd.run,
    "diagnose": diagnose_cmd.run,
}


def main(argv=None) -> int:
    args = _build_root_parser().parse_args(argv)
    try:
        return _DISPATCH[args.command](args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
PY

# ---------- src/deeppeek/shared/log.py ----------
cat > src/deeppeek/shared/log.py <<'PY'
"""Verbosity-aware stderr helpers."""
import sys

_VERBOSE = False


def set_verbose(v: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(v)


def info(msg: str) -> None:
    print(msg, file=sys.stderr)


def debug(msg: str) -> None:
    if _VERBOSE:
        print(f"[debug] {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)
PY

# ---------- src/deeppeek/recover/__init__.py ----------
cat > src/deeppeek/recover/__init__.py <<'PY'
"""recover — the 9-pass file-recovery pipeline."""
from .pipeline import RecoverConfig, run

__all__ = ["RecoverConfig", "run"]
PY

# ---------- src/deeppeek/capture/__init__.py ----------
cat > src/deeppeek/capture/__init__.py <<'PY'
"""capture — receive a DeepSeek conversation from the browser extension."""
from .models import Conversation, Message
from .service import CaptureConfig, capture_to_markdown
from .formatter import format_conversation
from .slicer import slice_messages

__all__ = [
    "Conversation", "Message",
    "CaptureConfig", "capture_to_markdown",
    "format_conversation", "slice_messages",
]
PY

# ---------- src/deeppeek/capture/latex.py ----------
cat > src/deeppeek/capture/latex.py <<'PY'
"""LaTeX delimiter normalisation: \\( … \\) -> $ … $ ; \\[ … \\] -> $$ … $$."""
import re


def convert_latex_delimiters(text: str) -> str:
    text = re.sub(r"\\\((.+?)\\\)", r"$\1$", text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.+?)\\\]", r"$$\1$$", text, flags=re.DOTALL)
    return text
PY

# ---------- src/deeppeek/capture/service.py ----------
cat > src/deeppeek/capture/service.py <<'PY'
"""High-level capture: run the local HTTP server, block on a payload,
render it to markdown/JSON, write to disk."""
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Conversation
from .server import ExportServer
from .slicer import slice_messages
from .formatter import format_conversation
from .latex import convert_latex_delimiters


@dataclass
class CaptureConfig:
    port: int = 8765
    watch: bool = False
    output: Optional[Path] = None           # None -> auto-name
    start_prompt: Optional[str] = None
    end_prompt: Optional[str] = None
    start_index: Optional[int] = None
    end_index: Optional[int] = None
    match_mode: str = "substring"
    context: int = 0
    include_thinking: bool = True
    include_hints: bool = True
    latex: bool = False
    as_json: bool = False
    timeout: Optional[float] = None


def _safe_title(title: Optional[str], maxlen: int = 30) -> str:
    if not title:
        return ""
    s = re.sub(r"[^a-zA-Z0-9-_\s]", "", title)
    s = re.sub(r"\s+", "_", s)
    return s[:maxlen].rstrip("_").strip()


def capture_to_markdown(cfg: CaptureConfig) -> Path:
    server = ExportServer(port=cfg.port, watch_mode=cfg.watch)
    server.start()
    print(f"Listening on http://127.0.0.1:{cfg.port}", file=sys.stderr)
    print("Open Zen to chat.deepseek.com and click the DEEPPEEK toolbar button.",
          file=sys.stderr)
    print("Press Ctrl+C to cancel.", file=sys.stderr)
    try:
        payload = server.wait_for_result(timeout=cfg.timeout)
    finally:
        server.stop()

    conversation = Conversation.from_json(payload)
    sliced = slice_messages(
        conversation.messages,
        start_prompt=cfg.start_prompt,
        end_prompt=cfg.end_prompt,
        start_index=cfg.start_index,
        end_index=cfg.end_index,
        match_mode=cfg.match_mode,
        context=cfg.context,
    )

    if cfg.as_json:
        out = json.dumps(
            {
                "title": conversation.title,
                "url": conversation.url,
                "messages": [m.to_dict() for m in sliced],
            },
            indent=2,
            ensure_ascii=False,
        )
    else:
        out = format_conversation(
            conversation.title,
            conversation.url,
            sliced,
            include_thinking=cfg.include_thinking,
            include_hints=cfg.include_hints,
        )
        if cfg.latex:
            out = convert_latex_delimiters(out)

    output_path = cfg.output
    if output_path is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
        safe = _safe_title(conversation.title)
        ext = ".json" if cfg.as_json else ".md"
        suffix = f"_{safe}" if safe else ""
        output_path = Path(f"DeepSeek{suffix}_{stamp}{ext}")

    output_path.write_text(out, encoding="utf-8")
    print(f"Wrote {output_path} ({len(sliced)} message(s))", file=sys.stderr)
    return output_path
PY

# ---------- src/deeppeek/extension/__init__.py ----------
cat > src/deeppeek/extension/__init__.py <<'PY'
"""extension — install / update / inspect the bundled WebExtension."""
from .install import install, uninstall
from .pack import build_xpi, extension_id, extension_version, source_hash
from .status import print_status
from .paths import assets_dir, source_dir

__all__ = [
    "install", "uninstall", "build_xpi",
    "extension_id", "extension_version", "source_hash",
    "print_status", "assets_dir", "source_dir",
]
PY

# ---------- src/deeppeek/extension/paths.py ----------
cat > src/deeppeek/extension/paths.py <<'PY'
"""Filesystem locations for the extension assets and the Zen profile."""
import os
import sys
from importlib import resources
from pathlib import Path


def assets_dir() -> Path:
    """Materialised path to the bundled extension source files."""
    try:
        return Path(str(resources.files("deeppeek.extension.assets")))
    except (ModuleNotFoundError, TypeError):
        # Fallback for editable / source-tree runs where the package
        # is not yet installed.
        return Path(__file__).resolve().parent / "assets"


# install.py imports `source_dir` — keep the alias.
source_dir = assets_dir


def zen_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/zen"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "zen"
    return Path.home() / ".zen"


def marker_path(profile_dir: Path) -> Path:
    return profile_dir / "deeppeek_extension.json"


def policies_path(profile_dir: Path) -> Path:
    return profile_dir / "distribution" / "policies.json"


def xpi_path(profile_dir: Path, ext_id: str) -> Path:
    return profile_dir / "extensions" / f"{ext_id}.xpi"
PY

# ---------- src/deeppeek/extension/dev.py ----------
cat > src/deeppeek/extension/dev.py <<'PY'
"""Print about:debugging instructions for extension iteration."""
import sys


def print_dev_instructions() -> int:
    print(
        "Extension dev loop:\n"
        "  1. open about:debugging#/runtime/this-firefox in Zen\n"
        "  2. click 'Load Temporary Add-on…'\n"
        "  3. select src/deeppeek/extension/assets/manifest.json\n"
        "  4. reload the DeepSeek tab and click the DEEPPEEK toolbar button\n"
        "\n"
        "Temporary installs survive no browser restart.\n"
        "Promote with `deeppeek extension install` when ready.",
        file=sys.stdout,
    )
    return 0
PY

# ---------- src/deeppeek/extension/status.py (REWRITE) ----------
cat > src/deeppeek/extension/status.py <<'PY'
"""Report whether the installed extension matches the bundled source."""
import json

from .pack import extension_version, source_hash
from .paths import marker_path, source_dir
from .profile import resolve_profile


def print_status(profile=None, as_json=False) -> int:
    src = source_dir()
    current_version = extension_version(src)
    current_hash = source_hash(src)
    profile_dir = resolve_profile(profile)
    marker = marker_path(profile_dir)

    report = {
        "current_version": current_version,
        "current_hash": current_hash,
        "profile": str(profile_dir),
    }

    if not marker.exists():
        report.update(installed=False, status="not-installed",
                      message="Run `deeppeek extension install`.")
    else:
        data = json.loads(marker.read_text())
        report.update(installed=True,
                      installed_version=data.get("version"),
                      installed_hash=data.get("source_hash"))
        if data.get("version") != current_version:
            report.update(status="stale-version",
                          message=f"Update: {data.get('version')} -> {current_version}")
        elif data.get("source_hash") != current_hash:
            report.update(status="stale-source",
                          message="Extension source changed; run `deeppeek extension update`.")
        else:
            report.update(status="up-to-date", message="Up to date.")

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Extension version (bundled): {current_version}")
        print(f"Profile:                     {profile_dir}")
        print(f"Status:                      {report['message']}")

    return 0 if report["status"] == "up-to-date" else 1
PY

# ---------- src/deeppeek/commands/recover.py ----------
cat > src/deeppeek/commands/recover.py <<'PY'
"""`deeppeek recover` — thin adapter over recover.pipeline.run()."""
import argparse
from pathlib import Path

from ..recover.pipeline import RecoverConfig, run as recover_run


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("recover", help="Recover source files from a markdown dump")
    p.add_argument("dump", type=Path)
    p.add_argument("--out", type=Path, default=Path("./recovered"))
    p.add_argument("--layout", choices=["flat", "header", "passthrough"], default="flat")
    p.add_argument("--remap", action="append", default=[], metavar="OLD=NEW")
    p.add_argument("--min-trust", choices=["HIGH", "MEDIUM", "LOW"], default="MEDIUM")
    p.add_argument("--llm-mode", choices=["off", "smart", "always"], default="off")
    p.add_argument("--llm-url", default=None)
    p.add_argument("--llm-model", default=None)
    p.add_argument("--llm-timeout", type=int, default=None)
    p.add_argument("--escalate-below", type=float, default=None)
    p.add_argument("--llm-max-calls", type=int, default=None)
    p.add_argument("--no-llm-ping", dest="llm_ping", action="store_false")
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("--trace", action="store_true")
    p.set_defaults(func=run)


def _parse_remap(items):
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"error: bad --remap {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def run(args: argparse.Namespace) -> int:
    cfg = RecoverConfig(
        dump_path=args.dump,
        out_dir=args.out,
        layout=args.layout,
        remap=_parse_remap(args.remap),
        min_trust=args.min_trust,
        llm_mode=args.llm_mode,
        llm_url=args.llm_url,
        llm_model=args.llm_model,
        llm_timeout=args.llm_timeout,
        escalate_below=args.escalate_below,
        llm_max_calls=args.llm_max_calls,
        llm_ping=args.llm_ping,
        cache_dir=args.cache_dir,
        verbose=getattr(args, "verbose", False),
        trace=args.trace,
    )
    return recover_run(cfg)
PY

# ---------- src/deeppeek/commands/capture.py ----------
cat > src/deeppeek/commands/capture.py <<'PY'
"""`deeppeek capture` — run the local server, save the conversation."""
import argparse
from pathlib import Path

from ..capture.service import CaptureConfig, capture_to_markdown


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("capture", help="Capture a DeepSeek conversation from the browser")
    p.add_argument("-o", "--output", type=Path, default=None)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--start-prompt")
    p.add_argument("--end-prompt")
    p.add_argument("--match-mode", choices=["substring", "exact", "regex"], default="substring")
    p.add_argument("--start-index", type=int)
    p.add_argument("--end-index", type=int)
    p.add_argument("--context", type=int, default=0)
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        out = capture_to_markdown(CaptureConfig(
            port=args.port,
            watch=args.watch,
            output=args.output,
            start_prompt=args.start_prompt,
            end_prompt=args.end_prompt,
            start_index=args.start_index,
            end_index=args.end_index,
            match_mode=args.match_mode,
            context=args.context,
            include_thinking=not args.no_thinking,
            include_hints=not args.no_hints,
            latex=args.latex,
            as_json=args.json,
            timeout=args.timeout,
        ))
    except KeyboardInterrupt:
        return 1
    return 0
PY

# ---------- src/deeppeek/commands/dig.py ----------
cat > src/deeppeek/commands/dig.py <<'PY'
"""`deeppeek dig` — capture + recover in one step."""
import argparse
import sys
from datetime import datetime
from pathlib import Path

from ..capture.service import CaptureConfig, capture_to_markdown
from ..recover.pipeline import RecoverConfig, run as recover_run


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dig", help="Capture a conversation AND recover its files")
    p.add_argument("--capture-to", type=Path, default=None)
    p.add_argument("--recover-to", type=Path, default=Path("./recovered"))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--start-prompt")
    p.add_argument("--end-prompt")
    p.add_argument("--match-mode", choices=["substring", "exact", "regex"], default="substring")
    p.add_argument("--start-index", type=int)
    p.add_argument("--end-index", type=int)
    p.add_argument("--context", type=int, default=0)
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--no-hints", action="store_true")
    p.add_argument("--latex", action="store_true")
    p.add_argument("--timeout", type=float, default=None)
    # recovery-side flags
    p.add_argument("--layout", choices=["flat", "header", "passthrough"], default="flat")
    p.add_argument("--min-trust", choices=["HIGH", "MEDIUM", "LOW"], default="MEDIUM")
    p.add_argument("--llm-mode", choices=["off", "smart", "always"], default="off")
    p.add_argument("--llm-url", default=None)
    p.add_argument("--llm-model", default=None)
    p.add_argument("--llm-timeout", type=int, default=None)
    p.add_argument("--trace", action="store_true")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    stamp = datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
    capture_path = args.capture_to or Path(f"DeepSeek_{stamp}.md")

    print("[dig 1/2] capturing from browser...", file=sys.stderr)
    try:
        capture_to_markdown(CaptureConfig(
            port=args.port,
            watch=args.watch,
            output=capture_path,
            start_prompt=args.start_prompt,
            end_prompt=args.end_prompt,
            start_index=args.start_index,
            end_index=args.end_index,
            match_mode=args.match_mode,
            context=args.context,
            include_thinking=not args.no_thinking,
            include_hints=not args.no_hints,
            latex=args.latex,
            timeout=args.timeout,
        ))
    except KeyboardInterrupt:
        return 1

    print(f"[dig 2/2] recovering files from {capture_path}...", file=sys.stderr)
    return recover_run(RecoverConfig(
        dump_path=capture_path,
        out_dir=args.recover_to,
        layout=args.layout,
        min_trust=args.min_trust,
        llm_mode=args.llm_mode,
        llm_url=args.llm_url,
        llm_model=args.llm_model,
        llm_timeout=args.llm_timeout,
        verbose=getattr(args, "verbose", False),
        trace=args.trace,
    ))
PY

# ---------- src/deeppeek/commands/extension.py ----------
cat > src/deeppeek/commands/extension.py <<'PY'
"""`deeppeek extension` — install / update / status / build / dev / uninstall."""
import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    e = sub.add_parser("extension", help="Manage the Zen WebExtension")
    esub = e.add_subparsers(dest="ext_cmd", required=True)

    ei = esub.add_parser("install", help="Install (or reinstall) the extension")
    ei.add_argument("--profile")
    ei.add_argument("--unsigned", action="store_true")
    ei.set_defaults(func=_install)

    eu = esub.add_parser("update", help="Rebuild and reinstall (requires prior install)")
    eu.add_argument("--profile")
    eu.set_defaults(func=_update)

    es = esub.add_parser("status", help="Report whether the extension is stale")
    es.add_argument("--profile")
    es.add_argument("--json", action="store_true")
    es.set_defaults(func=_status)

    eb = esub.add_parser("build", help="Produce an .xpi without touching a profile")
    eb.add_argument("-o", "--output")
    eb.set_defaults(func=_build)

    ed = esub.add_parser("dev", help="Print about:debugging instructions")
    ed.set_defaults(func=_dev)

    eun = esub.add_parser("uninstall", help="Remove the extension from Zen")
    eun.add_argument("--profile")
    eun.set_defaults(func=_uninstall)


def _install(args) -> int:
    from ..extension.install import install
    return install(profile=args.profile, unsigned=args.unsigned)


def _update(args) -> int:
    from ..extension.install import install
    return install(profile=args.profile, unsigned=False, require_existing=True)


def _status(args) -> int:
    from ..extension.status import print_status
    return print_status(profile=args.profile, as_json=args.json)


def _build(args) -> int:
    from ..extension.pack import build_xpi
    from ..extension.paths import source_dir
    out = Path(args.output) if args.output else Path("dist/deeppeek.xpi")
    build_xpi(source_dir(), out)
    print(f"Built {out}")
    return 0


def _dev(args) -> int:
    from ..extension.dev import print_dev_instructions
    return print_dev_instructions()


def _uninstall(args) -> int:
    from ..extension.install import uninstall
    return uninstall(profile=args.profile)


def run(args: argparse.Namespace) -> int:
    return args.func(args)
PY

# ---------- src/deeppeek/commands/diagnose.py ----------
cat > src/deeppeek/commands/diagnose.py <<'PY'
"""`deeppeek diagnose` — environment sanity check."""
import argparse
import shutil
import sys


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("diagnose", help="Print environment / extension / LLM status")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print(f"python: {sys.version.split()[0]}  ({sys.executable})")
    try:
        import markdown_it  # noqa: F401
        print("markdown-it-py: ok")
    except ImportError:
        print("markdown-it-py: MISSING — `pip install markdown-it-py`")

    try:
        from ..extension.paths import source_dir, zen_root
        from ..extension.pack import extension_version, extension_id
        src = source_dir()
        print(f"extension assets: {src}")
        try:
            print(f"  version: {extension_version(src)}")
            print(f"  id:      {extension_id(src)}")
        except SystemExit as e:
            print(f"  manifest: {e}")
        print(f"zen root: {zen_root()} ({'exists' if zen_root().exists() else 'absent'})")
    except Exception as e:
        print(f"extension: {e}")

    try:
        from ..extension.status import print_status
        print("\n--- installed extension status ---")
        print_status()
    except SystemExit as e:
        print(f"  status: {e}")

    if shutil.which("zen") or shutil.which("zen-browser"):
        print("zen binary: ok")
    else:
        print("zen binary: not on PATH (may still be installed)")

    return 0
PY

# ---------- tests/conftest.py ----------
cat > tests/conftest.py <<'PY'
"""Make `src/` importable when running tests without an editable install."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
PY

# ---------- pyproject.toml ----------
cat > pyproject.toml <<'TOML'
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "deeppeek"
version = "0.1.0"
description = "Capture DeepSeek conversations and recover source files from them"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
authors = [{ name = "tekakutli" }]
dependencies = ["markdown-it-py>=3.0"]

[project.optional-dependencies]
dev = ["pytest>=7", "pytest-cov", "ruff", "mypy"]

[project.scripts]
deeppeek = "deeppeek.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/deeppeek"]

[tool.hatch.build.targets.wheel.force-include]
"src/deeppeek/extension/assets" = "deeppeek/extension/assets"

[tool.pytest.ini_options]
testpaths = ["tests"]
TOML

# ---------- minimal README ----------
cat > README.md <<'MD'
# deeppeek

Capture DeepSeek conversations from the browser and recover source files from them.

## Install (dev)

    python -m venv .venv
    . .venv/bin/activate
    pip install -e '.[dev]'

## Use

    deeppeek capture  -o chat.md          # save a conversation
    deeppeek recover  chat.md --out ./r   # recover its files
    deeppeek dig      --recover-to ./r    # both, in one command

    deeppeek extension install            # write the WebExtension into Zen
    deeppeek extension status             # is the installed extension stale?
    deeppeek diagnose                     # environment sanity check
MD

echo "✓ all missing files written"
