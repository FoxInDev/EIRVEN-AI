import threading
import time
from pathlib import Path

from eirven_ai.config import Settings
from eirven_ai.database import Database
from eirven_ai.tools import ToolExecutor


def settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "workspace"; workspace.mkdir()
    return Settings(
        root_dir=tmp_path, data_dir=tmp_path / "data", workspace_dir=workspace,
        host="127.0.0.1", port=7860, llm_backend="ollama", ollama_url="http://127.0.0.1:11434",
        model="main", fast_model="fast", code_model="code", vision_model="vision",
        embedding_model="embed", gguf_model_path="", context_size=8192, gpu_layers=0,
        chat_num_ctx=4096, task_num_ctx=8192, chat_num_predict=512, task_num_predict=2048,
        keep_alive="30m", max_agent_steps=24, command_timeout=300, max_parallel_tasks=1,
        enable_commands=True, enable_desktop_control=False, enable_browser=False, auto_memory=True,
        auto_route=True, whisper_model="tiny", whisper_device="auto", whisper_compute_type="auto",
        piper_model="", voice_silence_ms=900, telegram_enabled=False, telegram_api_id=0,
        telegram_api_hash="", telegram_phone="", telegram_min_reply_interval=8.0,
    )


def test_running_process_can_be_stopped(tmp_path: Path):
    cfg = settings(tmp_path)
    (cfg.workspace_dir / "wait.py").write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
    tools = ToolExecutor(cfg, Database(cfg.data_dir / "eirven.db"))
    result = {}

    def run():
        result.update(tools.execute("run_command", {"command": "python wait.py", "timeout": 30}))

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.25)
    tools.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["ok"] is False
    assert "останов" in result["error"].lower()
