# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass

from .release_policy import TEXT_MODEL, VISION_MODEL
from typing import Any


@dataclass(slots=True)
class HardwareProfile:
    os: str
    cpu: str
    cpu_cores: int
    cpu_threads: int
    ram_gb: float
    gpu: str
    vram_gb: float
    cuda_available: bool
    recommended_fast_model: str
    recommended_main_model: str
    recommended_code_model: str
    recommended_vision_model: str
    recommended_whisper_model: str
    tier: str
    runtime_mode: str
    recommended_parallelism: int
    quality_profile: str
    supported_local: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(command: list[str], timeout: int = 8) -> str:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, shell=False,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
    except Exception:
        pass
    return ""


def _memory_gb() -> float:
    try:
        import psutil  # type: ignore
        return round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception:
        pass
    if os.name == "nt":
        value = _run(["powershell", "-NoProfile", "-Command", "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)"])
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return 0.0
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3), 1)
    except Exception:
        return 0.0


def _cpu_name() -> str:
    value = platform.processor().strip()
    if value:
        return value
    if os.name == "nt":
        result = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)"])
        if result:
            return result
    return platform.machine() or "Unknown CPU"


def _gpu_info() -> tuple[str, float, bool]:
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        output = _run([nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if output:
            first = output.splitlines()[0]
            parts = [part.strip() for part in first.rsplit(",", 1)]
            try:
                return parts[0], round(float(parts[1]) / 1024, 1), True
            except (IndexError, ValueError):
                return first, 0.0, True
    if os.name == "nt":
        output = _run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"])
        if output:
            try:
                parsed = json.loads(output)
                rows = parsed if isinstance(parsed, list) else [parsed]
                best = max(rows, key=lambda row: int(row.get("AdapterRAM") or 0))
                return str(best.get("Name") or "Unknown GPU"), round(int(best.get("AdapterRAM") or 0) / (1024**3), 1), "nvidia" in str(best.get("Name") or "").lower()
            except Exception:
                pass
    return "Integrated / unknown GPU", 0.0, False


def detect_hardware() -> HardwareProfile:
    """Choose the largest responsive local profile that fits this computer.

    Keeping the working model inside GPU/RAM is materially faster than forcing one
    oversized checkpoint on every owner.  Qwen 3.5 keeps chat, code, planning and
    vision on one family, so changing computer class does not change capabilities.
    """
    ram = _memory_gb()
    gpu, vram, cuda = _gpu_info()
    threads = os.cpu_count() or 1
    physical = threads
    try:
        import psutil  # type: ignore
        physical = psutil.cpu_count(logical=False) or threads
    except Exception:
        pass

    is_apple_silicon = platform.system() == "Darwin" and platform.machine().casefold() in {"arm64", "aarch64"}
    if is_apple_silicon and ram >= 24:
        main_model, fast_model, tier, runtime_mode = "qwen3.5:9b", "qwen3.5:4b", "quality", "apple_silicon"
    elif is_apple_silicon and ram >= 12:
        main_model, fast_model, tier, runtime_mode = "qwen3.5:4b", "qwen3.5:2b", "balanced", "apple_silicon"
    elif is_apple_silicon:
        main_model, fast_model, tier, runtime_mode = "qwen3.5:2b", "qwen3.5:2b", "compact", "apple_silicon"
    elif vram >= 22 and ram >= 32:
        main_model, fast_model, tier, runtime_mode = "qwen3.5:27b", "qwen3.5:9b", "ultra", "gpu_resident"
    elif vram >= 7 and ram >= 16:
        # A single Qwen 3.5 checkpoint owns chat, tool planning and vision on an 8 GB
        # card.  Ollama intentionally permits only one resident model on this tier;
        # alternating 4B admission/vision with a 9B planner evicted the warm model and
        # added an observed 8-9 second load before almost every action.  The 4B model
        # is multimodal and tool-capable, stays fully on this GPU, and produces its
        # first useful text/vision result inside the interactive budget.  Keeping one
        # capable model is also more reliable than trying to predict a route before the
        # model has understood the request.
        main_model, fast_model, tier, runtime_mode = "qwen3.5:4b", "qwen3.5:4b", "responsive", "gpu_resident"
    elif vram >= 4.5 or (ram >= 24 and physical >= 8):
        main_model, fast_model, tier = "qwen3.5:4b", "qwen3.5:2b", "balanced"
        runtime_mode = "gpu_resident" if vram >= 4.5 else "cpu_optimized"
    else:
        main_model, fast_model, tier, runtime_mode = "qwen3.5:2b", "qwen3.5:2b", "compact", "low_memory"
    vision = fast_model
    parallel = 2 if vram >= 12 and ram >= 24 else 1
    quality_profile = f"EIRVEN_ADAPTIVE_{tier.upper()}_V3"
    supported = ram >= 8 or vram >= 3

    return HardwareProfile(
        os=f"{platform.system()} {platform.release()}", cpu=_cpu_name(),
        cpu_cores=physical, cpu_threads=threads, ram_gb=ram, gpu=gpu, vram_gb=vram,
        cuda_available=cuda,
        recommended_fast_model=fast_model,
        recommended_main_model=main_model,
        recommended_code_model=main_model,
        recommended_vision_model=vision,
        recommended_whisper_model="large-v3-turbo",
        tier=tier,
        runtime_mode=runtime_mode,
        recommended_parallelism=parallel,
        quality_profile=quality_profile,
        supported_local=supported,
    )
