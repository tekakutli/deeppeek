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
