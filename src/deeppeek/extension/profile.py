import configparser
import os
import sys
from pathlib import Path
from typing import List, Optional

from .paths import zen_root


def find_profiles() -> List[Path]:
    root = zen_root()
    if not root.exists():
        return []

    profiles: List[Path] = []
    ini = root / "profiles.ini"
    if ini.exists():
        cp = configparser.ConfigParser()
        cp.read(ini)
        for section in cp.sections():
            if not section.startswith("Profile"):
                continue
            if not cp.has_option(section, "Path"):
                continue
            raw = cp.get(section, "Path")
            is_rel = cp.has_option(section, "IsRelative") and cp.get(section, "IsRelative") == "1"
            full = (root / raw) if is_rel else Path(raw)
            if full.exists():
                profiles.append(full)

    if not profiles:
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "prefs.js").exists():
                profiles.append(child)
    return profiles


def resolve_profile(name: Optional[str] = None) -> Path:
    if name:
        p = Path(name).expanduser()
        if p.exists() and p.is_dir():
            return p
        for cand in find_profiles():
            if cand.name == name:
                return cand
        raise SystemExit(f"profile {name!r} not found")

    env = os.environ.get("ZEN_PROFILE")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
        raise SystemExit(f"ZEN_PROFILE points to a path that does not exist: {p}")

    profiles = find_profiles()
    if not profiles:
        raise SystemExit(
            "Zen profile not found. Install Zen or set $ZEN_PROFILE to your profile directory."
        )
    if len(profiles) == 1:
        return profiles[0]

    print("Multiple Zen profiles found:", file=sys.stderr)
    for p in profiles:
        print(f"  {p.name}  ({p})", file=sys.stderr)
    raise SystemExit("Re-run with --profile <name>.")
