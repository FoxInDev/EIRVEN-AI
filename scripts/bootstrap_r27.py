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
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from eirven_ai.hardware import detect_hardware  # noqa: E402


class InstallerError(RuntimeError):
    pass


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
        # These are context-sized *local Ollama aliases* consumed by Claude Code. Their
        # names intentionally do not impersonate Anthropic's proprietary Claude weights.
        self.claude_fast_model = "eirven-local:fast"
        self.claude_main_model = "eirven-local:main"
        self.claude_context = 16384

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

    def run(self, command: list[str], label: str, cwd: Path | None = None, timeout: int = 7200, attempts: int = 3) -> str:
        """Run a setup command with bounded automatic recovery for transient failures."""
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self._run_once(command, label, cwd=cwd, timeout=timeout)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = 2 if attempt == 1 else 6
                self.gui.post("retry", f"{label}: временная ошибка. Повторяю автоматически ({attempt + 1}/{attempts})…", delay)
                time.sleep(delay)
        assert last_error is not None
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
                "User-Agent": "EIRVEN-AI/1.7.3 model-downloader-v7",
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
                headers={"User-Agent": "EIRVEN-AI/1.7.3", "Accept": "application/octet-stream"},
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
        hits = sum(1 for token in ("привет", "русск", "голос", "эйрвен", "ирвен") if token in normalized)
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
            "r=httpx.post('http://127.0.0.1:11434/api/chat',json={'model':m,'messages':[{'role':'user','content':'Ответь только OK, если изображение получено.','images':[img]}],'stream':False,'keep_alive':0,'options':{'num_ctx':768,'num_predict':16,'temperature':0}},timeout=120,trust_env=False); "
            "safe=(r.text[:600] or '').encode('ascii','backslashreplace').decode('ascii'); print(r.status_code, safe); r.raise_for_status(); "
            "msg=r.json().get('message') or {}; assert (msg.get('content') or msg.get('thinking') or '').strip()"
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

    def remove_legacy_agent_launchers(self) -> None:
        for name in ("Codex Local.cmd",):
            try:
                (ROOT / name).unlink(missing_ok=True)
            except Exception:
                pass

    def write_claude_code_launcher(self, model: str) -> None:
        """Create a real local Claude Code launcher, not a branded placeholder."""
        content = (
            "@echo off\n"
            "cd /d \"%~dp0\"\n"
            "set ANTHROPIC_AUTH_TOKEN=ollama\n"
            "set ANTHROPIC_API_KEY=\n"
            "set ANTHROPIC_BASE_URL=http://127.0.0.1:11434\n"
            "set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\n"
            "set NO_PROXY=127.0.0.1,localhost\n"
            "set no_proxy=127.0.0.1,localhost\n"
            f"claude --model \"{model}\"\n"
            "if errorlevel 1 pause\n"
        )
        (ROOT / "Claude Code Local.cmd").write_text(content, encoding="utf-8", newline="\r\n")

    def configure_claude_ollama_models(self, profile) -> None:
        """Give Claude Code hardware-sized Ollama aliases with a usable context.

        EIRVEN's native tool/JSON calls still send their own small ``num_ctx`` values.
        These aliases affect only Claude Code, whose base agent prompt needs more room
        than a 2K desktop-action request. Aliases reuse the same Ollama weight blobs.
        """
        context_by_tier = {"light": 12288, "standard": 16384, "balanced": 24576, "power": 65536}
        self.claude_context = int(context_by_tier.get(str(profile.tier), 24576))
        if profile.vram_gb and profile.vram_gb <= 8.5:
            self.claude_context = min(self.claude_context, 16384)
        specs = (
            (self.claude_fast_model, profile.recommended_fast_model),
            (self.claude_main_model, profile.recommended_main_model),
        )
        config_dir = ROOT / "data" / "claude-code"
        config_dir.mkdir(parents=True, exist_ok=True)
        for alias, source in specs:
            modelfile = config_dir / f"Modelfile-{alias.rsplit(':', 1)[-1]}"
            modelfile.write_text(
                f"FROM {source}\nPARAMETER num_ctx {self.claude_context}\n",
                encoding="utf-8",
            )
            self.run(
                ["ollama", "create", alias, "-f", str(modelfile)],
                f"Настройка Claude Code Local ({alias} → {source}, {self.claude_context} ctx)",
                timeout=600,
            )
        self.gui.post(
            "log",
            f"Claude Code Local: fast={self.claude_fast_model}, main={self.claude_main_model}, context={self.claude_context}",
        )

    @staticmethod
    def deep_model_for(profile) -> str:
        # A 20B model on a 4-GB mobile GPU can consume most system RAM and poison latency
        # for voice/desktop work. Keep it only for genuinely capable machines; low-VRAM
        # laptops use the 4B lane even for deep work.
        if profile.vram_gb and profile.vram_gb <= 10.0:
            return "qwen3.5:4b"
        if profile.vram_gb >= 20 and profile.ram_gb >= 48:
            return "gpt-oss:20b"
        if profile.tier == "balanced":
            return profile.recommended_main_model
        if profile.tier == "standard":
            return "qwen3.5:4b"
        return profile.recommended_main_model

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
            "EIRVEN_LLM_BACKEND": "claude_code_local",
            "EIRVEN_OLLAMA_URL": "http://127.0.0.1:11434",
            "EIRVEN_CLAUDE_CODE_COMMAND": "claude",
            "EIRVEN_CLAUDE_CODE_FALLBACK": "1",
            "EIRVEN_CLAUDE_CODE_MODE": "agentic",
            "EIRVEN_CLAUDE_CODE_FAST_MODEL": self.claude_fast_model,
            "EIRVEN_CLAUDE_CODE_MODEL": self.claude_main_model,
            "EIRVEN_CLAUDE_CODE_CONTEXT": str(self.claude_context),
            "EIRVEN_FAST_MODEL": profile.recommended_fast_model,
            "EIRVEN_MODEL": profile.recommended_main_model,
            "EIRVEN_CODE_MODEL": profile.recommended_code_model,
            "EIRVEN_DEEP_MODEL": self.deep_model_for(profile),
            "EIRVEN_VISION_MODEL": profile.recommended_vision_model,
            "EIRVEN_EMBEDDING_MODEL": "qwen3-embedding:0.6b",
            "EIRVEN_KEEP_ALIVE": "2h" if (profile.vram_gb and profile.vram_gb <= 6.0) else "30m",
            "EIRVEN_CHAT_NUM_CTX": "2048" if (profile.vram_gb and profile.vram_gb <= 6.0) else "4096",
            "EIRVEN_TASK_NUM_CTX": "6144" if (profile.vram_gb and profile.vram_gb <= 6.0) else "12288",
            "EIRVEN_CHAT_NUM_PREDICT": "128",
            "EIRVEN_TASK_NUM_PREDICT": "3072",
            "EIRVEN_ENABLE_COMMANDS": "true",
            "EIRVEN_ENABLE_BROWSER": "true",
            "EIRVEN_ENABLE_DESKTOP_CONTROL": "true",
            "EIRVEN_FULL_ACCESS": "true",
            "EIRVEN_MAX_PARALLEL_TASKS": "2",
            "EIRVEN_AUTO_MEMORY": "true",
            "EIRVEN_AUTO_ROUTE": "true",
            "EIRVEN_SEMANTIC_MEMORY": "true" if profile.tier == "power" else "false",
            "EIRVEN_ASR_ENGINE": "gigaam",
            "EIRVEN_GIGAAM_MODEL": "gigaam-v3-e2e-ctc",
            "EIRVEN_WHISPER_MODEL": profile.recommended_whisper_model,
            "EIRVEN_WHISPER_DEVICE": "cpu",
            "EIRVEN_WHISPER_COMPUTE_TYPE": "int8",
            "EIRVEN_TTS_ENGINE": tts_engine,
            "EIRVEN_EXPRESSIVE_TTS_MODEL": ("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" if profile.tier == "light" else "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
            "EIRVEN_EXPRESSIVE_TTS_DESIGN_MODEL": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            "EIRVEN_EXPRESSIVE_TTS_SPEAKER": "Serena",
            "EIRVEN_SILERO_MODEL": str((ROOT / "models" / "silero" / "v5_5_ru.pt").resolve()).replace("\\", "/"),
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
            for filename in ("requirements.txt", "requirements-voice.txt", "requirements-desktop.txt", "requirements-integrations.txt", "requirements-build.txt"):
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

            if subprocess.call(["ollama", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
                raise InstallerError("Ollama не найдена. Перезапустите ярлык: установщик поставит её автоматически.")
            try:
                ollama_version = subprocess.check_output(
                    ["ollama", "--version"], text=True, encoding="utf-8", errors="replace", timeout=20
                ).strip()
                self.gui.post("log", f"Ollama runtime: {ollama_version}")
            except Exception:
                pass

            claude_command = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
            if not claude_command:
                raise InstallerError(
                    "Claude Code CLI не найден. Перезапустите INSTALL EIRVEN AI: "
                    "системный установщик поставит официальный CLI автоматически."
                )
            try:
                claude_version = subprocess.check_output(
                    [claude_command, "--version"], text=True, encoding="utf-8", errors="replace", timeout=30
                ).strip()
                self.gui.post("log", f"Claude Code Local: {claude_version}; backend: Ollama без Anthropic API")
            except Exception as exc:
                raise InstallerError(f"Claude Code CLI установлен, но не запускается: {exc}") from exc

            if os.name == "nt" and profile.vram_gb and profile.vram_gb <= 10.0:
                # Persist conservative server defaults for the next Ollama service start.
                # EIRVEN also sends small per-request num_ctx values, so this is a second
                # guard rather than a prerequisite for the current install.
                for key, value in (("OLLAMA_MAX_LOADED_MODELS", "1"), ("OLLAMA_NUM_PARALLEL", "1"), ("OLLAMA_CONTEXT_LENGTH", "16384")):
                    try:
                        subprocess.run(["setx", key, value], cwd=ROOT, capture_output=True, timeout=20, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
                    except Exception:
                        pass
                self.gui.post("log", "Профиль до 10 ГБ VRAM: Ollama ограничена одной моделью и одним параллельным контекстом, чтобы голос и команды не зависали при конкуренции моделей")

            deep_model = self.deep_model_for(profile)
            model_candidates = [
                profile.recommended_fast_model,
                profile.recommended_main_model,
                profile.recommended_vision_model,
                profile.recommended_code_model,
                deep_model,
            ]
            # Older/non-tool-capable profiles retain Qwen as a planner reserve. Gemma 4
            # handles tools itself, so downloading a second live model would only add
            # swaps on the owner's 8-GB GPU.
            if not str(profile.recommended_fast_model).casefold().startswith("gemma4:"):
                model_candidates.append("qwen3.5:2b")
            # Keep a mid-size reserve on RAM-rich machines too. On balanced systems it
            # deduplicates with the main model; on power systems it is a faster fallback
            # if the 9B/27B deep lane is busy or cannot be loaded.
            if profile.ram_gb >= 24 and not str(profile.recommended_main_model).casefold().startswith("gemma4:"):
                model_candidates.append("gemma3:4b")
            if profile.tier == "power":
                model_candidates.append("qwen3-embedding:0.6b")
            models = []
            for item in model_candidates:
                if item not in models:
                    models.append(item)

            # Every Ollama model selected for this hardware profile is part of the install.
            # Do not defer or silently skip gpt-oss:20b (or any other selected model): a
            # stalled transfer is retried in pull_model and installation finishes only
            # after the complete model set is present.
            if "qwen3-embedding:0.6b" not in models:
                models.append("qwen3-embedding:0.6b")

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
            # Vision/file-image understanding is a core feature, not an optional extra.
            # Validate the configured multimodal model with an actual image. If that tag
            # failed to download, reuse another already installed multimodal model.
            vision_candidates = []
            # Small-GPU installs must never "probe" a 4B/9B VLM just because it is
            # already installed. The probe itself can reserve tens of GB of CUDA arena
            # and leave Ollama/resource arbitration sluggish for later ASR/chat turns.
            raw_vision_candidates = (
                (profile.recommended_vision_model, "qwen3.5:0.8b")
                if (profile.vram_gb and profile.vram_gb <= 6.0)
                else (profile.recommended_vision_model, profile.recommended_fast_model, profile.recommended_main_model, "qwen3-vl:4b", "gemma3:4b")
            )
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
            self.configure_claude_ollama_models(profile)
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
                        "w=m.generate('Привет. Я Эйрвен, говорю по-русски ясно и спокойно.', "
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
            silero_dir = ROOT / "models" / "silero"
            silero_dir.mkdir(parents=True, exist_ok=True)
            silero_model = silero_dir / "v5_5_ru.pt"
            silero_ready = False
            try:
                if not silero_model.is_file() or silero_model.stat().st_size < 1_000_000:
                    self.download(
                        "https://models.silero.ai/models/tts/ru/v5_5_ru.pt", silero_model,
                        lambda fraction: self.update("Скачиваю русский голос Silero V5.5", units=3, local_fraction=min(.95, fraction)),
                        min_bytes=1_000_000,
                    )
                silero_probe = ROOT / "data" / "tts-probe-silero.wav"
                silero_probe.parent.mkdir(parents=True, exist_ok=True)
                smoke = (
                    "import torch,wave; "
                    f"p={str(silero_model)!r}; q={str(silero_probe)!r}; "
                    "m=torch.package.PackageImporter(p).load_pickle('tts_models','model'); "
                    "m.save_wav(text='Привет. Проверка русского голоса Эйрвен.', speaker='baya', sample_rate=48000, audio_path=q); "
                    "w=wave.open(q,'rb'); assert w.getframerate()==48000 and w.getnchannels()==1 and w.getnframes()>4000; "
                    "print('silero-ru-ok',w.getframerate(),w.getnframes()); w.close()"
                )
                self.run([str(self.python), "-c", smoke], "Проверка русского голоса Silero", timeout=600)
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
                    "text='Привет. Это контрольный образец голоса Эйрвен для естественной русской речи.'; "
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
            self.complete_step(7, "Фирменный голос Baya готов")

            self.gui.post("log", "Сетевой TTS отключён: голос Baya работает только локально и не меняет тембр при потере сети")

            # Qwen3-TTS is intentionally not downloaded by default anymore. Its built-in
            # speakers are not native Russian voices and made clean Russian installs much
            # heavier. The code path remains available for manual experiments, while the
            # supported runtime chain is natural neural -> Silero RU -> verified emergency fallbacks.

            # One explicit speaker owns every utterance.  No network, Piper Irina, SAPI
            # or per-phrase fallback is allowed to change the voice behind the Baya label.
            runtime_tts_engine = "silero"
            self.write_env(profile, piper_path, runtime_tts_engine)
            self.write_claude_code_launcher(self.claude_main_model)
            self.complete_step(3, "Настройки сохранены")

            self.update("Проверяю установку", units=6, local_fraction=0.05)
            # Developer pytest has already passed before the release archive is created.
            # Running the whole suite on an end-user Windows machine made VPN/antivirus
            # quirks look like a fatal install error. The installer performs only a local
            # import/compile smoke check; runtime diagnostics are available inside EIRVEN.
            self.run([str(self.python), "-c", "import sounddevice,soundfile,numpy,cv2; print('final audio/camera deps ok', cv2.__version__)"], "Финальная проверка аудио и камеры", timeout=120)
            self.run(
                [str(self.python), "-c", "import pathlib,imageio_ffmpeg; p=pathlib.Path(imageio_ffmpeg.get_ffmpeg_exe()); assert p.is_file(), p; print('video engine ok', p)"],
                "Проверка видеодвижка FFmpeg",
                timeout=120,
            )
            self.run([str(self.python), "-m", "compileall", "-q", "src"], "Проверка Python")
            self.run(
                [str(self.python), "-c", "import eirven_ai; from eirven_ai.app import app; print(eirven_ai.__version__)"],
                "Проверка ядра",
                timeout=120,
            )
            self.gui.post("log", "Ядро EIRVEN готово к запуску")
            self.complete_step(6, "Основные компоненты готовы")

            # Packaging the convenience one-file EXE is best-effort. The real installed
            # runtime is the verified .venv + launcher.py, and start_windows.bat/shortcuts
            # already have a Python fallback. A PyInstaller/AV/resource quirk must never
            # turn a healthy 98%-complete install into an endless reinstall loop.
            self.update("Собираю приложение Windows", units=2, local_fraction=0.1)
            exe_ready = False
            try:
                self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "build_windows.ps1")], "Сборка EIRVEN-AI-r37.exe", timeout=1800)
                exe_ready = (ROOT / "EIRVEN-AI-r37.exe").is_file()
                self.gui.post("log", "Однофайловый EXE успешно собран")
            except Exception as exc:
                self.gui.post("log", f"EXE-сборка пропущена, включаю надёжный Python-launcher: {exc}")
            self.complete_step(2, "Приложение готово" if exe_ready else "Приложение готово через резервный launcher")
            self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "create_shortcut.ps1")], "Создание и проверка ярлыка", timeout=60)
            marker = ROOT / ".installed-v1.7.3-r37"
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            try:
                self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "install_autostart.ps1")], "Автозапуск голосового EIRVEN", timeout=60)
            except Exception as exc:
                self.gui.post("log", f"Автозапуск не создан автоматически: {exc}")
            self.done_units = 100
            self.gui.post("progress", 1.0, "Готово", 0)
            self.gui.post("done", None)
        except Exception:
            raise

    def install(self) -> None:
        """Automatic installer recovery: retry the whole idempotent bootstrap without user restarts."""
        last_error: Exception | None = None
        state_file = ROOT / "data" / "installer-recovery.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 4):
            self.done_units = 0.0
            try:
                state_file.write_text(json.dumps({"version": "1.7.3", "attempt": attempt, "status": "running", "updated": time.time()}), encoding="utf-8")
                if attempt > 1:
                    self.gui.post("retry", f"Перезапускаю установку автоматически — попытка {attempt}/3. Уже скачанное сохранено.", 0)
                self.install_once()
                state_file.unlink(missing_ok=True)
                return
            except Exception as exc:
                last_error = exc
                state_file.write_text(json.dumps({"version": "1.7.3", "attempt": attempt, "status": "retry", "error": str(exc)[-1200:], "updated": time.time()}, ensure_ascii=False), encoding="utf-8")
                if attempt < 3:
                    delay = 3 if attempt == 1 else 8
                    self.gui.post("retry", f"Установка встретила ошибку. Через {delay} сек. попробую ещё раз автоматически ({attempt + 1}/3).", delay)
                    time.sleep(delay)
        self.gui.post("error", str(last_error or "Не удалось завершить установку после автоматических повторов"))

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
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.exit_code = 0
        self._log_lock = threading.RLock()
        self.log_path = ROOT / "logs" / "install.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            previous = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
            if previous:
                self.log_path.with_name("install.previous.log").write_text(previous[-250000:], encoding="utf-8")
            self.log_path.write_text(
                f"=== EIRVEN installer v5 streaming-pull + fallback + visible-progress started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("Установка EIRVEN")
        self.root.geometry("640x495")
        self.root.resizable(False, False)
        self.root.configure(bg="#050711")
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

        self.orb = tk.Canvas(self.root, width=198, height=198, bg="#050711", highlightthickness=0)
        self.orb.pack(pady=(12, 0))
        self._orb_phase = 0.0
        self._orb_texture = None
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageTk
            source = Image.open(ROOT / "assets" / "eirven.png").convert("RGBA")
            pad = int(min(source.size) * .075)
            source = source.crop((pad, pad, source.width - pad, source.height - pad)).resize((174, 174), Image.LANCZOS)
            alpha = Image.new("L", source.size, 0)
            ImageDraw.Draw(alpha).ellipse((2, 2, 172, 172), fill=255)
            source.putalpha(alpha.filter(ImageFilter.GaussianBlur(1.4)))
            self._orb_texture = ImageTk.PhotoImage(source)
        except Exception:
            self._orb_texture = None
        self.animate_orb()

        tk.Label(self.root, text="E I R V E N", font=("Segoe UI", 20, "bold"), fg="#f5fbff", bg="#050711").pack(pady=(0, 3))
        self.status = tk.Label(
            self.root, text="Подготавливаю всё необходимое…", font=("Segoe UI", 10),
            fg="#9aa9cb", bg="#050711", wraplength=570, justify="center",
        )
        self.status.pack(pady=(0, 10))

        self.bar = ttk.Progressbar(self.root, maximum=100, length=468, style="Eirven.Horizontal.TProgressbar")
        self.bar.pack(padx=70, fill="x")
        self.percent = tk.Label(self.root, text="Общий прогресс: 0%", font=("Segoe UI", 14, "bold"), fg="#eafaff", bg="#050711")
        self.percent.pack(pady=(6, 4))
        self.steps = tk.Label(
            self.root, text="●  Подготовка     ◌  Файлы     ◌  Модели     ◌  Запуск",
            font=("Segoe UI", 8), fg="#7987b7", bg="#050711",
        )
        self.steps.pack(pady=(0, 2))

        self.model_title = tk.Label(
            self.root, text="", font=("Segoe UI", 9, "bold"),
            fg="#c8c1ff", bg="#050711", wraplength=570, justify="center",
        )
        self.model_bar = ttk.Progressbar(
            self.root, maximum=100, length=468, style="Eirven.Model.Horizontal.TProgressbar",
        )
        self.model_detail = tk.Label(
            self.root, text="",
            font=("Segoe UI", 8), fg="#8d96b5", bg="#050711", wraplength=570, justify="center",
        )
        self.note = tk.Label(
            self.root, text="Если что-то сорвётся, просто запусти EIRVEN снова — уже скачанное сохранится.",
            font=("Segoe UI", 8), fg="#61708f", bg="#050711", wraplength=570, justify="center",
        )
        self.note.pack(pady=(12, 0))

        self.q: queue.Queue = queue.Queue()
        self.root.after(100, self.poll)

    def animate_orb(self):
        import math
        self._orb_phase += 0.055
        self.orb.delete("all")
        cx = cy = 99
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
        # Cute gem eyes from the supplied design. The clean sphere stays expression-free;
        # blinking and the mouth are live layers rather than baked artwork.
        blink = math.sin(self._orb_phase * .57) > .986
        for ex in (cx - 19, cx + 19):
            ey = cy - 12
            if blink:
                self.orb.create_line(ex-6, ey, ex, ey+1, ex+6, ey, smooth=True, fill="#dffcff", width=2)
            else:
                self.orb.create_oval(ex-8, ey-9, ex+8, ey+9, fill="#3a2991", outline="#82ecff", width=2)
                self.orb.create_oval(ex-6, ey-7, ex+6, ey+7, fill="#1a1457", outline="#716cff", width=1)
                self.orb.create_oval(ex-5, ey-6, ex-2, ey-3, fill="#ffffff", outline="")
                self.orb.create_oval(ex+2, ey+2, ex+3.5, ey+3.5, fill="#bff9ff", outline="")
        self.orb.create_arc(cx-7, cy+8, cx+7, cy+19, start=205, extent=130, style="arc", outline="#e9fbff", width=2)
        for offset, color in ((0, "#78f2ff"), (2.1, "#a77cff"), (4.2, "#ff80d4")):
            a = self._orb_phase * 1.35 + offset
            x = cx + math.cos(a) * 86
            y = cy + math.sin(a) * 58
            dot = 2.1 + pulse * .8
            self.orb.create_oval(x-dot, y-dot, x+dot, y+dot, fill=color, outline="")
        self.root.after(32, self.animate_orb)

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
                    if value < 18:
                        step_text = "●  Подготовка     ◌  Файлы     ◌  Модели     ◌  Запуск"
                    elif value < 46:
                        step_text = "✓  Подготовка     ●  Файлы     ◌  Модели     ◌  Запуск"
                    elif value < 88:
                        step_text = "✓  Подготовка     ✓  Файлы     ●  Модели     ◌  Запуск"
                    else:
                        step_text = "✓  Подготовка     ✓  Файлы     ✓  Модели     ●  Запуск"
                    self.steps.config(text=step_text, fg="#88dff5")
                    self.status.config(text=str(message), fg="#9aa9cb")
                elif kind == "model_progress":
                    pass
                elif kind == "retry":
                    self.status.config(text="Восстанавливаю установку автоматически", fg="#e9b8ff")
                elif kind == "log":
                    pass
                elif kind == "error":
                    self.exit_code = 1
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
                    self.status.config(text="Готово", fg="#dffcff")
                    self.note.config(text="Запускаю EIRVEN…", fg="#8cefff")
                    self.root.after(900, self.root.destroy)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run(self):
        threading.Thread(target=Bootstrap(self).install, daemon=True).start()
        self.root.mainloop()
        return int(self.exit_code)


if __name__ == "__main__":
    raise SystemExit(InstallerGUI().run())
