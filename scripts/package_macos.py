from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "EIRVEN-macOS-r67"
ROOT_FILES = (
    ".env.example", "BUILD_INFO.json", "EIRVEN_VERSION.txt", "LICENSE",
    "MACOS_README.md", "README.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
    "pyproject.toml", "requirements.txt", "requirements-desktop.txt",
    "requirements-integrations.txt", "requirements-voice.txt",
)
MAC_SCRIPTS = ("build_macos.sh", "install_macos.command", "macos_entry.py")


def candidates() -> list[Path]:
    output: list[Path] = []
    for name in ROOT_FILES:
        path = ROOT / name
        if path.is_file():
            output.append(path)
    for name in MAC_SCRIPTS:
        path = ROOT / "scripts" / name
        if path.is_file():
            output.append(path)
    for base in (ROOT / "src" / "eirven_ai", ROOT / "assets"):
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                output.append(path)
    return sorted(set(output), key=lambda item: str(item).casefold())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    destination = args.output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in candidates():
            relative = source.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{PREFIX}/{relative}")
            info.date_time = (2026, 8, 24, 0, 0, 0)
            executable = source.suffix in {".sh", ".command"}
            info.external_attr = ((0o755 if executable else 0o644) & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    print(f"{destination}\t{destination.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
