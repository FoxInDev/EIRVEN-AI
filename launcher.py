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
CURRENT_BUILD = "r37-mobile-clean"


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


def _autostart_requested(argv: list[str] | None = None) -> bool:
    return "--autostart" in (sys.argv[1:] if argv is None else argv)


def _windows_platform() -> bool:
    return os.name == "nt"


def _request_elevation(*, autostart: bool = False) -> bool:
    """Ask for one Windows UAC consent before starting the local core.

    EIRVEN stays an interactive user process (not a Windows service), so the elevated
    core can still see and control the owner's desktop, including elevated windows.
    If the owner declines UAC, the launcher continues in standard mode instead of
    making the application unusable.
    """
    # Windows logon must not stall behind a UAC prompt.  The interactive desktop
    # launcher can still request full access, while the Startup shortcut deliberately
    # boots the local voice/orb runtime with the owner's normal desktop token.
    if autostart or os.name != "nt" or not _env_wants_full_access() or _is_admin():
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


def _repair_existing_autostart() -> bool:
    """Migrate an already-enabled r29 Startup shortcut to the quiet launcher."""
    if not _windows_platform():
        return False
    try:
        appdata = os.environ.get("APPDATA", "").strip()
        if not appdata:
            return False
        shortcut = (
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "EIRVEN AI.lnk"
        )
        marker = APP_ROOT / "data" / ".autostart-quiet-launcher"
        if marker.is_file():
            return True
        script = APP_ROOT / "scripts" / "install_autostart.ps1"
        if not shortcut.is_file() or not script.is_file():
            return False
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(APP_ROOT), capture_output=True, text=True, timeout=20,
            creationflags=flags,
        )
        if result.returncode != 0:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("quiet-launcher-v1", encoding="ascii")
        return True
    except Exception:
        return False


def _installed_python() -> Path | None:
    candidates = [APP_ROOT / ".venv" / "Scripts" / "pythonw.exe", APP_ROOT / ".venv" / "Scripts" / "python.exe"]
    return next((item for item in candidates if item.exists()), None)


def _runtime_imports_ready(python: Path) -> bool:
    """Cheap sanity check before repairing a missing release marker.

    A partial venv from a failed first install must not be mistaken for a complete
    installation merely because python.exe exists.
    """
    try:
        exe = python
        if exe.name.casefold() == "pythonw.exe":
            console = exe.with_name("python.exe")
            if console.is_file():
                exe = console
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        result = subprocess.run(
            [str(exe), "-c", "import eirven_ai,fastapi,uvicorn,httpx,pydantic"],
            cwd=str(APP_ROOT), capture_output=True, timeout=20, creationflags=flags,
        )
        return result.returncode == 0
    except Exception:
        return False


def _write_mobile_network_status(port: int, ready: bool, detail: str) -> None:
    """Expose the launch-time firewall result to the desktop phone panel."""
    try:
        logs = APP_ROOT / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / "mobile_network.json"
        pending = path.with_suffix(".tmp")
        pending.write_text(
            json.dumps(
                {
                    "port": int(port),
                    "firewall_ready": bool(ready),
                    "detail": str(detail or "").strip(),
                    "updated_at": int(time.time()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pending.replace(path)
    except Exception:
        pass


def _ensure_mobile_firewall(python: Path, port: int) -> bool:
    """Allow only this EIRVEN runtime/port from the directly connected subnet.

    The rule applies to every Windows network category because Windows defaults new
    home Wi-Fi networks to Public.  `LocalSubnet`, the dedicated venv executable and
    the exact runtime port keep the exception narrower than the normal Windows
    "allow Python" dialog, while the API still requires the mobile pairing token.
    """
    if os.name != "nt":
        _write_mobile_network_status(port, True, "Локальная сеть готова.")
        return True
    program = str(Path(python).resolve())
    env = {
        **os.environ,
        "EIRVEN_FIREWALL_PROGRAM": program,
        "EIRVEN_FIREWALL_PORT": str(int(port)),
    }
    script = r"""
$ErrorActionPreference = 'Stop'
$port = [int]$env:EIRVEN_FIREWALL_PORT
$name = "EIRVEN Mobile LAN ($port)"
$existing = @(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)
foreach ($rule in $existing) {
    $portFilter = $rule | Get-NetFirewallPortFilter
    $addressFilter = $rule | Get-NetFirewallAddressFilter
    if ($rule.Enabled -eq 'True' -and $rule.Action -eq 'Allow' -and
        [string]$portFilter.Protocol -eq 'TCP' -and
        [string]$portFilter.LocalPort -eq [string]$port -and
        [string]$addressFilter.RemoteAddress -match 'LocalSubnet') {
        [Console]::Out.Write('EIRVEN-FIREWALL-READY')
        exit 0
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    [Console]::Error.Write('UAC_REQUIRED')
    exit 5
}
$existing | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $name -Group 'EIRVEN' -Direction Inbound `
    -Action Allow -Enabled True -Profile Any -Protocol TCP -LocalPort $port `
    -RemoteAddress LocalSubnet | Out-Null
[Console]::Out.Write('EIRVEN-FIREWALL-READY')
"""
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-Command", script,
            ],
            cwd=str(APP_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, creationflags=flags,
        )
        ready = result.returncode == 0 and "EIRVEN-FIREWALL-READY" in result.stdout
        if ready:
            detail = (
                f"Windows Firewall разрешает EIRVEN на порту {port} "
                "только устройствам этого локального сегмента."
            )
        elif result.returncode == 5 or "UAC_REQUIRED" in result.stderr:
            detail = (
                "Windows Firewall пока блокирует телефон. Закрой EIRVEN, запусти "
                "ярлык ещё раз и подтверди системный запрос UAC."
            )
        else:
            reason = " ".join(str(result.stderr or "").split())[:180]
            detail = "Не удалось настроить Windows Firewall. " + (
                reason or "Перезапусти EIRVEN через ярлык с подтверждением UAC."
            )
    except Exception as exc:
        ready = False
        detail = f"Не удалось проверить Windows Firewall: {exc}"
    _write_mobile_network_status(port, ready, detail)
    return ready


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


def _running_build(port: int) -> str:
    """Read the active build directly, bypassing browser/system proxies."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        conn.request("GET", "/api/preferences", headers={"Connection": "close"})
        response = conn.getresponse()
        value = json.loads(response.read(16_000).decode("utf-8", errors="replace"))
        conn.close()
        return str(value.get("build") or "").strip() if response.status == 200 else ""
    except Exception:
        return ""


def _stop_outdated_runtime(port: int) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        conn.request("POST", "/api/system/shutdown", body=b"", headers={"Connection": "close"})
        response = conn.getresponse()
        response.read(2_000)
        conn.close()
        if response.status not in {200, 202, 204}:
            return False
    except Exception:
        return False
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if not _direct_ping(port, timeout=.35):
            return True
        time.sleep(.2)
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
        # A stale user/system environment variable must never silently turn the
        # phone server back into localhost-only mode.  The HTTP guard still exposes
        # only the token-scoped mobile surface to private-LAN clients.
        "EIRVEN_HOST": "0.0.0.0",
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
    def __init__(self, *, quiet: bool = False) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.quiet = bool(quiet)
        self.root = tk.Tk(); self.root.title("EIRVEN AI"); self.root.geometry("590x292"); self.root.resizable(False, False); self.root.configure(bg="#060817")
        if self.quiet:
            self.root.withdraw()
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
            if self.quiet:
                self.root.deiconify(); self.root.lift()
            self.progress.stop(); self.status.config(text="Нужна помощь"); self.details.config(text=message[:220], fg="#ff8b9a")
        self.root.after(0, show)

    def worker(self) -> None:
        first_install = False
        try:
            port = _find_existing_eirven()
            active_build = _running_build(port) if port else ""
            if port and active_build and active_build != CURRENT_BUILD:
                self.set("Обновляю EIRVEN", f"Завершаю предыдущую сборку {active_build}")
                if not _stop_outdated_runtime(port):
                    raise RuntimeError(
                        "Предыдущая версия ещё работает. Отключи её в настройках или "
                        "перезагрузи Windows, затем снова запусти установку."
                    )
                port = None
            if port:
                python = _installed_python()
                if python is not None:
                    self.set("Проверяю доступ с телефона", f"Локальная сеть · порт {port}")
                    _ensure_mobile_firewall(python, port)
                self.set("EIRVEN уже работает", "Показываю сферу. Интерфейс откроется по клику.")
                _show_orb(port); time.sleep(.25); self.done(); return

            python = _installed_python()
            # Each release marker runs the idempotent upgrade once.
            marker = APP_ROOT / ".installed-v1.7.3-r37"
            if python is not None and not marker.exists() and _runtime_imports_ready(python):
                try:
                    marker.write_text("launcher-repaired-marker", encoding="utf-8")
                except Exception:
                    pass
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
            self.set("Настраиваю доступ с телефона", f"Локальная сеть · порт {port}")
            firewall_ready = _ensure_mobile_firewall(python, port)
            detail = f"127.0.0.1:{port}"
            if not firewall_ready:
                detail += " · телефону может мешать Windows Firewall"
            self.set("Запускаю локальный сервер", detail)
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
    autostart = _autostart_requested()
    _repair_existing_autostart()
    if not _request_elevation(autostart=autostart):
        LauncherWindow(quiet=autostart).run()
