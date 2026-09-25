# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _runtime_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def _runtime_executable() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path("/Applications/Ollama.app/Contents/Resources/ollama")
    return str(candidate) if candidate.is_file() else ""


def _installed_models() -> set[str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {str(row.get("name") or "").casefold() for row in data.get("models", [])}
    except Exception:
        return set()


def _prepare(root: Path, status) -> None:
    executable = _runtime_executable()
    if not executable:
        webbrowser.open("https://ollama.com/download/mac")
        raise RuntimeError("Установи бесплатный локальный компонент с открывшейся страницы и запусти Эрви снова.")
    if not _runtime_ready():
        subprocess.run(["open", "-gja", "Ollama"], check=False)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not _runtime_ready():
            time.sleep(.5)
    if not _runtime_ready():
        raise RuntimeError("Локальный контур не запустился. Открой приложение Ollama один раз и повтори запуск Эрви.")

    from eirven_ai.hardware import detect_hardware
    profile = detect_hardware()
    model = profile.recommended_main_model
    status("Подготавливаю интеллект…")
    if model.casefold() not in _installed_models():
        completed = subprocess.run([executable, "pull", model], check=False)
        if completed.returncode != 0:
            raise RuntimeError("Подготовка прервалась. Повтори запуск — загруженная часть сохранена.")

    values = {
        "EIRVEN_ROOT_DIR": str(root),
        "EIRVEN_DATA_DIR": str(root / "data"),
        "EIRVEN_WORKSPACE_DIR": str(root / "workspace"),
        "EIRVEN_HOST": "127.0.0.1",
        "EIRVEN_PORT": "7860",
        "EIRVEN_LLM_BACKEND": "ollama",
        "EIRVEN_MODEL": model,
        "EIRVEN_FAST_MODEL": model,
        "EIRVEN_CODE_MODEL": model,
        "EIRVEN_DEEP_MODEL": model,
        "EIRVEN_VISION_MODEL": profile.recommended_vision_model,
        "EIRVEN_STRICT_RELEASE_MODEL": "true",
        "EIRVEN_ENABLE_COMMANDS": "true",
        "EIRVEN_ENABLE_BROWSER": "true",
        "EIRVEN_ENABLE_DESKTOP_CONTROL": "false",
        "EIRVEN_OPEN_BROWSER": "true",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    (root / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    os.environ.update(values)


def main() -> int:
    import tkinter as tk
    from tkinter import messagebox

    root_dir = Path.home() / "Library" / "Application Support" / "EIRVEN AI"
    window = tk.Tk()
    window.title("Эрви")
    window.geometry("420x180")
    window.resizable(False, False)
    window.configure(bg="#080b17")
    label = tk.Label(window, text="Подготавливаю Эрви…", fg="#eaf8ff", bg="#080b17", font=("Helvetica", 17, "bold"))
    label.pack(expand=True)
    detail = tk.Label(window, text="Первый запуск может занять больше времени", fg="#8795b7", bg="#080b17", font=("Helvetica", 11))
    detail.pack(pady=(0, 35))
    outcome: dict[str, object] = {}

    def status(value: str) -> None:
        window.after(0, lambda: label.configure(text=value))

    def worker() -> None:
        try:
            _prepare(root_dir, status)
            outcome["ok"] = True
        except Exception as exc:
            outcome["error"] = str(exc)
        finally:
            window.after(0, window.destroy)

    threading.Thread(target=worker, daemon=True).start()
    window.mainloop()
    if not outcome.get("ok"):
        messagebox.showerror("Эрви", str(outcome.get("error") or "Не удалось завершить подготовку"))
        return 1

    from eirven_ai.app import main as run_app
    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
