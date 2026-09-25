# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def owner_training_active() -> bool:
    """Return True when the owner-only WSL QLoRA pipeline is still alive.

    The explicit environment flag wins. On Windows we then check the PID file used by
    v2.6.6. The probe is read-only: it never signals, pauses or changes training.
    """
    explicit = os.getenv("EIRVEN_TRAINING_COEXIST")
    if explicit is not None:
        return _truthy(explicit)
    if os.name != "nt":
        return False
    command = (
        "p=/var/lib/eirven-full/training_wrapper.pid; "
        "test -s \"$p\" || exit 1; "
        "pid=$(cat \"$p\" 2>/dev/null); "
        "test -n \"$pid\" && kill -0 \"$pid\" 2>/dev/null"
    )
    try:
        result = subprocess.run(
            ["wsl.exe", "-e", "bash", "-lc", command],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2.5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def write_runtime_marker(root_dir: Path, active: bool) -> None:
    try:
        logs = Path(root_dir) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "training_coexist.txt").write_text(
            "ACTIVE: runtime is forced to CPU-safe local inference; training is untouched.\n"
            if active else "INACTIVE: normal runtime model routing is enabled.\n",
            encoding="utf-8",
        )
    except Exception:
        pass
