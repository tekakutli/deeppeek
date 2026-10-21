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
