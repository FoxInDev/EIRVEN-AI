# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .identity import CANONICAL_ASSISTANT_NAME, IdentityService
from .system_browser import open_url as open_system_url, open_app_window


def _work_area_at(x: int, y: int) -> tuple[int, int, int, int] | None:
    """Рабочая область монитора, где сейчас точка (x, y): экран без панели задач.

    Берём у Windows при каждом движении, а не из размера экрана, запомненного Tk:
    тот при масштабе 150–200% бывает вдвое меньше настоящего — отсюда сфера
    «не шла дальше середины». Рабочая область учитывает панель задач, где бы она
    ни стояла, и правильна для двух мониторов. Вызов идёт из того же потока, что
    и координаты мыши, — поэтому они всегда в одной системе координат.
    Возвращает (слева, сверху, справа, снизу) или None, если узнать не вышло.
    """
    import os as _os
    if _os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        monitor = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)   # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        if work.right - work.left < 200 or work.bottom - work.top < 200:
            return None
        return int(work.left), int(work.top), int(work.right), int(work.bottom)
    except Exception:
        return None


def _window_scale(root: Any) -> float:
    """Во сколько раз растянуть рисунок сферы — по тому, что Windows реально
    применяет к окну.

    GetDpiForWindow отвечает для конкретного окна: если Windows сама растягивает
    его картинкой (окно не знает о масштабе), ответ 96 — растягивать второй раз не
    нужно. Если окно знает о масштабе — ответ реальный (144 при 150%, 192 при 200%),
    и рисунок нужно увеличить, иначе сфера выйдет вдвое меньше. Так рисунок и
    размещение окна всегда в одной системе координат, в каком бы режиме ни был Tk.
    """
    import os as _os
    dpi = 0.0
    if _os.name == "nt":
        try:
            import ctypes
            root.update_idletasks()
            getter = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
            if getter is not None:
                dpi = float(getter(root.winfo_id()) or 0)
        except Exception:
            dpi = 0.0
    if dpi <= 0:
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
    return max(1.0, min(3.0, dpi / 96.0))



class DesktopCompanion:
    """Living always-on-top canonical EIRVEN sphere with human activity cards."""

    def __init__(self, db: Database, identity: IdentityService, ui_url: str, root_dir: Any = None):
        self.db = db
        self.identity = identity
        self.ui_url = ui_url
        # Корень установки — только для журнала. Без него сфера работает, просто молча.
        self.root_dir = root_dir
        # Эти пять строк по ошибке оказались внутри _log вместо конструктора: метод
        # журнала был вставлен посреди __init__ и разрезал его. Замок, поток и окно
        # не создавались — start() падал с «нет атрибута _lock», сфера не появлялась,
        # а переключатель в настройках отвечал ошибкой 500.
        self._thread: threading.Thread | None = None
        self._root = None
        self._lock = threading.RLock()
        self._visible = False
        self._status_provider: Callable[[], dict[str, Any]] | None = None
        # Откуда брать текущую эмоцию; задаётся сервисами. Пусто — всегда «радостная».
        self.emotion_source: Callable[[], str] | None = None

    def _emotion_now(self) -> str:
        source = self.emotion_source
        if source is None:
            return "joy"
        try:
            return str(source() or "joy")
        except Exception:
            return "joy"

    def _log(self, event: str, **fields: Any) -> None:
        if not self.root_dir:
            return
        try:
            from .trace import log_event
            log_event(self.root_dir, event, **fields)
        except Exception:
            pass

    def set_voice_status_provider(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._status_provider = provider

    def set_status_provider(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._status_provider = provider

    def _status(self) -> dict[str, Any]:
        try:
            return self._status_provider() if self._status_provider else {}
        except Exception:
            return {}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.show()
                return
            self._thread = threading.Thread(target=self._run_logged, daemon=True, name="eirven-orb")
            self._thread.start()
            self._log("COMPANION_STARTED")

    def _run_logged(self) -> None:
        # Раньше упавший поток сферы просто исчезал: сферы нет, в журнале пусто.
        # Теперь причина остаётся в loggg2.txt с полной трассировкой.
        try:
            self._run()
            self._log("COMPANION_STOPPED")
        except Exception as exc:
            import traceback
            self._log("COMPANION_CRASH", error=str(exc)[:300], error_type=type(exc).__name__,
                      traceback=traceback.format_exc()[-4000:])

    def _open_fullscreen_ui(self) -> None:
        # Интерфейс Эрви — окном-приложением, а не вкладкой браузера.
        target = self.ui_url
        if self.root_dir:
            try:
                from urllib.parse import urlparse
                from . import app_key
                port = urlparse(self.ui_url).port or 7860
                target = app_key.entry_url(self.root_dir, port)
            except Exception as exc:
                self._log("UI_WINDOW_KEY_FAILED", error=str(exc)[:160])
        opened = open_app_window(target)
        self._log("UI_WINDOW_OPEN", source="sphere", ok=bool(opened))

    @staticmethod
    def _clean_goal(value: str, limit: int = 60) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" .,-")
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    @staticmethod
    def _pretty_action(step: str, goal: str) -> str:
        low = f"{step} {goal}".casefold()
        if any(x in low for x in ("откры", "launch", "open")):
            return "Открываю нужное место."
        if any(x in low for x in ("ищ", "search", "най")):
            return "Уже ищу. Проверяю, чтобы выбрать верно."
        if any(x in low for x in ("отправ", "пиш", "send", "сообщен")):
            return "Пишу и сразу проверяю отправку."
        if any(x in low for x in ("корзин", "добав")):
            return "Добавляю и сразу проверяю результат."
        if any(x in low for x in ("загруз", "скач", "download")):
            return "Загружаю. Всё идёт."
        if any(x in low for x in ("провер", "verify", "свер")):
            return "Перепроверяю, чтобы всё было точно."
        if any(x in low for x in ("воспро", "музык", "трек", "play")):
            return "Настраиваю музыку."
        if goal:
            return "Занимаюсь этим."
        return "Я рядом."

    @staticmethod
    def _clamp_anchor(anchor: dict[str, int], win_w: int, win_h: int,
                      screen_w: int, screen_h: int, taskbar_h: int = 48) -> None:
        """Keep the companion inside the usable work area and above the taskbar."""
        max_x = max(0, int(screen_w) - int(win_w))
        max_y = max(0, int(screen_h) - int(win_h) - max(0, int(taskbar_h)))
        anchor["x"] = max(0, min(int(anchor.get("x") or 0), max_x))
        anchor["y"] = max(0, min(int(anchor.get("y") or 0), max_y))

    def _human_comment(self, status: dict[str, Any]) -> str:
        if not bool(self.db.get_setting("desktop_comments_enabled", True)):
            return ""
        try:
            proactive = self.db.get_setting("proactive_last_comment", None)
            if isinstance(proactive, dict) and float(proactive.get("expires_at") or 0.0) > time.time():
                text = self._clean_goal(proactive.get("text") or "", limit=145)
                if text:
                    return text
        except Exception:
            pass
        if status.get("onboarding_complete") is False:
            return "Давай сначала познакомимся."
        state = str(status.get("state") or "")
        if state == "armed":
            return "Я здесь. Говори."
        if state == "hearing":
            return "Слушаю тебя. Не торопись."
        if state == "recognizing":
            return "Поняла. Собираю мысль целиком."
        if state == "thinking":
            return "Секунду. Подбираю лучший ход."
        if status.get("speaking"):
            return ""

        runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
        step = self._clean_goal(runtime.get("step") or runtime.get("action") or "")
        goal = self._clean_goal(runtime.get("goal") or "")
        if runtime.get("cancellable") and (step or goal):
            return self._pretty_action(step, goal)

        task = status.get("active_task") if isinstance(status.get("active_task"), dict) else {}
        task_step = self._clean_goal(task.get("current_step") or task.get("title") or "")
        task_status = str(task.get("status") or "").casefold()
        if task_status in {"done", "completed", "success"}:
            return "Результат задачи подтверждён."
        if task_status == "partial":
            return "Задача завершена частично — проверка не пройдена."
        if task_step:
            return self._pretty_action(task_step, task_step)
        return "Всё спокойно. Я рядом."

    def _run(self) -> None:
        try:
            import tkinter as tk
            from PIL import Image, ImageTk
        except Exception as exc:
            self._log("COMPANION_NO_TK", error=str(exc)[:300])
            return

        identity = self.identity.get()
        sphere_size = max(96, min(int(identity.desktop_avatar_size or 108), 150))

        # Режим масштаба Tk не навязываем. Прежняя версия переводила поток сферы в
        # «физический» режим до создания окна — но Tk на Windows изначально живёт в
        # логических координатах. Окно ставилось по физической высоте экрана
        # (например, 1407 при высоте 1800), а расставлялось по логической (1200) —
        # и оказывалось ниже нижнего края. Сфера существовала, но её не было видно.
        # Теперь режим остаётся таким, каким его застал процесс, а под масштаб мы
        # подстраиваемся по тому, что Windows реально применяет к окну — ниже.
        dpi_mode = "process-default"

        try:
            root = tk.Tk()
        except Exception as exc:
            self._log("COMPANION_WINDOW_FAILED", error=str(exc)[:300], dpi_mode=dpi_mode)
            return
        # Коэффициент масштаба: 1.0 при 100%, 2.0 при 200%. Всё рисуется в базовых
        # размерах и растягивается на него — так сфера одинаковой величины на
        # любом экране, а не уменьшается вдвое на ноутбуке.
        # Сначала сделать окно безрамочным и служебным — и только потом замерять.
        # Замер масштаба создаёт окно (update_idletasks). Раньше это происходило
        # ДО overrideredirect, и Windows успевала завести окно как обычное — с
        # кнопкой на панели задач и значком Tk, пером. Сделать окно безрамочным
        # после этого кнопку уже не убирает. Служебное окно (toolwindow) Windows
        # не показывает ни на панели задач, ни в Alt+Tab.
        try:
            root.overrideredirect(True)
            root.attributes("-toolwindow", True)
        except Exception:
            pass
        ui_scale = _window_scale(root)
        base_w, base_h = 430, 214
        win_w, win_h = int(round(base_w * ui_scale)), int(round(base_h * ui_scale))
        self._log("COMPANION_DISPLAY",
                  dpi_mode=dpi_mode, scale=round(ui_scale, 2),
                  screen_w=root.winfo_screenwidth(), screen_h=root.winfo_screenheight(),
                  window_w=win_w, window_h=win_h)
        root.title(CANONICAL_ASSISTANT_NAME)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", min(1.0, max(.74, identity.desktop_avatar_opacity)))
            root.wm_attributes("-transparentcolor", "#010204")
        except Exception:
            pass
        root.configure(bg="#010204")
        canvas = tk.Canvas(root, width=win_w, height=win_h, bg="#010204", highlightthickness=0, bd=0)
        canvas.pack()
        screen_h = root.winfo_screenheight()
        # Начальное место — левый нижний угол рабочей области, над панелью задач.
        area = _work_area_at(0, 0)
        if area is not None:
            anchor = {"x": area[0] + 18, "y": max(area[1] + 18, area[3] - win_h - 24)}
        else:
            anchor = {"x": 18, "y": max(18, screen_h - win_h - 72)}
        root.geometry(f"{win_w}x{win_h}+{anchor['x']}+{anchor['y']}")
        self._log("COMPANION_PLACED", x=anchor["x"], y=anchor["y"], w=win_w, h=win_h,
                  screen_h=screen_h, scale=round(ui_scale, 2))

        texture = None
        try:
            image_path = Path(__file__).resolve().parent / "web" / "eirven-orb.png"
            scaled_size = max(1, int(round(sphere_size * ui_scale)))
            image = Image.open(image_path).convert("RGBA")
            # Глаза — как в окне Эрви. В самой картинке их нет: в окне их рисуют
            # стили поверх, поэтому сфера на рабочем столе была без глаз. Рисуем в
            # полном размере картинки и только потом уменьшаем — так блики не рассыпаются.
            try:
                from .orb_eyes import add_eyes
                image = add_eyes(image)
            except Exception as exc:
                self._log("COMPANION_EYES_FAILED", error=str(exc)[:200])
            image = image.resize((scaled_size, scaled_size), Image.LANCZOS)
            texture = ImageTk.PhotoImage(image)
        except Exception:
            texture = None

        # Картинки эмоций. Сфера в них занимает около трёх четвертей кадра — остальное
        # аура, светящийся круг и «z Z», — поэтому кадр чуть крупнее прежней сферы,
        # чтобы сам шар остался того же размера. Глаза на них не рисуем: лицо у каждой
        # эмоции своё. Не нашлось картинок — показываем прежнюю сферу с глазами.
        emotion_textures: dict[str, Any] = {}
        try:
            emo_dir = Path(__file__).resolve().parent / "web" / "emotions"
            emo_size = max(1, int(round(sphere_size * ui_scale / 0.75)))
            for emo_name in ("joy", "surprise", "thinking", "focused", "laugh", "sleep"):
                # Версии для рабочего стола: окно сферы прозрачно только по одному
                # цвету, полупрозрачность Tk не умеет — любой полупрозрачный пиксель
                # смешивался с почти чёрным фоном окна, и за сферой вставало чёрное
                # пятно. В desk/ фон снаружи контура полностью прозрачен, а всё внутри,
                # включая тёмную середину сферы, непрозрачно.
                emo_path = emo_dir / "desk" / f"{emo_name}.png"
                if not emo_path.is_file():
                    emo_path = emo_dir / f"{emo_name}.png"
                if emo_path.is_file():
                    emo_img = Image.open(emo_path).convert("RGBA").resize((emo_size, emo_size), Image.LANCZOS)
                    # Растягивание сглаживает края и снова рождает полупрозрачные
                    # пиксели — а их Tk смешал бы с чёрным фоном окна. Края строго:
                    # либо видно, либо нет.
                    emo_img.putalpha(emo_img.getchannel("A").point(lambda v: 255 if v >= 128 else 0))
                    emotion_textures[emo_name] = ImageTk.PhotoImage(emo_img)
            self._log("COMPANION_EMOTIONS", loaded=sorted(emotion_textures))
        except Exception as exc:
            self._log("COMPANION_EMOTIONS_FAILED", error=str(exc)[:200])

        phase = 0.0
        dragging = {"x": 0, "y": 0, "moved": False}
        cached_status: dict[str, Any] = {}
        last_status_at = 0.0
        comment = ""
        comment_until = 0.0
        last_comment = ""

        def open_ui(_event=None) -> None:
            if not dragging["moved"]:
                self._open_fullscreen_ui()

        def down(event) -> None:
            dragging.update(x=event.x_root, y=event.y_root, moved=False)

        def move(event) -> None:
            dx, dy = event.x_root - dragging["x"], event.y_root - dragging["y"]
            if abs(dx) + abs(dy) > 3:
                dragging["moved"] = True
            dragging["x"], dragging["y"] = event.x_root, event.y_root
            anchor["x"] += dx
            anchor["y"] += dy
            # Граница — рабочая область монитора под курсором: экран без панели
            # задач. Прежняя версия расширяла границу до положения мыши, чтобы сфера
            # не упиралась в середину экрана, — но по вертикали мышь уходит на панель
            # задач, и сфера уезжала за неё. Рабочая область решает обе задачи сразу.
            area = _work_area_at(event.x_root, event.y_root)
            if area is not None:
                left, top, right, bottom = area
                anchor["x"] = max(left, min(anchor["x"], right - win_w))
                anchor["y"] = max(top, min(anchor["y"], bottom - win_h))
            else:
                self._clamp_anchor(anchor, win_w, win_h, root.winfo_screenwidth(), root.winfo_screenheight())
            root.geometry(f"+{anchor['x']}+{anchor['y']}")

        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<B1-Motion>", move)
        canvas.bind("<ButtonRelease-1>", open_ui)

        def rounded_rect(x1, y1, x2, y2, r=18, **kwargs):
            points = [
                x1+r,y1, x2-r,y1, x2,y1, x2,y1+r,
                x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
                x1,y2, x1,y2-r, x1,y1+r, x1,y1,
            ]
            return canvas.create_polygon(points, smooth=True, splinesteps=20, **kwargs)

        def animate() -> None:
            nonlocal phase, cached_status, last_status_at, comment, comment_until, last_comment
            try:
                if not root.winfo_exists():
                    return
            except Exception:
                return
            phase += 0.055
            now = time.monotonic()
            if now - last_status_at >= 0.18:
                cached_status = self._status()
                last_status_at = now
                candidate = self._human_comment(cached_status)
                if candidate and candidate != last_comment:
                    comment = candidate
                    last_comment = candidate
                    comment_until = now + 5.4
                elif candidate:
                    comment = candidate
                    comment_until = max(comment_until, now + .45)

            status = cached_status
            input_level = max(0.0, min(1.0, float(status.get("input_level") or 0.0)))
            speaking = bool(status.get("speaking"))
            state = str(status.get("state") or "")
            active = bool(status.get("session_active")) or state in {"onboarding", "armed", "hearing", "recognizing", "thinking"} or speaking
            try:
                motion_enabled = bool(self.db.get_setting("sphere_motion", True))
                intensity = str(self.db.get_setting("sphere_intensity", "vivid") or "vivid")
            except Exception:
                motion_enabled, intensity = True, "vivid"
            intensity_factor = {"soft": 0.72, "balanced": 0.92, "vivid": 1.12}.get(intensity, 1.0)
            activity = max(input_level, 0.72 if active else 0.08, 0.64 if speaking else 0.0) * intensity_factor

            canvas.delete("all")
            cx = 82.0
            cy = 112.0
            pulse = (math.sin(phase * (1.35 + activity * 2.4)) + 1) / 2

            # Окно больше не двигается само. Раньше оно каждый кадр сдвигалось на
            # ±4 пикселя, округлённые до целого, — на таких малых смещениях
            # округление давало видимые скачки по пикселю. Сфера остаётся на
            # месте и живёт пульсацией и переливами внутри холста.

            # Keep the desktop sprite truly background-free.  A blurred, partially
            # transparent halo is composited against Tk's chroma-key colour on Windows
            # and becomes the large dark disk that used to sit behind the mini sphere.
            current_tex = emotion_textures.get(self._emotion_now()) or texture
            if current_tex is not None:
                canvas.create_image(cx, cy, image=current_tex)
            else:
                core = sphere_size * (.38 + .018 * math.sin(phase * 1.2) + activity * .04)
                canvas.create_oval(cx-core, cy-core, cx+core, cy+core, fill="#13265a", outline="#8cf6ff", width=2)

            if comment and now <= comment_until:
                x1, y1, x2, y2 = 155, 38, 416, 124
                # Layered glass: soft depth, tinted volume, inner edge and a live
                # specular highlight.  Tk has no per-widget backdrop filter, so the
                # layers intentionally mimic the same optical hierarchy as the web UI.
                rounded_rect(x1+4, y1+7, x2+3, y2+8, 22, fill="#030611", outline="#030611")
                rounded_rect(x1, y1, x2, y2, 21, fill="#091126", outline="#6d9ed2", width=2)
                rounded_rect(x1+2, y1+2, x2-2, y2-2, 19, fill="#111d3d", outline="#856ed1", width=1)
                rounded_rect(x1+5, y1+5, x2-5, y1+25, 14, fill="#17294d", outline="")
                canvas.create_line(x1+22, y1+5, x2-28, y1+5, fill="#9eddf2", width=1)
                canvas.create_polygon([156,88,139,99,158,103], fill="#111d3d", outline="#856ed1")
                live_r = 3.5 + pulse * 1.2
                canvas.create_oval(177-live_r, 57-live_r, 177+live_r, 57+live_r, fill="#78efff", outline="#d8ffff")
                canvas.create_text(190, 50, anchor="nw", text=CANONICAL_ASSISTANT_NAME, fill="#8ceeff", font=("Segoe UI", 8, "bold"))
                canvas.create_text(174, 70, anchor="nw", text=comment, fill="#f4f7ff", width=int(221 * ui_scale),
                                   font=("Segoe UI", 10), justify="left")

            # Всё нарисовано в базовых размерах — растягиваем на масштаб экрана.
            # Картинка и шрифты уже подогнаны: картинка построена в нужном размере,
            # а шрифты в пунктах масштабируются сами в режиме с учётом масштаба.
            if ui_scale != 1.0:
                canvas.scale("all", 0, 0, ui_scale, ui_scale)
            root.after(32, animate)

        with self._lock:
            self._root = root
            self._visible = True
        animate()
        try:
            root.mainloop()
        finally:
            with self._lock:
                self._root = None
                self._visible = False

    def show(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.deiconify)
                self._visible = True
            except Exception:
                pass

    def hide(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.withdraw)
                self._visible = False
            except Exception:
                pass

    def stop(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "visible": self._visible,
            "type": "canonical_orb",
        }
