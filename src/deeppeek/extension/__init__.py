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
