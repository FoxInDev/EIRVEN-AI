from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from .agent import LocalAgent
from .applications import ApplicationError, ApplicationService
from .companion import DesktopCompanion
from .creative import CreativeService
from .browser import BrowserAutomation
from .chat import ChatService
from .chat_jobs import ChatJobManager
from .config import Settings
from .database import Database
from .hardware import HardwareProfile, detect_hardware
from .identity import IdentityService
from .llm import ModelGateway
from .game import GamePilot
from .memory import MemoryStore
from .model_router import ModelRouter
from .orchestrator import IntentRouter
from .projects import ProjectBuilder
from .social import RelationshipStore, SocialMirror
from .style import StyleStore
from .tasks import TaskContext, TaskManager, TaskNeedsUser
from .telegram_service import TelegramMonitor
from .tools import ToolExecutor
from .voice import VoiceService
from .voice_daemon import NativeVoiceDaemon
from .system_browser import open_url as open_system_url
from .modes import ModeController
from .proactive import ProactiveObserver
from .ambient import AmbientMusic
from .runtime_control import RuntimeControl
from .capabilities import CapabilityRegistry
from .offline_cache import OfflineCache
from .interface_learning import InterfaceLearning
from .desktop_operator import DesktopOperator
from .app_skills import AppSkills
from .selftest import StartupSelfTest
from .action_planner import ActionPlanner
from .result_verifier import ResultVerifier
from .recovery import RecoveryEngine
from .universal_workflow import UniversalWorkflowEngine
from .autonomous_workflow import AutonomousWorkflowEngine
from .mission_engine import MissionEngine


TELEGRAM_RULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "chats": {"type": "array", "items": {"type": "string"}},
        "pattern": {"type": "string"},
        "reply": {"type": "string"},
        "mode": {"type": "string", "enum": ["template", "ai"]},
        "max_per_hour": {"type": "integer"},
    },
    "required": ["name", "chats", "pattern", "reply", "mode", "max_per_hour"],
}


MOVIE_CHOICES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "choices": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["title", "year", "why"],
            },
        }
    },
    "required": ["choices"],
}


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    hardware: HardwareProfile
    gateway: ModelGateway
    router: ModelRouter
    memory: MemoryStore
    style: StyleStore
    relationships: RelationshipStore
    chat: ChatService
    chat_jobs: ChatJobManager
    social: SocialMirror
    projects: ProjectBuilder
    browser: BrowserAutomation
    tools: ToolExecutor
    agent: LocalAgent
    voice: VoiceService
    telegram: TelegramMonitor
    tasks: TaskManager
    intents: IntentRouter
    identity: IdentityService
    applications: ApplicationService
    companion: DesktopCompanion
    game: GamePilot
    creative: CreativeService
    voice_daemon: NativeVoiceDaemon | None = None
    camera: Any | None = None
    modes: ModeController | None = None
    proactive: ProactiveObserver | None = None
    ambient: AmbientMusic | None = None
    runtime: RuntimeControl | None = None
    capabilities: CapabilityRegistry | None = None
    offline_cache: OfflineCache | None = None
    learning: InterfaceLearning | None = None
    desktop_operator: DesktopOperator | None = None
    app_skills: AppSkills | None = None
    selftest: StartupSelfTest | None = None
    planner: ActionPlanner | None = None
    verifier: ResultVerifier | None = None
    recovery: RecoveryEngine | None = None
    universal_workflow: UniversalWorkflowEngine | None = None
    autonomous_workflow: AutonomousWorkflowEngine | None = None
    mission_engine: MissionEngine | None = None


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or Settings.load()
    db = Database(settings.data_dir / "eirven.db")
    desktop_override = db.get_setting("desktop_control_enabled", None)
    if not db.get_setting("v031_desktop_default_applied", False):
        # v0.3.1 intentionally enables the single computer-access permission by default.
        settings.enable_desktop_control = True
        db.set_setting("desktop_control_enabled", True)
        db.set_setting("v031_desktop_default_applied", True)
    elif isinstance(desktop_override, bool):
        settings.enable_desktop_control = desktop_override
    hardware = detect_hardware()
    identity = IdentityService(db)

    def self_gendered(female: str, male: str) -> str:
        try:
            return female if identity.get().gender == "female" else male
        except Exception:
            return female

    def gender_guard(text: str) -> str:
        """Keep background-agent reports aligned with the selected identity gender."""
        try:
            if identity.get().gender != "female":
                return text
        except Exception:
            pass
        import re
        pairs = {
            "сделал":"сделала", "открыл":"открыла", "нашёл":"нашла", "нашел":"нашла",
            "понял":"поняла", "решил":"решила", "проверил":"проверила", "запустил":"запустила",
            "выполнил":"выполнила", "смог":"смогла", "увидел":"увидела", "заметил":"заметила",
            "отправил":"отправила", "закрыл":"закрыла", "включил":"включила", "выключил":"выключила",
            "исправил":"исправила", "починил":"починила", "загрузил":"загрузила", "получил":"получила",
            "добавил":"добавила", "выбрал":"выбрала", "ответил":"ответила", "готов":"готова",
            "был":"была", "рад":"рада", "согласен":"согласна", "уверен":"уверена",
            "закончил":"закончила", "начал":"начала", "продолжил":"продолжила",
            "подготовил":"подготовила", "сохранил":"сохранила", "создал":"создала",
            "установил":"установила", "удалил":"удалила", "обновил":"обновила",
            "изменил":"изменила", "настроил":"настроила", "подключил":"подключила",
            "перешёл":"перешла", "перешел":"перешла", "вернулся":"вернулась",
            "остановился":"остановилась", "разобрался":"разобралась", "ошибся":"ошиблась",
            "попробовал":"попробовала", "написал":"написала", "прочитал":"прочитала",
            "скачал":"скачала",
        }
        out = str(text or "")
        for male, female in pairs.items():
            out = re.sub(rf"\bя\s+{male}\b", lambda m, f=female: ("Я " if m.group(0)[:1].isupper() else "я ") + f, out, flags=re.I)
            out = re.sub(rf"(^|[.!?]\s+)({male})\b", lambda m, f=female: m.group(1) + (f[:1].upper()+f[1:] if m.group(2)[:1].isupper() else f), out, flags=re.I)
        out = re.sub(r"\bбыла\s+уверен\b", "была уверена", out, flags=re.I)
        out = re.sub(r"\bбыла\s+рад\b", "была рада", out, flags=re.I)
        out = re.sub(r"\bбыла\s+согласен\b", "была согласна", out, flags=re.I)
        return out

    # One understandable permission controls all desktop interaction. Game adapters are
    # internal implementation details and no longer require a second checkbox.
    settings.enable_game_control = bool(settings.enable_desktop_control)
    gateway = ModelGateway(settings)
    router = ModelRouter(settings, gateway, hardware)
    memory = MemoryStore(
        db, gateway, settings.embedding_model, semantic_enabled=settings.semantic_memory
    )
    style = StyleStore(db)
    relationships = RelationshipStore(db)
    browser = BrowserAutomation(settings)
    applications = ApplicationService(browser, settings.data_dir / "application_index.json")
    # Warm the Start-menu index in the background so the first "open X" command does
    # not pay the PowerShell enumeration cost.
    threading.Thread(target=applications.list_installed, daemon=True, name="eirven-app-index").start()
    tools = ToolExecutor(settings, db, browser, applications)
    agent = LocalAgent(settings, gateway, tools, style)
    projects = ProjectBuilder(settings, gateway)
    voice = VoiceService(settings, db, identity)
    telegram = TelegramMonitor(settings, db, gateway, style)
    tasks = TaskManager(db, settings.max_parallel_tasks)
    intents = IntentRouter()
    companion = DesktopCompanion(
        db, identity, f"http://{settings.host}:{settings.port}/ui/"
    )
    game = GamePilot(settings, gateway, tools)
    creative = CreativeService(settings)
    camera = None
    modes = ModeController(settings, db, applications, tools, camera)
    runtime = RuntimeControl()
    offline_cache = OfflineCache(db)
    learning = InterfaceLearning(db)

    chat = ChatService(settings, db, gateway, router, memory, style, identity)
    chat.attach_runtime(tools, tasks, modes=modes, camera=camera, voice=voice)
    chat_jobs = ChatJobManager(db, chat, max_workers=2)

    services = Services(
        settings=settings,
        db=db,
        hardware=hardware,
        gateway=gateway,
        router=router,
        memory=memory,
        style=style,
        relationships=relationships,
        chat=chat,
        chat_jobs=chat_jobs,
        social=SocialMirror(settings, gateway, style, relationships),
        projects=projects,
        browser=browser,
        tools=tools,
        agent=agent,
        voice=voice,
        telegram=telegram,
        tasks=tasks,
        intents=intents,
        identity=identity,
        applications=applications,
        companion=companion,
        game=game,
        creative=creative,
        camera=camera,
        modes=modes,
        runtime=runtime,
        offline_cache=offline_cache,
        learning=learning,
    )

    # Components that need the assembled Services graph are bound in a second phase.
    runtime.bind(services)
    # ToolExecutor is created before RuntimeControl.  Bind it now so every risky GUI
    # primitive can respect the voice pre-commit hold without coupling tools.py to the
    # whole Services graph.
    setattr(tools, "runtime_control", runtime)
    capabilities = CapabilityRegistry(services)
    desktop_operator = DesktopOperator(services, learning)
    app_skills = AppSkills(services, desktop_operator)
    startup_selftest = StartupSelfTest(services)
    planner = ActionPlanner()
    # Bind the live desktop/app components before constructing recovery/workflow
    # engines: those engines capture/access these references immediately.
    services.capabilities = capabilities
    services.desktop_operator = desktop_operator
    services.app_skills = app_skills
    verifier = ResultVerifier(services)
    services.verifier = verifier
    recovery = RecoveryEngine(services)
    services.recovery = recovery
    universal_workflow = UniversalWorkflowEngine(services)
    autonomous_workflow = AutonomousWorkflowEngine(services)
    mission_engine = MissionEngine(services)
    services.selftest = startup_selftest
    services.planner = planner
    services.verifier = verifier
    services.recovery = recovery
    services.universal_workflow = universal_workflow
    services.autonomous_workflow = autonomous_workflow
    services.mission_engine = mission_engine
    setattr(modes, "runtime", runtime)
    setattr(chat, "runtime", runtime)
    setattr(chat, "capabilities", capabilities)
    setattr(chat, "offline_cache", offline_cache)
    setattr(chat, "learning", learning)
    setattr(chat, "desktop_operator", desktop_operator)
    setattr(chat, "app_skills", app_skills)
    setattr(chat, "recovery", recovery)
    setattr(chat, "verifier", verifier)
    setattr(chat, "planner", planner)
    setattr(chat, "universal_workflow", universal_workflow)
    setattr(chat, "autonomous_workflow", autonomous_workflow)
    setattr(chat, "mission_engine", mission_engine)

    def notify(context: TaskContext, content: str, metadata: dict[str, Any]) -> None:
        if context.conversation_id:
            memory.add_message(context.conversation_id, "assistant", gender_guard(content), metadata)

    def project_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        model = router.task_model("project")
        with tools.task_scope(context.stop_event):
            with gateway.background(context.stop_event):
                result = projects.build_production(context, payload, tools, agent, model)
        verification = "Тесты прошли" if result.get("verified") else "Нужна ручная проверка тестов"
        notify(
            context,
            (
                f"Проект «{result['project_name']}» готов. {verification}.\n"
                f"Папка: {result['project_path']}\n"
                f"Архив: {result['archive_path']}"
            ),
            {"task_id": context.task_id, "kind": "project", "result": result},
        )
        return result

    def project_change_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        model = router.task_model("project")
        with tools.task_scope(context.stop_event):
            with gateway.background(context.stop_event):
                result = projects.modify_production(context, payload, tools, agent, model)
        verification = "Тесты прошли" if result.get("verified") else "Нужна ручная проверка тестов"
        notify(
            context,
            (
                f"Изменения в проекте «{result['project_name']}» готовы. {verification}.\n"
                f"Папка: {result['project_path']}\nАрхив: {result['archive_path']}"
            ),
            {"task_id": context.task_id, "kind": "project_change", "result": result},
        )
        return result

    def agent_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        task = str(payload.get("task") or "").strip()
        if not task:
            raise ValueError("Пустая задача")
        context.set_total(1)
        context.update("Выполняю задачу", completed_steps=0, progress=0.02)
        with gateway.background(context.stop_event):
            report = agent.run(
                task,
                model=router.agent_model(task),
                max_steps=settings.max_agent_steps,
                external_stop_event=context.stop_event,
            )
        context.update("Задача завершена", completed_steps=1, progress=0.99)
        report = gender_guard(report)
        result = {"report": report}
        notify(
            context,
            f"Фоновая задача завершена.\n\n{report}",
            {"task_id": context.task_id, "kind": "agent", "result": result},
        )
        return result

    def mission_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        result = mission_engine.run_task(context, payload)
        summary = str(result.get("summary") or "Миссия завершена.")
        notify(
            context,
            summary,
            {"task_id": context.task_id, "kind": "mission", "result": result},
        )
        return result

    def repair_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Background diagnostic/repair lane that remains pre-emptible by live voice chat."""
        problem = str(payload.get("problem") or "").strip()
        if not problem:
            raise ValueError("Не описана проблема")
        context.set_total(5)
        context.update("Собираю состояние системы", completed_steps=0, progress=0.05)
        evidence: dict[str, Any] = {"problem": problem}
        try:
            evidence["windows"] = tools.execute("window_list", {"max_windows": 60})
        except Exception as exc:
            evidence["windows_error"] = str(exc)
        context.update("Проверяю процессы и экран", completed_steps=1, progress=0.18)
        try:
            evidence["processes"] = tools.execute("process_list", {"limit": 120})
        except Exception as exc:
            evidence["process_error"] = str(exc)
        try:
            evidence["screen"] = tools.execute("screenshot", {})
        except Exception as exc:
            evidence["screen_error"] = str(exc)
        context.update("Ищу актуальные причины и решения", completed_steps=2, progress=0.34)
        try:
            evidence["web"] = tools.execute("web_search", {"query": f"{problem} Windows 10 решение исправить", "max_results": 6})
        except Exception as exc:
            evidence["web_error"] = str(exc)
        context.update("Диагностирую и исправляю", completed_steps=3, progress=0.5)
        prompt = (
            "Команда владельца: ПОЧИНИ. Нужно реально диагностировать и по возможности исправить проблему на этом компьютере. "
            "Используй существующие приложения/окна, файлы, процессы, PowerShell, экран и web_search. Не создавай новый программный проект. "
            "Сначала проверь факты, затем делай минимальные обратимые исправления и после каждого важного шага проверяй результат. "
            "Если нужен логин, физическое подключение устройства или недостаёт однозначных данных — остановись и чётко попроси владельца об одном конкретном действии. "
            "Не удаляй пользовательские данные и не отключай защиту Windows.\n\n"
            f"Проблема: {problem}\n\nНачальные данные:\n{json.dumps(evidence, ensure_ascii=False, default=str)[:24000]}"
        )
        with gateway.background(context.stop_event):
            report = agent.run(
                prompt, model=router.agent_model(problem), max_steps=min(settings.max_agent_steps, 14),
                external_stop_event=context.stop_event,
            )
        report = gender_guard(report)
        context.update("Проверяю результат", completed_steps=4, progress=0.88)
        # One cheap final observation helps catch fixes that did not change the UI/process state.
        try:
            evidence["final_windows"] = tools.execute("window_list", {"max_windows": 60})
        except Exception:
            pass
        context.update("Ремонт завершён", completed_steps=5, progress=0.99)
        result = {"problem": problem, "report": report, "evidence": evidence}
        notify(context, self_gendered(f"Диагностику закончила. {report}", f"Диагностику закончил. {report}"), {"task_id": context.task_id, "kind": "repair", "result": result})
        return result

    def screen_query_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        import base64
        from pathlib import Path

        question = str(payload.get("question") or "Что видно на экране?").strip()
        context.set_total(2)
        context.update("Смотрю на текущий экран", completed_steps=0, progress=0.08)
        shot = tools.execute("screenshot", {})
        path = str((shot.get("result") or {}).get("path") or "") if shot.get("ok") else ""
        if not path or not Path(path).is_file():
            raise RuntimeError(f"Не удалось получить снимок экрана: {shot}")
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        context.update("Анализирую то, что реально видно", completed_steps=1, progress=0.45)
        vision_model=router.task_model("vision")
        # Make this background vision task disposable too; it must not poison the next
        # interactive voice turn on small GPUs.
        try:
            for resident in gateway.installed_models():
                low=str(resident).casefold()
                if low!=str(vision_model).casefold() and any(k in low for k in ("gemma","qwen","gpt-oss","devstral","moondream")):
                    try: gateway.unload(resident)
                    except Exception: pass
        except Exception:
            pass
        with gateway.background(context.stop_event):
            message = gateway.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты зрительный модуль EIRVEN. Анализируй только текущий скриншот владельца. "
                            "Не выдумывай скрытые данные. Если это график или рынок, описывай наблюдаемые "
                            "паттерны и неопределённость, а не обещай будущую цену."
                        ),
                    },
                    {"role": "user", "content": question, "images": [encoded]},
                ],
                model=vision_model,
                temperature=0.10,
                think=False,
                num_ctx=768,
                num_predict=120,
                keep_alive="0",
                timeout_seconds=7,
            )
        answer = str(message.get("content") or "").strip() or "Не смог уверенно разобрать экран."
        context.update("Готово", completed_steps=2, progress=0.99)
        result = {"answer": answer, "screenshot": path}
        notify(context, answer, {"task_id": context.task_id, "kind": "screen_query", "result": result})
        return result

    def git_action_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        import re
        from pathlib import Path

        task = str(payload.get("task") or "").strip()
        remote = str(payload.get("remote") or "").strip()
        context.set_total(4)
        context.update("Нахожу репозиторий", completed_steps=0, progress=0.05)

        path: Path | None = None
        explicit = re.search(r"([A-Za-z]:\\[^\n\r\"<>|?*]+)", task)
        if explicit:
            candidate = Path(explicit.group(1).strip().rstrip(".,"))
            if candidate.is_dir():
                path = candidate
        if path is None:
            named = re.search(r"(?:проект(?:у|а)?\s+(?:с\s+именем\s+)?|папк\w*\s+)[\"«]?([A-Za-zА-Яа-яЁё0-9_. -]{2,64})[\"»]?", task, re.IGNORECASE)
            if named:
                name = named.group(1).strip()
                name = re.split(r"\s+(?:на|в)\s+(?:рабоч|мо|тво|репозитор)|\s+надо\b|\s+нужно\b", name, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                found = tools.execute("system_find", {"name": name, "root": str(Path.home()), "max_results": 20, "max_depth": 6})
                rows = found.get("result") if found.get("ok") else []
                for item in rows or []:
                    candidate = Path(str(item.get("path"))) if isinstance(item, dict) else Path(str(item))
                    if candidate.is_dir() and ((candidate / ".git").exists() or candidate.name.casefold() == name.casefold()):
                        path = candidate
                        break
        if path is None and context.conversation_id:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT result,input FROM tasks WHERE conversation_id=? AND kind IN ('project','project_change') ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                    (context.conversation_id,),
                ).fetchone()
            if row:
                try: result = json.loads(row["result"] or "{}")
                except Exception: result = {}
                try: inp = json.loads(row["input"] or "{}")
                except Exception: inp = {}
                raw = str(result.get("project_path") or "")
                if raw and Path(raw).is_dir(): path = Path(raw)
                elif inp.get("name"):
                    candidate = projects.project_root(str(inp["name"]))
                    if candidate.is_dir(): path = candidate
        if path is None:
            raise TaskNeedsUser("Не нашёл, какой репозиторий коммитить. Назови папку или открой её и скажи её имя.")

        context.update(f"Проверяю Git: {path}", completed_steps=1, progress=0.25)
        status = tools.execute("powershell", {"command": "git status --porcelain=v1 -b", "cwd": str(path), "timeout": 60})
        if not status.get("ok") or int(status.get("result", {}).get("returncode", 1)) != 0:
            raise RuntimeError(f"Git недоступен в {path}: {status}")
        context.update("Добавляю изменения и создаю коммит", completed_steps=2, progress=0.5)
        add = tools.execute("powershell", {"command": "git add -A; git commit -m 'eirven: update'", "cwd": str(path), "timeout": 180})
        add_text = json.dumps(add, ensure_ascii=False)
        if not add.get("ok"):
            raise RuntimeError(add_text)

        wants_push = bool(remote or re.search(r"\b(запуш|push|репозитор)\w*", task, re.IGNORECASE))
        push_result: dict[str, Any] | None = None
        if wants_push:
            context.update("Отправляю в репозиторий", completed_steps=3, progress=0.75)
            if remote:
                escaped = remote.replace("'", "''")
                cmd = f"$r=git remote; if ($r -contains 'origin') {{ git remote set-url origin '{escaped}' }} else {{ git remote add origin '{escaped}' }}; git push -u origin HEAD"
            else:
                cmd = "git push"
            push_result = tools.execute("powershell", {"command": cmd, "cwd": str(path), "timeout": 900})
            details = json.dumps(push_result, ensure_ascii=False).lower()
            if (not push_result.get("ok")) or int(push_result.get("result", {}).get("returncode", 1)) != 0:
                if any(x in details for x in ("authentication", "permission denied", "publickey", "sign in", "authorization", "could not read")):
                    raise TaskNeedsUser("Git требует авторизацию. Войди в GitHub/подтверди SSH-доступ, затем напиши «готово».")
                raise RuntimeError(f"Push не удался: {push_result}")
        context.update("Git готов", completed_steps=4, progress=0.99)
        result = {"path": str(path), "status": status, "commit": add, "push": push_result}
        notify(context, f"Git-задача выполнена для {path}.", {"task_id": context.task_id, "kind": "git_action", "result": result})
        return result

    def crypto_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        context.set_total(2)
        symbol = str(payload.get("symbol") or "bitcoin")
        currency = str(payload.get("currency") or "usd")
        context.update("Получаю актуальную цену", completed_steps=0, progress=0.05)
        price = browser.crypto_price(symbol, currency)
        context.update("Открываю источник в браузере", completed_steps=1)
        browser_result: dict[str, Any] | None = None
        if payload.get("open_browser", True):
            url=f"https://www.coingecko.com/en/coins/{price['asset']}"
            open_system_url(url)
            browser_result={"url":url,"browser":"system_default"}
        context.update("Готово", completed_steps=2, progress=0.99)
        result = {"price": price, "browser": browser_result}
        value = price.get("price")
        notify(
            context,
            f"Актуальная цена {price.get('asset', symbol)}: {value} {currency.upper()}.",
            {"task_id": context.task_id, "kind": "crypto_price", "result": result},
        )
        return result

    def telegram_rule_handler(
        context: TaskContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        request = str(payload.get("request") or "").strip()
        context.set_total(3)
        context.update("Разбираю правило Telegram", completed_steps=0, progress=0.05)
        prompt = f"""
Преобразуй просьбу владельца в одно правило автоответа Telegram.
Просьба: {request}

Правила:
- chats: конкретные @username, ID или названия через массив.
- Если владелец явно сказал всем/во всех чатах, используй ["*"] .
- pattern: Python regex для входящего текста; для всех сообщений ".*".
- reply: точный шаблон ответа. Допустимы {{name}}, {{chat}}, {{text}}.
- mode=template, если ответ полностью задан; mode=ai, если нужно генерировать по инструкции.
- max_per_hour от 1 до 30.
Верни только JSON.
""".strip()
        with gateway.background(context.stop_event):
            rule = gateway.json(
                [
                {"role": "system", "content": "Ты создаёшь строгое правило автоматизации."},
                {"role": "user", "content": prompt},
            ],
            model=router.chat_route("быстро разобрать правило").model,
            temperature=0.1,
            schema=TELEGRAM_RULE_SCHEMA,
                num_predict=800,
            )
        context.update("Сохраняю правило", completed_steps=1)
        telegram.save_rules([rule])
        context.update("Запускаю мониторинг", completed_steps=2)
        status = telegram.start()
        context.update("Мониторинг работает", completed_steps=3, progress=0.99)
        result = {"rule": rule, "status": status}
        notify(
            context,
            f"Правило Telegram сохранено. Статус: {status.get('message', 'запущено')}.",
            {"task_id": context.task_id, "kind": "telegram_rule", "result": result},
        )
        return result

    def identity_change_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        context.set_total(1)
        values = {key: value for key, value in payload.items() if value is not None}
        updated = identity.update(values)
        context.update("Настройки личности сохранены", completed_steps=1, progress=0.99)
        return updated.to_dict()

    def application_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("application") or payload.get("query") or "").strip()
        context.set_total(2)
        context.update("Ищу приложение на компьютере", completed_steps=0, progress=0.1)
        try:
            result = applications.launch(query)
            context.update("Приложение запущено", completed_steps=2, progress=0.99)
            notify(
                context,
                self_gendered(f"Запустила приложение «{result.get('name', query)}».", f"Запустил приложение «{result.get('name', query)}»."),
                {"task_id": context.task_id, "kind": "application_launch", "result": result},
            )
            return result
        except ApplicationError as exc:
            # First try an already-open window without spending an LLM call. This makes
            # "открой Telegram"/"открой VS Code" deterministic even while a project is
            # using Ollama in the background.
            context.update("В меню Пуск не нашёл — проверяю уже открытые окна", completed_steps=1, progress=0.35)
            try:
                listed = tools.execute("window_list", {})
                windows = listed.get("result") if isinstance(listed, dict) and listed.get("ok") else []
                needle = query.casefold()
                aliases = {
                    "телеграм": ("telegram", "телеграм"), "тг": ("telegram", "телеграм"),
                    "telegram": ("telegram", "телеграм"), "vscode": ("visual studio code", "code"),
                    "браузер": ("chrome", "edge", "firefox", "opera", "brave"),
                }
                terms = aliases.get(needle, (needle,))
                match = next((w for w in windows or [] if any(term in str(w.get("title", "")).casefold() for term in terms)), None)
                if match:
                    focused = tools.execute("window_focus", {"handle": int(match["handle"])})
                    result = {"fallback": "existing_window", "window": focused}
                    context.update("Окно открыто", completed_steps=2, progress=0.99)
                    notify(context, self_gendered(f"Открыла уже запущенное окно «{match.get('title', query)}».", f"Открыл уже запущенное окно «{match.get('title', query)}»."), {"task_id": context.task_id, "kind": "application_launch", "result": result})
                    return result
            except Exception:
                pass

            # Telegram has a canonical web client; opening it is safer and faster than
            # asking a model to invent navigation steps when the desktop app is absent.
            if query.casefold() in {"telegram", "телеграм", "тг"}:
                import webbrowser
                open_system_url("https://web.telegram.org/")
                result = {"fallback": "web", "url": "https://web.telegram.org/"}
                context.update("Telegram открыт в браузере", completed_steps=2, progress=0.99)
                notify(context, self_gendered("Открыла Telegram Web в браузере по умолчанию.", "Открыл Telegram Web в браузере по умолчанию."), {"task_id": context.task_id, "kind": "application_launch", "result": result})
                return result

            # For an arbitrary application, fall back to the universal agent. It is
            # explicitly forbidden from creating a software project for a launch request.
            context.update("Ищу ярлык или другой способ запуска", completed_steps=1, progress=0.55)
            with gateway.background(context.stop_event):
                report = agent.run(
                    f"Пользователь попросил открыть или запустить: {query}. "
                    f"Быстрый поиск приложения вернул: {exc}. Найди существующее приложение, окно, "
                    "ярлык или веб-версию и открой наиболее естественный вариант. Не создавай проект, "
                    "не создавай исходники и не устанавливай новое ПО без отдельной просьбы.",
                    model=router.agent_model(query),
                    max_steps=min(settings.max_agent_steps, 8),
                    external_stop_event=context.stop_event,
                )
            context.update("Готово", completed_steps=2, progress=0.99)
            result = {"fallback": "desktop_agent", "report": report}
            notify(context, self_gendered(f"Выполнила запрос через управление компьютером.\n\n{report}", f"Выполнил запрос через управление компьютером.\n\n{report}"), {"task_id": context.task_id, "kind": "application_launch", "result": result})
            return result

    def media_recommend_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        request = str(payload.get("request") or "Посоветуй хороший фильм").strip()
        context.set_total(2)
        context.update("Подбираю фильмы", completed_steps=0, progress=0.08)
        with gateway.background(context.stop_event):
            response = gateway.json(
                [
                {"role": "system", "content": "Ты кинокритик. Предложи ровно три разных фильма, без выдуманных названий."},
                {"role": "user", "content": request},
            ],
            model=router.chat_route(request).model,
            temperature=0.6,
            schema=MOVIE_CHOICES_SCHEMA,
                num_predict=700,
            )
        choices = list(response.get("choices") or [])[:3]
        if context.conversation_id:
            db.set_setting(f"pending_movies:{context.conversation_id}", choices)
        lines = ["Выбери голосом или сообщением: «первый», «второй» или «третий»." ]
        for index, choice in enumerate(choices, 1):
            lines.append(f"{index}. {choice.get('title')} ({choice.get('year')}) — {choice.get('why')}")
        context.update("Варианты готовы", completed_steps=2, progress=0.99)
        result = {"choices": choices}
        notify(context, "\n".join(lines), {"task_id": context.task_id, "kind": "media_recommend", "result": result})
        return result

    def media_open_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        free_only = bool(payload.get("free_only", False))
        if not title:
            raise ValueError("Не выбран фильм")
        context.set_total(2)
        context.update("Ищу, где посмотреть легально", completed_steps=0, progress=0.15)
        result = applications.search_legal_movie(title, free_only=free_only)
        context.update("Открыл варианты в браузере", completed_steps=2, progress=0.99)
        notify(context, self_gendered(f"Открыла поиск легальных вариантов для «{title}».", f"Открыл поиск легальных вариантов для «{title}»."), {"task_id": context.task_id, "kind": "media_open", "result": result})
        return result

    def creative_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Не описано изображение")
        use_as_avatar = bool(payload.get("use_as_avatar", False))
        context.set_total(3)
        context.update("Подготавливаю локальную генерацию", completed_steps=0, progress=0.05)
        result = creative.generate_image(
            prompt,
            width=int(payload.get("width") or 768),
            height=int(payload.get("height") or 768),
            steps=int(payload.get("steps") or 24),
        )
        context.update("Изображение создано", completed_steps=2)
        if use_as_avatar:
            identity.update({"custom_avatar_path": result["path"]})
            companion.stop()
            companion.start()
            result["used_as_avatar"] = True
        context.update("Готово", completed_steps=3, progress=0.99)
        notify(context, f"Изображение готово: {result['path']}", {"task_id": context.task_id, "kind": "creative_image", "result": result})
        return result

    def game_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        goal = str(payload.get("goal") or "играть безопасно").strip()
        window_title = str(payload.get("window_title") or "Minecraft")
        max_minutes = int(payload.get("max_minutes") or 15)
        result = game.run(context, goal, window_title=window_title, max_minutes=max_minutes)
        notify(context, f"Игровая задача остановлена. Статус: {result.get('status')}; шагов: {result.get('steps')}.", {"task_id": context.task_id, "kind": "game", "result": result})
        return result

    def background(handler):
        def wrapped(context: TaskContext, payload: dict[str, Any]):
            with gateway.background(context.stop_event):
                return handler(context, payload)
        return wrapped

    tasks.register("project", background(project_handler))
    tasks.register("project_change", background(project_change_handler))
    tasks.register("agent", background(agent_handler))
    tasks.register("mission", background(mission_handler))
    tasks.register("repair", background(repair_handler))
    tasks.register("git_action", background(git_action_handler))
    tasks.register("screen_query", background(screen_query_handler))
    tasks.register("crypto_price", background(crypto_handler))
    tasks.register("telegram_rule", background(telegram_rule_handler))
    tasks.register("identity_change", background(identity_change_handler))
    tasks.register("application_launch", background(application_handler))
    tasks.register("media_recommend", background(media_recommend_handler))
    tasks.register("media_open", background(media_open_handler))
    tasks.register("game", background(game_handler))
    tasks.register("creative_image", background(creative_handler))
    # r21 removes neuro-music entirely: the output device belongs to speech and media,
    # and the UI no longer exposes a synthetic background-audio control.
    services.ambient = None
    services.voice_daemon = NativeVoiceDaemon(services)

    def companion_status() -> dict[str, Any]:
        status = dict(services.voice_daemon.status())
        try:
            status["runtime"] = dict(services.runtime.status()) if services.runtime is not None else {}
        except Exception:
            status["runtime"] = {}
        try:
            active = next(
                (
                    row for row in services.tasks.list(limit=30)
                    if str(row.get("status") or "") in {"queued", "running", "waiting_user"}
                ),
                None,
            )
            status["active_task"] = dict(active) if isinstance(active, dict) else {}
        except Exception:
            status["active_task"] = {}
        try:
            status["style"] = services.style.get().to_dict()
        except Exception:
            status["style"] = {}
        return status

    try:
        services.companion.set_status_provider(companion_status)
    except Exception:
        pass
    services.proactive = ProactiveObserver(db, lambda: services.voice_daemon, tools)
    return services
