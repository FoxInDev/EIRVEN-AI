from pathlib import Path

from eirven_ai.config import Settings
from eirven_ai.hardware import HardwareProfile
from eirven_ai.model_router import ModelRouter
from eirven_ai.orchestrator import IntentRouter


class FakeGateway:
    def models(self):
        return ["qwen3:4b", "qwen3:8b", "qwen3-vl:4b"]


def settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path, data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        host="127.0.0.1", port=7860, llm_backend="ollama", ollama_url="http://127.0.0.1:11434",
        model="qwen3:8b", fast_model="qwen3:4b", code_model="qwen3:8b", vision_model="qwen3-vl:4b",
        embedding_model="qwen3-embedding:0.6b", gguf_model_path="", context_size=8192, gpu_layers=0,
        chat_num_ctx=8192, task_num_ctx=16384, chat_num_predict=768, task_num_predict=4096,
        keep_alive="30m", max_agent_steps=24, command_timeout=300, max_parallel_tasks=1,
        enable_commands=True, enable_desktop_control=False, enable_browser=True, auto_memory=True,
        auto_route=True, whisper_model="small", whisper_device="auto", whisper_compute_type="auto",
        piper_model="", voice_silence_ms=900, telegram_enabled=False, telegram_api_id=0,
        telegram_api_hash="", telegram_phone="", telegram_min_reply_interval=8.0,
    )


def hardware() -> HardwareProfile:
    return HardwareProfile(
        os="Windows", cpu="CPU", cpu_cores=8, cpu_threads=16, ram_gb=32, gpu="GPU", vram_gb=8,
        cuda_available=True, recommended_fast_model="qwen3:4b", recommended_main_model="qwen3:8b",
        recommended_code_model="qwen3:8b", recommended_vision_model="qwen3-vl:4b",
        recommended_whisper_model="small", tier="balanced",
    )


def test_simple_chat_disables_thinking(tmp_path: Path):
    route = ModelRouter(settings(tmp_path), FakeGateway(), hardware()).chat_route("Привет, как дела?")
    assert route.model == "qwen3:4b"
    assert route.think is False


def test_complex_code_uses_main_model(tmp_path: Path):
    route = ModelRouter(settings(tmp_path), FakeGateway(), hardware()).chat_route("Спроектируй сложный Python API и тесты")
    assert route.model == "qwen3:8b"
    assert route.think is True


def test_intent_router_creates_background_actions():
    router = IntentRouter()
    assert router.route("Создай приложение для учёта финансов").kind == "project"
    assert router.route("Посмотри цену битка сейчас").kind == "crypto_price"
    assert router.route("Привет, как ты?") is None
