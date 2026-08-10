#!/usr/bin/env python3
"""Build a deterministic, filtered RED upload archive and hash manifest."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "skills" / "mars-research-assistant"
PREFIX = "mars-research-assistant"
DEVELOPMENT_DIR_PARTS = {"tests", "docs", ".git", ".venv", "__pycache__"}
PERMISSIONS = (
    "public_network_research",
    "on_demand_uv_environment",
    "user_selected_local_artifact_write",
    "explicitly_confirmed_google_drive_write",
)


def _included_files() -> list[Path]:
    if not RUNTIME.is_dir():
        raise ValueError("runtime package is missing")
    files = sorted(
        path
        for path in RUNTIME.rglob("*")
        if path.is_file()
        and not any(
            part in DEVELOPMENT_DIR_PARTS
            for part in path.relative_to(RUNTIME).parts
        )
    )
    if any(path.is_symlink() for path in files):
        raise ValueError("runtime package contains a symbolic link")
    return files


def _zip_info(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def build(output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes, bool]] = []
    file_hashes: dict[str, str] = {}
    for path in _included_files():
        relative = path.relative_to(RUNTIME).as_posix()
        name = f"{PREFIX}/{relative}"
        content = path.read_bytes()
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        entries.append((name, content, executable))
        file_hashes[name] = sha256(content).hexdigest()
    internal_manifest = {
        "red_upload_schema": 1,
        "package": PREFIX,
        "permissions": list(PERMISSIONS),
        "file_sha256": file_hashes,
        "markdown_only_degradation": (
            "Use SKILL.md for discovery and the npx skills add command; do not "
            "claim scripts or the on-demand uv environment are installed."
        ),
    }
    internal_bytes = (
        json.dumps(internal_manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    entries.append(
        (f"{PREFIX}/RED_UPLOAD_MANIFEST.json", internal_bytes, False)
    )
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w") as archive:
        for name, content, executable in sorted(entries):
            archive.writestr(_zip_info(name, executable), content)
    temporary.replace(output)
    sidecar = {
        **internal_manifest,
        "archive": output.name,
        "archive_sha256": sha256(output.read_bytes()).hexdigest(),
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    manifest = build(arguments.output)
    print(f"built RED upload bundle: {arguments.output.resolve()}")
    print(f"wrote hash manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
