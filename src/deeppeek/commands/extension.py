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
