# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Где хранятся объёмные данные Эрви.

Приложение и виртуальное окружение остаются на системном диске: они привязаны к
профилю пользователя и весят немного. Место занимают модели — Ollama и голос, — и
именно их можно вынести на другой диск. Все три инструмента (установщик,
деинсталлятор и перенос) читают путь отсюда, чтобы не разойтись в мнениях о том,
где что лежит.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

APP_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EIRVEN AI"
CONFIG_PATH = APP_ROOT / "storage.json"

# Значения по умолчанию — исторические расположения, чтобы уже установленные копии
# продолжили работать без переноса.
DEFAULT_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EIRVEN"
DEFAULT_OLLAMA_MODELS = Path.home() / ".ollama" / "models"


def _read() -> dict[str, Any]:
    try:
        if CONFIG_PATH.is_file():
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    except Exception:
        pass
    return {}


def data_root() -> Path:
    """Папка для голосовой модели и прочих больших файлов Эрви."""
    raw = str(_read().get("data_root") or "").strip()
    return Path(raw) if raw else DEFAULT_DATA_ROOT


def ollama_models_dir() -> Path:
    """Папка моделей Ollama. Пусто — значит используется её собственная по умолчанию."""
    raw = str(_read().get("ollama_models") or "").strip()
    return Path(raw) if raw else DEFAULT_OLLAMA_MODELS


def ollama_program_dir() -> Path | None:
    raw = str(_read().get("ollama_program") or "").strip()
    return Path(raw) if raw else None


def silero_path() -> Path:
    return data_root() / "models" / "silero" / "v5_5_ru.pt"


def free_space_gb(path: Path) -> float:
    """Свободное место на диске, которому принадлежит путь."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / (1024 ** 3)
    except Exception:
        return 0.0


def save(
    *,
    data_root_path: str | os.PathLike[str] | None = None,
    ollama_models: str | os.PathLike[str] | None = None,
    ollama_program: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Сохранить выбранные пути. Пустое значение возвращает вариант по умолчанию."""
    current = _read()
    if data_root_path is not None:
        current["data_root"] = str(data_root_path) if str(data_root_path).strip() else ""
    if ollama_models is not None:
        current["ollama_models"] = str(ollama_models) if str(ollama_models).strip() else ""
    if ollama_program is not None:
        current["ollama_program"] = str(ollama_program) if str(ollama_program).strip() else ""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return current


def describe() -> dict[str, Any]:
    """Текущая раскладка — для интерфейса и для журналов."""
    return {
        "app_root": str(APP_ROOT),
        "data_root": str(data_root()),
        "ollama_models": str(ollama_models_dir()),
        "ollama_program": str(ollama_program_dir() or ""),
        "silero": str(silero_path()),
        "free_app_gb": round(free_space_gb(APP_ROOT), 1),
        "free_data_gb": round(free_space_gb(data_root()), 1),
    }


def apply_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Подставить пути в окружение дочерних процессов.

    Ollama берёт расположение моделей из OLLAMA_MODELS — это её штатный способ, и
    он надёжнее, чем переносить папку вручную и надеяться, что она найдётся.
    """
    target = env if env is not None else os.environ
    models = ollama_models_dir()
    if str(models) and models != DEFAULT_OLLAMA_MODELS:
        target["OLLAMA_MODELS"] = str(models)
    target["EIRVEN_SILERO_MODEL"] = str(silero_path())
    return target  # type: ignore[return-value]
