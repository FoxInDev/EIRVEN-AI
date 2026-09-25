# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "EIRVEN-Source-r67"
ROOT_FILES = (
    ".env.example", "BUILD_INFO.json", "EIRVEN_VERSION.txt", "LICENSE", "README.md",
    "SECURITY.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "requirements.txt",
    "requirements-build.txt", "requirements-desktop.txt",
    "requirements-integrations.txt", "requirements-voice.txt",
)


def candidates() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for base in (ROOT / "src", ROOT / "scripts", ROOT / "assets", ROOT / "tests"):
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files.append(path)
    return sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix().casefold())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in candidates():
            relative = source.relative_to(ROOT).as_posix()
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise RuntimeError(f"Unsafe source path: {relative}")
            info = zipfile.ZipInfo(f"{PREFIX}/{relative}", date_time=(2026, 8, 24, 0, 0, 0))
            info.external_attr = (0o644 & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"CRC failed: {bad}")
    print(f"{output}\t{output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
