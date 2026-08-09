from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(command: list[str], timeout: int = 8) -> str:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, shell=False,
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
    """Choose latency-first local models and reserve heavy models for hard work.

    Interactive desktop control must stay on a small resident multimodal model. Larger
    models are selected only for normal/deep work so a request such as "open Telegram"
    never pays a 20B+ model load penalty.
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

    if vram >= 20 and ram >= 48:
        tier = "power"
        fast, main, code, vision = "qwen3.5:4b", "qwen3.5:9b", "devstral:24b", "qwen3.5:4b"
        whisper = "large-v3-turbo"
    elif vram >= 10 and ram >= 24:
        tier = "balanced"
        fast, main, code, vision = "qwen3.5:4b", "qwen3.5:9b", "qwen3.5:9b", "qwen3.5:4b"
        whisper = "large-v3-turbo"
    elif ram >= 28:
        tier = "balanced"
        # 32-GB-class laptops with 4-GB mobile GPUs need a smaller always-hot lane.
        # Keep Qwen 2B resident for immediate dialogue/tool routing, Gemma 4B for
        # richer non-trivial chat, and gpt-oss only for explicit deep/background work.
        fast, main, code, vision = "qwen3.5:2b", "gemma3:4b", "qwen3.5:4b", "moondream:1.8b-v2-q4_0"
        whisper = "large-v3-turbo"
    elif ram >= 16:
        tier = "standard"
        fast = main = code = vision = "gemma3:4b"
        whisper = "large-v3-turbo"
    else:
        tier = "light"
        fast, main, code, vision = "qwen3.5:0.8b", "qwen3.5:2b", "qwen3.5:2b", "moondream:1.8b-v2-q4_0"
        whisper = "base"

    # Vision is the most memory-sensitive lane. A 4-GB mobile GPU must never try
    # to reserve a 4B/9B vision context; Ollama can otherwise attempt multi-GB CUDA
    # allocations and poison latency for every later turn. Keep a disposable 0.8B VLM
    # for <=6 GB VRAM while retaining the strongest sensible text/deep models in RAM.
    if vram and vram <= 6.0:
        vision = "moondream:1.8b-v2-q4_0"

    return HardwareProfile(
        os=f"{platform.system()} {platform.release()}", cpu=_cpu_name(),
        cpu_cores=physical, cpu_threads=threads, ram_gb=ram, gpu=gpu, vram_gb=vram,
        cuda_available=cuda, recommended_fast_model=fast, recommended_main_model=main,
        recommended_code_model=code, recommended_vision_model=vision,
        recommended_whisper_model=whisper, tier=tier,
    )
