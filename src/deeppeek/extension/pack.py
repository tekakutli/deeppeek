import hashlib
import json
import zipfile
from pathlib import Path
from typing import Iterator, Tuple


EXCLUDE_DIR_NAMES = {".git", "node_modules", "dist", "tests", "__pycache__"}


def _iter_files(source: Path) -> Iterator[Tuple[Path, Path]]:
    for p in sorted(source.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(source)
        if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        # `.py` is excluded along with `.pyc` and `.md`: the package
        # marker `assets/__init__.py` exists so that
        # `importlib.resources.files("deeppeek.extension.assets")`
        # resolves, but it must never be shipped inside the XPI.
        # The extension loader in Zen ignores unknown files, but a
        # stray Python module in the payload is noise at best and
        # an audit smell at worst.
        if p.suffix in {".pyc", ".md", ".py"}:
            continue
        yield rel, p


def _zipinfo(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def read_manifest(source: Path) -> dict:
    mp = source / "manifest.json"
    if not mp.exists():
        raise SystemExit(f"manifest.json not found at {mp}")
    return json.loads(mp.read_text(encoding="utf-8"))


def extension_id(source: Path) -> str:
    m = read_manifest(source)
    try:
        return m["browser_specific_settings"]["gecko"]["id"]
    except (KeyError, TypeError):
        raise SystemExit("manifest.json is missing browser_specific_settings.gecko.id")


def extension_version(source: Path) -> str:
    m = read_manifest(source)
    if "version" not in m:
        raise SystemExit("manifest.json is missing version")
    return m["version"]


def source_hash(source: Path) -> str:
    h = hashlib.sha256()
    for rel, p in _iter_files(source):
        h.update(str(rel).encode("utf-8"))
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return "sha256:" + h.hexdigest()


def build_xpi(source: Path, out: Path) -> Tuple[Path, str]:
    source = Path(source)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w") as z:
        for rel, p in _iter_files(source):
            z.writestr(_zipinfo(rel.as_posix()), p.read_bytes())
    return out, source_hash(source)
