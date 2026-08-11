from __future__ import annotations

import io
import json
import os
import re
import socket
import unicodedata
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageStat

from .config import Settings


class CreativeError(RuntimeError):
    pass


class CreativeService:
    """Optional local image generation through a user-owned ComfyUI instance."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.comfyui_url
        self.output_dir = settings.data_dir / "generated"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.engine_root = settings.root_dir / "data" / "photo_engine"
        self._engine_process: subprocess.Popen | None = None
        self._installer_process: subprocess.Popen | None = None
        self._engine_lock = threading.Lock()
        self._engine_start_error = ""

    def install_status(self) -> dict[str, Any]:
        path = self.engine_root / "install_status.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _port_open(host: str = "127.0.0.1", port: int = 8188) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.35):
                return True
        except OSError:
            return False

    @staticmethod
    def _engine_device_args(python: Path) -> list[str]:
        """Prefer CUDA, but make the one-click engine usable on CPU-only Windows too."""
        try:
            result = subprocess.run(
                [str(python), "-c", "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0 and result.stdout.strip().casefold().endswith("cuda"):
                return ["--lowvram"]
        except Exception:
            pass
        return ["--cpu"]

    def ensure_local_engine(self) -> bool:
        """Start the private ComfyUI installed by EIRVEN, if present."""
        with self._engine_lock:
            self._engine_start_error = ""
            if self._engine_process is not None and self._engine_process.poll() is None:
                return True
            python = self.engine_root / ".venv" / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            main = self.engine_root / "ComfyUI" / "main.py"
            if not python.is_file() or not main.is_file():
                return False
            if self._port_open("127.0.0.1", 8188):
                self._engine_start_error = (
                    "Порт 8188 уже занят другой программой. Закрой её или измени EIRVEN_COMFYUI_URL, "
                    "затем повтори запуск фото-движка."
                )
                return False
            logs = self.settings.root_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            output = (logs / "photo_engine.log").open("a", encoding="utf-8")
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            try:
                self._engine_process = subprocess.Popen(
                    [
                        str(python),
                        str(main),
                        "--listen",
                        "127.0.0.1",
                        "--port",
                        "8188",
                        *self._engine_device_args(python),
                    ],
                    cwd=main.parent,
                    stdout=output,
                    stderr=output,
                    creationflags=flags,
                )
            except Exception as exc:
                self._engine_start_error = f"Не удалось запустить локальный фото-движок: {exc}"
                return False
            finally:
                output.close()
            return True

    def health(self) -> dict[str, Any]:
        started_local = False
        try:
            response = httpx.get(f"{self.base_url}/system_stats", timeout=3, trust_env=False)
            response.raise_for_status()
            checkpoints = self._checkpoints()
            return {
                "ok": bool(checkpoints),
                "url": self.base_url,
                "checkpoints": checkpoints,
                "detail": (
                    "Локальный генератор готов."
                    if checkpoints
                    else "ComfyUI запущен, но checkpoint-модель не найдена."
                ),
            }
        except Exception as exc:
            if self.base_url in {"http://127.0.0.1:8188", "http://localhost:8188"}:
                started_local = self.ensure_local_engine()
                if started_local:
                    for _ in range(12):
                        time.sleep(0.5)
                        try:
                            response = httpx.get(
                                f"{self.base_url}/system_stats", timeout=1, trust_env=False
                            )
                            response.raise_for_status()
                            checkpoints = self._checkpoints()
                            if checkpoints:
                                return {
                                    "ok": True,
                                    "url": self.base_url,
                                    "checkpoints": checkpoints,
                                    "detail": "Локальный генератор готов.",
                                }
                        except Exception:
                            continue
            install = self.install_status()
            return {
                "ok": False,
                "url": self.base_url,
                "error": str(exc),
                "detail": (
                    self._engine_start_error
                    or str(install.get("detail") or "")
                    or (
                        "Генератор ещё не установлен. Нажми «Установить генератор»: "
                        "EIRVEN сама скачает изолированный ComfyUI и две модели."
                    )
                ),
                "install": install,
                "starting": started_local,
            }

    def _checkpoints(self) -> list[str]:
        response = httpx.get(
            f"{self.base_url}/object_info/CheckpointLoaderSimple",
            timeout=10,
            trust_env=False,
        )
        response.raise_for_status()
        data = response.json()
        names = (
            data.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [[]])[0]
        )
        return [str(item) for item in names if str(item).strip()]

    def _checkpoint(self, mode: str = "realistic") -> str:
        if self.settings.comfyui_checkpoint:
            return self.settings.comfyui_checkpoint
        try:
            names = self._checkpoints()
            if names:
                mode = str(mode or "realistic").casefold()
                anime_hints = ("animagine-xl-4.0-opt", "animagine", "anime", "illustrious", "anything", "pony")
                realistic_hints = (
                    "sd_xl_base_1.0", "sd_xl", "sdxl", "realvis", "realistic", "juggernaut", "photon", "flux"
                )
                wanted = anime_hints if mode == "anime" else realistic_hints
                opposite = realistic_hints if mode == "anime" else anime_hints

                def rank(name: str) -> tuple[int, int, str]:
                    lowered = name.casefold()
                    wanted_rank = next((index for index, hint in enumerate(wanted) if hint in lowered), 999)
                    opposite_hit = any(hint in lowered for hint in opposite)
                    return (1 if opposite_hit and wanted_rank == 999 else 0, wanted_rank, lowered)

                return min(names, key=rank)
        except Exception as exc:
            raise CreativeError(f"Не удалось прочитать модели ComfyUI: {exc}") from exc
        raise CreativeError("В ComfyUI не найден checkpoint")

    @staticmethod
    def _workflow(
        prompt: str,
        negative: str,
        checkpoint: str,
        width: int,
        height: int,
        steps: int,
        seed: int,
        output_width: int | None = None,
        output_height: int | None = None,
    ) -> dict[str, Any]:
        workflow: dict[str, Any] = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": steps,
                    "cfg": 7.0,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["4", 0],
                },
            },
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "EIRVEN", "images": ["6", 0]},
            },
        }
        if output_width and output_height:
            workflow["8"] = {
                "class_type": "ImageScale",
                "inputs": {
                    "upscale_method": "lanczos",
                    "width": int(output_width),
                    "height": int(output_height),
                    "crop": "disabled",
                    "image": ["6", 0],
                },
            }
            workflow["7"]["inputs"]["images"] = ["8", 0]
        return workflow

    @staticmethod
    def _finalize_image(
        data: bytes,
        target: Path,
        *,
        output_width: int | None = None,
        output_height: int | None = None,
    ) -> dict[str, Any]:
        """Decode, sanity-check, CPU-upscale and atomically save a generated image."""
        try:
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                if opened.width < 256 or opened.height < 256:
                    raise CreativeError("Генератор вернул изображение неожиданно маленького размера.")
                image = opened.convert("RGB")
        except CreativeError:
            raise
        except Exception as exc:
            raise CreativeError(f"Генератор вернул повреждённый файл изображения: {exc}") from exc

        source_width, source_height = image.size
        final_width = int(output_width or source_width)
        final_height = int(output_height or source_height)
        if final_width < 256 or final_height < 256 or final_width > 4096 or final_height > 4096:
            raise CreativeError("Некорректный размер итогового изображения.")
        if image.size != (final_width, final_height):
            image = image.resize((final_width, final_height), Image.Resampling.LANCZOS)

        probe = image.copy()
        probe.thumbnail((128, 128), Image.Resampling.BILINEAR)
        stats = ImageStat.Stat(probe)
        ranges = [high - low for low, high in probe.getextrema()]
        if max(ranges or [0]) < 4 or max(stats.stddev or [0.0]) < 1.5:
            raise CreativeError("Результат выглядит однотонным или пустым; попробуй генерацию ещё раз.")

        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(target.suffix + ".tmp")
        image.save(pending, format="PNG", compress_level=6)
        pending.replace(target)
        try:
            with Image.open(target) as verify:
                verify.verify()
            with Image.open(target) as verify_size:
                actual_size = verify_size.size
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise CreativeError(f"Не удалось проверить сохранённый результат: {exc}") from exc
        if actual_size != (final_width, final_height):
            target.unlink(missing_ok=True)
            raise CreativeError("Размер сохранённого результата не совпал с выбранным форматом.")
        if target.stat().st_size < 4096:
            target.unlink(missing_ok=True)
            raise CreativeError("Сохранённый результат слишком мал и выглядит повреждённым.")
        return {
            "source_width": source_width,
            "source_height": source_height,
            "output_width": final_width,
            "output_height": final_height,
            "validated": True,
            "upscale": "pillow-lanczos" if (source_width, source_height) != (final_width, final_height) else "none",
        }

    def generate_image(
        self,
        prompt: str,
        *,
        negative: str = "low quality, blurry, text, watermark, deformed",
        width: int = 768,
        height: int = 768,
        steps: int = 24,
        timeout: int = 1800,
        checkpoint_mode: str = "realistic",
        output_width: int | None = None,
        output_height: int | None = None,
    ) -> dict[str, Any]:
        if not self.health().get("ok"):
            raise CreativeError(
                "Локальная генерация изображений не настроена. Запустите ComfyUI и укажите EIRVEN_COMFYUI_URL."
            )
        checkpoint = self._checkpoint(checkpoint_mode)
        width = max(256, min(int(width) // 64 * 64, 1536))
        height = max(256, min(int(height) // 64 * 64, 1536))
        steps = max(8, min(int(steps), 80))
        client_id = secrets.token_hex(12)
        workflow = self._workflow(
            prompt,
            negative,
            checkpoint,
            width,
            height,
            steps,
            secrets.randbelow(2**31 - 1),
            None,
            None,
        )
        response = httpx.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=30,
            trust_env=False,
        )
        response.raise_for_status()
        prompt_id = str(response.json().get("prompt_id") or "")
        if not prompt_id:
            raise CreativeError("ComfyUI не вернул ID задачи")
        deadline = time.monotonic() + timeout
        output: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            history = httpx.get(
                f"{self.base_url}/history/{prompt_id}", timeout=20, trust_env=False
            )
            history.raise_for_status()
            item = history.json().get(prompt_id)
            if item:
                output = item
                break
            time.sleep(1.2)
        if not output:
            raise CreativeError("Генерация превысила лимит времени")
        images: list[dict[str, Any]] = []
        for node in output.get("outputs", {}).values():
            images.extend(node.get("images") or [])
        if not images:
            raise CreativeError("ComfyUI завершил задачу без изображения")
        first = images[0]
        params = {
            "filename": first.get("filename"),
            "subfolder": first.get("subfolder", ""),
            "type": first.get("type", "output"),
        }
        view_response = httpx.get(
            f"{self.base_url}/view", params=params, timeout=120, trust_env=False
        )
        view_response.raise_for_status()
        target = self.output_dir / f"generated-{prompt_id[:12]}.png"
        validation = self._finalize_image(
            view_response.content,
            target,
            output_width=output_width,
            output_height=output_height,
        )
        return {
            "path": str(target),
            "prompt_id": prompt_id,
            "checkpoint": checkpoint,
            "width": width,
            "height": height,
            **validation,
        }

    @staticmethod
    def _adult_prompt_allowed(prompt: str) -> tuple[bool, str]:
        raw = unicodedata.normalize("NFKC", str(prompt or "").strip()).casefold()
        value = "".join(char for char in raw if unicodedata.category(char) != "Cf")
        value = re.sub(r"\s+", " ", value)
        if len(value) < 8:
            return False, "Опиши сцену подробнее — хотя бы одним полным предложением."

        # A narrow confusable map catches common mixed Cyrillic/Latin bypasses without changing
        # the actual prompt sent to the model.
        latinized = value.translate(
            str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h"})
        )

        def spaced_keyword(text: str, keyword: str) -> bool:
            pattern = r"(?<!\w)" + r"[\W_]*".join(re.escape(char) for char in keyword) + r"(?!\w)"
            return re.search(pattern, text, re.IGNORECASE) is not None

        minor_pattern = (
            r"\b(?:реб[её]нок|дет(?:и|ей|ский|ская)|малолет|несовершеннолет\w*|"
            r"школьни[кц]\w*|подрост\w*|teen|child|kid|minor|underage|loli|shota)\b"
        )
        minor_spaced = ("teen", "child", "minor", "underage", "loli", "shota", "подросток", "школьница")
        if re.search(minor_pattern, value, re.IGNORECASE) or any(
            spaced_keyword(value, word) or spaced_keyword(latinized, word) for word in minor_spaced
        ):
            return False, "Раздел создаёт только вымышленных взрослых персонажей 21+."

        real_person_pattern = (
            r"\b(?:раздень\w*|раздев\w*|nudify|deepfake|face\s*swap|faceswap|"
            r"copy\s+(?:the\s+)?face|likeness\s+of|looks?\s+like|celebrity|знаменитост\w*|"
            r"реальн\w+\s+(?:человек\w*|девушк\w*|женщин\w*|мужчин\w*))\b"
            r"|похож\w*\s+на|лицо\s+(?:с|из)\s+фото"
        )
        real_spaced = ("nudify", "deepfake", "faceswap")
        if re.search(real_person_pattern, value, re.IGNORECASE) or re.search(real_person_pattern, latinized, re.IGNORECASE) or any(
            spaced_keyword(value, word) or spaced_keyword(latinized, word) for word in real_spaced
        ):
            return False, (
                "Нельзя копировать внешность реального человека или «раздевать» фото. "
                "Опиши полностью вымышленного взрослого персонажа."
            )
        return True, ""

    def generate_adult_image(
        self,
        prompt: str,
        *,
        mode: str = "realistic",
        aspect: str = "portrait",
        timeout: int = 1800,
    ) -> dict[str, Any]:
        """Generate a fictional 21+ character and upscale the final pixels to 4K.

        This route intentionally accepts text only.  It never performs face transfer,
        photo editing or real-person likeness preservation.
        """
        allowed, reason = self._adult_prompt_allowed(prompt)
        if not allowed:
            raise CreativeError(reason)
        mode = str(mode or "realistic").casefold()
        if mode not in {"realistic", "anime"}:
            raise CreativeError("Выбери режим «Реализм» или «Аниме».")
        aspect = str(aspect or "portrait").casefold()
        dimensions = {
            "portrait": (832, 1216, 2160, 3840),
            "square": (1024, 1024, 3840, 3840),
            "landscape": (1216, 832, 3840, 2160),
        }
        if aspect not in dimensions:
            raise CreativeError("Неизвестное соотношение сторон.")
        width, height, out_width, out_height = dimensions[aspect]
        style = (
            "professional RAW photography, photorealistic skin texture, cinematic soft light, "
            "natural pose, 85mm lens, realistic anatomy"
            if mode == "realistic"
            else "masterpiece anime illustration, precise line art, detailed eyes, clean cel shading"
        )
        positive = (
            f"{style}, fictional adult woman, age 25, no resemblance to a real person, "
            f"anatomically correct hands, five fingers on each hand, coherent limbs, {prompt.strip()}"
        )
        negative = (
            "child, teen, minor, underage, loli, shota, real person, celebrity, face copy, "
            "lowres, blurry, motion blur, bad anatomy, bad hands, malformed hands, fused fingers, "
            "extra fingers, missing fingers, extra limbs, missing limbs, deformed face, asymmetrical eyes, "
            "duplicate body, cropped head, text, logo, watermark, jpeg artifacts"
        )
        result = self.generate_image(
            positive,
            negative=negative,
            width=width,
            height=height,
            steps=36 if mode == "realistic" else 32,
            timeout=timeout,
            checkpoint_mode=mode,
            output_width=out_width,
            output_height=out_height,
        )
        result.update({"mode": mode, "aspect": aspect, "fictional_adult": True, "quality": "4k"})
        return result
