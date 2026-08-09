from __future__ import annotations

import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

# The source-only release can run these parser tests before optional runtime packages are
# installed. ChatService itself does not use HTTPX in the tested deterministic paths.
try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("httpx")
    stub.Response = object
    stub.Timeout = object
    sys.modules["httpx"] = stub

from eirven_ai.chat import ChatService
from eirven_ai.cognition import AgentCognition
from eirven_ai.desktop_operator import DesktopOperator
from eirven_ai.telegram_service import TelegramError, TelegramMonitor
from eirven_ai.tools import ToolExecutor
from eirven_ai.voice import VoiceService
from eirven_ai.voice_daemon import NativeVoiceDaemon


class _SettingsDB:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get_setting(self, key: str, default=None):
        return self.values.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.values[key] = value


class R26AgentTests(unittest.TestCase):
    def test_compound_telegram_command_keeps_every_payload(self) -> None:
        text = "открой Telegram, напиши Тиме: привет Кириллу привет и в избранное отправь привет"
        self.assertEqual(
            ChatService._parse_telegram_batch(text),
            [("тима", "привет"), ("кирилл", "привет"), ("Избранное", "привет")],
        )
        self.assertEqual(ChatService._parse_telegram_command("в избранное напиши привет"), ("Избранное", "привет"))

    def test_bare_shutdown_is_precise(self) -> None:
        self.assertTrue(ChatService._self_shutdown_requested("выключи"))
        self.assertTrue(ChatService._self_shutdown_requested("Эрви выключись"))
        self.assertFalse(ChatService._self_shutdown_requested("выключи музыку"))

    def test_voice_cancel_bypasses_wake_and_tolerates_asr_variant(self) -> None:
        identity = SimpleNamespace(get=lambda: SimpleNamespace(assistant_name="Эрви"))
        daemon = NativeVoiceDaemon.__new__(NativeVoiceDaemon)
        daemon.services = SimpleNamespace(identity=identity)
        for phrase in ("Отмена", "Первая отмена", "Эрви, отмени задачу", "остановить"):
            self.assertTrue(daemon._is_emergency_cancel(phrase), phrase)
        self.assertFalse(daemon._is_emergency_cancel("останови музыку"))

    def test_asr_probe_is_a_valid_silent_wav(self) -> None:
        payload = VoiceService._probe_wav(seconds=0.1)
        self.assertTrue(payload.startswith(b"RIFF"))
        with wave.open(__import__("io").BytesIO(payload), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16_000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertGreater(wav.getnframes(), 1_000)

    def test_telegram_result_rejects_description_substring(self) -> None:
        operator = DesktopOperator.__new__(DesktopOperator)
        wrong = {
            "visible": True, "enabled": True, "control_type": "Button",
            "class_name": "ListItem Button", "name": "@vtememd 76477 subscribers в теме КТ",
            "rectangle": [20, 300, 700, 410],
        }
        right = {**wrong, "name": "Тима Тима last seen recently"}
        self.assertIsNone(operator._telegram_result_score(wrong, "тима"))
        self.assertIsNotNone(operator._telegram_result_score(right, "тима"))

    def test_file_checkpoint_and_skill_learning(self) -> None:
        db = _SettingsDB()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cognition = AgentCognition(db, root)
            target = root / "project.py"
            target.write_text("before", encoding="utf-8")
            cognition.capture_file(target, label="edit")
            target.write_text("after", encoding="utf-8")
            self.assertTrue(cognition.undo_last()["ok"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            for _ in range(3):
                result = cognition.record_outcome(
                    "почини проект", "VS Code", strategy=1, ok=True, verified=True,
                    steps=[{"action": "run_tests", "reason": "verified"}],
                )
            self.assertTrue(result["skill_suggestion"])
            self.assertTrue(cognition.save_last_as_skill("Починка проекта")["ok"])

    def test_remote_control_requires_exact_allowlist(self) -> None:
        monitor = TelegramMonitor.__new__(TelegramMonitor)
        monitor.db = _SettingsDB()
        with self.assertRaises(TelegramError):
            monitor.save_remote_config(True, ["*"])
        config = monitor.save_remote_config(True, ["12345", "@owner_chat"], "Эрви,")
        self.assertEqual(config["chats"], ["12345", "owner_chat"])

    def test_cancel_generation_survives_global_reset(self) -> None:
        executor = ToolExecutor.__new__(ToolExecutor)
        executor.stop_event = threading.Event()
        executor._scope = threading.local()
        executor._stop_lock = threading.RLock()
        executor._stop_generation = 0
        executor._process_lock = threading.RLock()
        executor._active_processes = {}
        with executor.task_scope(threading.Event()):
            self.assertFalse(executor._stop_requested())
            executor.stop()
            executor.reset_stop()
            self.assertTrue(executor._stop_requested())


if __name__ == "__main__":
    unittest.main()
