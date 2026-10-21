import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .pack import (
    build_xpi,
    extension_id,
    extension_version,
    source_hash,
)
from .paths import (
    marker_path,
    policies_path,
    source_dir,
    xpi_path,
)
from .profile import resolve_profile


def _read_policies(path: Path) -> dict:
    if not path.exists():
        return {"policies": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"{path} is malformed JSON ({e}). Fix it or rename it to policies.json.bak and retry."
        )
    if "policies" not in data or not isinstance(data["policies"], dict):
        data["policies"] = {}
    return data


def _write_policies(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install(profile=None, unsigned: bool = False, require_existing: bool = False) -> int:
    src = source_dir()
    ext_id = extension_id(src)
    version = extension_version(src)
    profile_dir = resolve_profile(profile)

    marker = marker_path(profile_dir)
    if require_existing and not marker.exists():
        raise SystemExit(
            "Extension is not installed yet. Run `dsx extension install` first."
        )

    dist_dir = Path("dist")
    xpi_tmp = dist_dir / f"{ext_id}.xpi"
    build_xpi(src, xpi_tmp)

    target_xpi = xpi_path(profile_dir, ext_id)
    target_xpi.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(xpi_tmp, target_xpi)

    policies_file = policies_path(profile_dir)
    policies = _read_policies(policies_file)
    policies["policies"].setdefault("ExtensionSettings", {})[ext_id] = {
        "installation_mode": "force_installed",
        "install_url": target_xpi.resolve().as_uri(),
    }
    _write_policies(policies_file, policies)

    if unsigned:
        user_js = profile_dir / "user.js"
        pref = 'user_pref("xpinstall.signatures.required", false);'
        existing = user_js.read_text(encoding="utf-8") if user_js.exists() else ""
        if pref not in existing:
            with user_js.open("a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(pref + "\n")
            print(
                "Note: --unsigned writes xpinstall.signatures.required=false to user.js. "
                "Only builds that honor this preference will accept the extension."
            )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "version": version,
                "source_hash": source_hash(src),
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "xpi_path": str(target_xpi),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Installed {ext_id} v{version} into {profile_dir}")
    print("Restart Zen to activate the extension.")
    return 0


def uninstall(profile=None) -> int:
    src = source_dir()
    ext_id = extension_id(src)
    profile_dir = resolve_profile(profile)

    xpi = xpi_path(profile_dir, ext_id)
    if xpi.exists():
        xpi.unlink()

    policies_file = policies_path(profile_dir)
    if policies_file.exists():
        try:
            data = json.loads(policies_file.read_text(encoding="utf-8"))
            settings = data.get("policies", {}).get("ExtensionSettings", {})
            if ext_id in settings:
                del settings[ext_id]
                if not settings:
                    data["policies"].pop("ExtensionSettings", None)
                policies_file.write_text(
                    json.dumps(data, indent=2) + "\n", encoding="utf-8"
                )
        except json.JSONDecodeError:
            pass

    marker = marker_path(profile_dir)
    if marker.exists():
        marker.unlink()

    print(f"Uninstalled {ext_id} from {profile_dir}")
    print("Restart Zen to complete removal.")
    return 0
