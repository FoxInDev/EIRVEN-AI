#!/usr/bin/env python3
"""Собрать на рабочий стол архив текущей версии Эрви для GitHub.

Что делает:
  1. Берёт ровно файлы текущей версии — по списку release_manifest.json — и
     сверяет каждый по SHA-256. Старые версии, данные, ключи, .venv, модели,
     логи и прочие файлы из папки в архив не попадают в принципе: копируется
     только то, что перечислено в манифесте.
  2. Проверяет эти файлы на случайно оставленные секреты (токены, ключи,
     пароли). Если что-то нашлось — архив не собирается.
  3. Кладёт на рабочий стол папку EIRVEN-AI-github-<сборка>\\EIRVEN-AI (её можно
     сразу пушить) и такой же ZIP.

Исходные файлы не изменяются: скрипт только читает и копирует. Лицензия
(LICENSE), условия для модификаций (NOTICE.md) и .gitignore уже входят в
исходники.

Запуск: двойной щелчок по MAKE_GITHUB_ARCHIVE.cmd или
    python make_github_archive.py [--out ПАПКА] [--allow-modified] [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INNER = "EIRVEN-AI"

SECRET_PATTERNS = [
    ("токен Telegram-бота", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("ключ API (sk-…)", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("токен GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("ключ Google API", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("ключ AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("закрытый ключ", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("пароль или ключ в коде", re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|secret|token)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]")),
]
TEXT_EXT = {".py", ".ps1", ".cmd", ".bat", ".json", ".toml", ".txt", ".md", ".html",
            ".js", ".css", ".ini", ".cfg", ".yml", ".yaml", ".xml", ".example", ".sh", ".command"}


def desktop_dir() -> Path:
    """Рабочий стол, в том числе перенесённый в OneDrive."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            raw = uuid.UUID("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}").bytes_le  # FOLDERID_Desktop
            guid = GUID.from_buffer_copy(raw)
            path = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) == 0:
                value = path.value
                ctypes.windll.ole32.CoTaskMemFree(path)
                if value and Path(value).is_dir():
                    return Path(value)
        except Exception:
            pass
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for candidate in (home / "Desktop", home / "OneDrive" / "Desktop", home / "Рабочий стол"):
        if candidate.is_dir():
            return candidate
    return home


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Архив текущей версии Эрви для GitHub")
    parser.add_argument("--source", default=str(ROOT), help="папка Эрви с release_manifest.json")
    parser.add_argument("--out", default="", help="куда положить результат (по умолчанию — рабочий стол)")
    parser.add_argument("--allow-modified", action="store_true",
                        help="взять файлы, изменённые после выпуска версии, как есть")
    parser.add_argument("--force", action="store_true", help="собрать, даже если нашлись похожие на секреты строки")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    manifest_path = source / "release_manifest.json"
    if not manifest_path.is_file():
        print(f"Не найден {manifest_path}. Запусти скрипт из папки Эрви, куда распакован патч.")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    build = str(manifest.get("build") or "dev")
    files: dict = manifest.get("files") or {}

    print(f"Эрви {manifest.get('version', '')} · {build} — {len(files)} файлов по манифесту")
    missing, modified = [], []
    for rel, meta in sorted(files.items()):
        path = source / rel
        if not path.is_file():
            missing.append(rel)
        elif sha256(path) != meta.get("sha256"):
            modified.append(rel)
    if missing:
        print("Нет файлов (распакуй патч целиком):")
        for rel in missing[:20]:
            print("  -", rel)
        return 2
    if modified and not args.allow_modified:
        print("Эти файлы отличаются от выпущенной версии:")
        for rel in modified[:20]:
            print("  -", rel)
        print("Если так и задумано — запусти с --allow-modified.")
        return 2

    findings = []
    for rel in sorted(files):
        path = source / rel
        if path.suffix.lower() not in TEXT_EXT and path.name != ".gitignore":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                value = match.group(0)
                shown = value[:6] + "…" + value[-3:] if len(value) > 12 else "…"
                findings.append(f"{rel}: {label} [{shown}]")
    if findings:
        print("Похоже на секреты — проверь перед публикацией:")
        for item in findings[:30]:
            print("  -", item)
        if not args.force:
            print("Архив не собран. Если это ложная тревога — запусти с --force.")
            return 3

    out_dir = Path(args.out).resolve() if args.out else desktop_dir()
    stage_root = out_dir / f"EIRVEN-AI-github-{build}"
    stage = stage_root / INNER
    if stage_root.exists():
        shutil.rmtree(stage_root)
    for rel in sorted(files):
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    shutil.copy2(manifest_path, stage / "release_manifest.json")

    zip_path = out_dir / f"EIRVEN-AI-github-{build}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, f"{INNER}/{path.relative_to(stage).as_posix()}")

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print()
    print(f"Готово: {len(files) + 1} файлов, {size_mb:.1f} МБ")
    print(f"  Папка для git push: {stage}")
    print(f"  Архив:              {zip_path}")
    if modified:
        print(f"  (взяты изменённые файлы: {len(modified)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
