from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

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

    def health(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.base_url}/system_stats", timeout=3)
            response.raise_for_status()
            return {"ok": True, "url": self.base_url}
        except Exception as exc:
            return {"ok": False, "url": self.base_url, "error": str(exc)}

    def _checkpoint(self) -> str:
        if self.settings.comfyui_checkpoint:
            return self.settings.comfyui_checkpoint
        try:
            response = httpx.get(
                f"{self.base_url}/object_info/CheckpointLoaderSimple", timeout=10
            )
            response.raise_for_status()
            data = response.json()
            names = (
                data.get("CheckpointLoaderSimple", {})
                .get("input", {})
                .get("required", {})
                .get("ckpt_name", [[]])[0]
            )
            if names:
                return str(names[0])
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
    ) -> dict[str, Any]:
        return {
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

    def generate_image(
        self,
        prompt: str,
        *,
        negative: str = "low quality, blurry, text, watermark, deformed",
        width: int = 768,
        height: int = 768,
        steps: int = 24,
        timeout: int = 1800,
    ) -> dict[str, Any]:
        if not self.health().get("ok"):
            raise CreativeError(
                "Локальная генерация изображений не настроена. Запустите ComfyUI и укажите EIRVEN_COMFYUI_URL."
            )
        checkpoint = self._checkpoint()
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
        )
        response = httpx.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=30,
        )
        response.raise_for_status()
        prompt_id = str(response.json().get("prompt_id") or "")
        if not prompt_id:
            raise CreativeError("ComfyUI не вернул ID задачи")
        deadline = time.monotonic() + timeout
        output: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            history = httpx.get(f"{self.base_url}/history/{prompt_id}", timeout=20)
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
        data = httpx.get(f"{self.base_url}/view", params=params, timeout=120).content
        suffix = Path(str(first.get("filename") or "image.png")).suffix or ".png"
        target = self.output_dir / f"generated-{prompt_id[:12]}{suffix}"
        target.write_bytes(data)
        return {
            "path": str(target),
            "prompt_id": prompt_id,
            "checkpoint": checkpoint,
            "width": width,
            "height": height,
        }
