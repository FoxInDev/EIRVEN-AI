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

    def update(self, message: str, units: float = 0, local_fraction: float = 0) -> None:
        fraction = min(0.99, (self.done_units + units * local_fraction) / self.total_units)
        # r21 installer deliberately shows one honest global percentage only.
        self.gui.post("progress", fraction, message)

    def complete_step(self, units: float, message: str) -> None:
        self.done_units += units
        self.update(message)

    def _run_once(self, command: list[str], label: str, cwd: Path | None = None, timeout: int = 7200) -> str:
        self.gui.post("log", f"> {' '.join(command)}")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=cwd or ROOT,
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

    def pull_model(self, model: str, unit_weight: float) -> None:
        installed = self.model_list()
        if model in installed or any(
            item.split(":")[0] == model.split(":")[0] and model.endswith(":latest")
            for item in installed
        ):
            self.gui.post("log", f"Модель уже есть: {model}")
            self.complete_step(unit_weight, f"Модель {model} готова")
            return

        self.update(f"Скачиваю модель {model}", units=unit_weight, local_fraction=0.01)
        try:
            payload = json.dumps({"model": model, "stream": True}).encode("utf-8")
            request = urllib.request.Request(
                "http://127.0.0.1:11434/api/pull",
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "EIRVEN-AI/1.2.2"},
                method="POST",
            )
            last_status = ""
            # Never send localhost through a VPN/system proxy. Ollama itself uses the
            # user's normal network/VPN when it downloads model layers.
            local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with local_opener.open(request, timeout=300) as response:
                for raw in response:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                    if event.get("error"):
                        raise InstallerError(str(event["error"]))
                    status = str(event.get("status") or "Скачивание")
                    total = int(event.get("total") or 0)
                    completed = int(event.get("completed") or 0)
                    fraction = completed / total if total > 0 else 0.02
                    fraction = max(0.01, min(fraction, 0.98))
                    self.update(
                        f"{status}: {model} ({fraction * 100:.1f}%)",
                        units=unit_weight,
                        local_fraction=fraction,
                    )
                    if status != last_status:
                        self.gui.post("log", f"{model}: {status}")
                        last_status = status
        except Exception as exc:
            self.gui.post("log", f"Прямой индикатор недоступен ({exc}); продолжаю через Ollama CLI")
            self.run(["ollama", "pull", model], f"Загрузка модели {model}", timeout=21600)
        self.complete_step(unit_weight, f"Модель {model} готова")

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
                headers={"User-Agent": "EIRVEN-AI/1.2.2", "Accept": "application/octet-stream"},
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
            "m=onnx_asr.load_model('gigaam-v3-e2e-rnnt', quantization='int8'); "
            f"t=str(m.recognize({str(wav_path)!r}) or '').lower(); "
            "print('EIRVEN_TTS_TRANSCRIPT='+t.replace('\n',' '))"
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
        for name in ("Codex Local.cmd", "Claude Code Local.cmd"):
            try:
                (ROOT / name).unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def deep_model_for(profile) -> str:
        # A 20B model on a 4-GB mobile GPU can consume most system RAM and poison latency
        # for voice/desktop work. Keep it only for genuinely capable machines; low-VRAM
        # laptops use the 4B lane even for deep work.
        if profile.vram_gb and profile.vram_gb <= 6.0:
            return "qwen3.5:4b"
        if profile.tier in {"power", "balanced"} and profile.ram_gb >= 24:
            return "gpt-oss:20b"
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
            "EIRVEN_HOST": "127.0.0.1",
            "EIRVEN_PORT": "7860",
            "EIRVEN_LLM_BACKEND": "ollama",
            "EIRVEN_OLLAMA_URL": "http://127.0.0.1:11434",
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
            "EIRVEN_GIGAAM_MODEL": "gigaam-v3-e2e-rnnt",
            "EIRVEN_WHISPER_MODEL": profile.recommended_whisper_model,
            "EIRVEN_WHISPER_DEVICE": "cpu",
            "EIRVEN_WHISPER_COMPUTE_TYPE": "int8",
            "EIRVEN_TTS_ENGINE": tts_engine,
            "EIRVEN_EXPRESSIVE_TTS_MODEL": ("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" if profile.tier == "light" else "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
            "EIRVEN_EXPRESSIVE_TTS_DESIGN_MODEL": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            "EIRVEN_EXPRESSIVE_TTS_SPEAKER": "Serena",
            "EIRVEN_SILERO_MODEL": str((ROOT / "models" / "silero" / "v5_5_ru.pt").resolve()).replace("\\", "/"),
            "EIRVEN_PIPER_MODEL": (str(piper_path).replace("\\", "/") if str(piper_path) not in {"", "."} and piper_path.exists() else ""),
            "EIRVEN_VOICE_SILENCE_MS": "600",
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

            if os.name == "nt" and profile.vram_gb and profile.vram_gb <= 6.0:
                # Persist conservative server defaults for the next Ollama service start.
                # EIRVEN also sends small per-request num_ctx values, so this is a second
                # guard rather than a prerequisite for the current install.
                for key, value in (("OLLAMA_MAX_LOADED_MODELS", "1"), ("OLLAMA_NUM_PARALLEL", "1"), ("OLLAMA_CONTEXT_LENGTH", "2048")):
                    try:
                        subprocess.run(["setx", key, value], cwd=ROOT, capture_output=True, timeout=20, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
                    except Exception:
                        pass
                self.gui.post("log", "Профиль 4–6 ГБ VRAM: Ollama ограничена одной моделью/одним параллельным контекстом; vision использует отдельную малую модель и не участвует в обычных GUI-шагax")

            deep_model = self.deep_model_for(profile)
            model_candidates = [
                profile.recommended_fast_model,
                profile.recommended_main_model,
                # Stock Gemma is used for chat/vision; keep this tiny model only as
                # the on-demand native tool-routing reserve for complex GUI actions.
                "qwen3.5:2b",
                # Multimodal model powers screen understanding and image attachments.
                profile.recommended_vision_model,
                profile.recommended_code_model,
                # A stronger model is kept for long project/reasoning turns; the small
                # fast model remains resident for immediate desktop actions.
                deep_model,
            ]
            # Keep a mid-size reserve on RAM-rich machines too. On balanced systems it
            # deduplicates with the main model; on power systems it is a faster fallback
            # if the 9B/27B deep lane is busy or cannot be loaded.
            if profile.ram_gb >= 24:
                model_candidates.append("gemma3:4b")
            if profile.tier == "power":
                model_candidates.append("qwen3-embedding:0.6b")
            models = []
            for item in model_candidates:
                if item not in models:
                    models.append(item)
            model_weight = 42 / max(1, len(models))
            required_models = {profile.recommended_fast_model, profile.recommended_main_model}
            for model in models:
                try:
                    self.pull_model(model, model_weight)
                except Exception as exc:
                    # A VPN/proxy/CDN hiccup must not turn an otherwise working local
                    # installation into a red fatal screen. Fast/main text models are the
                    # only hard requirement, and even those may already be installed under
                    # a compatible tag from a previous EIRVEN version.
                    self.gui.post("log", f"Модель {model} пока не установлена: {exc}")
                    self.complete_step(model_weight, f"Пропускаю {model}; можно докачать позже")

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

            required_bases = {m.split(":", 1)[0] for m in required_models}
            installed_bases = {m.split(":", 1)[0] for m in installed_after}
            if not required_bases.intersection(installed_bases):
                raise InstallerError(
                    "Не удалось подготовить ни одной основной текстовой модели Ollama. "
                    "Проверьте VPN/интернет и запустите установщик ещё раз — уже скачанные файлы не потеряются."
                )
            self.remove_legacy_agent_launchers()

            # Local Russian fallback voices. Natural r8.1 chooses Chatterbox/Edge/Silero;
            # Piper speakers are verified downloads used only when the neural TTS cannot
            # start. A broken individual fallback must not prevent the voice daemon from
            # reaching the primary engine.
            voice_dir = ROOT / "models" / "piper"
            voice_dir.mkdir(parents=True, exist_ok=True)
            voice_names = ("irina",)
            voice_files: dict[str, Path] = {}
            self.update("Подготавливаю локальные голоса", units=7, local_fraction=0.03)
            last_voice_error = ""
            for index, voice_name in enumerate(voice_names):
                stem = f"ru_RU-{voice_name}-medium.onnx"
                model_path = voice_dir / stem
                config_path = voice_dir / f"{stem}.json"
                base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/{voice_name}/medium"
                try:
                    healthy = self.valid_piper(model_path, config_path) and self.runtime_validate_piper(model_path)
                    for attempt in range(2):
                        if healthy:
                            break
                        model_path.unlink(missing_ok=True)
                        config_path.unlink(missing_ok=True)
                        self.download(
                            f"{base}/{model_path.name}?download=true", model_path,
                            lambda fraction, n=voice_name, i=index: self.update(
                                f"Скачиваю голос {n}", units=7,
                                local_fraction=min(0.94, (i + fraction) / len(voice_names)),
                            ),
                            min_bytes=10_000_000,
                        )
                        self.download(f"{base}/{config_path.name}?download=true", config_path, lambda _: None, min_bytes=500)
                        healthy = self.valid_piper(model_path, config_path) and self.runtime_validate_piper(model_path)
                        if not healthy:
                            self.gui.post("log", f"Голос {voice_name}: ONNX повреждён, повтор {attempt + 1}/2")
                    if healthy:
                        voice_files[voice_name] = model_path
                    else:
                        model_path.unlink(missing_ok=True)
                        raise InstallerError(f"Голос {voice_name} дважды не прошёл реальную ONNX-проверку")
                except Exception as exc:
                    last_voice_error = str(exc)
                    self.gui.post("log", f"Голос {voice_name} пока не скачан: {exc}")
            # Public release has one canonical Baya identity. Piper Irina is the verified local fallback.
            piper_path = voice_files.get("irina") or Path()
            piper_ready = str(piper_path) not in {"", "."} and piper_path.exists() and self.valid_piper(piper_path, Path(str(piper_path) + ".json"))
            if piper_ready:
                self.complete_step(7, f"Локальные голоса готовы: {len(voice_files)}")
            else:
                self.complete_step(7, "Голос можно докачать из диагностики")
                self.gui.post("log", f"Piper пока недоступен: {last_voice_error or 'файлы не получены'}")

            self.update("Подготавливаю русское распознавание речи", units=7, local_fraction=0.05)
            # GigaAM v3 is the primary Russian ASR. Model download is intentionally
            # non-fatal: a VPN/CDN issue must not prevent EIRVEN from launching, and
            # faster-whisper remains an isolated CPU fallback.
            code = (
                "import onnx_asr; "
                "m=onnx_asr.load_model('gigaam-v3-e2e-rnnt', quantization='int8'); "
                "print(type(m).__name__)"
            )
            try:
                self.run([str(self.python), "-c", code], "Загрузка GigaAM v3", timeout=7200)
                self.complete_step(7, "Русское распознавание речи готово")
            except Exception as exc:
                self.gui.post("log", f"GigaAM пока не прогрет: {exc}")
                self.gui.post("log", "EIRVEN запустится сразу; GigaAM повторит загрузку при первом голосовом запросе, а Whisper останется резервом.")
                self.complete_step(7, "Распознавание речи будет подготовлено при первом запуске")

            preferred_tts_engine = "natural"
            # The working Jarvis reference keeps speech as a separate backend. On a
            # CUDA machine we install Chatterbox Multilingual as the high-naturalness
            # Russian path; it is local and requires no API key. Installation is
            # opportunistic so CPU-only systems keep the fast Silero path.
            if profile.cuda_available and profile.vram_gb >= 6:
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
                    preferred_tts_engine = "natural"
                    self.gui.post("log", "Chatterbox Multilingual RU готов: локальный естественный голос без API")
                except Exception as exc:
                    self.gui.post("log", f"Chatterbox RU не активирован; использую быстрый русский Silero: {exc}")

            # Native Russian Silero V5.5 is the reliable low-latency TTS. It has
            # Russian-specific stress/homograph handling and writes its own 48 kHz WAV.
            try:
                self.run([str(self.python), "-c", "import torch; print(torch.__version__)"], "Проверка PyTorch для русского голоса", timeout=90)
            except Exception:
                self.run([str(self.python), "-m", "pip", "install", "--upgrade", "torch"], "Установка PyTorch для русского голоса", timeout=7200)
            silero_dir = ROOT / "models" / "silero"
            silero_dir.mkdir(parents=True, exist_ok=True)
            silero_model = silero_dir / "v5_5_ru.pt"
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
                    "m.save_wav(text='Привет. Проверка русского голоса Эйрвен.', speaker='kseniya', sample_rate=48000, audio_path=q); "
                    "w=wave.open(q,'rb'); assert w.getframerate()==48000 and w.getnchannels()==1 and w.getnframes()>4000; "
                    "print('silero-ru-ok',w.getframerate(),w.getnframes()); w.close()"
                )
                self.run([str(self.python), "-c", smoke], "Проверка русского голоса Silero", timeout=600)
                quality = self.speech_roundtrip_quality(silero_probe)
                silero_probe.unlink(missing_ok=True)
                if quality is False:
                    # Keep the natural selector: runtime tries local Chatterbox first, then
                    # no-key Edge neural speech, then local Silero/SAPI fallbacks.
                    preferred_tts_engine = "natural"
                    self.gui.post("log", "Silero структурно исправен, но не прошёл контроль разборчивости; не назначаю его основным")
                else:
                    self.gui.post("log", "Silero V5.5 RU готов: 48 кГц, русские ударения и омографы")
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
            except Exception as exc:
                self.gui.post("log", f"Silero RU пока недоступен, оставляю резервные голоса: {exc}")

            # Probe the natural no-key neural Russian path once during install. Runtime
            # will use it only for the explicit Svetlana/Dmitry neural presets and will
            # fail over quickly when offline.
            try:
                edge_probe = ROOT / "data" / "tts-probe-edge.mp3"
                edge_probe.parent.mkdir(parents=True, exist_ok=True)
                edge_code = (
                    "import asyncio,edge_tts,os; "
                    f"p={str(edge_probe)!r}; "
                    "asyncio.run(edge_tts.Communicate('Привет. Я Эйрвен, говорю естественно по-русски.', 'ru-RU-SvetlanaNeural').save(p)); "
                    "assert os.path.getsize(p)>1000; print('edge-ru-ok')"
                )
                self.run([str(self.python), "-c", edge_code], "Проверка естественного нейросетевого русского голоса", timeout=90)
                edge_probe.unlink(missing_ok=True)
                self.gui.post("log", "Нейросетевой резервный русский голос доступен без API-ключа")
            except Exception as exc:
                self.gui.post("log", f"Нейросетевой сетевой голос сейчас недоступен; локальные голоса останутся рабочими: {exc}")

            # Qwen3-TTS is intentionally not downloaded by default anymore. Its built-in
            # speakers are not native Russian voices and made clean Russian installs much
            # heavier. The code path remains available for manual experiments, while the
            # supported runtime chain is natural neural -> Silero RU -> verified emergency fallbacks.

            # On small-GPU laptops use the deterministic local Russian engine as the
            # runtime default. Voice presets still select distinct Silero speakers, but a
            # slow/offline Edge request can no longer turn the first reply into SAPI or a
            # 30-second wait.
            runtime_tts_engine = "natural"
            self.write_env(profile, piper_path if piper_ready else Path(), runtime_tts_engine)
            self.complete_step(3, "Настройки сохранены")

            self.update("Проверяю установку", units=6, local_fraction=0.05)
            # Developer pytest has already passed before the release archive is created.
            # Running the whole suite on an end-user Windows machine made VPN/antivirus
            # quirks look like a fatal install error. The installer performs only a local
            # import/compile smoke check; runtime diagnostics are available inside EIRVEN.
            self.run([str(self.python), "-c", "import sounddevice,soundfile,numpy,cv2; print('final audio/camera deps ok', cv2.__version__)"], "Финальная проверка аудио и камеры", timeout=120)
            self.run([str(self.python), "-m", "compileall", "-q", "src"], "Проверка Python")
            self.run(
                [str(self.python), "-c", "import eirven_ai; from eirven_ai.app import app; print(eirven_ai.__version__)"],
                "Проверка ядра",
                timeout=120,
            )
            self.gui.post("log", "Ядро EIRVEN готово к запуску")
            self.complete_step(6, "Основные компоненты готовы")

            marker = ROOT / ".installed-v1.2.2-public"
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            try:
                self.update("Собираю приложение Windows", units=2, local_fraction=0.1)
                self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "build_windows.ps1")], "Сборка EIRVEN-AI.exe", timeout=1800)
                self.complete_step(2, "Приложение собрано")
            except Exception as exc:
                self.gui.post("log", f"EXE не собран, будет использован надёжный ярлык CMD: {exc}")
            try:
                self.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "create_shortcut.ps1")], "Создание ярлыка", timeout=60)
            except Exception as exc:
                self.gui.post("log", f"Ярлык не создан автоматически: {exc}")
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
                state_file.write_text(json.dumps({"version": "1.2.2", "attempt": attempt, "status": "running", "updated": time.time()}), encoding="utf-8")
                if attempt > 1:
                    self.gui.post("retry", f"Перезапускаю установку автоматически — попытка {attempt}/3. Уже скачанное сохранено.", 0)
                self.install_once()
                state_file.unlink(missing_ok=True)
                return
            except Exception as exc:
                last_error = exc
                state_file.write_text(json.dumps({"version": "1.2.2", "attempt": attempt, "status": "retry", "error": str(exc)[-1200:], "updated": time.time()}, ensure_ascii=False), encoding="utf-8")
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
        self.root = tk.Tk()
        self.root.title("Установка EIRVEN")
        self.root.geometry("600x445")
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

        self.orb = tk.Canvas(self.root, width=198, height=198, bg="#050711", highlightthickness=0)
        self.orb.pack(pady=(20, 0))
        self._orb_phase = 0.0
        self._orb_texture = None
        try:
            from PIL import Image, ImageTk
            source = Image.open(ROOT / "assets" / "eirven.png").convert("RGBA")
            source.thumbnail((174, 174), Image.LANCZOS)
            self._orb_texture = ImageTk.PhotoImage(source)
        except Exception:
            self._orb_texture = None
        self.animate_orb()

        tk.Label(self.root, text="E I R V E N", font=("Segoe UI", 20, "bold"), fg="#f5fbff", bg="#050711").pack(pady=(0, 3))
        self.status = tk.Label(self.root, text="Подготавливаю всё необходимое…", font=("Segoe UI", 10), fg="#9aa9cb", bg="#050711")
        self.status.pack(pady=(0, 16))

        self.bar = ttk.Progressbar(self.root, maximum=100, length=468, style="Eirven.Horizontal.TProgressbar")
        self.bar.pack(padx=58, fill="x")
        self.percent = tk.Label(self.root, text="0%", font=("Segoe UI", 18, "bold"), fg="#eafaff", bg="#050711")
        self.percent.pack(pady=(10, 0))
        self.note = tk.Label(self.root, text="Если сеть или Windows дадут сбой, я попробую ещё раз сама.", font=("Segoe UI", 9), fg="#61708f", bg="#050711", wraplength=500, justify="center")
        self.note.pack(pady=(8, 0))

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
        for offset, color in ((0, "#78f2ff"), (2.1, "#a77cff"), (4.2, "#ff80d4")):
            a = self._orb_phase * 1.35 + offset
            x = cx + math.cos(a) * 86
            y = cy + math.sin(a) * 58
            dot = 2.1 + pulse * .8
            self.orb.create_oval(x-dot, y-dot, x+dot, y+dot, fill=color, outline="")
        self.root.after(32, self.animate_orb)

    def post(self, kind, *args):
        self.q.put((kind, args))

    def poll(self):
        try:
            while True:
                kind, args = self.q.get_nowait()
                if kind == "progress":
                    fraction, message = args[:2]
                    value = max(0, min(100, int(round(float(fraction) * 100))))
                    self.bar["value"] = value
                    self.percent.config(text=f"{value}%")
                    self.status.config(text=str(message), fg="#9aa9cb")
                elif kind == "retry":
                    message = str(args[0]) if args else "Повторяю автоматически…"
                    self.status.config(text="Восстанавливаю установку", fg="#e9b8ff")
                    self.note.config(text=message, fg="#bda9e8")
                elif kind == "log":
                    pass
                elif kind == "error":
                    self.exit_code = 1
                    self.status.config(text="Не удалось завершить установку", fg="#ff86a0")
                    self.note.config(text=(str(args[0])[:180] + "\nМожно запустить установщик ещё раз — скачанное сохранено."), fg="#ff9aaf")
                elif kind == "done":
                    self.exit_code = 0
                    self.bar["value"] = 100
                    self.percent.config(text="100%")
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
