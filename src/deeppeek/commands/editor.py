"""`deeppeek editor` — local LLM pipeline for conversation turns.

Three input sources, one pipeline:

  · Daemon (default):
        deeppeek editor --script rewrite
    Arms the injected Copy Turn buttons; each click runs through
    the script + LLM and prints the reply.  Ctrl+C to stop and
    uninstall.

  · File input (one-shot):
        deeppeek editor --script rewrite --file path/to/text.md
    Reads the file, runs through the script, prints, exits.

  · Argument string (one-shot):
        deeppeek editor --script rewrite "some prompt text"
    Uses the joined arguments as input, same pipeline, exits.

One CLI invocation runs exactly one script.  Composition is
expressed *inside* scripts: a script that wants another script's
work calls `llm_scripts.run_script(name, input_value, ctx)` from
its own `on_response`, and that callee may itself call
`run_script(...)`, and so on.  The pipeline is code, not CLI
syntax.  Values flow between scripts as in-process Python objects
(dicts / lists / strings); there is no stdout round-trip for
intermediate results.

Every invocation writes one .md file per input:

    /tmp/deeppeek_editor_<ts>/
        143022_inline.md
        143145_turn_22_of_26.md

YAML frontmatter + `## Input` + one `## Stage` section + `## Final
Output`.  The input is flushed to disk before any stage runs, so a
crash mid-pipeline never loses the user's text.

Script-specific flags: `register(sub, script_name=...)` accepts
the `--script` value that `cli.py` peeked from argv.  If the target
script defines `register_flags(parser)`, it is invoked here to
install script-specific flags (e.g. `complete_paths` installs
`--paths_absolute`).  Those flags exist only when the script is
the one on the command line.

`ctx["_llm_client"]` is injected so scripts can invoke each other
via `llm_scripts.run_script(...)` and still reach the LLM when the
nested target needs it.

Tab hints
---------
Same six flags as `deeppeek answer` (`--new-tab`, `--container`,
`--container-id`, `--reuse-tab`, `--reuse-tab-in`,
`--reuse-tab-by`), validated and translated by `_tab_hints.py`.
They travel to the daemon in the request body under
`deeppeek.tab`, so they only take effect when `--url` points at a
deeppeek daemon and it honours them.  E.g.:

    TAB=$(deeppeek daemon --which-tab)
    deeppeek editor --reuse-tab-by "$TAB" --script rewrite "rewrite this"

In daemon mode (interactive Copy Turn), the hint applies to every
turn — each click routes through the pinned tab.  That's usually
what you want; if it isn't, don't combine daemon mode with a pin.
"""
from __future__ import annotations

import base64
import json
import queue
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..llm import LLMClient, DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT
from .. import llm_scripts
from ..transport.server import ExportServer
from . import _tab_hints


# ---- helpers ------------------------------------------------------

def _send_and_wait(server, event, ack_name, timeout, extra=None):
    batch_id = uuid.uuid4().hex[:12]
    payload = {"batch_id": batch_id}
    if extra:
        payload.update(extra)
    server._broadcast(event, payload)
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
            f"error: batch_id mismatch for {ack_name}")
    return ack.get("result") or {}


def _yaml_scalar(v):
    if isinstance(v, Path):
        v = str(v)
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
    return "'" + str(v).replace("'", "''") + "'"


def _write_input_file(session_dir, filename, md, header_ctx):
    """Fallback-first: write the input to disk BEFORE running the
    script, so a mid-pipeline crash never loses the user's text."""
    path = session_dir / filename
    ts = datetime.now(timezone.utc)

    lines = ["---"]
    for key, val in header_ctx.items():
        if key == "session_dir":
            continue
        lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append(f"created_at: {_yaml_scalar(ts.isoformat())}")
    lines.append("status: 'pending'")
    lines.append("---")
    lines.append("")
    lines.append("## Input")
    lines.append("")
    lines.append(md)
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def _append_stage(path, index, name, *, prompt, response, output,
                  status, error, latency_ms):
    body = path.read_text()
    section = [f"\n## Stage {index + 1}: {name}\n",
               f"status: {status}",
               f"latency_ms: {latency_ms}"]
    if error:
        section.append(f"error: {error!r}")
    section.append("")
    if prompt is not None:
        section.append("### Prompt\n")
        section.append(prompt.rstrip("\n"))
        section.append("")
    if response is not None:
        section.append("### Response\n")
        section.append(response.rstrip("\n"))
        section.append("")
    if output is not None:
        section.append("### Output\n")
        section.append(output.rstrip("\n"))
        section.append("")
    body += "\n".join(section)
    path.write_text(body)


def _append_final(path, display, *, overall_status):
    body = path.read_text()
    body = body.replace("status: 'pending'",
                        f"status: '{overall_status}'", 1)
    body += f"\n## Final Output\n\n{display}\n"
    path.write_text(body)


def _display(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)


# ---- stage execution ---------------------------------------------

def _run_stage(input_value, args, script_mod, script_name,
               session_dir, client, script_ctx, base_extra, path,
               deeppeek_hints=None):
    """Run one script: build_prompt → optional LLM → optional
    on_response → write .md → print.  Returns 0 on success, 1 on
    failure.

    The ctx carries `_llm_client` so `llm_scripts.run_script(...)`
    calls from within `on_response`/`build_prompt` can reach the
    LLM when the nested target requires it.

    `deeppeek_hints` is forwarded verbatim to `client.call` so the
    daemon (when the LLM URL points at it) can route the request to
    the tab the user pinned with `--reuse-tab-by`.
    """
    stage_t0 = time.monotonic()

    ctx = llm_scripts.build_ctx(
        script=script_name,
        model=args.model,
        session_dir=session_dir,
        **script_ctx,
        **base_extra,
        stage_index=0,
        stage_total=1,
        is_first=True,
        is_last=True,
        input_kind="text",
        _llm_client=client,
    )

    prompt = None
    response = None
    try:
        prompt = script_mod.build_prompt(input_value, ctx)
        if getattr(script_mod, "requires_llm", True):
            response = client.call(
                prompt,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                tag=f"editor:{script_name}",
                deeppeek_hints=deeppeek_hints,
            )
            if response is None:
                raise RuntimeError(
                    "LLM returned None (see LLM log above)")
        else:
            response = ""
    except Exception as e:
        dt = int((time.monotonic() - stage_t0) * 1000)
        _append_stage(path, 0, script_name, prompt=prompt,
                      response=None, output=None,
                      status="llm_error", error=str(e),
                      latency_ms=dt)
        _append_final(path, "", overall_status="llm_error")
        print(f"  [editor] {script_name} error: {e}", file=sys.stderr)
        return 1

    dt = int((time.monotonic() - stage_t0) * 1000)

    output = response
    on_response = getattr(script_mod, "on_response", None)
    if callable(on_response):
        try:
            result = on_response(response, ctx)
            if result is not None:
                output = result
        except Exception as e:
            _append_stage(path, 0, script_name, prompt=prompt,
                          response=response, output=None,
                          status="script_error", error=str(e),
                          latency_ms=dt)
            _append_final(path, "", overall_status="script_error")
            print(f"  [editor] {script_name}.on_response threw: {e}",
                  file=sys.stderr)
            return 1

    display = output if isinstance(output, str) else _display(output)
    _append_stage(path, 0, script_name, prompt=prompt,
                  response=response, output=display,
                  status="ok", error=None, latency_ms=dt)
    print(f"  [editor] {script_name} ok ({dt}ms)", file=sys.stderr)
    _append_final(path, display, overall_status="ok")
    print(display, file=sys.stdout, flush=True)
    return 0


# ---- one-shot mode ------------------------------------------------

def _run_one_shot(args, script_mod, script_name, session_dir,
                  client, script_ctx, deeppeek_hints=None):
    hhmmss = datetime.now().strftime("%H%M%S")

    if args.file is not None:
        try:
            md = args.file.read_text()
        except OSError as e:
            print(f"error: cannot read {args.file}: {e}",
                  file=sys.stderr)
            return 1
        stem = args.file.stem or "input"
        filename = f"{hhmmss}_{stem}.md"
        input_label = f"{args.file} ({len(md):,} chars)"
        header_extra = {"input_file": str(args.file.resolve())}
    else:
        md = " ".join(args.text)
        filename = f"{hhmmss}_inline.md"
        input_label = f"inline text ({len(md):,} chars)"
        header_extra = {}

    print(f"Session dir: {session_dir}", file=sys.stderr)
    print(f"[editor] input: {input_label}", file=sys.stderr)

    header_ctx = {**script_ctx, **header_extra,
                  "script": script_name, "model": args.model}
    path = _write_input_file(session_dir, filename, md, header_ctx)
    rc = _run_stage(md, args, script_mod, script_name,
                    session_dir, client, script_ctx,
                    base_extra={}, path=path,
                    deeppeek_hints=deeppeek_hints)
    print(f"[editor] saved: {path}", file=sys.stderr)
    return rc


# ---- daemon mode --------------------------------------------------

def _run_daemon(args, script_mod, script_name, session_dir,
                client, script_ctx, deeppeek_hints=None):
    try:
        server = ExportServer(port=args.port)
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"error: port {args.port} is already in use — is "
                  f"another deeppeek command running?",
                  file=sys.stderr)
            return 1
        raise

    state = {"busy": False}

    def _gate(ack):
        if ack.get("__ack") != "download":
            return True
        return not state["busy"]

    server.set_post_gate(_gate)

    installed = False
    try:
        print(f"Waiting for extension on "
              f"http://127.0.0.1:{args.port}/events "
              f"(up to {args.timeout:.0f}s)...", file=sys.stderr)
        if not server.wait_for_extension(timeout=args.timeout):
            print("error: no extension connected within timeout.",
                  file=sys.stderr)
            return 1

        result = _send_and_wait(
            server, "hijack-turn-install", "hijack-turn-install",
            args.timeout, extra={"port": args.port})
        if not result.get("success"):
            print(f"error: install failed: {result.get('reason')}",
                  file=sys.stderr)
            return 2
        installed = True

        print(f"\nEditor installed.", file=sys.stderr)
        print(f"Script: {script_name}", file=sys.stderr)
        print(f"Session dir: {session_dir}", file=sys.stderr)
        print(f"Click any Copy Turn button in the DeepSeek tab.",
              file=sys.stderr)
        print(f"Press Ctrl+C to stop and uninstall.\n",
              file=sys.stderr)

        while True:
            ack = server.wait_for_result(timeout=None)
            if ack.get("__ack") != "download":
                continue

            state["busy"] = True
            try:
                position = ack.get("position") or {}
                md = base64.b64decode(
                    ack.get("base64") or "").decode(
                    "utf-8", errors="replace")

                turn = position.get("turn")
                total = position.get("totalTurns")
                hhmmss = datetime.now().strftime("%H%M%S")
                filename = f"{hhmmss}_turn_{turn}_of_{total}.md"

                base_extra = {
                    "chat_title": ack.get("chat_title"),
                    "chat_url": ack.get("chat_url"),
                    "turn_number": turn,
                    "total_turns": total,
                    "message_index": position.get("index"),
                    "total_messages": position.get("total"),
                    "role": position.get("role"),
                    "method": position.get("method"),
                }
                header_ctx = {**script_ctx, **base_extra,
                              "script": script_name,
                              "model": args.model}
                path = _write_input_file(
                    session_dir, filename, md, header_ctx)
                print(f"\n[editor] turn {turn}/{total} → {path.name}",
                      file=sys.stderr)
                _run_stage(md, args, script_mod, script_name,
                           session_dir, client, script_ctx,
                           base_extra=base_extra, path=path,
                           deeppeek_hints=deeppeek_hints)
            finally:
                state["busy"] = False

    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
        return 0
    finally:
        if installed:
            try:
                server._broadcast("hijack-turn-uninstall",
                                  {"batch_id": "shutdown"})
                time.sleep(0.3)
            except BaseException:
                pass
        server.stop()


# ---- argparse wiring ----------------------------------------------

def register(sub, script_name=None):
    p = sub.add_parser(
        "editor",
        help="Run each Copy Turn (or file, or arg string) through the "
             "configured script + local LLM and print the reply",
    )
    p.add_argument(
        "text", nargs="*", metavar="TEXT",
        help="one-shot: inline prompt text (omit both TEXT and --file "
             "for daemon mode)",
    )
    p.add_argument(
        "--script", default="passthrough", metavar="NAME",
        help="registered llm_scripts name (default: passthrough)",
    )
    p.add_argument(
        "--file", type=Path, default=None, metavar="PATH",
        help="one-shot: read input from PATH instead of positional "
             "TEXT (mutually exclusive with TEXT)",
    )
    p.add_argument(
        "--dir", type=Path, default=None, metavar="PATH",
        help="parent directory for the per-invocation session "
             "subdirectory (default: /tmp).  Each run creates "
             "PATH/<YYYYMMDD_HHMMSS>/ so re-runs never overwrite.",
    )
    p.add_argument(
        "--port", type=int, default=8765,
        help="extension SSE port (daemon mode only; default: 8765)",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="extension SSE connect timeout (daemon mode only; "
             "default: 30)",
    )
    p.add_argument("--url", default=None,
                   help="LLM URL override (default: llm.client default)")
    p.add_argument("--model", default=DEFAULT_LLM_MODEL)
    p.add_argument("--llm-timeout", type=int, default=DEFAULT_LLM_TIMEOUT)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--llm-no-stream", action="store_true",
                   help="disable streaming the LLM response to stderr "
                        "(default: stream tokens live)")

    # ---- tab hints (shared with `deeppeek answer`) ----------------
    _tab_hints.register_tab_flags(p)

    # Register script-conditional flags.  `script_name` is the
    # peeked `--script` value passed by `cli.py`, or None when the
    # user did not specify `--script` at all (and when the full
    # parser is being built for `--help` / bare `deeppeek`).
    #
    # Skipping discovery when None keeps bare `deeppeek`,
    # `deeppeek --help`, `deeppeek editor`, and `deeppeek editor
    # --help` quiet — no `llm_scripts` discovery, no warnings about
    # unrelated .py files that happen to live next to the bundled
    # scripts.  Only an explicit `--script <NAME>` on the command
    # line triggers the target script's `register_flags`.
    #
    # `argparse` still defaults `args.script` to "passthrough"
    # inside the subparser, so the runtime behaviour of
    # `deeppeek editor "text"` is unchanged — `run()` sees
    # `args.script == "passthrough"` and loads that script there.
    if script_name is not None and llm_scripts.is_script(script_name):
        mod = llm_scripts.load_script(script_name)
        reg_flags = getattr(mod, "register_flags", None)
        if callable(reg_flags):
            try:
                reg_flags(p)
            except Exception as e:
                print(
                    f"[editor] warning: {script_name}.register_flags "
                    f"threw: {e}",
                    file=sys.stderr)

    p.set_defaults(func=run)
    return p


def run(args) -> int:
    if args.file is not None and args.text:
        print("error: --file and positional TEXT are mutually exclusive",
              file=sys.stderr)
        return 1

    # Validate tab flags before doing any disk or network work, so
    # bad combinations fail fast (and no session dir is created).
    try:
        tab_hints = _tab_hints.build_tab_hints(args)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    deeppeek_hints = {"tab": tab_hints} if tab_hints else None

    try:
        script_mod = llm_scripts.load_script(args.script)
    except KeyError as e:
        msg = e.args[0] if e.args else "unknown script"
        print(f"error: {msg}", file=sys.stderr)
        return 1

    # Script-supplied ctx contributions (from this script's flags).
    script_ctx = {}
    extractor = getattr(script_mod, "ctx_from_args", None)
    if callable(extractor):
        try:
            got = extractor(args)
            if got is None:
                got = {}
            if not isinstance(got, dict):
                raise TypeError(
                    f"ctx_from_args returned "
                    f"{type(got).__name__}, expected dict")
            script_ctx = got
        except Exception as e:
            print(f"error: {args.script}.ctx_from_args threw: {e}",
                  file=sys.stderr)
            return 1
    elif callable(getattr(script_mod, "register_flags", None)):
        print(f"[editor] warning: {args.script} defines register_flags "
              f"but not ctx_from_args — its flags will parse but be "
              f"ignored", file=sys.stderr)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.dir is not None:
        base = args.dir.resolve()
        session_dir = base / ts
    else:
        session_dir = Path("/tmp") / f"deeppeek_editor_{ts}"
    session_dir.mkdir(parents=True, exist_ok=True)

    client_kwargs = {"model": args.model, "timeout": args.llm_timeout,
                     "stream": not getattr(args, "llm_no_stream", False)}
    if args.url:
        client_kwargs["url"] = args.url
    client = LLMClient(**client_kwargs)

    one_shot = args.file is not None or bool(args.text)
    if one_shot:
        return _run_one_shot(args, script_mod, args.script,
                             session_dir, client, script_ctx,
                             deeppeek_hints=deeppeek_hints)
    return _run_daemon(args, script_mod, args.script,
                       session_dir, client, script_ctx,
                       deeppeek_hints=deeppeek_hints)
