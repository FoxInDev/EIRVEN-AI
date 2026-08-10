from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
DEFAULT_PORT = 7860


def _env_wants_full_access() -> bool:
    env_path = APP_ROOT / ".env"
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if raw.strip().upper().startswith("EIRVEN_FULL_ACCESS="):
                return raw.split("=", 1)[1].strip().lower() in {"1", "true", "yes", "on", "да"}
    except Exception:
        pass
    return True


def _is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _request_elevation() -> bool:
    """Ask for one Windows UAC consent before starting the local core.

    EIRVEN stays an interactive user process (not a Windows service), so the elevated
    core can still see and control the owner's desktop, including elevated windows.
    If the owner declines UAC, the launcher continues in standard mode instead of
    making the application unusable.
    """
    if os.name != "nt" or not _env_wants_full_access() or _is_admin():
        return False
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            executable = str(Path(sys.executable).resolve())
            params = ""
        else:
            executable = str(Path(sys.executable).resolve())
            params = subprocess.list2cmdline([str(Path(__file__).resolve())])
        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, str(APP_ROOT), 1)
        if int(code) > 32:
            return True
    except Exception:
        pass
    os.environ["EIRVEN_FULL_ACCESS_DENIED"] = "1"
    return False


def _installed_python() -> Path | None:
    candidates = [APP_ROOT / ".venv" / "Scripts" / "pythonw.exe", APP_ROOT / ".venv" / "Scripts" / "python.exe"]
    return next((item for item in candidates if item.exists()), None)


def _direct_ping(port: int, timeout: float = 1.0) -> bool:
    """Direct localhost probe that never goes through VPN/system proxies."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/api/ping", headers={"Connection": "close"})
        response = conn.getresponse()
        body = response.read(700).decode("utf-8", errors="replace").lower()
        conn.close()
        return response.status == 200 and "eirven" in body and '"ok"' in body
    except Exception:
        return False


def _port_used(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _runtime_port() -> int | None:
    path = APP_ROOT / "logs" / "runtime_port"
    try:
        value = int(path.read_text(encoding="ascii").strip())
        return value if 1024 <= value <= 65535 else None
    except Exception:
        return None


def _find_existing_eirven() -> int | None:
    preferred = []
    runtime = _runtime_port()
    if runtime:
        preferred.append(runtime)
    preferred.extend(port for port in range(DEFAULT_PORT, DEFAULT_PORT + 12) if port not in preferred)
    for port in preferred:
        if _direct_ping(port):
            return port
    return None


def _choose_free_port() -> int:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 30):
        if not _port_used(port):
            return port
    raise RuntimeError("Не найден свободный локальный порт для EIRVEN")


def _run_installer() -> int:
    script = APP_ROOT / "scripts" / "ensure_runtime.ps1"
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=APP_ROOT)


def _start_server(python: Path, port: int) -> subprocess.Popen:
    logs = APP_ROOT / "logs"; logs.mkdir(exist_ok=True)
    (logs / "runtime_port").write_text(str(port), encoding="ascii")
    log = (logs / "supervisor.log").open("a", encoding="utf-8")
    env = {
        **os.environ,
        "EIRVEN_ROOT_DIR": str(APP_ROOT),
        "EIRVEN_OPEN_BROWSER": "false",
        "EIRVEN_PORT": str(port),
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    return subprocess.Popen([str(python), "-m", "eirven_ai.supervisor"], cwd=APP_ROOT, env=env, stdout=log, stderr=log, creationflags=flags)


def _show_orb(port: int) -> None:
    try:
        import urllib.request
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/companion/show", data=b"", method="POST")
        urllib.request.urlopen(request, timeout=1.0).read()
    except Exception:
        pass


class LauncherWindow:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.root = tk.Tk(); self.root.title("EIRVEN AI"); self.root.geometry("590x292"); self.root.resizable(False, False); self.root.configure(bg="#060817")
        try:
            self.root.iconbitmap(str(APP_ROOT / "assets" / "eirven.ico"))
        except Exception:
            pass
        tk.Label(self.root, text="E I R V E N", font=("Segoe UI", 10), fg="#dce8ff", bg="#060817").place(x=24, y=18)
        card = tk.Frame(self.root, bg="#0a1027", highlightbackground="#344a82", highlightthickness=1)
        card.place(x=20, y=48, width=550, height=218)
        self.orb = tk.Canvas(card, width=170, height=170, bg="#0a1027", highlightthickness=0)
        self.orb.place(x=13, y=22)
        self._phase = 0.0
        self._orb_texture = None
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageTk
            image = Image.open(APP_ROOT / "assets" / "eirven.png").convert("RGBA")
            pad = int(min(image.size) * .075)
            image = image.crop((pad, pad, image.width - pad, image.height - pad)).resize((145, 145), Image.LANCZOS)
            alpha = Image.new("L", image.size, 0)
            ImageDraw.Draw(alpha).ellipse((2, 2, 143, 143), fill=255)
            image.putalpha(alpha.filter(ImageFilter.GaussianBlur(1.2)))
            self._orb_texture = ImageTk.PhotoImage(image)
        except Exception:
            pass
        self._animate_orb()
        self.status = tk.Label(card, text="Запускаю EIRVEN…", font=("Segoe UI", 17, "bold"), fg="#f4f8ff", bg="#0a1027", anchor="w"); self.status.place(x=190, y=46, width=330)
        self.details = tk.Label(card, text="Проверяю локальный сервис", font=("Segoe UI", 10), fg="#96a5c9", bg="#0a1027", anchor="w", justify="left", wraplength=320); self.details.place(x=190, y=86, width=330, height=46)
        style=ttk.Style(); style.theme_use("clam"); style.configure("Launcher.Horizontal.TProgressbar", troughcolor="#161d3a", background="#63eaff", bordercolor="#26355f", lightcolor="#63eaff", darkcolor="#ef79d9", thickness=11)
        self.progress=ttk.Progressbar(card, mode="indeterminate", length=318, style="Launcher.Horizontal.TProgressbar"); self.progress.place(x=190, y=150); self.progress.start(10)
        tk.Label(card, text="Локально • Claude Code + Ollama • без API", font=("Segoe UI", 8), fg="#61739b", bg="#0a1027", anchor="w").place(x=190, y=177, width=320)

    def _animate_orb(self) -> None:
        import math
        self._phase += .06
        self.orb.delete("all")
        cx = cy = 85
        pulse = (math.sin(self._phase * 1.25) + 1) / 2
        for index in range(7, 0, -1):
            radius = 60 + index * 2.4 + pulse * index * .5
            color = ("#273b85", "#31529f", "#4872bf", "#5b8bd3", "#7257d2", "#9b55cf", "#d16dbb")[index-1]
            self.orb.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline=color, width=1)
        if self._orb_texture is not None:
            self.orb.create_image(cx, cy, image=self._orb_texture)
        blink = math.sin(self._phase * .52) > .986
        for ex in (cx-16, cx+16):
            if blink:
                self.orb.create_line(ex-5, cy-9, ex, cy-8.5, ex+5, cy-9, smooth=True, fill="#e9fbff", width=2)
            else:
                self.orb.create_oval(ex-7, cy-16, ex+7, cy-2, fill="#38288e", outline="#8beeff", width=2)
                self.orb.create_oval(ex-5, cy-14, ex+5, cy-4, fill="#1b1458", outline="#746cff", width=1)
                self.orb.create_oval(ex-4.5, cy-13.5, ex-1.5, cy-10.5, fill="#ffffff", outline="")
                self.orb.create_oval(ex+1.5, cy-7, ex+3, cy-5.5, fill="#bff9ff", outline="")
        self.orb.create_arc(cx-7, cy+2, cx+7, cy+12, start=205, extent=130, style="arc", outline="#edfbff", width=2)
        self.root.after(34, self._animate_orb)

    def set(self, status: str, details: str = "") -> None:
        self.root.after(0, lambda: self.status.config(text=status)); self.root.after(0, lambda: self.details.config(text=details))

    def done(self) -> None:
        self.root.after(0, self.root.destroy)

    def fail(self, message: str) -> None:
        def show():
            self.progress.stop(); self.status.config(text="Нужна помощь"); self.details.config(text=message[:220], fg="#ff8b9a")
        self.root.after(0, show)

    def worker(self) -> None:
        first_install = False
        try:
            port = _find_existing_eirven()
            if port:
                self.set("EIRVEN уже работает", "Показываю сферу. Интерфейс откроется по клику.")
                _show_orb(port); time.sleep(.25); self.done(); return

            python = _installed_python()
            # r23 intentionally has a new marker.  Extracting this release over an older
            # v1.2.2 install must run the idempotent upgrade once so the newly named EXE
            # is rebuilt with the new PE icon instead of keeping a cached purple binary.
            marker = APP_ROOT / ".installed-v1.6.1-r29"
            if python is None or not marker.exists():
                first_install = True
                self.set("Первый запуск", "Устанавливаю недостающие компоненты.")
                code = _run_installer()
                if code != 0:
                    raise RuntimeError("Установка не завершилась. Открой logs/install.log — уже установленные компоненты сохранены.")
                python = _installed_python()
                if python is None:
                    raise RuntimeError("Не найдено окружение Python после установки")

            port = _find_existing_eirven() or _choose_free_port()
            self.set("Запускаю локальный сервер", f"127.0.0.1:{port}")
            process = _start_server(python, port)
            started = time.monotonic()
            hard_deadline = started + 600
            slow_notice = False
            while time.monotonic() < hard_deadline:
                if _direct_ping(port, timeout=.8):
                    self.set("Готово", "Эйрвен запускается.")
                    _show_orb(port)
                    if first_install:
                        try:
                            webbrowser.open(f"http://127.0.0.1:{port}/ui/?welcome=1")
                        except Exception:
                            pass
                    time.sleep(.3); self.done(); return
                if process.poll() is not None:
                    # Supervisor may have intentionally exited because another instance won the race.
                    existing = _find_existing_eirven()
                    if existing:
                        _show_orb(existing); self.done(); return
                    raise RuntimeError("Сервер завершился. EIRVEN сохранил причину в logs/server.log")
                elapsed = time.monotonic() - started
                if elapsed > 90 and not slow_notice:
                    slow_notice = True
                    self.set("Первый запуск занимает дольше обычного", "Сервер жив. Жду и не переустанавливаю компоненты.")
                time.sleep(.45)
            raise RuntimeError("Сервер не поднялся за 10 минут. Проверь logs/server.log; повторная установка не требуется.")
        except Exception as exc:
            self.fail(str(exc))

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start(); self.root.mainloop()


if __name__ == "__main__":
    if not _request_elevation():
        LauncherWindow().run()
