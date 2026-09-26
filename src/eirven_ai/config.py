from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .release_policy import TEXT_MODEL, VISION_MODEL, enforce_process_environment



def _apply_storage_layout(root: Path) -> None:
    """Учесть диск, выбранный при установке.

    Установщик может положить модели на другой диск и записать выбор в
    storage.json. Без чтения этого файла приложение продолжило бы искать их на
    системном диске, и голос молча не поднялся бы — ровно тот случай, который уже
    выглядел как «модель пропала». Явно заданные переменные окружения имеют
    приоритет: их выставляет человек и переопределять их нельзя.
    """
    try:
        config_file = root / "storage.json"
        if not config_file.is_file():
            return
        import json as _json

        value = _json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return
        data_root = str(value.get("data_root") or "").strip()
        if data_root and not os.getenv("EIRVEN_SILERO_MODEL"):
            os.environ["EIRVEN_SILERO_MODEL"] = str(
                Path(data_root) / "models" / "silero" / "v5_5_ru.pt"
            )
        ollama_models = str(value.get("ollama_models") or "").strip()
        # Переназначаем каталог моделей только если он действительно существует и
        # непустой. Съёмный диск могли отключить, а папку — удалить: тогда Ollama
        # ушла бы в пустоту и потеряла бы все модели разом, включая зрение. Лучше
        # молча остаться на прежнем расположении, чем увести её в никуда.
        if ollama_models and not os.getenv("OLLAMA_MODELS"):
            candidate = Path(ollama_models)
            try:
                usable = candidate.is_dir() and any(candidate.iterdir())
            except Exception:
                usable = False
            if usable:
                os.environ["OLLAMA_MODELS"] = ollama_models
    except Exception:
        # Раскладка хранения не должна мешать запуску: без неё работают значения
        # по умолчанию, которые верны для обычной установки.
        pass


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    root_dir: Path
    data_dir: Path
    workspace_dir: Path
    host: str
    port: int
    llm_backend: str
    ollama_url: str
    model: str
    fast_model: str
    code_model: str
    vision_model: str
    embedding_model: str
    gguf_model_path: str
    context_size: int
    gpu_layers: int
    chat_num_ctx: int
    task_num_ctx: int
    chat_num_predict: int
    task_num_predict: int
    keep_alive: str
    llm_first_token_timeout: float
    llm_inactivity_timeout: float
    max_agent_steps: int
    command_timeout: int
    max_parallel_tasks: int
    enable_commands: bool
    enable_desktop_control: bool
    enable_browser: bool
    auto_memory: bool
    auto_route: bool
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    piper_model: str
    voice_silence_ms: int
    telegram_enabled: bool
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    telegram_min_reply_interval: float
    full_access: bool = True
    semantic_memory: bool = False
    enable_game_control: bool = False
    companion_enabled: bool = True
    strict_release_model: bool = False
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_checkpoint: str = ""
    deep_model: str = "qwen3.5:4b"
    asr_engine: str = "gigaam"
    gigaam_model: str = "gigaam-v3-e2e-ctc"
    tts_engine: str = "auto"
    silero_model: str = ""
    expressive_tts_model: str = ""
    expressive_tts_speaker: str = "Serena"
    expressive_tts_design_model: str = ""
    # Production routing is model-first: semantic admission and the reactive
    # tool loop own all natural-language actions.  Legacy deterministic helpers
    # remain callable for compatibility/regression tests, but are not a front door.
    model_only_mode: bool = True
    confirmation_mode: str = "full"

    @classmethod
    def load(cls) -> "Settings":
        # Run scripts always cd to project root. This fallback also supports imports from elsewhere.
        root = Path(os.getenv("EIRVEN_ROOT_DIR", Path.cwd())).expanduser().resolve()
        _load_dotenv(root / ".env")
        _apply_storage_layout(root)
        # Model keys are defaults here; build_services applies the measured hardware
        # profile before any inference component is constructed.
        enforce_process_environment()

        data = Path(os.getenv("EIRVEN_DATA_DIR", "./data")).expanduser()
        workspace = Path(os.getenv("EIRVEN_WORKSPACE_DIR", "./workspace")).expanduser()
        if not data.is_absolute():
            data = root / data
        if not workspace.is_absolute():
            workspace = root / workspace
        data = data.resolve()
        workspace = workspace.resolve()
        data.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)

        model = os.getenv("EIRVEN_MODEL", TEXT_MODEL).strip() or TEXT_MODEL
        return cls(
            root_dir=root,
            data_dir=data,
            workspace_dir=workspace,
            host=os.getenv("EIRVEN_HOST", "0.0.0.0"),
            port=_int("EIRVEN_PORT", 7860),
            llm_backend=os.getenv("EIRVEN_LLM_BACKEND", "ollama").strip().lower(),
            ollama_url=os.getenv("EIRVEN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            model=model,
            fast_model=os.getenv("EIRVEN_FAST_MODEL", model).strip() or model,
            code_model=os.getenv("EIRVEN_CODE_MODEL", model).strip() or model,
            deep_model=os.getenv("EIRVEN_DEEP_MODEL", model).strip() or model,
            vision_model=os.getenv("EIRVEN_VISION_MODEL", VISION_MODEL).strip() or VISION_MODEL,
            embedding_model="",
            gguf_model_path=os.getenv("EIRVEN_GGUF_MODEL_PATH", ""),
            context_size=_int("EIRVEN_CONTEXT_SIZE", 32768),
            gpu_layers=_int("EIRVEN_GPU_LAYERS", 0),
            chat_num_ctx=_int("EIRVEN_CHAT_NUM_CTX", 4096),
            task_num_ctx=_int("EIRVEN_TASK_NUM_CTX", 8192),
            chat_num_predict=_int("EIRVEN_CHAT_NUM_PREDICT", 384),
            task_num_predict=_int("EIRVEN_TASK_NUM_PREDICT", 4096),
            keep_alive=os.getenv("EIRVEN_KEEP_ALIVE", "2h"),
            llm_first_token_timeout=_float("EIRVEN_LLM_FIRST_TOKEN_TIMEOUT", 120.0),
            llm_inactivity_timeout=_float("EIRVEN_LLM_INACTIVITY_TIMEOUT", 45.0),
            max_agent_steps=_int("EIRVEN_MAX_AGENT_STEPS", 24),
            command_timeout=_int("EIRVEN_COMMAND_TIMEOUT", 300),
            max_parallel_tasks=max(1, min(_int("EIRVEN_MAX_PARALLEL_TASKS", 2), 4)),
            enable_commands=_bool("EIRVEN_ENABLE_COMMANDS", True),
            enable_desktop_control=_bool("EIRVEN_ENABLE_DESKTOP_CONTROL", True),
            full_access=_bool("EIRVEN_FULL_ACCESS", True),
            enable_browser=_bool("EIRVEN_ENABLE_BROWSER", True),
            auto_memory=_bool("EIRVEN_AUTO_MEMORY", True),
            auto_route=_bool("EIRVEN_AUTO_ROUTE", True),
            whisper_model=os.getenv("EIRVEN_WHISPER_MODEL", "large-v3-turbo"),
            whisper_device=os.getenv("EIRVEN_WHISPER_DEVICE", "cpu"),
            whisper_compute_type=os.getenv("EIRVEN_WHISPER_COMPUTE_TYPE", "int8"),
            piper_model=os.getenv("EIRVEN_PIPER_MODEL", ""),
            asr_engine=os.getenv("EIRVEN_ASR_ENGINE", "gigaam").strip().lower(),
            gigaam_model=os.getenv("EIRVEN_GIGAAM_MODEL", "gigaam-v3-e2e-ctc").strip(),
            tts_engine=os.getenv("EIRVEN_TTS_ENGINE", "auto").strip().lower(),
            silero_model=os.getenv("EIRVEN_SILERO_MODEL", "").strip(),
            expressive_tts_model=os.getenv("EIRVEN_EXPRESSIVE_TTS_MODEL", "").strip(),
            expressive_tts_speaker=os.getenv("EIRVEN_EXPRESSIVE_TTS_SPEAKER", "Serena").strip(),
            expressive_tts_design_model=os.getenv("EIRVEN_EXPRESSIVE_TTS_DESIGN_MODEL", "").strip(),
            model_only_mode=_bool("EIRVEN_MODEL_ONLY_MODE", True),
            voice_silence_ms=_int("EIRVEN_VOICE_SILENCE_MS", 600),
            telegram_enabled=_bool("EIRVEN_TELEGRAM_ENABLED", False),
            telegram_api_id=_int("EIRVEN_TELEGRAM_API_ID", 0),
            telegram_api_hash=os.getenv("EIRVEN_TELEGRAM_API_HASH", ""),
            telegram_phone=os.getenv("EIRVEN_TELEGRAM_PHONE", ""),
            telegram_min_reply_interval=max(
                2.0, _float("EIRVEN_TELEGRAM_MIN_REPLY_INTERVAL", 8.0)
            ),
            semantic_memory=_bool("EIRVEN_SEMANTIC_MEMORY", False),
            enable_game_control=_bool("EIRVEN_ENABLE_GAME_CONTROL", False),
            companion_enabled=_bool("EIRVEN_COMPANION_ENABLED", True),
            strict_release_model=True,
            comfyui_url=os.getenv("EIRVEN_COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            comfyui_checkpoint=os.getenv("EIRVEN_COMFYUI_CHECKPOINT", ""),
        )
