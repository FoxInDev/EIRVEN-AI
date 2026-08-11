from __future__ import annotations

import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .identity import IdentityService
from .system_browser import open_url as open_system_url


class DesktopCompanion:
    """Living always-on-top EIRVEN sphere with expressive eyes and human activity cards."""

    def __init__(self, db: Database, identity: IdentityService, ui_url: str):
        self.db = db
        self.identity = identity
        self.ui_url = ui_url
        self._thread: threading.Thread | None = None
        self._root = None
        self._lock = threading.RLock()
        self._visible = False
        self._status_provider: Callable[[], dict[str, Any]] | None = None

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
            self._thread = threading.Thread(target=self._run, daemon=True, name="eirven-orb")
            self._thread.start()

    def _open_fullscreen_ui(self) -> None:
        open_system_url(self.ui_url)

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
            return "Готово. Всё получилось."
        if task_step:
            return self._pretty_action(task_step, task_step)
        return "Всё спокойно. Я рядом."

    @staticmethod
    def _eye_mode(status: dict[str, Any]) -> str:
        expressive = {
            "amused", "sad", "empathetic", "curious", "concerned", "proud",
            "tired", "warm", "calm", "energetic", "quiet", "strict",
        }
        state = str(status.get("state") or "")
        if state in {"armed", "hearing"}:
            return "listening"
        if state in {"recognizing", "thinking"}:
            return "thinking"
        if bool(status.get("speaking")):
            emotion = str(status.get("speaking_emotion") or status.get("response_emotion") or "")
            return emotion if emotion in expressive else "speaking"
        recent = str(status.get("response_emotion") or "")
        try:
            age = float(status.get("emotion_age_seconds"))
        except (TypeError, ValueError):
            age = 999.0
        if recent in expressive and age <= 3.5:
            return recent
        mood = status.get("mood") if isinstance(status.get("mood"), dict) else {}
        mood_emotion = str(mood.get("emotion") or "")
        try:
            mood_strength = float(mood.get("strength") or 0.0)
        except (TypeError, ValueError):
            mood_strength = 0.0
        if mood_emotion in expressive and mood_strength >= .34:
            return mood_emotion
        task = status.get("active_task") if isinstance(status.get("active_task"), dict) else {}
        if str(task.get("status") or "").casefold() in {"done", "completed", "success"}:
            return "happy"
        return "idle"

    def _run(self) -> None:
        try:
            import tkinter as tk
            from PIL import Image, ImageTk
        except Exception:
            return

        identity = self.identity.get()
        sphere_size = max(96, min(int(identity.desktop_avatar_size or 108), 150))
        win_w, win_h = 430, 214
        try:
            root = tk.Tk()
        except Exception:
            return
        root.title(identity.assistant_name)
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
        anchor = {"x": 18, "y": max(18, screen_h - win_h - 72)}
        root.geometry(f"{win_w}x{win_h}+{anchor['x']}+{anchor['y']}")

        texture = None
        try:
            image_path = Path(__file__).resolve().parent / "web" / "eirven-orb.png"
            image = Image.open(image_path).convert("RGBA").resize((sphere_size, sphere_size), Image.LANCZOS)
            texture = ImageTk.PhotoImage(image)
        except Exception:
            texture = None

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

        def _gem_eye(ex: float, ey: float, width: float, height: float, *, blink: bool, glow: str, curious: float = 0.0) -> None:
            """Draw a tiny jewel-like eye from the reference, never a human eyeball."""
            if blink:
                canvas.create_line(ex-width*.48, ey, ex, ey+1.1, ex+width*.48, ey, smooth=True, fill=glow, width=5, capstyle="round")
                canvas.create_line(ex-width*.48, ey, ex, ey+1.1, ex+width*.48, ey, smooth=True, fill="#ecfbff", width=2, capstyle="round")
                return
            for grow, color, outline in ((3.2, "#372071", glow), (1.5, "#5846bb", "#a3edff"), (0.0, "#18205e", "#e4fbff")):
                canvas.create_oval(
                    ex-width/2-grow, ey-height/2-grow, ex+width/2+grow, ey+height/2+grow,
                    fill=color, outline=outline, width=1,
                )
            dot = max(1.4, width * .12)
            canvas.create_oval(ex-width*.26-dot/2, ey-height*.28-dot/2, ex-width*.26+dot/2, ey-height*.28+dot/2, fill="#ffffff", outline="")
            canvas.create_oval(ex+width*.15-dot*.28, ey+height*.14-dot*.28, ex+width*.15+dot*.28, ey+height*.14+dot*.28, fill="#aef8ff", outline="")

        def draw_eyes(cx: float, cy: float, mode: str, activity: float) -> None:
            try:
                if not bool(self.db.get_setting("desktop_eyes_enabled", True)):
                    return
            except Exception:
                pass
            blink = math.sin(phase * .88) > .986
            y = cy - sphere_size * .105 + math.sin(phase * .16) * .65
            spread = sphere_size * .145
            presets = {
                "idle":(13.0,15.0,"#78dfff"), "listening":(14.2,16.2,"#55edff"),
                "thinking":(13.0,14.5,"#9e75ff"), "speaking":(13.8,15.6,"#6beaff"),
                "happy":(14.0,13.0,"#ff8fe3"), "amused":(14.2,13.0,"#ff7bd8"),
                "sad":(12.8,14.0,"#7698ff"), "empathetic":(13.5,14.5,"#8cc7ff"),
                "curious":(13.6,15.2,"#65f2e6"), "concerned":(12.8,14.2,"#ff9b88"),
                "proud":(14.0,14.0,"#cf83ff"), "tired":(13.0,10.5,"#8791c6"),
                "warm":(13.8,14.5,"#ff9fcf"), "calm":(13.4,14.5,"#70d9ff"),
                "energetic":(14.8,16.0,"#58f2ff"), "quiet":(12.6,12.0,"#8179c7"),
                "strict":(13.0,12.5,"#ff8a9f"),
            }
            width, height, glow = presets.get(mode, presets["idle"])
            talk = math.sin(phase * 1.35) * .5 if mode in {"speaking", "amused", "energetic"} else 0.0
            _gem_eye(cx-spread, y-talk, width*(1.08 if mode == "curious" else 1), height, blink=blink, glow=glow, curious=.05 if mode == "curious" else 0)
            _gem_eye(cx+spread, y+talk, width*(.94 if mode == "curious" else 1), height, blink=blink, glow=glow)

            speech = max(0.0, min(1.0, float(cached_status.get("speech_level") or 0.0)))
            mouth_y = cy + sphere_size * .09
            mouth_w = sphere_size * (.055 + speech * .035)
            if mode == "speaking" and speech > .03:
                mouth_h = sphere_size * (.018 + speech * .07)
                canvas.create_oval(cx-mouth_w/2-2, mouth_y-mouth_h/2-2, cx+mouth_w/2+2, mouth_y+mouth_h/2+2, fill="#5a226f", outline="#64eaff", width=2)
                canvas.create_arc(cx-mouth_w*.34, mouth_y, cx+mouth_w*.34, mouth_y+mouth_h*.34, start=200, extent=140, style="arc", outline="#ffb8ed", width=1)
            else:
                smile = 3.0 if mode in {"happy", "amused", "warm", "proud"} else 1.0
                canvas.create_arc(cx-mouth_w/2, mouth_y-smile, cx+mouth_w/2, mouth_y+5+smile, start=200, extent=140, style="arc", outline="#e9fbff", width=2)

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
            cx = 82 + (math.sin(phase * .73) * 5.2 if motion_enabled else 0.0)
            cy = 112 + (math.cos(phase * .61) * 5.2 if motion_enabled else 0.0)
            pulse = (math.sin(phase * (1.35 + activity * 2.4)) + 1) / 2

            if motion_enabled and not dragging["moved"]:
                dx = int(round(math.sin(phase * .16) * 4))
                dy = int(round(math.cos(phase * .13) * 3))
                root.geometry(f"+{anchor['x'] + dx}+{anchor['y'] + dy}")

            # Keep the desktop sprite truly background-free.  A blurred, partially
            # transparent halo is composited against Tk's chroma-key colour on Windows
            # and becomes the large dark disk that used to sit behind the mini sphere.
            if texture is not None:
                canvas.create_image(cx, cy, image=texture)
            else:
                core = sphere_size * (.38 + .018 * math.sin(phase * 1.2) + activity * .04)
                canvas.create_oval(cx-core, cy-core, cx+core, cy+core, fill="#13265a", outline="#8cf6ff", width=2)

            # The glass texture stays expression-free; the live face is a controllable
            # layer that can blink and follow syllabic speech energy.
            draw_eyes(cx, cy, self._eye_mode(status), activity)

            if comment and now <= comment_until:
                x1, y1, x2, y2 = 155, 38, 416, 124
                # Liquid-glass style card: layered borders, tiny live indicator and hierarchy.
                rounded_rect(x1, y1, x2, y2, 21, fill="#0b1025", outline="#36477f", width=2)
                rounded_rect(x1+2, y1+2, x2-2, y2-2, 19, fill="#111a38", outline="#7658bb", width=1)
                canvas.create_polygon([156,88,139,99,158,103], fill="#111a38", outline="#7658bb")
                canvas.create_oval(174, 54, 181, 61, fill="#7cf1ff", outline="")
                canvas.create_text(190, 50, anchor="nw", text=str(self.identity.get().assistant_name or "Эйрвен")[:24], fill="#8ceeff", font=("Segoe UI", 8, "bold"))
                canvas.create_text(174, 70, anchor="nw", text=comment, fill="#f4f7ff", width=221,
                                   font=("Segoe UI", 10), justify="left")

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
            "type": "living_orb_eyes",
            "eyes_enabled": bool(self.db.get_setting("desktop_eyes_enabled", True)),
        }
