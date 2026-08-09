from __future__ import annotations

import argparse
import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    ".git", ".github", ".venv", "venv", "build", "dist", "release", "tests",
    "__pycache__", ".pytest_cache", ".ruff_cache", "models", "logs",
}
EXCLUDED_FILES = {
    ".env", "RELEASE_MANIFEST.json", "loggg2.txt", "loggg2.prev.txt",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log"}


def include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in rel.parts):
        return False
    if rel.as_posix() in EXCLUDED_FILES:
        return False
    if path.suffix.casefold() in EXCLUDED_SUFFIXES:
        return False
    if rel.parts and rel.parts[0] in {"data", "workspace"} and path.name != ".gitkeep":
        return False
    return path.is_file()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a clean EIRVEN Windows release archive")
    parser.add_argument("--version", default="v1.2.2")
    parser.add_argument("--output", default=str(ROOT / "release"))
    args = parser.parse_args()

    version = args.version.strip()
    if not version.startswith("v"):
        version = "v" + version
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"EIRVEN-Windows-{version}.zip"

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as zf:
        for path in sorted(ROOT.rglob("*")):
            if not include(path):
                continue
            rel = path.relative_to(ROOT).as_posix()
            zf.write(path, f"EIRVEN/{rel}")

    checksum = sha256(archive)
    sums = out / "SHA256SUMS.txt"
    sums.write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
