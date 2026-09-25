# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Иконка Эрви в трее.

Иконки в трее у Эрви не было никогда: библиотека pystray стояла в зависимостях и
ставилась при установке, но ни одна строка кода её не использовала. Пока
интерфейс открывался в браузере, это было терпимо. Теперь Эрви — только
приложение, и человеку нужен понятный способ вернуться в неё: закрыл окно —
открыл снова из трея, как у любой программы.

Иконка живёт в процессе сервера, потому что лончер после запуска закрывается, а
сервер работает всё время, пока работает Эрви. Любой сбой здесь не должен
задевать остальное: без иконки Эрви по-прежнему открывается ярлыком и сферой.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from .trace import log_event


class TrayIcon:
    """Иконка в области уведомлений Windows с меню Эрви."""

    def __init__(self, settings: Any, open_ui: Callable[[], Any],
                 show_orb: Callable[[], Any], hide_orb: Callable[[], Any],
                 shutdown: Callable[[], Any]) -> None:
        self.settings = settings
        self._open_ui = open_ui
        self._show_orb = show_orb
        self._hide_orb = hide_orb
        self._shutdown = shutdown
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    def _log(self, event: str, **fields: Any) -> None:
        try:
            log_event(self.settings.root_dir, event, **fields)
        except Exception:
            pass

    def _image(self) -> Any:
        """Картинка иконки — сфера Эрви. Если её нет, простой круг, но не падение."""
        from PIL import Image, ImageDraw
        web = Path(__file__).resolve().parent / "web"
        for name in ("eirven-icon.png", "eirven-orb.png"):
            path = web / name
            if path.is_file():
                try:
                    return Image.open(path).convert("RGBA").resize((64, 64), Image.LANCZOS)
                except Exception:
                    continue
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((6, 6, 58, 58), fill=(120, 96, 230, 255))
        return image

    def _safe(self, action: Callable[[], Any], label: str) -> Callable[..., None]:
        """Обёртка пункта меню: сбой одного действия не валит иконку целиком."""
        def run(*_args: Any) -> None:
            try:
                action()
                self._log("TRAY_ACTION", action=label)
            except Exception as exc:
                self._log("TRAY_ACTION_FAILED", action=label, error=str(exc)[:200])
        return run

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            import pystray
        except Exception as exc:
            self._log("TRAY_UNAVAILABLE", error=str(exc)[:200])
            return False
        try:
            menu = pystray.Menu(
                # default=True — это действие на двойной клик по иконке.
                pystray.MenuItem("Открыть Эрви", self._safe(self._open_ui, "open"), default=True),
                pystray.MenuItem("Показать сферу", self._safe(self._show_orb, "show_orb")),
                pystray.MenuItem("Скрыть сферу", self._safe(self._hide_orb, "hide_orb")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выйти из Эрви", self._safe(self._exit, "exit")),
            )
            self._icon = pystray.Icon("eirven", self._image(), "Эрви", menu)
        except Exception as exc:
            self._log("TRAY_CREATE_FAILED", error=str(exc)[:200])
            return False

        def loop() -> None:
            try:
                self._icon.run()
            except Exception as exc:
                self._log("TRAY_LOOP_FAILED", error=str(exc)[:200])

        self._thread = threading.Thread(target=loop, daemon=True, name="eirven-tray")
        self._thread.start()
        self._log("TRAY_STARTED")
        return True

    def _exit(self) -> None:
        # Сначала убираем иконку, иначе после выхода она «висит» до наведения мыши.
        self.stop()
        self._shutdown()

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
