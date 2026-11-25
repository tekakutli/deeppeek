"""Shared tab-hint flags for commands that forward to the daemon.

Both `deeppeek answer` and `deeppeek editor` accept the same set of
mutually-related flags for choosing which browser tab the daemon
targets:

    --new-tab               open a fresh tab (default)
    --reuse-tab             reuse whichever tab is focused
    --reuse-tab-in NAME     reuse-or-open a tab in container NAME
    --reuse-tab-by TAB_ID   pin to this exact tab id
    --container NAME        new tab in container NAME
    --container-id ID       new tab in raw cookieStoreId ID

`--reuse-tab-by` complements `deeppeek daemon --which-tab`, which
prints the id of the currently-focused DeepSeek tab.  Scriptable:

    TAB=$(deeppeek daemon --which-tab)
    deeppeek answer --reuse-tab-by "$TAB" "hi"

Tab hints only take effect when `--url` points at the deeppeek
daemon.  Intermediaries (llama-swap etc.) must preserve the
`deeppeek` request-body field for the hints to reach the daemon.

Pulled out of answer.py so editor.py can share it verbatim — the
predecessor of this module was the `--tab-id` flag on `answer`,
which editor.py never picked up.  Keeping the two commands on the
same helper guarantees they stay in lockstep next time the flag
names change (e.g. the daemon's `--reuse-tab-id` →
`--reuse-tab-by` rename).
"""
import sys


def register_tab_flags(p):
    """Add the six tab-hint flags to argparse parser `p`."""
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--new-tab", action="store_true",
        help="Open a new DeepSeek tab for this request (default).",
    )
    mode_group.add_argument(
        "--reuse-tab", action="store_true",
        help="Reuse the currently active DeepSeek tab.  Container "
             "flags are not compatible with this mode.",
    )
    mode_group.add_argument(
        "--reuse-tab-in", default=None, metavar="NAME",
        help="Reuse an existing tab in the named container, or open "
             "one if none exists.  Requires the daemon and extension "
             "to support reuse (see project docs).",
    )
    mode_group.add_argument(
        "--reuse-tab-by", type=int, default=None, metavar="TAB_ID",
        help="Pin to this exact browser tab id (as reported by "
             "`deeppeek daemon --which-tab`).  Skips open-tab "
             "entirely, so it works across container lines — the "
             "tab's container is whatever it already is.  Mutually "
             "exclusive with --reuse-tab, --reuse-tab-in, --container, "
             "and --container-id.  The id is only valid while that "
             "tab remains open.",
    )
    p.add_argument(
        "--container", default=None, metavar="NAME",
        help="Open a new tab in the named Firefox/Zen container.  "
             "Mutually exclusive with --container-id, --reuse-tab, "
             "--reuse-tab-in, and --reuse-tab-by.",
    )
    p.add_argument(
        "--container-id", default=None, metavar="ID",
        help="Open a new tab in the container with this raw "
             "cookieStoreId (e.g. 'firefox-container-3').  "
             "Mutually exclusive with --container, --reuse-tab, "
             "--reuse-tab-in, and --reuse-tab-by.",
    )


def build_tab_hints(args):
    """Translate parsed tab flags into a `deeppeek.tab` payload.

    Returns None when no tab hint was given; otherwise a dict with at
    least `mode`.  Raises SystemExit(1) — printing to stderr — on
    invalid combinations.

    Both commands call this before any network work, so bad
    combinations fail fast without opening tabs or binding ports.
    """
    def _err(msg: str):
        print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(1)

    if args.container and args.container_id:
        _err("--container and --container-id are mutually exclusive")

    if args.reuse_tab_by is not None:
        if args.container or args.container_id:
            _err("--reuse-tab-by cannot be combined with --container "
                 "or --container-id (the target tab already has its "
                 "own container)")
        return {"mode": "by_id", "tab_id": args.reuse_tab_by}

    if args.reuse_tab:
        if args.container or args.container_id:
            _err("--reuse-tab cannot be combined with --container or "
                 "--container-id (reuse targets the active tab, "
                 "whose container is whatever it already is)")
        return {"mode": "reuse_active"}

    if args.reuse_tab_in:
        if args.container or args.container_id:
            _err("--reuse-tab-in and --container/--container-id are "
                 "mutually exclusive — --reuse-tab-in already selects "
                 "a container")
        return {"mode": "reuse_in_container",
                "container": args.reuse_tab_in}

    if args.container:
        return {"mode": "new", "container": args.container}

    if args.container_id:
        return {"mode": "new", "container_id": args.container_id}

    if args.new_tab:
        return {"mode": "new"}

    return None
