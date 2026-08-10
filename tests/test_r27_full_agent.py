from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("httpx")
    stub.Response = object
    stub.Timeout = object
    sys.modules["httpx"] = stub

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine
from eirven_ai.chat import ChatService
from eirven_ai.dialogue import (
    is_affirmative_confirmation,
    is_chat_pairing_request,
    is_pc_shutdown_cancel_request,
    is_pc_shutdown_request,
    is_resume_confirmation,
)
from eirven_ai.llm import ClaudeCodeLocalBackend
from eirven_ai.identity import VOICE_CATALOG
from eirven_ai.telegram_service import TelegramMonitor
from eirven_ai.universal_workflow import UniversalWorkflowEngine
from eirven_ai.voice import VoiceService
from eirven_ai.voice_daemon import NativeVoiceDaemon


class _DB:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get_setting(self, key: str, default=None):
        return self.values.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.values[key] = value


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        return {"ok": True, "result": {"verified": True, **arguments}}


class _Event:
    def __init__(self, text: str, chat_id: int = 777, *, outgoing: bool = True) -> None:
        self.raw_text = text
        self.chat_id = chat_id
        self.out = outgoing
        self.replies: list[str] = []

    async def get_chat(self):
        return SimpleNamespace(username="owner", title="Нужный чат", first_name="")

    async def get_sender(self):
        return SimpleNamespace(first_name="Дима", username="owner")

    async def reply(self, text: str):
        self.replies.append(text)


class R27FullAgentTests(unittest.TestCase):
    def test_resume_understands_wake_word_and_word_order(self) -> None:
        accepted = (
            "готов", "Эрви, готов", "я уже вошёл, Эрви, продолжай",
            "продолжай, я готов", "код ввёл, можно дальше",
        )
        for phrase in accepted:
            self.assertTrue(is_resume_confirmation(phrase), phrase)
        self.assertFalse(is_resume_confirmation("Эрви, ты готов?"))
        self.assertFalse(is_resume_confirmation("я не готов"))

    def test_both_workflows_prioritize_persisted_checkpoint(self) -> None:
        db = _DB()
        services = SimpleNamespace(db=db)
        for cls in (AutonomousWorkflowEngine, UniversalWorkflowEngine):
            engine = cls.__new__(cls)
            engine.services = services
            db.set_setting(engine._pending_key("chat"), {"goal": "войти и продолжить", "goals": ["войти"]})
            self.assertTrue(engine.should_handle("Эрви, я готов", "chat"), cls.__name__)

    def test_repeated_ready_never_cancels_active_resume(self) -> None:
        chat = ChatService.__new__(ChatService)
        chat._trace = lambda *_args, **_kwargs: None
        chat.memory = SimpleNamespace(ensure_conversation=lambda cid, _mode: cid or "chat")
        chat._lock = threading.RLock()
        active = threading.Event()
        chat._stop_events = {"chat": active}
        chat.autonomous_workflow = None
        chat.universal_workflow = None
        events = list(chat.stream_events(
            "Эрви, готов, продолжай", "chat", persist_user=False, persist_assistant=False,
        ))
        self.assertFalse(active.is_set())
        self.assertEqual(events[-1]["route"]["action"], "checkpoint_resume_in_progress")

    def test_pc_shutdown_requires_confirmation(self) -> None:
        chat = ChatService.__new__(ChatService)
        chat.db = _DB()
        chat.tools = _Tools()
        self.assertTrue(is_pc_shutdown_request("компьютер, Эрви, выключи"))
        self.assertTrue(is_pc_shutdown_request("Эрви, выруби ноутбук"))
        self.assertFalse(is_pc_shutdown_request("Эрви, не выключай компьютер"))
        self.assertTrue(is_pc_shutdown_cancel_request("Эрви, не выключай компьютер"))
        self.assertFalse(is_pc_shutdown_request("Эрви, отключи интернет на компьютере"))
        self.assertFalse(is_pc_shutdown_request("Эрви, выключи монитор компьютера"))
        self.assertTrue(is_affirmative_confirmation("да, выключай"))
        acted, answer, route = chat._power_control_turn("Эрви, выключи компьютер", "phone")
        self.assertTrue(acted)
        self.assertTrue(route["needs_user"])
        self.assertFalse(chat.tools.calls)
        acted, answer, route = chat._power_control_turn("да, выключай", "phone")
        self.assertTrue(acted)
        self.assertIn("15", answer)
        self.assertEqual(chat.tools.calls, [("system_power", {"action": "shutdown", "delay_seconds": 15})])

    def test_stale_telegram_send_never_hijacks_greeting(self) -> None:
        chat = ChatService.__new__(ChatService)
        chat.db = _DB()
        chat.identity = None
        chat.db.set_setting("pending_send", {
            "recipient": "Избранное", "platform": "telegram", "message": "",
            "awaiting": "message", "at": time.time(),
        })
        acted, answer, route = chat._pending_send_turn("Привет")
        self.assertFalse(acted)
        self.assertEqual(answer, "")
        self.assertEqual(route, {})
        self.assertEqual(chat.db.get_setting("pending_send")["message"], "")

    def test_ambiguous_send_continuation_requires_confirmation(self) -> None:
        chat = ChatService.__new__(ChatService)
        chat.db = _DB()
        chat.identity = None
        chat.db.set_setting("pending_send", {
            "recipient": "Избранное", "platform": "telegram", "message": "",
            "awaiting": "message", "at": time.time(),
        })
        acted, answer, route = chat._pending_send_turn("Буду через десять минут")
        self.assertTrue(acted)
        self.assertTrue(route["needs_user"])
        self.assertIn("да, отправляй", answer)
        self.assertTrue(chat.db.get_setting("pending_send")["confirm"])

    def test_self_shutdown_tolerates_common_asr_ending(self) -> None:
        self.assertTrue(ChatService._self_shutdown_requested("Эрви, выключися"))

    def test_file_publish_collects_hosting_and_domain_across_turns(self) -> None:
        chat = ChatService.__new__(ChatService)
        chat.db = _DB()
        chat._recent_attachment_paths = lambda _cid: []
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "site.zip"
            artifact.write_bytes(b"zip")
            query, acted, answer, route, paths = chat._guided_file_publish_turn(
                "Отправил файл, загрузи его на хостинг", "phone", [str(artifact)],
            )
            self.assertTrue(acted)
            self.assertIn("Куда", answer)
            query, acted, answer, route, paths = chat._guided_file_publish_turn("Beget", "phone", [])
            self.assertTrue(acted)
            self.assertIn("домен", answer.casefold())
            query, acted, answer, route, paths = chat._guided_file_publish_turn("example.com", "phone", [])
            self.assertFalse(acted)
            self.assertEqual(route["action"], "publish_brief_complete")
            self.assertIn("Beget", query)
            self.assertIn("example.com", query)
            self.assertIn(str(artifact), query)

    def test_telegram_pairing_replaces_chat_id_from_owner_phrase(self) -> None:
        monitor = TelegramMonitor.__new__(TelegramMonitor)
        monitor.db = _DB()
        monitor._pairing_lock = threading.RLock()
        monitor._pairing = {"code": "123456", "replace": True, "expires_at": time.time() + 600}
        monitor._status = {"running": True, "message": "ready"}
        monitor._remote_handler = lambda *_args: {"answer": "ok"}
        monitor._recent = {}
        monitor._last_reply = {}
        event = _Event("Эрви, сюда буду отправлять команды", chat_id=777, outgoing=True)
        self.assertTrue(is_chat_pairing_request(event.raw_text))
        asyncio.run(monitor._handle_event(event))
        remote = monitor.remote_config()
        self.assertTrue(remote["enabled"])
        self.assertEqual(remote["chats"], ["777"])
        self.assertFalse(monitor.pairing_status()["active"])
        self.assertTrue(event.replies)

    def test_remote_wake_prefix_tolerates_punctuation(self) -> None:
        self.assertEqual(TelegramMonitor._remote_command("Эрви выключи компьютер", "Эрви,"), "выключи компьютер")
        self.assertEqual(TelegramMonitor._remote_command("эйрвен: открой VS Code", "Эрви,"), "открой VS Code")
        self.assertIsNone(TelegramMonitor._remote_command("открой VS Code", "Эрви,"))

    def test_remote_command_must_be_owner_outgoing_message(self) -> None:
        monitor = TelegramMonitor.__new__(TelegramMonitor)
        monitor.db = _DB()
        monitor.db.set_setting("telegram_remote_control", {"enabled": True, "chats": ["777"], "prefix": "Эрви,"})
        monitor.db.set_setting("telegram_rules", [])
        monitor._pairing_lock = threading.RLock()
        monitor._pairing = {}
        calls: list[tuple[str, str]] = []
        monitor._remote_handler = lambda command, chat_id: calls.append((command, chat_id)) or {"answer": "ok"}
        monitor._recent = {}
        monitor._last_reply = {}
        event = _Event("Эрви, выключи компьютер", chat_id=777, outgoing=False)
        asyncio.run(monitor._handle_event(event))
        self.assertEqual(calls, [])
        self.assertEqual(event.replies, [])

    def test_claude_code_prompt_keeps_system_and_dialogue_separate(self) -> None:
        backend = ClaudeCodeLocalBackend.__new__(ClaudeCodeLocalBackend)
        system, prompt = backend._claude_prompt([
            {"role": "system", "content": "Отвечай кратко"},
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Привет!"},
            {"role": "user", "content": "Продолжай"},
        ])
        self.assertEqual(system, "Отвечай кратко")
        self.assertIn("Владелец: Привет", prompt)
        self.assertIn("Эрви: Привет!", prompt)
        self.assertTrue(prompt.endswith("Эрви:"))

    def test_claude_code_uses_hardware_aliases_for_fast_and_main_lanes(self) -> None:
        backend = ClaudeCodeLocalBackend.__new__(ClaudeCodeLocalBackend)
        backend.settings = SimpleNamespace(fast_model="tiny", model="main-source")
        backend.default_model = "main-source"
        backend.claude_fast_model = "eirven-claude:fast"
        backend.claude_main_model = "eirven-claude:main"
        self.assertEqual(backend._claude_model("tiny"), "eirven-claude:fast")
        self.assertEqual(backend._claude_model("main-source"), "eirven-claude:main")
        self.assertEqual(backend._claude_model("deep-source"), "deep-source")

    def test_duplicate_voice_command_is_ignored_while_first_is_running(self) -> None:
        daemon = NativeVoiceDaemon.__new__(NativeVoiceDaemon)
        daemon._last_accepted_command = "привет"
        daemon._last_accepted_at = time.monotonic()
        daemon._generation_active = threading.Event(); daemon._generation_active.set()
        daemon._speaking = threading.Event()
        daemon.services = SimpleNamespace(voice=SimpleNamespace(status=lambda: {"synthesis_active": False}))
        self.assertTrue(daemon._duplicate_command("Привет!", time.monotonic()))

    def test_repeated_fuzzy_wake_greeting_stays_instant(self) -> None:
        chat = ChatService.__new__(ChatService)
        chat.identity = None
        chat.cognition = None
        self.assertIsNotNone(chat._instant_reply("Эрве, привет"))
        self.assertIsNotNone(chat._instant_reply("Привет. Эрви. Эрве, привет."))

    def test_low_energy_vad_false_positive_does_not_open_utterance(self) -> None:
        # Values come from the attached r28 trace: background RMS 0.006–0.008
        # previously opened a 34.98-second recording even when energy never rose.
        self.assertFalse(NativeVoiceDaemon._start_frame_likely(0.0070, True, 0.0065))
        self.assertTrue(NativeVoiceDaemon._start_frame_likely(0.0180, True, 0.0065))
        self.assertFalse(NativeVoiceDaemon._recording_frame_voiced(0.0070, True, 0.0065, 0.025))
        self.assertTrue(NativeVoiceDaemon._recording_frame_voiced(0.0180, True, 0.0065, 0.025))

    def test_public_voice_locks_real_silero_baya(self) -> None:
        self.assertEqual(VOICE_CATALOG["irina_soft"]["preferred_engine"], "silero")
        self.assertEqual(VOICE_CATALOG["irina_soft"]["silero_speaker"], "baya")
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "v5_5_ru.pt"
            model.write_bytes(b"model")
            service = VoiceService.__new__(VoiceService)
            service.settings = SimpleNamespace(silero_model=str(model))
            service.identity = SimpleNamespace(get=lambda: SimpleNamespace(voice_key="irina_soft"))
            service._tts_worker = SimpleNamespace(
                preload=lambda path, *, engine, timeout: calls.append((engine, path)),
            )
            service._voice_runtime = {}
            service._tts_ready = threading.Event()
            service.synthesize = lambda *_args, **_kwargs: "cached.wav"
            service._prewarm_tts()
            self.assertEqual(calls, [("silero", str(model.resolve()))])
            self.assertEqual(service._voice_runtime["locked_tts_engine"], "silero")
            self.assertEqual(service._voice_runtime["locked_tts_speaker"], "baya")
            self.assertTrue(service._tts_ready.is_set())

    def test_listening_ready_does_not_wait_for_tts(self) -> None:
        service = VoiceService.__new__(VoiceService)
        service._stt_ready = threading.Event(); service._stt_ready.set()
        service._tts_ready = threading.Event()
        self.assertTrue(service.interactive_ready())
        self.assertTrue(service.wait_until_ready(0.01))

    def test_public_orb_has_real_alpha_channel(self) -> None:
        from PIL import Image
        path = Path(__file__).resolve().parents[1] / "src" / "eirven_ai" / "web" / "eirven-orb.png"
        image = Image.open(path).convert("RGBA")
        lo, hi = image.getchannel("A").getextrema()
        self.assertEqual(lo, 0)
        self.assertEqual(hi, 255)


if __name__ == "__main__":
    unittest.main()
