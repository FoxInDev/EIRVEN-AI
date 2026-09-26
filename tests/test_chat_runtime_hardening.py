from __future__ import annotations

import threading
import types
from types import SimpleNamespace

import pytest

from eirven_ai.chat import ChatService
from eirven_ai.chat_jobs import ChatJobManager
from eirven_ai.database import Database, utc_now
from eirven_ai.llm import GenerationMetrics
from eirven_ai.mail_service import MailError, MailService
from eirven_ai.model_router import ModelRoute, ModelRouter
from eirven_ai.runtime_control import RuntimeControl
from eirven_ai.universal_workflow import UniversalWorkflowEngine


class FakeDB:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get_setting(self, key: str, default=None):
        return self.values.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.values[key] = value


def bare_chat(db: FakeDB | None = None) -> ChatService:
    chat = object.__new__(ChatService)
    chat.db = db or FakeDB()
    chat._lock = threading.RLock()
    chat._stop_events = {}
    chat._conversation_locks = {}
    chat._turn_context = threading.local()
    chat.mail = None
    chat.universal_workflow = None
    chat.autonomous_workflow = None
    chat.runtime = None
    return chat


def test_messenger_pending_is_conversation_bound_and_tamper_evident() -> None:
    chat = bare_chat()
    saved = chat._save_messenger_pending(
        {"recipient": "Alice", "platform": "telegram", "message": "hello", "confirm": True},
        "conversation-a",
    )
    assert saved and saved["fingerprint"]
    assert chat._load_messenger_pending("conversation-a")
    assert chat._load_messenger_pending("conversation-b") is None

    key = chat._messenger_pending_key("conversation-a")
    envelope = dict(chat.db.get_setting(key, {}))
    envelope["payload"] = {**envelope["payload"], "message": "tampered"}
    chat.db.set_setting(key, envelope)
    assert chat._load_messenger_pending("conversation-a") is None


def test_telegram_confirmation_binds_resolved_recipient_exact_text_and_surface() -> None:
    chat = bare_chat()
    chat.identity = None
    current = [{"handle": 77, "pid": 9, "title": "Telegram"}]

    class Tools:
        def execute(self, name, _args):
            if name == "window_list":
                return {"ok": True, "result": list(current)}
            return {"ok": True, "result": {}}

    sent = []

    class Skills:
        @staticmethod
        def open(_target):
            return {"ok": True, "verified": True, "window": dict(current[0])}

        @staticmethod
        def send_telegram(recipient, message, *, expected_surface=None):
            sent.append((recipient, message, dict(expected_surface or {})))
            return {"ok": True, "verified": True, "completed": True}

    chat.tools = Tools()
    chat.app_skills = Skills()
    chat.memory = SimpleNamespace(search=lambda *_args, **_kwargs: [
        {"content": "мама в Telegram записана как Мамуля"},
    ])
    staged = chat._save_messenger_pending({
        "recipient": "мама", "platform": "telegram", "message": "Привет",
        "confirm": True,
    }, "telegram-exact")
    payload = dict(staged["payload"])
    assert payload["recipient"] == "Мамуля"
    assert payload["message"] == "Привет"
    assert payload["surface"] == {"handle": 77, "pid": 9, "title": "Telegram"}

    acted, _answer, route = chat._pending_send_turn("да, отправляй", "telegram-exact")
    assert acted and route["action"] == "telegram_send_verified"
    assert sent == [("Мамуля", "Привет", payload["surface"])]
    chat._pending_send_turn("да", "telegram-exact")
    assert len(sent) == 1


def test_telegram_surface_drift_requires_a_new_confirmation_before_send() -> None:
    chat = bare_chat()
    chat.identity = None
    current = [{"handle": 10, "pid": 1, "title": "Telegram"}]

    class Tools:
        def execute(self, name, _args):
            if name == "window_list":
                return {"ok": True, "result": list(current)}
            return {"ok": True, "result": {}}

    sent = []

    class Skills:
        @staticmethod
        def open(_target):
            return {"ok": True, "verified": True, "window": dict(current[0])}

        @staticmethod
        def send_telegram(recipient, message, *, expected_surface=None):
            sent.append((recipient, message, dict(expected_surface or {})))
            return {"ok": True, "verified": True, "completed": True}

    chat.tools = Tools()
    chat.app_skills = Skills()
    chat.memory = SimpleNamespace(search=lambda *_args, **_kwargs: [])
    chat._save_messenger_pending({
        "recipient": "Тима", "platform": "telegram", "message": "привет",
        "confirm": True,
    }, "telegram-drift")
    current[:] = [{"handle": 11, "pid": 2, "title": "Telegram"}]

    acted, answer, route = chat._pending_send_turn("да", "telegram-drift")
    assert acted and route["action"] == "send_surface_reconfirmation"
    assert "новое подтверждение" in answer.casefold()
    assert sent == []
    chat._pending_send_turn("да", "telegram-drift")
    assert sent == [("Тима", "привет", {"handle": 11, "pid": 2, "title": "Telegram"})]


def test_batch_confirmation_prompt_contains_every_exact_message() -> None:
    prompt = ChatService._messenger_confirmation_prompt({
        "batch": [["Алиса", "Первый текст"], ["Боб", "Второй текст"]],
    })
    assert "Алиса: «Первый текст»" in prompt
    assert "Боб: «Второй текст»" in prompt


def test_workflow_checkpoint_supersedes_stale_send_confirmations() -> None:
    chat = bare_chat()
    chat._save_messenger_pending(
        {"recipient": "Alice", "platform": "telegram", "message": "old", "confirm": True},
        "conversation-a",
    )

    class Workflow:
        @staticmethod
        def _pending_key(cid: str) -> str:
            return f"workflow:{cid}"

        @staticmethod
        def has_pending(_cid: str) -> bool:
            return True

    chat.universal_workflow = Workflow()
    chat.db.set_setting("workflow:conversation-a", {
        "risk_confirmation_fingerprint": "exact-risk-fingerprint",
        "saved_at": 100.0,
    })
    owner = chat._pending_confirmation_owner("conversation-a")
    assert owner and owner.kind == "workflow"
    assert owner.fingerprint == "exact-risk-fingerprint"
    assert chat._load_messenger_pending("conversation-a") is None


def test_cancel_clears_only_the_relevant_conversation() -> None:
    chat = bare_chat()
    chat._save_messenger_pending({"recipient": "A", "message": "one"}, "one")
    chat._save_messenger_pending({"recipient": "B", "message": "two"}, "two")
    chat._cancel_pending_actions("one")
    assert chat._load_messenger_pending("one") is None
    assert chat._load_messenger_pending("two") is not None


def test_mail_pending_requires_matching_conversation_and_fingerprint() -> None:
    db = FakeDB()
    service = MailService(db, gateway=None, settings=None)
    db.set_setting(service.DRAFTS_KEY, [{
        "id": "d1", "to": "recipient@example.test", "subject": "subject", "body": "body",
    }])
    staged = service.stage_draft(1, conversation_id="conversation-a")
    fingerprint = staged["confirmation_fingerprint"]
    assert service.pending("conversation-a", fingerprint=fingerprint)
    assert service.pending("conversation-b", fingerprint=fingerprint) is None
    assert service.pending("conversation-a", fingerprint="wrong") is None
    with pytest.raises(MailError):
        service.send_pending()


def test_runtime_finish_and_stop_are_idle_and_ignore_late_background_steps() -> None:
    runtime = RuntimeControl()
    generation = runtime.begin("turn", "music", lane="interactive", cancellable=True)
    runtime.finish("done", generation=generation)
    runtime.step("late proactive observation", tool="window_elements")
    status = runtime.status()
    assert status["action"] == "idle"
    assert status["lane"] == "idle"
    assert status["goal"] == ""
    assert status["cancellable"] is False
    assert status["step"] == "Результат подтверждён"

    runtime.begin("turn", "music", lane="interactive", cancellable=True)
    runtime.stop_interactive()
    runtime.step("late proactive observation", tool="window_elements")
    status = runtime.status()
    assert status["action"] == "idle"
    assert status["cancellable"] is False
    assert status["step"] == "Остановлено"


def test_stop_all_pauses_proactive_observation_until_next_owner_turn() -> None:
    events: list[tuple[str, str]] = []
    proactive = types.SimpleNamespace(
        pause=lambda reason: events.append(("pause", reason)),
        resume=lambda reason: events.append(("resume", reason)),
    )
    runtime = RuntimeControl(types.SimpleNamespace(proactive=proactive))
    runtime.stop_all()
    assert events == [("pause", "stop_all")]
    assert runtime.status()["action"] == "idle"
    runtime.new_turn("продолжай")
    assert events[-1] == ("resume", "owner_turn")


def test_stream_wrapper_finishes_and_unregisters_after_unhandled_error() -> None:
    chat = bare_chat()

    class Memory:
        @staticmethod
        def ensure_conversation(cid, _mode):
            return cid or "generated"

    chat.memory = Memory()
    chat.runtime = RuntimeControl()
    chat._trace = lambda *_args, **_kwargs: None

    def broken_impl(self, *_args, **_kwargs):
        yield {"type": "start", "conversation_id": "c", "route": {}}
        raise RuntimeError("boom")

    chat._stream_events_impl = types.MethodType(broken_impl, chat)
    events = list(chat.stream_events("hello", "c"))
    assert events[-1]["type"] == "error"
    assert "c" not in chat._stop_events
    assert chat.runtime.status()["cancellable"] is False
    assert chat.runtime.status()["action"] == "idle"


def test_priority_arbitration_never_acquires_desktop_lock() -> None:
    chat = bare_chat()
    called = []
    chat._priority_control_turn_locked = lambda query, cid: (called.append((query, cid)) or (False, "", {}))

    class ExplodingLock:
        def __enter__(self):
            raise AssertionError("desktop lock must not be acquired by arbitration")

        def __exit__(self, *_args):
            return False

    chat.app_skills = types.SimpleNamespace(services=types.SimpleNamespace(desktop_lock=ExplodingLock()))
    assert chat._priority_control_turn("hello", "c") == (False, "", {})
    assert called == [("hello", "c")]


def test_image_actions_are_not_misrouted_as_plain_vision_questions() -> None:
    assert ChatService._image_action_request("опубликуй это фото на сайте")
    assert ChatService._image_action_request("сделай фон белым")
    assert not ChatService._image_action_request("что изображено на фото?")


def test_image_only_turn_emits_and_persists_accepted_vision_answer(tmp_path) -> None:
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"not decoded because vision is isolated in this regression")
    persisted: list[tuple[str, str, str, object]] = []

    class Memory:
        @staticmethod
        def ensure_conversation(cid, _mode):
            return cid or "vision-conversation"

        @staticmethod
        def add_message(cid, role, content, metadata=None):
            persisted.append((cid, role, content, metadata))
            return len(persisted)

    chat = bare_chat()
    chat.memory = Memory()
    chat.settings = SimpleNamespace(model_only_mode=False, vision_model="resident-vision")
    chat.identity = None
    chat._trace = lambda *_args, **_kwargs: None
    chat._pending_confirmation_owner = lambda _cid: None
    chat._explicit_local_paths = lambda _query: []
    chat._guided_file_publish_turn = (
        lambda query, _cid, _paths: (query, False, "", {}, [])
    )
    chat._vision_for_path = lambda _path, _question: "На изображении виден микрофон."
    chat.enforce_gender = lambda answer: answer

    events = list(chat.stream_events(
        "Что на изображении?", "vision-conversation", image_paths=[str(image)],
    ))

    assert [event["type"] for event in events] == ["start", "token", "done"]
    assert events[1]["full"] == "На изображении виден микрофон."
    assert events[-1]["route"]["action"] == "vision_direct"
    assert [(role, content) for _cid, role, content, _meta in persisted] == [
        ("user", "Что на изображении?"),
        ("assistant", "На изображении виден микрофон."),
    ]


def test_connected_mail_read_is_sealed_inside_read_only_adapter() -> None:
    chat = bare_chat()

    class Mail:
        review_calls: list[int] = []

        @staticmethod
        def configured() -> bool:
            return True

        @staticmethod
        def pending(*_args, **_kwargs):
            return None

        def review_read_only(self, *, limit: int):
            self.review_calls.append(limit)
            return {
                "verified": True,
                "unread": 2,
                "messages": [{"subject": "one"}, {"subject": "two"}],
            }

        @staticmethod
        def summarize_read_only(_query, _review):
            return "Проверенная важная сводка"

    chat.mail = Mail()
    acted, answer, route = chat._mail_turn(
        "Эрви, проверь мою почту и расскажи, что важного", "mail-conversation",
    )
    assert acted is True
    assert answer == "Проверенная важная сводка"
    assert route["action"] == "authoritative_mail_read"
    assert route["connector"] == "configured-imap"
    assert route["read_only"] is True
    assert chat.mail.review_calls == [20]


def test_connected_mail_skips_foreground_arbitration_before_read_only_adapter() -> None:
    chat = bare_chat()

    class Mail:
        @staticmethod
        def configured() -> bool:
            return True

    class Tools:
        @staticmethod
        def execute(name, _arguments):
            raise AssertionError(f"sealed mail route must not call {name}")

    chat.mail = Mail()
    chat.tools = Tools()
    chat.tasks = None
    chat._pending_confirmation_owner = lambda *_args, **_kwargs: None
    chat._narrow_service_turn = lambda *_args, **_kwargs: (False, "", {})
    chat._phone_guidance_turn = lambda *_args, **_kwargs: (False, "", {})
    chat._power_control_turn = lambda *_args, **_kwargs: (False, "", {})
    chat._self_shutdown_requested = lambda *_args, **_kwargs: False

    assert chat._priority_control_turn(
        "Эрви, проверь мою почту и расскажи, что важного", "mail-conversation",
    ) == (False, "", {})


def test_connected_mail_adapter_consumes_mutations_without_web_fallback() -> None:
    chat = bare_chat()

    class Mail:
        @staticmethod
        def configured() -> bool:
            return True

        @staticmethod
        def pending(*_args, **_kwargs):
            return None

        @staticmethod
        def review_read_only(*_args, **_kwargs):
            raise AssertionError("a write request must not silently become an inbox read")

    chat.mail = Mail()
    acted, answer, route = chat._mail_turn(
        "Напиши письмо клиенту и отправь его", "mail-conversation",
    )
    assert acted is True
    assert "веб-почту не открываю" in answer
    assert route["action"] == "authoritative_mail_clarification"


def test_connected_mail_prepare_replies_creates_local_drafts_without_smtp() -> None:
    chat = bare_chat()

    class Mail:
        @staticmethod
        def configured() -> bool:
            return True

        @staticmethod
        def pending(*_args, **_kwargs):
            return None

        @staticmethod
        def review(**kwargs):
            assert kwargs == {"limit": 20, "prepare_replies": True, "move_spam": False, "read_only": False}
            return {"verified": True, "drafts": [{"id": "1"}, {"id": "2"}]}

    chat.mail = Mail()
    acted, answer, route = chat._mail_turn(
        "Подготовь ответы на важные письма", "mail-drafts-conversation",
    )
    assert acted is True
    assert "2 локальных черновика" in answer
    assert "Ничего не отправлено" in answer
    assert route["action"] == "authoritative_mail_prepare_drafts"
    assert route["prepare_replies"] is True
    assert route["move_spam"] is False


def test_brightness_command_cannot_fall_into_video_lane() -> None:
    chat = bare_chat()
    chat.settings = SimpleNamespace(root_dir=".")

    class Tools:
        @staticmethod
        def execute(name, _args):
            assert name == "powershell"
            return {"ok": True, "result": {"returncode": 0, "stdout": '{"requested":10,"current":10}'}}

    chat.tools = Tools()
    acted, answer, route = chat._screen_brightness_turn("Снизь яркость экрана до 10%")
    assert acted is True
    assert "10%" in answer
    assert route["action"] == "screen_brightness"
    assert route["verified"] is True


def test_explicit_media_start_bypasses_generic_workflow_with_verified_adapter() -> None:
    chat = bare_chat()
    chat.universal_workflow = object()
    chat.app_skills = SimpleNamespace(
        play_music=lambda query: {
            "ok": True, "completed": True, "verified": True, "query": query,
        },
    )
    chat._pending_confirmation_owner = lambda *_args, **_kwargs: None
    chat._pending_send_turn = lambda *_args, **_kwargs: (False, "", {})
    chat._mail_request = lambda *_args, **_kwargs: False
    chat._route_clarification_turn = lambda *_args, **_kwargs: (False, "", {})
    chat._trace = lambda *_args, **_kwargs: None
    acted, _answer, route = chat._priority_control_turn_locked("Включи яндекс музыку", "media-direct")
    assert acted is True
    assert route["action"] == "r23_media_start"
    assert route["verified"] is True


def test_mail_review_read_only_forces_imap_readonly_and_no_side_effects() -> None:
    db = FakeDB()
    service = MailService(db, gateway=None, settings=None)
    selected: list[bool] = []

    class Client:
        def select(self, _folder, readonly=False):
            selected.append(bool(readonly))
            return "OK", []

        @staticmethod
        def uid(command, *_args):
            assert command == "search"
            return "OK", [b""]

        @staticmethod
        def logout():
            return None

    service._imap = lambda: (Client(), {"auto_move_obvious_spam": True})
    service._folders = lambda _client: ("Spam", "Sent")
    result = service.review_read_only(limit=20)
    assert selected == [True]
    assert result["unread"] == 0
    assert result["spam_moved"] == 0
    assert result["drafts"] == []


def test_adaptive_output_budget_targets_measurable_artifact_only() -> None:
    router = object.__new__(ModelRouter)
    router.settings = SimpleNamespace(task_num_predict=4096, task_num_ctx=8192)
    long_route = ModelRoute("model", False, 3072, 384, 0.5, "ordinary")
    router.adapt_output_budget(
        "Дай ровно 80 коротких нумерованных пунктов и затем END_MARKER_80.",
        long_route,
    )
    assert long_route.num_predict >= 2752
    assert long_route.num_predict <= 4096
    assert long_route.num_ctx > 3072

    short_route = ModelRoute("model", False, 3072, 384, 0.5, "ordinary")
    router.adapt_output_budget("Как дела?", short_route)
    assert short_route.num_predict == 384
    assert short_route.num_ctx == 3072


def test_length_finish_is_continued_and_stream_keeps_final_marker() -> None:
    chat = bare_chat()
    chat.settings = SimpleNamespace(
        task_num_predict=4096,
        task_num_ctx=8192,
        llm_first_token_timeout=10,
        llm_inactivity_timeout=10,
    )

    class Gateway:
        def __init__(self):
            self.calls = 0
            self.last_metrics = None

        def stream_chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert not any("Предыдущий ответ оборвался" in str(x.get("content")) for x in messages)
                yield "1. alpha\n2. beta\n"
                self.last_metrics = GenerationMetrics(
                    model="model", generated_tokens=384, requested_tokens=384,
                    generation_seconds=2.0, finish_reason="length", hit_token_limit=True,
                )
            else:
                assert any("Предыдущий ответ оборвался" in str(x.get("content")) for x in messages)
                yield "3. omega\nEND_MARKER_80"
                self.last_metrics = GenerationMetrics(
                    model="model", generated_tokens=80, requested_tokens=768,
                    generation_seconds=0.5, finish_reason="stop", hit_token_limit=False,
                )

    chat.gateway = Gateway()
    route = ModelRoute("model", False, 3072, 384, 0.5, "test")
    generator = chat._stream_answer_with_continuations(
        [{"role": "user", "content": "give a list"}], route, threading.Event(),
    )
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as done:
            answer, metrics = done.value
            break

    assert chat.gateway.calls == 2
    assert answer.endswith("END_MARKER_80")
    assert events[-1]["full"] == answer
    assert metrics["continued_segments"] == 1
    assert metrics["generated_tokens"] == 464
    assert metrics["finish_reason"] == "stop"
    assert metrics["truncated"] is False


def test_long_answer_marker_survives_job_and_message_storage(tmp_path) -> None:
    db = Database(tmp_path / "eirven-test.sqlite3")
    now = utc_now()
    marker = "END_MARKER_STORAGE"
    answer = ("очень длинный ответ\n" * 5000) + marker
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,title,mode,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("conversation", "test", "Друг", now, now),
        )
        conn.execute(
            "INSERT INTO chat_jobs(id,conversation_id,status,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("job", "conversation", "running", now, now),
        )
    manager = ChatJobManager(db, chat=SimpleNamespace())
    manager._finish("job", "done", answer, "", {"model": "test"}, {"truncated": False})
    with db.connect() as conn:
        job = conn.execute("SELECT answer, partial FROM chat_jobs WHERE id='job'").fetchone()
        message = conn.execute(
            "SELECT content FROM messages WHERE conversation_id='conversation' AND role='assistant'"
        ).fetchone()
    assert job["answer"].endswith(marker)
    assert job["partial"].endswith(marker)
    assert message["content"].endswith(marker)
    assert job["answer"] == answer == message["content"]


def test_greetings_are_never_served_from_instant_template() -> None:
    chat = bare_chat()
    assert chat._instant_reply("Эрви, привет") is None
    assert chat._instant_reply("Как ты?") is None


def test_context_independent_turn_omits_stale_dialog_but_keeps_current_query() -> None:
    chat = bare_chat()
    chat.style = SimpleNamespace(get=lambda: SimpleNamespace(prompt=lambda: "style"))
    chat.memory = SimpleNamespace(
        prompt_context=lambda _query: "",
        get_summary=lambda _cid: {"summary": "старая тема"},
        history=lambda _cid, limit=8: [
            {"role": "user", "content": "включи музыку"},
            {"role": "assistant", "content": "старый ответ"},
        ],
    )
    chat.education = SimpleNamespace(
        search=lambda _query, limit=4: [],
        prompt_context_from_hits=lambda _query, _hits: "",
    )
    chat.phone_sync = None
    chat.camera = None
    chat.identity = SimpleNamespace(get=lambda: SimpleNamespace(
        gender="female", user_address="Даниил", emotion_mode="auto",
        action_commentary="adaptive",
    ))
    chat.cognition = None
    chat.settings = SimpleNamespace(root_dir=".")
    chat._task_context = lambda _cid: ""
    chat._project_file_context = lambda _cid, _query: ""
    chat._attachment_media_context = lambda _paths: ("", [])
    chat._encode_images = lambda _paths: []

    messages = chat._messages("conversation", "Друг", "Привет", include_history=False)
    assert [item["role"] for item in messages] == ["system", "user"]
    assert messages[-1]["content"] == "Привет"
    assert "старый ответ" not in str(messages)
    assert "старая тема" not in str(messages)


def test_semantic_decision_is_single_uncached_envelope_and_typed_action_wins() -> None:
    calls = []

    class Gateway:
        @staticmethod
        def installed_models():
            return ["fast"]

        @staticmethod
        def json(_messages, **_kwargs):
            calls.append(1)
            # A small model can contradict its coarse route. The concrete capability
            # must keep connected mail out of conversational generation.
            return {
                "route": "conversation", "action": "mail_review", "target": "",
                "media_action": "unknown", "platform": "", "recipient": "",
                "message": "", "single_step": True, "confidence": 0.91,
                "needs_history": False,
            }

    services = SimpleNamespace(
        gateway=Gateway(), tools=None, desktop_operator=None,
        settings=SimpleNamespace(
            fast_model="fast", model="fast", code_model="fast", keep_alive="5m",
            llm_first_token_timeout=5.0, root_dir=".",
        ),
        mail=SimpleNamespace(configured=lambda: True), hardware=None,
    )
    engine = UniversalWorkflowEngine(services)
    engine._trace = lambda *_args, **_kwargs: None
    decision = engine.semantic_decision("Проверь мою почту и расскажи важное")
    assert calls == [1]
    assert decision["route"] == "computer_action"
    assert decision["action"] == "mail_review"


def test_unknown_action_envelope_cannot_authorize_desktop_tools() -> None:
    """A coarse routing mistake must fail closed into ordinary conversation.

    This is the protocol invariant behind capability/advice questions such as
    ``Умеешь монтировать видео?``: an envelope without a concrete action grants no
    authority to observe, focus or mutate the desktop.
    """

    class Gateway:
        @staticmethod
        def installed_models():
            return ["fast"]

        @staticmethod
        def json(_messages, **_kwargs):
            return {
                "turn_kind": "capability_question",
                "route": "computer_action", "action": "unknown",
                "target": "Умеешь монтировать видео?", "confidence": 0.99,
                "side_effect_required": False, "needs_history": False,
            }

    services = SimpleNamespace(
        gateway=Gateway(), tools=None, desktop_operator=None,
        settings=SimpleNamespace(
            fast_model="fast", model="fast", code_model="fast", keep_alive="5m",
            llm_first_token_timeout=5.0, chat_num_ctx=3072, root_dir=".",
        ),
        mail=SimpleNamespace(configured=lambda: False), hardware=None,
    )
    engine = UniversalWorkflowEngine(services)
    engine._trace = lambda *_args, **_kwargs: None

    decision = engine.semantic_decision("Умеешь монтировать видео?")

    assert decision["action"] == "unknown"
    assert decision["route"] == "conversation"


def test_semantic_service_slot_is_verbatim_grounded_and_effect_contract_is_typed() -> None:
    class Gateway:
        @staticmethod
        def installed_models():
            return ["fast"]

        @staticmethod
        def json(_messages, **_kwargs):
            return {
                "turn_kind": "action", "route": "computer_action",
                "action": "media_control", "target": "Яндекс Музыку",
                "target_kind": "service", "service": "Яндекс Музыку",
                "media_action": "play", "side_effect_required": True,
                "single_step": True, "confidence": 0.99, "needs_history": False,
            }

    services = SimpleNamespace(
        gateway=Gateway(), tools=None, desktop_operator=None,
        settings=SimpleNamespace(
            fast_model="fast", model="fast", code_model="fast", keep_alive="5m",
            llm_first_token_timeout=5.0, chat_num_ctx=3072, root_dir=".",
        ),
        mail=SimpleNamespace(configured=lambda: True), hardware=None,
    )
    engine = UniversalWorkflowEngine(services)
    engine._trace = lambda *_args, **_kwargs: None

    decision = engine.semantic_decision("Включи Яндекс Музыку")
    hint = engine._hint_from_semantic_decision(decision)

    assert decision["target_kind"] == "service"
    assert decision["service"] == "Яндекс Музыку"
    assert decision["target"] == "Яндекс Музыку"
    assert hint["service"] == "Яндекс Музыку"
    assert hint["side_effect_required"] is True


def test_semantic_pending_context_produces_typed_clarification_response() -> None:
    prompts = []

    class Gateway:
        @staticmethod
        def installed_models():
            return ["fast"]

        @staticmethod
        def json(messages, **_kwargs):
            prompts.append(str(messages[-1]["content"]))
            return {
                "turn_kind": "clarification_response", "route": "computer_action",
                "action": "media_control", "target": "Яндекс Музыке",
                "target_kind": "service", "service": "Яндекс Музыке",
                "media_action": "play", "side_effect_required": True,
                "confidence": 0.98, "needs_history": True,
            }

    services = SimpleNamespace(
        gateway=Gateway(), tools=None, desktop_operator=None,
        settings=SimpleNamespace(
            fast_model="fast", model="fast", code_model="fast", keep_alive="5m",
            llm_first_token_timeout=5.0, chat_num_ctx=3072, root_dir=".",
        ),
        mail=SimpleNamespace(configured=lambda: True), hardware=None,
    )
    engine = UniversalWorkflowEngine(services)
    engine._trace = lambda *_args, **_kwargs: None

    decision = engine.semantic_decision(
        "В Яндекс Музыке",
        pending_context={
            "original_task": "Включи музыку",
            "question": "В каком сервисе продолжить?",
            "missing_fields": ["service_or_player"],
        },
    )

    assert "PENDING_CONTEXT=" in prompts[0]
    assert "service_or_player" in prompts[0]
    assert decision["turn_kind"] == "clarification_response"
    assert decision["route"] == "computer_action"
    assert decision["needs_history"] is True
    assert decision["service"] == "Яндекс Музыке"


def test_model_only_semantic_envelope_covers_reported_action_phrasings() -> None:
    calls = []

    class Gateway:
        @staticmethod
        def installed_models():
            return ["fast"]

        @staticmethod
        def json(messages, **_kwargs):
            prompt = str(messages[-1]["content"])
            calls.append(prompt)
            common = {
                "route": "computer_action", "target": "",
                "media_action": "unknown", "platform": "", "recipient": "",
                "message": "", "single_step": True, "needs_history": False,
                "confidence": 0.96,
            }
            if "Открой яндекс музыку и включи мою волну" in prompt:
                return {
                    **common, "action": "open_service", "target": "яндекс музыку",
                    "media_action": "play", "single_step": False,
                }
            if "Открой телеграм и напиши Тиме привет" in prompt:
                return {
                    **common, "action": "message_send", "platform": "телеграм",
                    "recipient": "Тиме", "message": "привет", "single_step": False,
                }
            if "Эрви, проверь мою почту и расскажи, что важного" in prompt:
                return {**common, "action": "mail_review", "single_step": False}
            if "Включи музыку" in prompt:
                return {**common, "action": "media_control", "media_action": "play"}
            raise AssertionError(prompt)

    services = SimpleNamespace(
        gateway=Gateway(), tools=None, desktop_operator=None,
        settings=SimpleNamespace(
            fast_model="fast", model="fast", code_model="fast", keep_alive="5m",
            llm_first_token_timeout=5.0, root_dir=".",
        ),
        mail=SimpleNamespace(configured=lambda: True), hardware=None,
    )
    engine = UniversalWorkflowEngine(services)
    engine._trace = lambda *_args, **_kwargs: None
    cases = [
        ("Включи музыку", "media_control", "", "play"),
        ("Открой яндекс музыку и включи мою волну", "open_service", "яндекс музыку", "play"),
        ("Открой телеграм и напиши Тиме привет", "message_send", "", "unknown"),
        ("Эрви, проверь мою почту и расскажи, что важного", "mail_review", "", "unknown"),
    ]
    for utterance, action, target, media_action in cases:
        decision = engine.semantic_decision(utterance)
        assert decision["route"] == "computer_action"
        assert decision["action"] == action
        assert decision["target"] == target
        assert decision["media_action"] == media_action
        assert decision["needs_history"] is False
    assert len(calls) == len(cases)
    assert decision["needs_history"] is False


def test_typed_mail_capability_reaches_read_only_connector_without_word_router() -> None:
    chat = bare_chat()

    class Mail:
        @staticmethod
        def configured():
            return True

        @staticmethod
        def pending(*_args, **_kwargs):
            return None

        @staticmethod
        def review_read_only(*, limit):
            assert limit == 20
            return {"verified": True, "unread": 1, "messages": [{"subject": "x"}]}

        @staticmethod
        def summarize_read_only(_query, _review):
            return "Сводка из подключённого IMAP"

    chat.mail = Mail()
    acted, answer, route = chat._typed_semantic_turn(
        "Что там у меня нового?", "mail-typed",
        {"action": "mail_review"},
    )
    assert acted is True
    assert answer == "Сводка из подключённого IMAP"
    assert route["action"] == "authoritative_mail_read"
    assert route["read_only"] is True


def test_typed_telegram_request_opens_or_reports_but_never_claims_send() -> None:
    chat = bare_chat()
    chat.memory = SimpleNamespace(search=lambda *_args, **_kwargs: [])
    chat.identity = None
    sent = []

    class Tools:
        @staticmethod
        def execute(name, _arguments):
            if name == "window_list":
                return {"ok": True, "result": []}
            return {"ok": True, "result": {}}

    class Skills:
        @staticmethod
        def open(_target):
            return {"ok": False, "error": "not visible"}

        @staticmethod
        def send_telegram(*args, **kwargs):
            sent.append((args, kwargs))
            raise AssertionError("initial request must only stage a confirmed draft")

    chat.tools = Tools()
    chat.app_skills = Skills()
    acted, answer, route = chat._typed_semantic_turn(
        "Открой Telegram и напиши Тиме привет", "telegram-typed",
        {
            "action": "message_send", "platform": "Telegram",
            "recipient": "Тиме", "message": "привет",
        },
    )
    assert acted is True
    assert sent == []
    assert route["completed"] is False
    assert route["verified"] is False
    assert "не отправляла" in answer.casefold()
    assert chat._load_messenger_pending("telegram-typed")


def test_every_mode_confirms_telegram_open_then_exact_send_separately() -> None:
    chat = bare_chat()
    chat.settings = SimpleNamespace(confirmation_mode="every")
    chat.identity = None
    chat.memory = SimpleNamespace(search=lambda *_args, **_kwargs: [])
    current = []
    opened = []
    sent = []

    class Tools:
        @staticmethod
        def execute(name, _arguments):
            if name == "window_list":
                return {"ok": True, "result": list(current)}
            return {"ok": True, "result": {}}

    class Skills:
        @staticmethod
        def open(target):
            opened.append(target)
            surface = {"handle": 91, "pid": 7, "title": "Telegram"}
            current[:] = [surface]
            return {"ok": True, "verified": True, "window": surface}

        @staticmethod
        def send_telegram(*args, **kwargs):
            sent.append((args, kwargs))
            raise AssertionError("second immutable confirmation has not happened")

    chat.tools = Tools()
    chat.app_skills = Skills()
    acted, answer, route = chat._typed_semantic_turn(
        "Открой телеграм и напиши Тиме привет", "telegram-every",
        {
            "action": "message_send", "platform": "телеграм",
            "recipient": "Тиме", "message": "привет",
        },
    )
    assert acted is True
    assert route["action"] == "open_service_confirmation"
    assert "ничего не введёт и не отправит" in answer
    assert opened == []
    first = chat._load_messenger_pending("telegram-every")
    assert first and first["payload"]["confirm_open"] is True

    acted, answer, route = chat._pending_send_turn("да, открыть", "telegram-every")
    assert acted is True
    assert route["action"] == "send_confirmation"
    assert route["verified"] is True
    assert "Отправить Тиме текст «привет»" in answer
    assert opened == ["telegram"]
    assert sent == []
    second = chat._load_messenger_pending("telegram-every")
    assert second and second["payload"]["confirm"] is True
    assert second["payload"]["surface"]["handle"] == 91


def test_superseded_stream_cannot_emit_or_persist_old_answer() -> None:
    chat = bare_chat()
    assistant_messages = []

    class Memory:
        @staticmethod
        def ensure_conversation(cid, _mode):
            return cid or "c"

        @staticmethod
        def add_message(_cid, role, content, metadata=None):
            if role == "assistant":
                assistant_messages.append((content, metadata))
            return 1

    chat.memory = Memory()
    chat._trace = lambda *_args, **_kwargs: None

    def stale_impl(self, *_args, **_kwargs):
        yield {"type": "start", "conversation_id": "c", "route": {"action": "chat"}}
        with self._lock:
            self._stop_events["c"] = threading.Event()
        yield {"type": "token", "content": "OLD", "full": "OLD"}
        yield {"type": "done", "conversation_id": "c", "answer": "OLD", "route": {"action": "chat"}, "metrics": {}}

    chat._stream_events_impl = types.MethodType(stale_impl, chat)
    events = list(chat.stream_events("old request", "c"))
    assert [event["type"] for event in events] == ["start"]
    assert assistant_messages == []


def test_cancelled_chat_job_cannot_be_resurrected_by_late_worker(tmp_path) -> None:
    db = Database(tmp_path / "cancel-race.sqlite3")
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,title,mode,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("c", "test", "Друг", now, now),
        )
        conn.execute(
            "INSERT INTO chat_jobs(id,conversation_id,status,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("old", "c", "cancelled", now, now),
        )
    manager = ChatJobManager(db, chat=SimpleNamespace())
    manager._finish("old", "done", "STALE", "", {"model": "test"}, {})
    with db.connect() as conn:
        job = conn.execute("SELECT status,answer FROM chat_jobs WHERE id='old'").fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id='c' AND role='assistant'"
        ).fetchone()["n"]
    assert job["status"] == "cancelled"
    assert job["answer"] == ""
    assert count == 0
