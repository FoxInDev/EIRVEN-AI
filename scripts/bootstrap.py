# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TRACE_LOCK = threading.Lock()


def _trace(event: str, **fields: object) -> None:
    """Записать строку установки в loggg2.txt — тот же файл, что у лончера и сервера.

    Установщик раньше писал только в logs/install.log, а при сбое человек видел
    на экране обрывок. Теперь каждый шаг и каждая ошибка с полным выводом команды
    попадают в общий журнал — его одного достаточно, чтобы разобрать проблему.
    """
    try:
        import json as _json
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mono": round(time.monotonic(), 3),
               "event": str(event), "source": "installer", **fields}
        text = _json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        path = ROOT / "loggg2.txt"
        with _TRACE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 12_000_000:
                backup = path.with_suffix(".prev.txt")
                try:
                    backup.unlink(missing_ok=True)
                    path.replace(backup)
                except Exception:
                    pass
            with path.open("a", encoding="utf-8") as out:
                out.write(text)
    except Exception:
        pass

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from eirven_ai.hardware import detect_hardware  # noqa: E402


def _windows_exe_file_version(path: Path) -> str:
    if os.name != "nt" or not path.is_file():
        return ""
    try:
        command = (
            "$p=$args[0];"
            "$v=(Get-Item -LiteralPath $p).VersionInfo.FileVersion;"
            "[Console]::Out.Write($v)"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command, str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _enable_windows_glass(root, tint: int = 0x000503) -> None:
    """Keep only a dark native title bar; Tk paints an entirely opaque window.

    SetWindowCompositionAttribute is deliberately avoided. On affected Windows/GPU
    combinations it can make otherwise valid Tk labels and controls disappear.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        root.update_idletasks()
        child = int(root.winfo_id())
        hwnd = int(ctypes.windll.user32.GetParent(child) or child)
        dark = ctypes.c_int(1)
        for attribute in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)
                )
                break
            except Exception:
                continue
        try:
            caption = ctypes.c_uint(int(tint) & 0x00FFFFFF)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
            )
        except Exception:
            pass
    except Exception:
        return


class InstallerError(RuntimeError):
    pass


def _find_ollama_executable() -> str:
    found = shutil.which("ollama") or shutil.which("ollama.exe")
    if found:
        return found
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", "")
        candidates = [
            Path(local) / "Programs" / "Ollama" / "ollama.exe" if local else Path(),
            Path(local) / "Ollama" / "ollama.exe" if local else Path(),
            Path(program_files) / "Ollama" / "ollama.exe" if program_files else Path(),
        ]
        for candidate in candidates:
            if candidate and candidate.is_file():
                directory = str(candidate.parent)
                current = os.environ.get("PATH", "")
                if directory.casefold() not in current.casefold():
                    os.environ["PATH"] = directory + os.pathsep + current
                return str(candidate)
    return ""


def _ollama_api_ready(timeout: float = 1.5) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:11434/api/version", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def _cpu_lacks_avx() -> bool:
    """True when the processor has no AVX support.

    Ollama's Windows builds require AVX. Pre-2011 AMD (K10/Phenom) and pre-2011
    Intel parts do not have it, so installation succeeds and the server then dies
    on an illegal instruction -- which surfaced only as a generic failure.
    """
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$s=(Get-CimInstance Win32_Processor).Name; "
             "$f=[bool](([System.Runtime.Intrinsics.X86.Avx]::IsSupported)); "
             "Write-Output $f"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        answer = (completed.stdout or "").strip().casefold()
        if answer in {"true", "false"}:
            return answer == "false"
    except Exception:
        pass
    return False


_OLLAMA_LAST_ERROR = ""


def _prepare_ollama_runtime() -> str:
    """Guarantee an installed and running local Ollama before model setup.

    ``ensure_runtime.ps1`` normally owns this step, but the bootstrap may be launched
    directly during a repair.  Re-running this helper is deliberately idempotent.
    """
    global _OLLAMA_LAST_ERROR
    _OLLAMA_LAST_ERROR = ""
    executable = _find_ollama_executable()
    if executable and _ollama_api_ready():
        return executable
    if os.name == "nt":
        script = ROOT / "scripts" / "ensure_ollama.ps1"
        if script.is_file():
            try:
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                     "-InstallIfMissing", "-StartServer"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1200,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if completed.returncode != 0:
                    # Keep the real reason instead of discarding it: a proxy, a
                    # blocked download and an unsupported CPU all looked identical
                    # from the outside before this.
                    _OLLAMA_LAST_ERROR = (
                        (completed.stderr or "").strip() or (completed.stdout or "").strip()
                    )[-800:]
            except Exception as exc:
                _OLLAMA_LAST_ERROR = str(exc)[:800]
    executable = _find_ollama_executable()
    if executable and not _ollama_api_ready():
        try:
            subprocess.Popen(
                [executable, "serve"], cwd=ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except Exception:
            pass
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if _ollama_api_ready(.8):
                break
            time.sleep(.75)
    return executable if executable and _ollama_api_ready() else ""


class Bootstrap:
    def __init__(self, gui: "InstallerGUI"):
        self.gui = gui
        self.venv = ROOT / ".venv"
        self.python = self.venv / "Scripts" / "python.exe"
        self.pythonw = self.venv / "Scripts" / "pythonw.exe"
        self.started = time.monotonic()
        self.total_units = 100.0
        self.done_units = 0.0
        self.model_plan: list[str] = []
        self.model_ready: set[str] = set()

    def update(self, message: str, units: float = 0, local_fraction: float = 0) -> None:
        fraction = min(0.99, (self.done_units + units * local_fraction) / self.total_units)
        # Keep the honest global percentage; model_progress owns the separate byte-level bar.
        self.gui.post("progress", fraction, message)

    def complete_step(self, units: float, message: str) -> None:
        self.done_units += units
        self.update(message)

    def _run_once(self, command: list[str], label: str, cwd: Path | None = None, timeout: int = 7200) -> str:
        self.gui.post("log", f"> {' '.join(command)}")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        child_env = os.environ.copy()
        # Python attached to a Windows pipe may otherwise emit cp1251 while the installer
        # reads UTF-8.  r28 turned a valid Russian Baya round-trip into replacement
        # characters and incorrectly rejected the selected voice.
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            command,
            cwd=cwd or ROOT,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        assert process.stdout is not None
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                raise InstallerError(f"Превышено время шага: {label}")
            line = process.stdout.readline()
            if line:
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                if clean:
                    lines.append(clean)
                    self.gui.post("log", clean[-300:])
            else:
                time.sleep(0.1)
        rest = process.stdout.read()
        if rest:
            lines.append(rest)
        if process.returncode != 0:
            raise InstallerError(f"{label} завершился с кодом {process.returncode}\n{''.join(lines)[-3000:]}")
        return "".join(lines)

    def ensure_ffmpeg(self) -> None:
        """Поставить FFmpeg для монтажа видео — надёжно и без риска сорвать установку.

        Раньше здесь стояла ОБЯЗАТЕЛЬНАЯ проверка: если в пакете imageio-ffmpeg не
        находился исполняемый файл, падала вся установка. А некоторые его версии в
        этот момент докачивают FFmpeg из интернета — при нестабильной сети проверка
        срывалась, и Эрви не ставилась вовсе из-за одной функции монтажа.

        Теперь FFmpeg кладётся в tools/ffmpeg/bin — туда, где монтаж ищет его первым
        делом. Источник основной — пакет imageio-ffmpeg, запасной — статическая сборка.
        Если не вышло ни то, ни другое, установка продолжается: без FFmpeg не будет
        работать только монтаж, а всё остальное запустится.
        """
        target_dir = ROOT / "tools" / "ffmpeg" / "bin"
        exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        target = target_dir / exe_name
        if target.is_file() and target.stat().st_size > 1_000_000:
            self.gui.post("log", "FFmpeg уже на месте")
            return

        # 1. Из установленного пакета imageio-ffmpeg — без отдельной загрузки.
        try:
            out = subprocess.run(
                [str(self.python), "-c",
                 "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
                capture_output=True, text=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            found = Path(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else None
            if found and found.is_file():
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(found, target)
                self.gui.post("log", "FFmpeg установлен")
                return
        except Exception as exc:
            self.gui.post("log", f"FFmpeg из пакета недоступен: {exc}")

        # 2. Запасной источник: статическая сборка для Windows. Несколько попыток,
        #    потому что именно сеть здесь чаще всего и подводит.
        if os.name != "nt":
            self.gui.post("log", "FFmpeg не найден — монтаж видео будет недоступен")
            return
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        archive = ROOT / "tools" / "ffmpeg-download.zip"
        for attempt in range(1, 4):
            try:
                archive.parent.mkdir(parents=True, exist_ok=True)
                self.gui.post("log", f"Скачиваю FFmpeg (попытка {attempt} из 3)")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=120) as response, open(archive, "wb") as fh:
                    shutil.copyfileobj(response, fh)
                import zipfile
                with zipfile.ZipFile(archive) as zf:
                    member = next((m for m in zf.namelist() if m.endswith("/bin/ffmpeg.exe")), None)
                    if not member:
                        raise RuntimeError("в архиве нет ffmpeg.exe")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                self.gui.post("log", "FFmpeg установлен")
                return
            except Exception as exc:
                self.gui.post("log", f"FFmpeg: попытка {attempt} не удалась ({exc})")
                time.sleep(3 * attempt)
            finally:
                try:
                    archive.unlink()
                except OSError:
                    pass
        # Не удалось — но установка идёт дальше. Монтаж подскажет, что FFmpeg нет.
        self.gui.post("log", "FFmpeg не удалось скачать — монтаж видео будет недоступен, остальное работает")

    def run(self, command: list[str], label: str, cwd: Path | None = None, timeout: int = 7200, attempts: int = 3) -> str:
        """Run a setup command with bounded automatic recovery for transient failures."""
        last_error: Exception | None = None
        _started = time.monotonic()
        _trace("INSTALL_STEP_START", label=label, command=" ".join(str(c) for c in command)[:600])
        for attempt in range(1, max(1, attempts) + 1):
            try:
                result = self._run_once(command, label, cwd=cwd, timeout=timeout)
                _trace("INSTALL_STEP_OK", label=label, attempt=attempt,
                       seconds=round(time.monotonic() - _started, 1))
                return result
            except Exception as exc:
                last_error = exc
                _trace("INSTALL_STEP_ERROR", label=label, attempt=attempt, of=attempts,
                       error=str(exc)[-4000:], error_type=type(exc).__name__)
                if attempt >= attempts:
                    break
                delay = 2 if attempt == 1 else 6
                self.gui.post("retry", f"{label}: временная ошибка. Повторяю автоматически ({attempt + 1}/{attempts})…", delay)
                time.sleep(delay)
        assert last_error is not None
        text = str(last_error)
        if "_multiarray_umath" in text or "DLL load failed" in text:
            # NumPy's own message blames the environment without naming the cause.
            # On Windows this is nearly always the missing Visual C++ runtime its
            # wheels link against, which Python's installer does not provide.
            repaired = False
            if os.name == "nt":
                try:
                    self.gui.post("log", "Похоже, не хватает Visual C++ Runtime — устанавливаю его и повторяю…")
                    installer = Path(os.environ.get("TEMP", ".")) / "eirven-vc_redist.x64.exe"
                    urllib.request.urlretrieve(
                        "https://aka.ms/vs/17/release/vc_redist.x64.exe", installer,
                    )
                    completed = subprocess.run(
                        [str(installer), "/install", "/quiet", "/norestart"],
                        timeout=300, check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    installer.unlink(missing_ok=True)
                    repaired = completed.returncode in (0, 3010, 1638)
                except Exception:
                    repaired = False
            if repaired:
                try:
                    return self._run_once(command, label, cwd=cwd, timeout=timeout)
                except Exception as exc:
                    last_error = exc
            raise RuntimeError(
                "Не хватает системной библиотеки Visual C++ Runtime — без неё не "
                "запускается NumPy.\n\n"
                "Установи её вручную и нажми «Повторить»:\n"
                "https://aka.ms/vs/17/release/vc_redist.x64.exe\n\n"
                f"Исходная ошибка: {text[:400]}"
            ) from last_error
        raise last_error

    def model_list(self) -> set[str]:
        try:
            output = subprocess.check_output(
                ["ollama", "list"], text=True, encoding="utf-8", errors="replace", timeout=30
            )
            return {line.split()[0] for line in output.splitlines()[1:] if line.strip()}
        except Exception:
            return set()

    @staticmethod
    def _model_is_installed(model: str, installed: set[str]) -> bool:
        wanted = model.casefold()
        if any(item.casefold() == wanted for item in installed):
            return True
        return model.endswith(":latest") and any(
            item.split(":", 1)[0].casefold() == model.split(":", 1)[0].casefold()
            for item in installed
        )

    @staticmethod
    def _human_bytes(value: int | float) -> str:
        size = max(0.0, float(value or 0))
        units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        digits = 0 if index == 0 else 1 if size >= 10 else 2
        return f"{size:.{digits}f} {units[index]}"

    @classmethod
    def _human_rate(cls, value: float) -> str:
        if value <= 0:
            return "скорость: ожидаю данные"
        return f"{cls._human_bytes(value)}/с"

    @staticmethod
    def _pull_phase_ru(status: str) -> str:
        low = status.casefold().strip()
        if "manifest" in low and ("pull" in low or "retriev" in low):
            return "Получаю описание модели"
        if low.startswith("pulling") or "download" in low:
            return "Скачиваю слой модели"
        if "verifying" in low or "verify" in low:
            return "Проверяю целостность модели"
        if "writing manifest" in low:
            return "Сохраняю модель в Ollama"
        if "removing" in low:
            return "Завершаю установку модели"
        if low == "success":
            return "Модель скачана"
        return status.strip() or "Подключаюсь к источнику модели"

    def _post_model_progress(
        self,
        model: str,
        *,
        detail: str,
        phase: str,
        percent: float | None = None,
        completed: int = 0,
        total: int = 0,
        speed: float = 0.0,
        idle_seconds: int = 0,
        attempt: int = 0,
        attempts: int = 0,
        source: str = "official",
    ) -> None:
        try:
            index = self.model_plan.index(model) + 1
        except ValueError:
            index = 0
        self.gui.post(
            "model_progress",
            {
                "model": model,
                "index": index,
                "count": len(self.model_plan),
                "ready": len(self.model_ready),
                "detail": detail,
                "phase": phase,
                "percent": percent,
                "completed": int(completed),
                "total": int(total),
                "speed": float(speed),
                "idle_seconds": int(idle_seconds),
                "attempt": int(attempt),
                "attempts": int(attempts),
                "source": source,
            },
        )

    @staticmethod
    def _ollama_pull_reader(
        source_model: str,
        events: "queue.Queue[tuple[str, object]]",
        cancel: threading.Event,
        response_box: dict[str, object],
    ) -> None:
        """Read Ollama's documented NDJSON pull stream without parsing console art."""
        payload = json.dumps({"model": source_model, "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/pull",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson, application/json",
                "User-Agent": "EIRVEN-AI/1.9.4 model-downloader-v12",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=150) as response:
                response_box["response"] = response
                while not cancel.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    try:
                        packet = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        events.put(("text", raw.decode("utf-8", errors="replace")[-500:]))
                        continue
                    events.put(("packet", packet))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(1200).decode("utf-8", errors="replace")
            except Exception:
                body = ""
            events.put(("error", f"HTTP {exc.code}: {body or exc.reason}"))
        except Exception as exc:
            if not cancel.is_set():
                events.put(("error", str(exc)))
        finally:
            response_box.pop("response", None)
            events.put(("done", None))

    def _ollama_log_tail(self, lines: int = 30) -> str:
        if os.name != "nt":
            return ""
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            return ""
        path = Path(local) / "Ollama" / "server.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
        except Exception:
            return ""

    def _restart_ollama_server(self) -> None:
        """Restart only the Ollama runtime after a genuinely stalled model transfer."""
        self.gui.post("log", "Ollama: перезапускаю локальный сервис, скачанные части моделей сохраняются")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if os.name == "nt":
            for image in ("ollama.exe", "ollama app.exe"):
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", image],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                        creationflags=creationflags,
                    )
                except Exception:
                    pass
        else:
            try:
                subprocess.run(["pkill", "-f", "ollama serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            except Exception:
                pass
        time.sleep(2)
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            self.gui.post("log", f"Ollama: не удалось вручную запустить serve: {exc}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with opener.open("http://127.0.0.1:11434/api/version", timeout=3) as response:
                    if response.status == 200:
                        self.gui.post("log", "Ollama: локальный сервис снова готов")
                        return
            except Exception:
                time.sleep(1)
        raise InstallerError("Ollama не поднялась после автоматического перезапуска")

    def pull_model(self, model: str, unit_weight: float) -> None:
        """Pull a model through Ollama's NDJSON API with visible, resumable progress.

        The CLI paints progress with terminal control characters and can therefore leave a
        GUI installer showing only ``Скачиваю модель``.  The API exposes exact
        ``completed``/``total`` byte counters.  A watchdog is based only on those counters,
        while a one-second heartbeat tells the owner how long the manifest or next byte has
        been pending.  For gpt-oss the official registry is tried first; if it repeatedly
        stops at the manifest, a compatible Hugging Face GGUF is downloaded through Ollama
        and copied to the required local ``gpt-oss:20b`` tag without duplicating its blob.
        """
        installed = self.model_list()
        if self._model_is_installed(model, installed):
            self.model_ready.add(model)
            self.gui.post("log", f"Модель уже есть: {model}")
            self._post_model_progress(
                model,
                detail="Уже скачана полностью — повторная загрузка не нужна",
                phase="Готово",
                percent=100.0,
            )
            self.complete_step(unit_weight, f"Модель {model} готова")
            return

        if model == "gpt-oss:20b":
            try:
                free_gb = shutil.disk_usage(ROOT.anchor or ROOT).free / (1024**3)
                self.gui.post("log", f"gpt-oss:20b: свободно на диске {free_gb:.1f} ГБ")
                if free_gb < 16.0:
                    self.gui.post(
                        "log",
                        f"Внимание: для большой модели может не хватить места (свободно {free_gb:.1f} ГБ); пробую загрузку без пропуска модели",
                    )
            except Exception:
                pass

        self.gui.post("log", "EIRVEN model downloader v5: потоковый API, байты/объём/скорость и секундный heartbeat")
        self.update(f"Подготавливаю загрузку {model}", units=unit_weight, local_fraction=0.01)

        official_source = model
        fallback_source = "hf.co/unsloth/gpt-oss-20b-GGUF:UD-Q4_K_XL"
        if model == "gpt-oss:20b":
            # Do not make the owner wait through ten identical 5-minute manifest hangs.
            # Alternate registries after two official attempts, then periodically retry the
            # official source in case the outage was short-lived.
            sources = [
                official_source,
                official_source,
                fallback_source,
                fallback_source,
                official_source,
                fallback_source,
            ]
        else:
            sources = [official_source] * 5
        attempts = len(sources)
        last_error: Exception | None = None
        highest_fraction = 0.01

        for attempt, source_model in enumerate(sources, start=1):
            source_kind = "official" if source_model == official_source else "fallback"
            source_label = "каталог Ollama" if source_kind == "official" else "резервный GGUF-источник"
            self.gui.post(
                "log",
                f"Ollama API: {model}, попытка {attempt}/{attempts}, источник: {source_model}",
            )
            self._post_model_progress(
                model,
                detail=f"Подключаюсь: {source_label} • попытка {attempt}/{attempts}",
                phase="Подключение",
                percent=None,
                attempt=attempt,
                attempts=attempts,
                source=source_kind,
            )

            events: queue.Queue[tuple[str, object]] = queue.Queue()
            cancel = threading.Event()
            response_box: dict[str, object] = {}
            reader = threading.Thread(
                target=self._ollama_pull_reader,
                args=(source_model, events, cancel, response_box),
                daemon=True,
            )
            reader.start()

            attempt_started = time.monotonic()
            last_state_at = attempt_started
            last_byte_at = attempt_started
            last_ui_at = 0.0
            last_status = ""
            last_log_key = ""
            layers: dict[str, tuple[int, int]] = {}
            reader_done = False
            stream_error = ""
            success_seen = False
            first_counter_seen = False
            resumed_bytes = 0
            speed_anchor_at = attempt_started
            speed_anchor_bytes = 0
            smoothed_speed = 0.0

            try:
                while not reader_done:
                    kind = ""
                    payload: object = None
                    try:
                        kind, payload = events.get(timeout=0.5)
                    except queue.Empty:
                        pass

                    now = time.monotonic()
                    if kind == "done":
                        reader_done = True
                    elif kind == "error":
                        stream_error = str(payload or "неизвестная ошибка потока")
                    elif kind == "text":
                        clean = str(payload or "").strip()
                        if clean:
                            self.gui.post("log", f"{model}: неожиданный ответ API: {clean[-500:]}")
                    elif kind == "packet" and isinstance(payload, dict):
                        error = str(payload.get("error") or "").strip()
                        if error:
                            stream_error = error
                        status = str(payload.get("status") or "").strip()
                        if status and status != last_status:
                            last_status = status
                            last_state_at = now
                            self.gui.post("log", f"{model}: {status}")
                        if status.casefold() == "success":
                            success_seen = True

                        try:
                            completed_value = max(0, int(payload.get("completed") or 0))
                            total_value = max(0, int(payload.get("total") or 0))
                        except (TypeError, ValueError):
                            completed_value = total_value = 0
                        digest = str(payload.get("digest") or status or "layer-unknown")
                        if total_value > 0:
                            old_completed, old_total = layers.get(digest, (0, 0))
                            layers[digest] = (
                                max(old_completed, completed_value),
                                max(old_total, total_value),
                            )

                        completed = sum(item[0] for item in layers.values())
                        total = sum(item[1] for item in layers.values())
                        if total > 0 and completed > 0 and not first_counter_seen:
                            first_counter_seen = True
                            resumed_bytes = completed
                            speed_anchor_bytes = completed
                            speed_anchor_at = now
                            last_byte_at = now
                            if resumed_bytes > 0:
                                self.gui.post(
                                    "log",
                                    f"{model}: Ollama нашла уже скачанные части: {self._human_bytes(resumed_bytes)}",
                                )
                        if completed > speed_anchor_bytes:
                            delta_time = max(0.001, now - speed_anchor_at)
                            if delta_time >= 0.35:
                                instant = (completed - speed_anchor_bytes) / delta_time
                                smoothed_speed = instant if smoothed_speed <= 0 else smoothed_speed * 0.72 + instant * 0.28
                                speed_anchor_bytes = completed
                                speed_anchor_at = now
                            last_byte_at = now

                    completed = sum(item[0] for item in layers.values())
                    total = sum(item[1] for item in layers.values())
                    percent = max(0.0, min(100.0, completed * 100.0 / total)) if total > 0 else None
                    if percent is not None:
                        highest_fraction = max(highest_fraction, min(0.98, percent / 100.0))

                    low_status = last_status.casefold()
                    if "verifying" in low_status or "verify" in low_status:
                        timeout_seconds = 900
                        idle_for = now - last_state_at
                    elif total <= 0 or "manifest" in low_status:
                        timeout_seconds = 90
                        idle_for = now - last_state_at
                    else:
                        timeout_seconds = 240
                        idle_for = now - last_byte_at

                    if now - last_ui_at >= 1.0 or reader_done:
                        phase = self._pull_phase_ru(last_status)
                        idle_seconds = max(0, int(idle_for))
                        visible_speed = smoothed_speed if idle_seconds < 3 else 0.0
                        if total > 0:
                            detail_parts = [
                                f"{percent:.1f}%",
                                f"{self._human_bytes(completed)} из {self._human_bytes(total)}",
                                self._human_rate(visible_speed),
                            ]
                            if resumed_bytes > 0:
                                detail_parts.append(f"при старте уже было {self._human_bytes(resumed_bytes)}")
                            if idle_seconds >= 2:
                                remaining = max(0, timeout_seconds - idle_seconds)
                                detail_parts.append(f"новых байтов нет {idle_seconds} с; восстановление через {remaining} с")
                            else:
                                detail_parts.append("данные поступают")
                            detail_parts.append(f"попытка {attempt}/{attempts}")
                            detail = " • ".join(detail_parts)
                        else:
                            remaining = max(0, timeout_seconds - idle_seconds)
                            detail = (
                                f"{phase} • ожидание {idle_seconds} с • "
                                f"автовосстановление через {remaining} с • попытка {attempt}/{attempts}"
                            )
                        self._post_model_progress(
                            model,
                            detail=detail,
                            phase=phase,
                            percent=percent,
                            completed=completed,
                            total=total,
                            speed=visible_speed,
                            idle_seconds=idle_seconds,
                            attempt=attempt,
                            attempts=attempts,
                            source=source_kind,
                        )
                        self.update(
                            f"{model}: {detail}",
                            units=unit_weight,
                            local_fraction=highest_fraction,
                        )
                        log_key = f"{last_status}|{int(percent or -1)}|{completed // (128 * 1024 * 1024)}"
                        if total > 0 and log_key != last_log_key:
                            self.gui.post("log", f"{model}: {detail}")
                            last_log_key = log_key
                        last_ui_at = now

                    if stream_error:
                        raise InstallerError(f"{source_label}: {stream_error}")
                    if not reader_done and idle_for >= timeout_seconds:
                        tail = self._ollama_log_tail(40)
                        if tail:
                            self.gui.post("log", "Ollama server.log перед восстановлением:\n" + tail[-8000:])
                        if total > 0:
                            raise InstallerError(
                                f"{source_label}: счётчик байтов не менялся {int(idle_for)} секунд "
                                f"({self._human_bytes(completed)} из {self._human_bytes(total)})"
                            )
                        raise InstallerError(
                            f"{source_label}: манифест не получен за {int(idle_for)} секунд"
                        )

                if stream_error:
                    raise InstallerError(f"{source_label}: {stream_error}")
                if not success_seen:
                    raise InstallerError(f"{source_label}: поток завершился без статуса success")

                if source_model != model:
                    detail = "Загрузка из резервного источника завершена • создаю локальный тег gpt-oss:20b"
                    self._post_model_progress(
                        model,
                        detail=detail,
                        phase="Регистрирую модель",
                        percent=99.5,
                        attempt=attempt,
                        attempts=attempts,
                        source=source_kind,
                    )
                    self.run(
                        ["ollama", "cp", source_model, model],
                        f"Регистрация {model} после резервной загрузки",
                        timeout=180,
                        attempts=2,
                    )

                installed = self.model_list()
                if not self._model_is_installed(model, installed):
                    raise InstallerError(f"Ollama завершила загрузку, но модель {model} не появилась в списке")

                self.model_ready.add(model)
                self._post_model_progress(
                    model,
                    detail="Скачана полностью и проверена Ollama",
                    phase="Готово",
                    percent=100.0,
                    completed=total,
                    total=total,
                    attempt=attempt,
                    attempts=attempts,
                    source=source_kind,
                )
                self.complete_step(unit_weight, f"Модель {model} готова")
                return
            except Exception as exc:
                last_error = exc
                cancel.set()
                response = response_box.get("response")
                if response is not None:
                    try:
                        response.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                reader.join(timeout=2.0)

                if attempt < attempts:
                    try:
                        # Restart after each real failure so the next attempt inherits the
                        # current VPN/proxy/routes and does not reuse a poisoned pull state.
                        self._restart_ollama_server()
                    except Exception as restart_exc:
                        self.gui.post("log", f"Ollama restart: {restart_exc}")

                    next_source = sources[attempt]
                    changing_source = next_source != source_model
                    if changing_source and next_source == fallback_source:
                        message = (
                            f"{model}: каталог Ollama повторно не ответил. Переключаюсь на "
                            f"резервный совместимый GGUF; скачанные части не удаляю ({attempt + 1}/{attempts})…"
                        )
                    elif changing_source:
                        message = (
                            f"{model}: резервный источник временно не ответил. Ещё раз пробую "
                            f"официальный каталог; части сохранены ({attempt + 1}/{attempts})…"
                        )
                    else:
                        message = (
                            f"{model}: поток остановился. Переподключаюсь; уже скачанные части "
                            f"сохранены ({attempt + 1}/{attempts})…"
                        )
                    delay = 4 if attempt <= 2 else 8
                    self.gui.post("retry", message, delay)
                    time.sleep(delay)

        raise InstallerError(
            f"Не удалось полностью скачать обязательную модель {model}: {last_error}. "
            r"Испробованы каталог Ollama и резервный GGUF-источник. Проверьте "
            r"%LOCALAPPDATA%\Ollama\server.log; EIRVEN не пропускает обязательную модель."
        )

    @staticmethod
    def download(url: str, path: Path, on_progress, *, min_bytes: int = 1, attempts: int = 3) -> None:
        """Download atomically and retry transient network/CDN failures."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            temp.unlink(missing_ok=True)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "EIRVEN-AI/1.9.4", "Accept": "application/octet-stream"},
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response, temp.open("wb") as target:
                    total = int(response.headers.get("Content-Length") or 0)
                    copied = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        copied += len(chunk)
                        on_progress(copied / total if total else 0.0)
                if temp.stat().st_size < min_bytes:
                    raise InstallerError(f"Скачан неполный файл {path.name}: {temp.stat().st_size} байт")
                head = temp.read_bytes()[:256].lstrip().lower()
                if head.startswith(b"version https://git-lfs") or head.startswith(b"<html") or b"<!doctype html" in head:
                    raise InstallerError(f"Вместо {path.name} CDN вернул служебный текст/HTML")
                temp.replace(path)
                return
            except Exception as exc:
                last_error = exc
                temp.unlink(missing_ok=True)
                if attempt < attempts:
                    time.sleep(2 if attempt == 1 else 6)
        raise InstallerError(f"Не удалось скачать {path.name} после {attempts} попыток: {last_error}")

    @staticmethod
    def valid_piper(model_path: Path, config_path: Path) -> bool:
        try:
            if not model_path.is_file() or model_path.stat().st_size < 10_000_000:
                return False
            head = model_path.read_bytes()[:256].lstrip().lower()
            if head.startswith(b"version https://git-lfs") or head.startswith(b"<html") or b"<!doctype html" in head:
                return False
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
            return config_path.stat().st_size > 500 and bool(payload.get("audio")) and bool(payload.get("language"))
        except Exception:
            return False

    def runtime_validate_piper(self, model_path: Path) -> bool:
        """Parse the actual ONNX protobuf inside the freshly created venv."""
        if not self.python.exists() or not model_path.is_file():
            return False
        code = (
            "import onnxruntime as ort; "
            f"s=ort.InferenceSession({str(model_path)!r}, providers=['CPUExecutionProvider']); "
            "assert s.get_inputs(); print('onnx-ok')"
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            result = subprocess.run(
                [str(self.python), "-c", code], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", timeout=90, creationflags=creationflags,
            )
            if result.returncode != 0:
                self.gui.post("log", f"ONNX-проверка {model_path.name}: {result.stdout[-500:].strip()}")
            return result.returncode == 0
        except Exception as exc:
            self.gui.post("log", f"ONNX-проверка {model_path.name} не прошла: {exc}")
            return False

    def speech_roundtrip_quality(self, wav_path: Path) -> bool | None:
        """Use the already-installed local Russian ASR as an intelligibility gate.

        Returns True/False when GigaAM can run and None when the ASR model is not
        available yet (for example a temporary CDN failure during installation).
        """
        code = (
            "import re,onnx_asr; "
            "m=onnx_asr.load_model('gigaam-v3-e2e-ctc', quantization='int8'); "
            f"t=str(m.recognize({str(wav_path)!r}) or '').lower(); "
            "print('EIRVEN_TTS_TRANSCRIPT='+t.replace(chr(10),' '))"
        )
        try:
            output = self.run([str(self.python), "-c", code], "Проверка разборчивости русского голоса", timeout=1200)
        except Exception as exc:
            self.gui.post("log", f"ASR-проверка голоса пропущена: {exc}")
            return None
        marker = "EIRVEN_TTS_TRANSCRIPT="
        transcript = output.split(marker, 1)[-1].strip().casefold() if marker in output else ""
        normalized = re.sub(r"[^а-яёa-z0-9 ]+", " ", transcript)
        hits = sum(1 for token in ("привет", "русск", "голос", "эрви", "ирвен") if token in normalized)
        good = hits >= 2 or ("привет" in normalized and len(normalized.split()) >= 3)
        self.gui.post("log", f"Контрольная расшифровка TTS: {transcript[:180] or 'пусто'}")
        return good

    def validate_vision_model(self, model: str) -> bool:
        """Send a tiny real image to Ollama so installation cannot report vision ready
        when the tag/runtime cannot actually accept image input."""
        # Generate the probe image with Pillow instead of embedding a tiny hand-written
        # PNG. r8 accidentally shipped a PNG whose header was readable but whose IDAT
        # stream was truncated; Ollama correctly rejected it with
        # "Failed to load image or audio file" for every otherwise-working VLM.
        code = (
            "import base64,httpx,io; from PIL import Image,ImageDraw; "
            "im=Image.new('RGB',(96,64),(245,245,245)); "
            "d=ImageDraw.Draw(im); d.rectangle((8,8,52,52),fill=(25,120,220)); d.ellipse((60,16,88,44),fill=(220,80,60)); "
            "buf=io.BytesIO(); im.save(buf,format='JPEG',quality=92); raw=buf.getvalue(); "
            "Image.open(io.BytesIO(raw)).load(); img=base64.b64encode(raw).decode('ascii'); "
            f"m={model!r}; "
            "r=httpx.post('http://127.0.0.1:11434/api/chat',json={'model':m,'messages':[{'role':'user','content':'Ответь только OK, если изображение получено.','images':[img]}],'stream':False,'keep_alive':'2h','options':{'num_ctx':768,'num_predict':16,'temperature':0}},timeout=120,trust_env=False); "
            "safe=(r.text[:600] or '').encode('ascii','backslashreplace').decode('ascii'); print(r.status_code, safe); r.raise_for_status(); "
            "data=r.json(); assert data.get('done') is True and not data.get('error'), data; msg=data.get('message') or {}; print('vision-content', (msg.get('content') or msg.get('thinking') or '')[:80])"
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            child_env = os.environ.copy()
            child_env['PYTHONIOENCODING'] = 'utf-8'
            child_env['PYTHONUTF8'] = '1'
            result = subprocess.run(
                [str(self.python), '-c', code], cwd=ROOT, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=180, creationflags=creationflags, env=child_env,
            )
            if result.returncode != 0:
                self.gui.post('log', f'Vision-проверка {model}: {(result.stdout + result.stderr)[-700:]}')
            return result.returncode == 0
        except Exception as exc:
            self.gui.post('log', f'Vision-проверка {model} не прошла: {exc}')
            return False

    def benchmark_model(self, model: str) -> dict[str, float | str] | None:
        """Measure a warm local response on the target PC through Ollama's real API."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(payload: dict[str, object], timeout: float):
            request = urllib.request.Request(
                "http://127.0.0.1:11434/api/chat",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
            )
            return opener.open(request, timeout=timeout)

        try:
            # Load once without generating text; the benchmark below measures the steady
            # state users get because the selected model remains resident for two hours.
            with post(
                {"model": model, "messages": [], "stream": False, "keep_alive": "2h"},
                300,
            ) as response:
                response.read()

            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Ответь по-русски одной короткой фразой: система готова."}],
                "stream": True,
                "think": False,
                "keep_alive": "2h",
                "options": {"temperature": 0, "num_ctx": 1024, "num_predict": 48},
            }
            started = time.monotonic()
            first_content: float | None = None
            final: dict[str, object] = {}
            with post(payload, 120) as response:
                while True:
                    raw = response.readline()
                    if not raw:
                        break
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                    final = event
                    message = event.get("message") or {}
                    if isinstance(message, dict) and str(message.get("content") or "") and first_content is None:
                        first_content = time.monotonic() - started
                    if bool(event.get("done")):
                        break
            total = time.monotonic() - started
            eval_count = int(final.get("eval_count") or 0)
            eval_seconds = float(final.get("eval_duration") or 0) / 1_000_000_000
            if first_content is None or eval_count <= 0:
                raise RuntimeError("модель не вернула видимый текст")
            result: dict[str, float | str] = {
                "model": model,
                "first_content_seconds": round(first_content, 3),
                "total_seconds": round(total, 3),
                "tokens_per_second": round(eval_count / eval_seconds, 2) if eval_seconds > 0 else 0.0,
            }
            self.gui.post(
                "log",
                f"Замер {model}: первый текст {result['first_content_seconds']} с, "
                f"скорость {result['tokens_per_second']} токена/с",
            )
            return result
        except Exception as exc:
            self.gui.post("log", f"Замер {model} не прошёл: {exc}")
            return None

    @staticmethod
    def _benchmark_is_fast(result: dict[str, float | str] | None) -> bool:
        if not result:
            return False
        return (
            float(result.get("first_content_seconds") or 999) <= 5.0
            and float(result.get("tokens_per_second") or 0) >= 6.0
        )

    @staticmethod
    def _benchmark_score(result: dict[str, float | str] | None) -> float:
        if not result:
            return float("inf")
        first = float(result.get("first_content_seconds") or 999)
        rate = max(0.1, float(result.get("tokens_per_second") or 0))
        return first * 2.0 + 32.0 / rate

    def calibrate_adaptive_model(self, profile, installed: set[str]) -> set[str]:
        """Measure the selected adaptive route and keep installation resumable."""
        selected = str(profile.recommended_main_model)
        current = self.benchmark_model(selected)
        if self._benchmark_is_fast(current):
            self.gui.post("log", "Интеллект готов к интерактивной работе")
            return installed
        if current:
            self.gui.post("log", "Первый ответ на этом компьютере может быть медленнее целевого; продолжаю адаптацию.")
        else:
            self.gui.post("log", "Замер скорости недоступен; сохраняю безопасный профиль.")
        return installed

    def remove_legacy_agent_launchers(self) -> None:
        for name in ("Codex Local.cmd",):
            try:
                (ROOT / name).unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def deep_model_for(profile) -> str:
        # Fixed-quality release: deep answers use the exact same checkpoint.
        return profile.recommended_main_model

    @staticmethod
    def silero_model_path() -> Path:
        """Keep the large voice model outside the extracted source/update folder.

        On Windows this also prevents a reinstall/update/cleanup of the source tree from
        racing the TTS validation step. The per-user LocalAppData directory requires no UAC
        and survives moving or replacing the EIRVEN source folder.
        """
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "").strip()
            if local:
                return Path(local) / "EIRVEN" / "models" / "silero" / "v5_5_ru.pt"
        return ROOT / "models" / "silero" / "v5_5_ru.pt"

    def write_env(self, profile, piper_path: Path, tts_engine: str = "silero") -> None:
        env_path = ROOT / ".env"
        old: dict[str, str] = {}
        if env_path.exists():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in raw and not raw.lstrip().startswith("#"):
                    k, v = raw.split("=", 1); old[k.strip()] = v.strip()
        values = {
            "EIRVEN_HOST": "0.0.0.0",
            "EIRVEN_PORT": "7860",
            "EIRVEN_LLM_BACKEND": "ollama",
            "EIRVEN_OLLAMA_URL": "http://127.0.0.1:11434",
            "EIRVEN_FAST_MODEL": profile.recommended_fast_model,
            "EIRVEN_MODEL": profile.recommended_main_model,
            "EIRVEN_RELEASE_MODEL": profile.recommended_main_model,
            "EIRVEN_STRICT_RELEASE_MODEL": "true",
            "EIRVEN_CODE_MODEL": profile.recommended_code_model,
            "EIRVEN_DEEP_MODEL": self.deep_model_for(profile),
            "EIRVEN_VISION_MODEL": profile.recommended_vision_model,
            "EIRVEN_EMBEDDING_MODEL": "",
            "EIRVEN_KEEP_ALIVE": "2h",
            "EIRVEN_CHAT_NUM_CTX": "4096",
            "EIRVEN_TASK_NUM_CTX": "8192",
            "EIRVEN_CHAT_NUM_PREDICT": "384",
            "EIRVEN_LLM_FIRST_TOKEN_TIMEOUT": "120",
            "EIRVEN_LLM_INACTIVITY_TIMEOUT": "45",
            "EIRVEN_TASK_NUM_PREDICT": "2048",
            "EIRVEN_ENABLE_COMMANDS": "true",
            "EIRVEN_ENABLE_BROWSER": "true",
            "EIRVEN_ENABLE_DESKTOP_CONTROL": "true",
            "EIRVEN_FULL_ACCESS": "false",
            "EIRVEN_MAX_PARALLEL_TASKS": "2",
            "EIRVEN_AUTO_MEMORY": "true",
            "EIRVEN_AUTO_ROUTE": "true",
            # Lexical/personal memory works without keeping a second embedding model
            # resident. Advanced users can opt in later without slowing first install.
            "EIRVEN_SEMANTIC_MEMORY": old.get("EIRVEN_SEMANTIC_MEMORY", "false"),
            "EIRVEN_ASR_ENGINE": "gigaam",
            "EIRVEN_GIGAAM_MODEL": "gigaam-v3-e2e-ctc",
            "EIRVEN_WHISPER_MODEL": profile.recommended_whisper_model,
            "EIRVEN_WHISPER_DEVICE": "cpu",
            "EIRVEN_WHISPER_COMPUTE_TYPE": "int8",
            "EIRVEN_TTS_ENGINE": tts_engine,
            "EIRVEN_EXPRESSIVE_TTS_MODEL": "",
            "EIRVEN_EXPRESSIVE_TTS_DESIGN_MODEL": "",
            "EIRVEN_EXPRESSIVE_TTS_SPEAKER": "Serena",
            "EIRVEN_SILERO_MODEL": str(self.silero_model_path().resolve()).replace("\\", "/"),
            "EIRVEN_PIPER_MODEL": (str(piper_path).replace("\\", "/") if str(piper_path) not in {"", "."} and piper_path.exists() else ""),
            "EIRVEN_VOICE_SILENCE_MS": "520",
            "EIRVEN_TELEGRAM_ENABLED": old.get("EIRVEN_TELEGRAM_ENABLED", "false"),
            "EIRVEN_TELEGRAM_API_ID": old.get("EIRVEN_TELEGRAM_API_ID", "0"),
            "EIRVEN_TELEGRAM_API_HASH": old.get("EIRVEN_TELEGRAM_API_HASH", ""),
            "EIRVEN_TELEGRAM_PHONE": old.get("EIRVEN_TELEGRAM_PHONE", ""),
            "EIRVEN_COMPANION_ENABLED": old.get("EIRVEN_COMPANION_ENABLED", "true"),
            "EIRVEN_ENABLE_GAME_CONTROL": old.get("EIRVEN_ENABLE_GAME_CONTROL", "false"),
            "EIRVEN_COMFYUI_URL": old.get("EIRVEN_COMFYUI_URL", "http://127.0.0.1:8188"),
        }
        env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")

    def install_once(self) -> None:
        try:
            self.gui.post("log", f"Папка: {ROOT}")
            profile = detect_hardware()
            self.gui.post("log", f"Профиль компьютера: {json.dumps(profile.to_dict(), ensure_ascii=False)}")

            self.update("Создаю изолированное окружение", units=5, local_fraction=0.1)
            if not self.python.exists():
                self.run([sys.executable, "-m", "venv", str(self.venv)], "Создание окружения")
            self.complete_step(5, "Окружение готово")

            self.update("Устанавливаю ядро EIRVEN", units=20, local_fraction=0.05)
            self.run([str(self.python), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], "Обновление pip")
            # requirements-build.txt is only for producing the distributable EXE.
            # Installing PyInstaller on an end-user machine is unnecessary and used
            # to make a missing build-only file break first launch.
            for filename in ("requirements.txt", "requirements-voice.txt", "requirements-desktop.txt", "requirements-integrations.txt"):
                if filename == "requirements-desktop.txt":
                    # r8.x could leave both opencv-python and opencv-contrib-python in the
                    # same venv. They own the same cv2 namespace and upgrades do not
                    # automatically remove the obsolete package. Rebuild this tiny stack
                    # cleanly before installing the pinned camera/gesture pair.
                    self.run(
                        [str(self.python), "-m", "pip", "uninstall", "-y",
                         "opencv-python", "opencv-python-headless", "opencv-contrib-python",
                         "opencv-contrib-python-headless", "mediapipe"],
                        "Очистка старого OpenCV/MediaPipe", timeout=300,
                    )
                self.run([str(self.python), "-m", "pip", "install", "-r", str(ROOT / filename)], f"Установка {filename}")
            self.run([str(self.python), "-m", "pip", "install", "-e", str(ROOT)], "Установка EIRVEN")
            self.run([str(self.python), "-c", "import sounddevice,soundfile,numpy,cv2; print('voice/camera deps ok',cv2.__version__)"], "Проверка микрофона и лёгкой камеры", timeout=120)
            self.complete_step(20, "Ядро установлено")

            self.update("Подготавливаю системный браузер", units=8, local_fraction=0.1)
            # r14 no longer downloads a separate Testing Chromium. The real desktop agent
            # controls the owner's default browser/profile through Windows UI Automation.
            self.gui.post("log", "Отдельный Chromium не нужен: desktop-agent использует браузер Windows по умолчанию")
            self.complete_step(8, "Системный браузер готов")

            ollama_executable = _prepare_ollama_runtime()
            if not ollama_executable:
                if _cpu_lacks_avx():
                    raise InstallerError(
                        "Процессор этого компьютера не поддерживает инструкции AVX, "
                        "а они обязательны для Ollama — локальные модели на нём "
                        "запустить не получится.\n\n"
                        "Это ограничение железа, а не установки: повторный запуск "
                        "не поможет. Нужен компьютер с процессором 2011 года или новее."
                    )
                detail = _OLLAMA_LAST_ERROR.strip()
                raise InstallerError(
                    "Не удалось автоматически установить или запустить Ollama. "
                    "Повторный запуск EIRVEN продолжит установку с уже скачанных файлов."
                    + (f"\n\nПричина: {detail}" if detail else "")
                )
            try:
                ollama_version = subprocess.check_output(
                    [ollama_executable, "--version"], text=True, encoding="utf-8", errors="replace", timeout=20
                ).strip()
                self.gui.post("log", f"Ollama runtime: {ollama_version}")
                self.gui.post("log", "Ollama bootstrap: установка/запуск подтверждены, API 127.0.0.1:11434 готов")
            except Exception as exc:
                raise InstallerError(f"Ollama установлена, но проверка runtime не прошла: {exc}") from exc

            # A future release_ready manifest may replace the adaptive source model with
            # a verified EIRVEN checkpoint. This source archive has no trained weights,
            # so the fast hardware profile remains active.
            asset_installer = ROOT / "scripts" / "install_release_assets.py"
            if asset_installer.is_file():
                self.run([str(self.python), str(asset_installer), "--auto"], "Проверка официальных EIRVEN assets", timeout=7200)
                try:
                    release_manifest = json.loads((ROOT / "release_assets" / "eirven_release_manifest.json").read_text(encoding="utf-8"))
                    release_model = str(release_manifest.get("release_model") or "").strip()
                    release_gguf = ROOT / "models" / "eirven" / "eirven-8b-Q6_K.gguf"
                    if release_manifest.get("status") == "release_ready" and release_model and release_gguf.is_file():
                        profile.recommended_fast_model = release_model
                        profile.recommended_main_model = release_model
                        profile.recommended_code_model = release_model
                        os.environ["EIRVEN_RELEASE_MODEL"] = release_model
                        self.gui.post("log", f"Fixed-quality release model active: {release_model}; hardware={profile.runtime_mode}")
                except Exception as exc:
                    self.gui.post("log", f"Release assets manifest: {exc}")

            # r42: Anthropic Claude is not a local Ollama model. EIRVEN does not
            # auto-install or impersonate Claude; the local backend is Ollama directly.
            self.gui.post("log", "LLM backend: прямой локальный Ollama. Облачные LLM не устанавливаются и не используются.")

            if os.name == "nt" and profile.vram_gb and profile.vram_gb <= 10.0:
                # Persist conservative server defaults for the next Ollama service start.
                # EIRVEN also sends small per-request num_ctx values, so this is a second
                # guard rather than a prerequisite for the current install.
                for key, value in (("OLLAMA_MAX_LOADED_MODELS", "1"), ("OLLAMA_NUM_PARALLEL", str(profile.recommended_parallelism)), ("OLLAMA_CONTEXT_LENGTH", "8192")):
                    try:
                        subprocess.run(["setx", key, value], cwd=ROOT, capture_output=True, timeout=20, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
                    except Exception:
                        pass
                self.gui.post("log", "Профиль до 10 ГБ VRAM: одна резидентная модель, один параллельный запрос и контекст 8192 для низкой задержки")

            deep_model = self.deep_model_for(profile)
            model_candidates = [
                profile.recommended_fast_model,
                profile.recommended_main_model,
                profile.recommended_vision_model,
                profile.recommended_code_model,
                deep_model,
            ]
            # DeepSeek-Coder-V2 handles language/code/UI plans; Qwen3-VL is loaded
            # only for images. Deduplication prevents accidental duplicate pulls.
            models = []
            for item in model_candidates:
                if item not in models:
                    models.append(item)

            self.gui.post("log", "Быстрая память: без отдельной embedding-модели и без переключения весов")

            installed_before = self.model_list()
            self.model_plan = list(models)
            self.model_ready = {
                item for item in self.model_plan
                if self._model_is_installed(item, installed_before)
            }
            ready_names = ", ".join(item for item in self.model_plan if item in self.model_ready)
            ready_detail = f"Уже скачано полностью: {len(self.model_ready)} из {len(self.model_plan)}"
            if ready_names:
                ready_detail += f" ({ready_names})"
            self._post_model_progress(
                self.model_plan[0] if self.model_plan else "",
                detail=ready_detail,
                phase="Проверка установленных моделей",
                percent=(len(self.model_ready) * 100.0 / len(self.model_plan)) if self.model_plan else 100.0,
            )
            self.gui.post("log", ready_detail)

            model_weight = 42 / max(1, len(models))
            required_models = set(models)
            for model in models:
                self.pull_model(model, model_weight)

            installed_after = self.model_list()
            installed_after = self.calibrate_adaptive_model(profile, installed_after)
            required_models.add(profile.recommended_main_model)
            installed_after = self.model_list()
            # Vision/file-image understanding is a core feature, not an optional extra.
            # Validate the configured multimodal model with an actual image. If that tag
            # failed to download, reuse another already installed multimodal model.
            vision_candidates = []
            # Small-GPU installs must never "probe" a 4B/9B VLM just because it is
            # already installed. The probe itself can reserve tens of GB of CUDA arena
            # and leave Ollama/resource arbitration sluggish for later ASR/chat turns.
            raw_vision_candidates = (profile.recommended_vision_model,)
            for candidate in raw_vision_candidates:
                if candidate not in vision_candidates and candidate in installed_after:
                    vision_candidates.append(candidate)
            validated_vision = ""
            for candidate in vision_candidates:
                self.gui.post("log", f"Проверяю анализ изображений: {candidate}")
                if self.validate_vision_model(candidate):
                    validated_vision = candidate
                    break
            if not validated_vision:
                # One last attempt: the dedicated model might have been skipped due a
                # transient pull error above. Retry it now because image analysis is required.
                try:
                    self.pull_model(profile.recommended_vision_model, 0)
                    installed_after = self.model_list()
                except Exception as exc:
                    self.gui.post("log", f"Повторная загрузка vision-модели: {exc}")
                if profile.recommended_vision_model in installed_after and self.validate_vision_model(profile.recommended_vision_model):
                    validated_vision = profile.recommended_vision_model
            if not validated_vision:
                # r14 desktop automation no longer depends on a VLM. Keep installation
                # usable even when a low-memory GPU/model backend cannot process images;
                # UI Automation + terminal/files remain fully available and vision can be
                # repaired/downloaded later from diagnostics.
                self.gui.post("log", "Vision-модель сейчас не прошла проверку. Продолжаю: desktop-agent работает без неё через UI Automation.")
            else:
                profile.recommended_vision_model = validated_vision
                self.gui.post("log", f"Vision-контур готов: {validated_vision}")

            missing_required = sorted(m for m in required_models if m not in installed_after)
            if missing_required:
                raise InstallerError(
                    "Не все обязательные модели Ollama установлены: " + ", ".join(missing_required) + ". "
                    "Установщик не будет запускать EIRVEN с неполным набором моделей."
                )
            self.gui.post("log", "Модельный контур: Ollama напрямую.")
            self.remove_legacy_agent_launchers()

            # r28 incorrectly downloaded Piper Irina and presented it as Baya.  Baya is
            # a real Silero v5.5 RU speaker and is installed/validated below; no second
            # female voice is downloaded or silently substituted anymore.
            piper_path = Path()
            self.update("Подготавливаю голос Baya", units=7, local_fraction=0.03)

            self.update("Подготавливаю русское распознавание речи", units=7, local_fraction=0.05)
            # GigaAM v3 is the primary Russian ASR. Model download is intentionally
            # non-fatal: a VPN/CDN issue must not prevent EIRVEN from launching, and
            # faster-whisper remains an isolated CPU fallback.
            code = (
                "import onnx_asr; "
                "m=onnx_asr.load_model('gigaam-v3-e2e-ctc', quantization='int8'); "
                "print(type(m).__name__)"
            )
            try:
                self.run([str(self.python), "-c", code], "Загрузка GigaAM v3", timeout=7200)
                self.complete_step(7, "Русское распознавание речи готово")
            except Exception as exc:
                self.gui.post("log", f"GigaAM пока не прогрет: {exc}")
                self.gui.post("log", "EIRVEN запустится сразу; GigaAM повторит загрузку при первом голосовом запросе, а Whisper останется резервом.")
                self.complete_step(7, "Распознавание речи будет подготовлено при первом запуске")

            # The working Jarvis reference keeps speech as a separate backend. On a
            # CUDA machine we install Chatterbox Multilingual as the high-naturalness
            # Russian path; it is local and requires no API key. Installation is
            # opportunistic so CPU-only systems keep the fast Silero path.
            # Chatterbox is intentionally experimental. On the attached RTX 3070 Ti
            # installation its package replaced CUDA PyTorch with a CPU wheel, failed its
            # probe and later caused 10–17 second fallback roulette. The supported release
            # uses one verified local Baya timbre; developers may opt in explicitly.
            if os.getenv("EIRVEN_INSTALL_EXPERIMENTAL_CHATTERBOX", "0") == "1" and profile.cuda_available and profile.vram_gb >= 12:
                try:
                    self.run(
                        [str(self.python), "-m", "pip", "install", "--upgrade", "chatterbox-tts==0.1.7"],
                        "Установка естественного многоязычного голоса Chatterbox", timeout=7200,
                    )
                    chatterbox_probe = ROOT / "data" / "tts-probe-chatterbox.wav"
                    chatterbox_probe.parent.mkdir(parents=True, exist_ok=True)
                    chatterbox_smoke = (
                        "import torch,soundfile as sf; from chatterbox.mtl_tts import ChatterboxMultilingualTTS; "
                        "assert torch.cuda.is_available(); "
                        "import inspect; sig=inspect.signature(ChatterboxMultilingualTTS.from_pretrained); "
                        "m=(ChatterboxMultilingualTTS.from_pretrained(device='cuda', t3_model='v3') "
                        "if 't3_model' in sig.parameters else ChatterboxMultilingualTTS.from_pretrained(device='cuda')); "
                        "w=m.generate('Привет. Я Эрви, говорю по-русски ясно и спокойно.', "
                        "language_id='ru', exaggeration=0.5, cfg_weight=0.4); "
                        "assert w is not None and w.numel()>4000; "
                        f"sf.write({str(chatterbox_probe)!r}, w.squeeze().detach().float().cpu().numpy(), int(m.sr), subtype='PCM_16'); "
                        "print('chatterbox-ru-ok', m.sr, w.numel())"
                    )
                    self.run([str(self.python), "-c", chatterbox_smoke], "Проверка естественного русского голоса", timeout=7200)
                    quality = self.speech_roundtrip_quality(chatterbox_probe)
                    chatterbox_probe.unlink(missing_ok=True)
                    if quality is False:
                        raise InstallerError("Chatterbox создал аудио, но контрольная русская фраза не распознаётся")
                    self.gui.post("log", "Chatterbox Multilingual RU готов: локальный естественный голос без API")
                except Exception as exc:
                    self.gui.post("log", f"Chatterbox RU не активирован; использую быстрый русский Silero: {exc}")

            # Native Russian Silero V5.5 is the reliable low-latency TTS. It has
            # Russian-specific stress/homograph handling and writes its own 48 kHz WAV.
            try:
                self.run([str(self.python), "-c", "import torch; print(torch.__version__)"], "Проверка PyTorch для русского голоса", timeout=90)
            except Exception as exc:
                # PyTorch owns the selected local Baya speaker. Retry a clean wheel once;
                # if it still cannot load, installation must report the real problem
                # instead of running with a different hidden voice.
                self.gui.post("log", f"PyTorch пока недоступен, пробую чистую загрузку: {exc}")
                try:
                    self.run(
                        [str(self.python), "-m", "pip", "install", "--upgrade", "--no-cache-dir", "torch"],
                        "Установка PyTorch для русского голоса", timeout=7200,
                    )
                except Exception as install_exc:
                    raise InstallerError(f"Не удалось установить движок голоса Baya: {install_exc}") from install_exc
            silero_model = self.silero_model_path()
            silero_model.parent.mkdir(parents=True, exist_ok=True)
            silero_ready = False
            try:
                # r42: the 138 MB Baya package lives in per-user LocalAppData on Windows.
                # The previous build stored it inside the extracted Desktop/source folder;
                # a second process/update cleanup could remove it between the parent stat()
                # and the TTS child process. Validate it from the exact child interpreter too.
                if not silero_model.is_file() or silero_model.stat().st_size < 1_000_000:
                    self.gui.post("log", f"Скачиваю официальный Silero v5_5_ru.pt (голос Baya) → {silero_model.parent}…")
                    self.download(
                        "https://models.silero.ai/models/tts/ru/v5_5_ru.pt", silero_model,
                        lambda fraction: self.update("Скачиваю русский голос Silero V5.5", units=3, local_fraction=min(.95, fraction)),
                        min_bytes=1_000_000,
                    )

                verify_path = (
                    "from pathlib import Path; "
                    f"p=Path({str(silero_model)!r}); "
                    "assert p.is_file(), f'voice file missing: {p}'; "
                    "assert p.stat().st_size>1000000, f'voice file truncated: {p.stat().st_size}'; "
                    "print('silero-file-ok', p, p.stat().st_size)"
                )
                try:
                    self.run([str(self.python), "-c", verify_path], "Проверка файла Silero Baya", timeout=60, attempts=1)
                except Exception as first_verify_exc:
                    # Retry only this asset. Never restart the whole EIRVEN installation.
                    self.gui.post("log", f"Файл Baya исчез/повреждён после загрузки ({first_verify_exc}). Повторяю только голосовой файл…")
                    silero_model.unlink(missing_ok=True)
                    fallback = (
                        "import torch; from pathlib import Path; "
                        f"p=Path({str(silero_model)!r}); p.parent.mkdir(parents=True,exist_ok=True); "
                        "torch.hub.download_url_to_file('https://models.silero.ai/models/tts/ru/v5_5_ru.pt',str(p),progress=True); "
                        "assert p.is_file() and p.stat().st_size>1000000, p; print('silero-redownload-ok',p.stat().st_size)"
                    )
                    self.run([str(self.python), "-c", fallback], "Повторная загрузка только Silero Baya", timeout=1800, attempts=2)
                    self.run([str(self.python), "-c", verify_path], "Повторная проверка файла Silero Baya", timeout=60, attempts=1)

                self.gui.post("log", f"Silero Baya загружен и виден дочернему процессу: {silero_model.stat().st_size / (1024*1024):.1f} МБ")
                silero_probe = ROOT / "data" / "tts-probe-silero.wav"
                silero_probe.parent.mkdir(parents=True, exist_ok=True)
                smoke = (
                    "import torch,wave; from pathlib import Path; "
                    f"p=Path({str(silero_model)!r}); q={str(silero_probe)!r}; "
                    "assert p.is_file(), f'Silero vanished before load: {p}'; "
                    "m=torch.package.PackageImporter(str(p)).load_pickle('tts_models','model'); "
                    "m.save_wav(text='Привет. Проверка русского голоса Эрви.', speaker='baya', sample_rate=48000, audio_path=q); "
                    "w=wave.open(q,'rb'); assert w.getframerate()==48000 and w.getnchannels()==1 and w.getnframes()>4000; "
                    "print('silero-ru-ok',w.getframerate(),w.getnframes()); w.close()"
                )
                self.run([str(self.python), "-c", smoke], "Проверка русского голоса Silero", timeout=600, attempts=1)
                quality = self.speech_roundtrip_quality(silero_probe)
                silero_probe.unlink(missing_ok=True)
                if quality is False:
                    raise InstallerError("Голос Baya создал WAV, но не прошёл локальный контроль разборчивости")
                self.gui.post("log", "Silero Baya V5.5 RU готов: 48 кГц, русские ударения и омографы")
                # Stable per-speaker reference clips let Chatterbox preserve distinct voice
                # identities instead of using one default timbre for every UI choice.
                refs = ROOT / "models" / "voice_refs"
                refs.mkdir(parents=True, exist_ok=True)
                ref_code = (
                    "import torch; from pathlib import Path; "
                    f"m=torch.package.PackageImporter({str(silero_model)!r}).load_pickle('tts_models','model'); "
                    f"r=Path({str(refs)!r}); "
                    "text='Привет. Это контрольный образец голоса Эрви для естественной русской речи.'; "
                    "[(m.save_wav(text=text,speaker=sp,sample_rate=48000,audio_path=str(r/(name+'.wav')))) for name,sp in "
                    "[('baya','baya')]]; "
                    "print('voice-refs-ok')"
                )
                self.run([str(self.python), "-c", ref_code], "Создание разных голосовых профилей", timeout=600)
                silero_ready = True
            except Exception as exc:
                self.gui.post("log", f"Голос Baya не прошёл проверку: {exc}")

            if not silero_ready:
                raise InstallerError("Голос Baya не готов. Повторите установку после проверки сети и PyTorch.")
            self.complete_step(7, "Русский голос и офлайн-резерв готовы")

            self.gui.post("log", "Основной голос Эрви: локальная Бая")

            runtime_tts_engine = "silero"
            self.write_env(profile, piper_path, runtime_tts_engine)
            self.complete_step(3, "Настройки сохранены")

            self.update("Проверяю установку", units=6, local_fraction=0.05)
            # Developer pytest has already passed before the release archive is created.
            # Running the whole suite on an end-user Windows machine made VPN/antivirus
            # quirks look like a fatal install error. The installer performs only a local
            # import/compile smoke check; runtime diagnostics are available inside EIRVEN.
            self.run([str(self.python), "-c", "import sounddevice,soundfile,numpy,cv2; print('final audio/camera deps ok', cv2.__version__)"], "Финальная проверка аудио и камеры", timeout=120)
            self.ensure_ffmpeg()
            self.run([str(self.python), "-m", "compileall", "-q", "src"], "Проверка Python")
            self.run(
                [str(self.python), "-c", "import eirven_ai; from eirven_ai.app import app; print(eirven_ai.__version__)"],
                "Проверка ядра",
                timeout=120,
            )
            self.gui.post("log", "Ядро EIRVEN готово к запуску")
            self.complete_step(6, "Основные компоненты готовы")

            # The signed/release EXE is copied into ROOT by the outer launcher. End-user
            # machines must never install PyInstaller or rebuild the downloaded program.
            self.update("Завершаю установку Windows", units=2, local_fraction=0.1)
            installed_exe = ROOT / "EIRVEN.exe"
            exe_version = _windows_exe_file_version(installed_exe) if installed_exe.is_file() else ""
            if exe_version == "2.4.0.72":
                self.gui.post("log", "Готовый EIRVEN.exe установлен без локальной пересборки")
            else:
                # Not fatal. start_windows.bat and the shortcut both already fall back
                # to ".venv\\Scripts\\pythonw.exe launcher.py", and the core check above
                # just proved that import path works here. A correctly-versioned
                # EIRVEN.exe dropped in later takes over automatically.
                reason = "не найден" if not installed_exe.is_file() else f"версия {exe_version or 'не определена'} вместо 2.4.0.72"
                self.gui.post("log", f"EIRVEN.exe ({reason}) — запуск пойдёт напрямую через .venv\\Scripts\\pythonw.exe launcher.py")
            self.complete_step(2, "Приложение готово")
            self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "create_shortcut.ps1")], "Создание и проверка ярлыка", timeout=60)
            marker = ROOT / ".installed-v2.4.0-r72-k4"
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            try:
                self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "install_autostart.ps1")], "Автозапуск голосового EIRVEN", timeout=60)
            except Exception as exc:
                self.gui.post("log", f"Автозапуск не создан автоматически: {exc}")
            self.done_units = 100
            self.gui.post("progress", 1.0, "Установка проверена", 0)
            self.gui.post("done", None)
        except Exception:
            raise

    def install(self) -> None:
        """Run one linear install; never restart the whole pipeline automatically."""
        state_file = ROOT / "data" / "installer-recovery.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        self.done_units = 0.0
        try:
            state_file.write_text(json.dumps({"version": "2.4.0", "build": "r72-k4", "status": "running", "updated": time.time()}), encoding="utf-8")
            self.install_once()
            state_file.unlink(missing_ok=True)
        except Exception as exc:
            state_file.write_text(json.dumps({"version": "2.4.0", "build": "r72-k4", "status": "failed", "error": str(exc)[-2000:], "updated": time.time()}, ensure_ascii=False), encoding="utf-8")
            self.gui.post("error", str(exc))

    def launch(self) -> None:
        logs = ROOT / "logs"; logs.mkdir(exist_ok=True)
        log = (logs / "supervisor.log").open("a", encoding="utf-8")
        python = self.pythonw if self.pythonw.exists() else self.python
        env = {**os.environ, "EIRVEN_OPEN_BROWSER": "false", "EIRVEN_ROOT_DIR": str(ROOT)}
        subprocess.Popen([str(python), "-m", "eirven_ai.supervisor"], cwd=ROOT, stdout=log, stderr=log, env=env)
        # Autostart is voice-first: the orb appears, while the full UI opens only
        # when the owner clicks the orb. Do not launch a browser during installation.
        time.sleep(1.2)


class InstallerGUI:
    def __init__(self, *, preview: bool = False):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.preview = bool(preview)
        self.exit_code = 0
        self._log_lock = threading.RLock()
        self.log_path = ROOT / "logs" / "install.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            previous = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
            if previous and not self.preview:
                self.log_path.with_name("install.previous.log").write_text(previous[-250000:], encoding="utf-8")
            if not self.preview:
                self.log_path.write_text(
                    f"=== EIRVEN installer 2.4 r72 started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n",
                    encoding="utf-8",
                )
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("Установка Эрви")
        self.root.geometry("760x660")
        self.root.minsize(700, 610)
        self.root.resizable(True, True)
        self.root.configure(bg="#03050d")
        try:
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass
        _enable_windows_glass(self.root)
        try:
            self.root.iconbitmap(str(ROOT / "assets" / "eirven.ico"))
        except Exception:
            pass

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Eirven.Horizontal.TProgressbar",
            troughcolor="#11162b", background="#67e8ff", bordercolor="#11162b",
            lightcolor="#67e8ff", darkcolor="#986cff", thickness=13,
        )
        style.configure(
            "Eirven.Model.Horizontal.TProgressbar",
            troughcolor="#11162b", background="#a77cff", bordercolor="#11162b",
            lightcolor="#a77cff", darkcolor="#ff80d4", thickness=9,
        )

        tk.Label(
            self.root, text="У С Т А Н О В К А", font=("Segoe UI", 9, "bold"),
            fg="#7f90b3", bg="#03050d",
        ).pack(pady=(14, 0))
        self.orb = tk.Canvas(self.root, width=194, height=194, bg="#03050d", highlightthickness=0)
        self.orb.pack(pady=(0, 0))
        self._orb_phase = 0.0
        self._orb_texture = None
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageTk
            # The same Retina source is used by the website, Windows UI and Android.
            # The canvas adds only the two canonical gem eyes; no nested pupils exist.
            source = Image.open(ROOT / "src" / "eirven_ai" / "web" / "eirven-orb.png").convert("RGBA")
            pad = int(min(source.size) * .075)
            source = source.crop((pad, pad, source.width - pad, source.height - pad)).resize((174, 174), Image.LANCZOS)
            alpha = Image.new("L", source.size, 0)
            ImageDraw.Draw(alpha).ellipse((2, 2, 172, 172), fill=255)
            source.putalpha(alpha.filter(ImageFilter.GaussianBlur(1.4)))
            self._orb_texture = ImageTk.PhotoImage(source)
        except Exception:
            self._orb_texture = None
        self.animate_orb()

        tk.Label(
            self.root, text="Эрви готовится к первому запуску",
            font=("Segoe UI", 21, "bold"), fg="#f5f8ff", bg="#03050d",
        ).pack(pady=(0, 4))
        self.status = tk.Label(
            self.root, text="Подготавливаю всё необходимое…", font=("Segoe UI", 10),
            fg="#9aa9cb", bg="#03050d", wraplength=620, justify="center",
        )
        self.status.pack(pady=(0, 10))

        self.bar = ttk.Progressbar(self.root, maximum=100, length=468, style="Eirven.Horizontal.TProgressbar")
        self.bar.pack(padx=70, fill="x")
        self.percent = tk.Label(self.root, text="Общий прогресс: 0%", font=("Segoe UI", 14, "bold"), fg="#eafaff", bg="#03050d")
        self.percent.pack(pady=(6, 4))
        self.steps = tk.Label(
            self.root, text="● Подготовка  ·  Ядро  ·  Интеллект  ·  Голос  ·  Зрение  ·  Телефон  ·  Проверка  ·  Запуск",
            font=("Segoe UI", 8), fg="#7987b7", bg="#03050d", wraplength=700, justify="center",
        )
        self.steps.pack(pady=(0, 2))

        self.model_title = tk.Label(
            self.root, text="", font=("Segoe UI", 9, "bold"),
            fg="#c8c1ff", bg="#03050d", wraplength=620, justify="center",
        )
        self.model_bar = ttk.Progressbar(
            self.root, maximum=100, length=468, style="Eirven.Model.Horizontal.TProgressbar",
        )
        self.model_detail = tk.Label(
            self.root, text="",
            font=("Segoe UI", 8), fg="#8d96b5", bg="#03050d", wraplength=620, justify="center",
        )
        self._model_widgets_visible = False
        self._model_indeterminate = False
        self.note = tk.Label(
            self.root, text="Если что-то сорвётся, просто запусти EIRVEN снова — уже скачанное сохранится.",
            font=("Segoe UI", 8), fg="#61708f", bg="#03050d", wraplength=620, justify="center",
        )
        self.note.pack(pady=(12, 0))

        controls = tk.Frame(self.root, bg="#03050d")
        controls.pack(pady=(10, 0))
        self.log_button = tk.Button(
            controls, text="Показать журнал", command=self.open_log,
            font=("Segoe UI", 9), fg="#eafaff", bg="#171d35", activebackground="#222b4c",
            activeforeground="#ffffff", relief="flat", padx=14, pady=6, cursor="hand2",
        )
        self.log_button.pack(side="left", padx=5)
        self.retry_button = tk.Button(
            controls, text="Повторить", command=self.retry,
            font=("Segoe UI", 9, "bold"), fg="#07101a", bg="#83edff", activebackground="#b6f5ff",
            relief="flat", padx=16, pady=6, cursor="hand2", state="disabled",
        )
        self.retry_button.pack(side="left", padx=5)

        self.q: queue.Queue = queue.Queue()
        self.root.after(100, self.poll)
        if self.preview:
            self.bar["value"] = 42
            self.percent.config(text="Общий прогресс: 42%")
            self.steps.config(
                text="✓ Подготовка  ·  ✓ Ядро  ·  ● Интеллект  ·  ◌ Голос  ·  ◌ Зрение  ·  ◌ Телефон  ·  ◌ Проверка  ·  ◌ Запуск",
                fg="#88dff5",
            )
            self.status.config(text="Настраиваю интеллект — уже скачанное сохраняется")
            self.model_title.config(text="Модель 1 из 2 · быстрый интеллект")
            self.model_title.pack(pady=(7, 2), before=self.note)
            self.model_bar.pack(padx=86, fill="x", before=self.note)
            self.model_bar["value"] = 58
            self.model_detail.config(text="58% · продолжаю с сохранённого места")
            self.model_detail.pack(pady=(2, 0), before=self.note)
            self._model_widgets_visible = True
            self.log_button.config(state="disabled")

    def animate_orb(self):
        import math
        self._orb_phase += 0.055
        self.orb.delete("all")
        cx = cy = 97
        dx = dy = 0.0
        pulse = (1 + math.sin(self._orb_phase * 1.35)) / 2
        # Soft anti-aliased-like aura around the supplied high-resolution artwork.
        for idx in range(9, 0, -1):
            r = 73 + idx * 2.1 + pulse * idx * .65
            shade = 66 + idx * 11
            color = f"#{min(180, shade+25):02x}{min(220, shade+60):02x}{min(255, shade+130):02x}"
            self.orb.create_oval(cx-r, cy-r, cx+r, cy+r, outline=color, width=1)
        if self._orb_texture is not None:
            dx = math.sin(self._orb_phase * .71) * 2.8
            dy = math.cos(self._orb_phase * .63) * 2.5
            self.orb.create_image(cx + dx, cy + dy, image=self._orb_texture)
        else:
            core = 63 + math.sin(self._orb_phase * 1.1) * 2
            self.orb.create_oval(cx-core, cy-core, cx+core, cy+core, fill="#14255a", outline="#8cf6ff", width=2)
            self.orb.create_text(cx, cy, text="E I R V E N", fill="#e7fbff", font=("Segoe UI", 8, "bold"))
        self.root.after(32, self.animate_orb)

    def open_log(self) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(self.log_path))
            else:
                webbrowser.open(self.log_path.as_uri())
        except Exception as exc:
            self.note.config(text=f"Журнал: {self.log_path}\n{exc}", fg="#ff9aaf")

    def retry(self) -> None:
        self.retry_button.config(state="disabled")
        self.exit_code = 0
        self.status.config(text="Повторяю с сохранённого этапа…", fg="#9aa9cb")
        self.note.config(text="Уже скачанные и проверенные компоненты не загружаются заново.", fg="#61708f")
        threading.Thread(target=Bootstrap(self).install, daemon=True).start()

    def _append_log(self, text: str) -> None:
        try:
            clean = str(text or "").replace("\r", "").rstrip()
            if not clean:
                return
            with self._log_lock:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(clean + "\n")
        except Exception:
            pass

    @staticmethod
    def _error_tail(value: object, limit: int = 460) -> str:
        text = str(value or "").replace("\r", "").strip()
        if not text:
            return "Неизвестная ошибка"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        tail = "\n".join(lines[-7:]) if lines else text
        return tail[-limit:]

    def post(self, kind, *args):
        # Всё, что установщик показывает человеку, — и в общий журнал. Особенно
        # ошибки: на экране они обрезаются, а здесь остаются целиком.
        if kind in {"log", "retry", "error", "done", "step"} and args is not None:
            _trace("INSTALL_" + str(kind).upper(), message=str(args[0])[:4000] if args else "")
        if kind == "log" and args:
            self._append_log(str(args[0]))
        elif kind in {"retry", "error"} and args:
            self._append_log(f"[{kind.upper()}] {args[0]}")
        elif kind == "done":
            self._append_log("[DONE] installation completed")
        self.q.put((kind, args))

    def poll(self):
        try:
            while True:
                kind, args = self.q.get_nowait()
                if kind == "progress":
                    fraction, message = args[:2]
                    value = max(0, min(100, int(round(float(fraction) * 100))))
                    self.bar["value"] = value
                    self.percent.config(text=f"Общий прогресс: {value}%")
                    stages = ("Подготовка", "Ядро", "Интеллект", "Голос", "Зрение", "Телефон", "Проверка", "Запуск")
                    active = min(len(stages) - 1, int(value * len(stages) / 101))
                    step_text = "  ·  ".join(("✓ " if idx < active else "● " if idx == active else "◌ ") + name for idx, name in enumerate(stages))
                    self.steps.config(text=step_text, fg="#88dff5")
                    self.status.config(text=str(message), fg="#9aa9cb")
                elif kind == "model_progress":
                    payload = args[0] if args and isinstance(args[0], dict) else {}
                    model = str(payload.get("model") or "")
                    if model:
                        if not self._model_widgets_visible:
                            self.model_title.pack(pady=(7, 2), before=self.note)
                            self.model_bar.pack(padx=86, fill="x", before=self.note)
                            self.model_detail.pack(pady=(2, 0), before=self.note)
                            self._model_widgets_visible = True
                        index = int(payload.get("index") or 0)
                        count = int(payload.get("count") or 0)
                        prefix = f"Модель {index} из {count} · " if index and count else ""
                        self.model_title.config(text=prefix + model)
                        percent = payload.get("percent")
                        if percent is None:
                            if not self._model_indeterminate:
                                self.model_bar.configure(mode="indeterminate")
                                self.model_bar.start(12)
                                self._model_indeterminate = True
                        else:
                            if self._model_indeterminate:
                                self.model_bar.stop()
                                self._model_indeterminate = False
                            self.model_bar.configure(mode="determinate")
                            self.model_bar["value"] = max(0, min(100, float(percent)))
                        phase = str(payload.get("phase") or "")
                        detail = str(payload.get("detail") or "")
                        self.model_detail.config(text=(phase + (" · " if phase and detail else "") + detail)[:360])
                elif kind == "retry":
                    self.status.config(text="Восстанавливаю установку автоматически", fg="#e9b8ff")
                elif kind == "log":
                    pass
                elif kind == "error":
                    self.exit_code = 1
                    self.retry_button.config(state="normal")
                    self.status.config(text="Не удалось завершить установку", fg="#ff86a0")
                    tail = self._error_tail(args[0] if args else "")
                    self.note.config(
                        text=(tail + "\nПодробности: logs\\install.log. Уже скачанное сохранено."),
                        fg="#ff9aaf",
                    )
                elif kind == "done":
                    self.exit_code = 0
                    self.bar["value"] = 100
                    self.percent.config(text="Общий прогресс: 100%")
                    self.status.config(text="Установка проверена", fg="#dffcff")
                    self.note.config(text="Запускаю EIRVEN…", fg="#8cefff")
                    self.root.after(900, self.root.destroy)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run(self):
        if not self.preview:
            self.root.after(
                160,
                lambda: threading.Thread(target=Bootstrap(self).install, daemon=True).start(),
            )
        self.root.mainloop()
        return int(self.exit_code)


if __name__ == "__main__":
    raise SystemExit(InstallerGUI(preview="--ui-preview" in sys.argv[1:]).run())
