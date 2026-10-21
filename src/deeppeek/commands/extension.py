"""`deeppeek extension` — install / update / status / build / dev /
uninstall / watchdog."""
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

    ew = esub.add_parser(
        "watchdog",
        help="Persistently enable/disable the 500ms watchdog "
             "(browser.storage.local).  Requires the extension to be "
             "connected.",
    )
    ew_sub = ew.add_subparsers(dest="wd_action", required=True)

    ew_on = ew_sub.add_parser("on", help="Enable the watchdog (default)")
    ew_on.set_defaults(func=_watchdog_on)

    ew_off = ew_sub.add_parser(
        "off",
        help="Disable the watchdog — trades ~1s extra CLI-start "
             "latency for ~0 background CPU while idle",
    )
    ew_off.set_defaults(func=_watchdog_off)

    ew_status = ew_sub.add_parser("status", help="Report current watchdog setting")
    ew_status.set_defaults(func=_watchdog_status)


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


def _watchdog_on(args) -> int:
    from ..extension.watchdog import set_watchdog
    return set_watchdog(True)


def _watchdog_off(args) -> int:
    from ..extension.watchdog import set_watchdog
    return set_watchdog(False)


def _watchdog_status(args) -> int:
    from ..extension.watchdog import get_status
    return get_status()


def run(args: argparse.Namespace) -> int:
    return args.func(args)
