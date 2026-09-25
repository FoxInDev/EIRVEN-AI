# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable


OUTPUT_REQUEST = re.compile(
    r"\b(?:отдай|пришли|скинь|прикрепи|верни|скачать|выдай|download)\w*.{0,70}\bфайл|"
    r"\bфайл\w*.{0,70}\b(?:отдай|пришли|скинь|прикрепи|скачать)",
    re.IGNORECASE | re.DOTALL,
)


def _walk_strings(value: Any, *, depth: int = 0) -> Iterable[str]:
    if depth > 7:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child, depth=depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_strings(child, depth=depth + 1)


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def discover_result_files(
    payload: Any,
    query: str,
    *,
    root_dir: Path,
    data_dir: Path,
    workspace_dir: Path,
    since: float = 0.0,
    limit: int = 8,
) -> list[Path]:
    """Find real result files without turning arbitrary assistant text into downloads."""

    root = Path(root_dir).resolve()
    approved = tuple(
        path.resolve()
        for path in (
            Path(workspace_dir),
            root / "video_results",
            root / "video",
            Path(data_dir) / "uploads",
            Path(data_dir) / "generated",
        )
        if path.exists()
    )
    if not approved:
        return []

    found: dict[str, Path] = {}
    bases = (root, Path(workspace_dir).resolve())
    for raw in _walk_strings(payload):
        value = raw.strip().strip('"\'`')
        if not value or len(value) > 2048 or "\n" in value:
            continue
        first = Path(value).expanduser()
        candidates = [first] if first.is_absolute() else [base / first for base in bases]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved.is_file() and _inside(resolved, approved):
                    found[str(resolved).casefold()] = resolved
            except OSError:
                continue

    # If the owner explicitly asks for a file, return the newest produced file even
    # when an older tool route forgot to expose its typed artifact field.
    if OUTPUT_REQUEST.search(query or "") and not found:
        candidates = []
        for folder in approved:
            try:
                for path in folder.rglob("*"):
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    path.stat()
                    candidates.append(path.resolve())
            except OSError:
                continue
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for path in candidates[:limit]:
            found[str(path).casefold()] = path

    output = sorted(found.values(), key=lambda item: item.stat().st_mtime, reverse=True)
    return output[: max(1, min(int(limit), 16))]


def public_file(path: Path, attachment_id: str) -> dict[str, Any]:
    return {
        "id": attachment_id,
        "name": path.name,
        "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "size": path.stat().st_size,
        "url": f"/api/uploads/{attachment_id}",
    }
