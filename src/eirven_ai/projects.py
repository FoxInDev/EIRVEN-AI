from __future__ import annotations

import json
import re
import shutil
import os
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .llm import LLMError, ModelGateway

if TYPE_CHECKING:
    from .agent import LocalAgent
    from .tasks import TaskContext
    from .tools import ToolExecutor


PROJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "architecture": {"type": "string"},
        "run_command": {"type": "string"},
        "test_command": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "required": ["summary", "architecture", "run_command", "test_command", "files"],
}


BLUEPRINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "architecture": {"type": "string"},
        "run_command": {"type": "string"},
        "test_command": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "purpose": {"type": "string"},
                },
                "required": ["path", "purpose"],
            },
        },
    },
    "required": ["summary", "architecture", "run_command", "test_command", "files"],
}


class ProjectBuilder:
    def __init__(self, settings: Settings, gateway: ModelGateway):
        self.settings = settings
        self.gateway = gateway

    @staticmethod
    def clean_name(name: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-")
        if not value:
            value = f"eirven-project-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        return value[:64]

    def project_root(self, name: str) -> Path:
        root = (self.settings.workspace_dir / self.clean_name(name)).resolve()
        if self.settings.workspace_dir not in root.parents:
            raise ValueError("Некорректный путь проекта")
        return root

    def generate_plan(
        self, name: str, description: str, model: str | None = None, *, num_predict: int | None = None
    ) -> dict[str, Any]:
        clean_name = self.clean_name(name)
        prompt = f"""
Создай полностью запускаемый небольшой Python-проект с нуля.
Имя: {clean_name}
Требования пользователя:
{description}

Условия:
- Сервер и бизнес-логика на Python.
- Локальный веб-интерфейс допустимо делать через FastAPI + HTML/CSS/минимальный JS.
- Не используй PHP, Node.js, npm и React.
- Для небольшой утилиты делай минимальную структуру: обычно main.py + README.md + .gitignore; тест только если он реально полезен.
- Максимально предпочитай стандартную библиотеку Python. Не добавляй pyproject/requirements/pytest, если внешние зависимости не нужны.
- Не пиши длинную архитектурную документацию: summary и architecture по 1 короткому предложению.
- Пользователю важна скорость до первого рабочего запуска, а не количество файлов.
- Не вставляй секреты и реальные токены.
- Пути относительные, без ../ и абсолютных путей.
- Содержимое каждого текстового файла верни полностью.
- Не используй Markdown-ограждения внутри JSON.

Верни только объект по JSON-схеме.
""".strip()
        result = self.gateway.json(
            [
                {"role": "system", "content": "Ты сильный Python-архитектор и пишешь рабочий код."},
                {"role": "user", "content": prompt},
            ],
            model=model or self.settings.code_model or self.settings.model,
            temperature=0.15,
            schema=PROJECT_SCHEMA,
            num_ctx=min(self.settings.task_num_ctx, 6144),
            num_predict=num_predict or min(self.settings.task_num_predict, 700),
            timeout_seconds=90,
        )
        self.validate_plan(result)
        return result

    def generate_blueprint(
        self, name: str, description: str, model: str | None = None
    ) -> dict[str, Any]:
        clean_name = self.clean_name(name)
        prompt = f"""
Спроектируй production-ready, но локально запускаемый Python-проект.
Имя проекта: {clean_name}
Техническое задание пользователя:
{description}

Составь точную архитектуру и список файлов. Не пиши содержимое файлов на этом шаге.
Требования:
- Только Python для backend/логики; допускается локальная HTML/CSS/JS оболочка, которую отдаёт FastAPI.
- Полная обработка ошибок, конфигурация через env, README, .gitignore, тесты.
- Минимум зависимостей; никаких секретов.
- Пути только относительные и безопасные.
- Не больше 35 файлов; каждый файл должен быть реально нужен.
- test_command и run_command должны быть одной командой без shell-конвейеров.
Верни только JSON по схеме.
""".strip()
        blueprint = self.gateway.json(
            [
                {"role": "system", "content": "Ты проектируешь компактные, проверяемые Python-системы."},
                {"role": "user", "content": prompt},
            ],
            model=model or self.settings.code_model,
            temperature=0.1,
            schema=BLUEPRINT_SCHEMA,
            num_ctx=min(self.settings.task_num_ctx, 12288),
            num_predict=850,
            timeout_seconds=120,
        )
        self.validate_blueprint(blueprint)
        return blueprint

    def generate_file(
        self,
        name: str,
        description: str,
        blueprint: dict[str, Any],
        file_item: dict[str, str],
        model: str,
    ) -> str:
        file_list = "\n".join(
            f"- {item['path']}: {item['purpose']}" for item in blueprint["files"]
        )
        prompt = f"""
Проект: {name}
ТЗ:
{description}

Архитектура:
{blueprint['architecture']}

Полный список файлов:
{file_list}

Сейчас создай файл: {file_item['path']}
Назначение: {file_item['purpose']}

Верни только полное содержимое этого файла без Markdown-ограждений и объяснений.
Код должен быть согласован с путями и импортами из списка. Не оставляй TODO вместо реализации.
""".strip()
        message = self.gateway.chat(
            [
                {"role": "system", "content": "Ты пишешь один законченный файл production Python-проекта."},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.15,
            think=False,
            num_ctx=min(self.settings.task_num_ctx, 8192),
            num_predict=min(self.settings.task_num_predict, 1400),
            timeout_seconds=150,
        )
        content = (message.get("content") or "").strip()
        content = re.sub(r"^```(?:\w+)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        allowed_empty = {"requirements.txt", ".gitkeep", ".env.example"}
        if not content and Path(file_item["path"]).name not in allowed_empty:
            raise ValueError(f"Модель вернула пустой файл {file_item['path']}")
        return content

    @staticmethod
    def _with_heartbeat(context: "TaskContext", label: str, func):
        """Keep long local inference visibly alive without inventing fake progress."""
        done = threading.Event()
        started = time.monotonic()

        def heartbeat() -> None:
            while not done.wait(8):
                elapsed = int(time.monotonic() - started)
                try:
                    context.update(f"{label} · {elapsed} сек.", progress=0.03)
                except Exception:
                    return

        thread = threading.Thread(target=heartbeat, daemon=True, name="eirven-project-heartbeat")
        thread.start()
        try:
            return func()
        finally:
            done.set()

    def validate_blueprint(self, blueprint: dict[str, Any]) -> None:
        if not isinstance(blueprint, dict) or not isinstance(blueprint.get("files"), list):
            raise ValueError("Модель вернула некорректную архитектуру")
        if not 1 <= len(blueprint["files"]) <= 35:
            raise ValueError("Архитектура должна содержать от 1 до 35 файлов")
        seen: set[str] = set()
        for item in blueprint["files"]:
            if not isinstance(item, dict):
                raise ValueError("Некорректный элемент списка файлов")
            path = Path(str(item.get("path", "")))
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Опасный путь файла: {path}")
            normalized = path.as_posix().lower()
            if normalized in seen:
                raise ValueError(f"Повторяющийся путь: {path}")
            seen.add(normalized)

    def validate_plan(self, plan: dict[str, Any]) -> None:
        if not isinstance(plan, dict) or not isinstance(plan.get("files"), list):
            raise ValueError("Некорректный план проекта")
        if len(plan["files"]) > 80:
            raise ValueError("Модель предложила слишком много файлов")
        total = 0
        for item in plan["files"]:
            if not isinstance(item, dict):
                raise ValueError("Некорректное описание файла")
            rel = Path(str(item.get("path", "")))
            content = str(item.get("content", ""))
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                raise ValueError(f"Опасный путь файла: {rel}")
            total += len(content.encode("utf-8"))
        if total > 5_000_000:
            raise ValueError("Сгенерированный проект превышает лимит 5 МБ")

    def create(self, name: str, plan: dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
        self.validate_plan(plan)
        root = self.project_root(name)
        if root.exists() and any(root.iterdir()) and not overwrite:
            raise FileExistsError(
                f"Папка {root} уже существует и не пуста. Разрешите перезапись осознанно."
            )
        root.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for item in plan["files"]:
            relative = Path(item["path"])
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"Выход из папки проекта: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8", newline="\n")
            written.append(str(relative))
        manifest = {
            "summary": plan.get("summary", ""),
            "architecture": plan.get("architecture", ""),
            "run_command": plan.get("run_command", ""),
            "test_command": plan.get("test_command", ""),
            "files": written,
        }
        (root / ".eirven_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"root": str(root), **manifest}

    def build_production(
        self,
        context: "TaskContext",
        payload: dict[str, Any],
        tools: "ToolExecutor",
        agent: "LocalAgent",
        model: str,
    ) -> dict[str, Any]:
        """Build, install, test and package a project with restart-safe checkpoints."""
        name = self.clean_name(str(payload.get("name") or ""))
        description = str(payload.get("description") or "").strip()
        if not description:
            raise ValueError("Нужно описание проекта")
        overwrite = bool(payload.get("overwrite", False))
        initial_live = [str(x).strip() for x in (payload.get("live_instructions") or []) if str(x).strip()]
        if initial_live:
            description += "\n\nПравки, добавленные во время запуска:\n- " + "\n- ".join(initial_live)
        live_seen = len(initial_live)
        root = self.project_root(name)
        state_path = root / ".eirven_build_state.json"

        blueprint: dict[str, Any]
        written: list[str]
        resumed = False
        if state_path.is_file():
            try:
                checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                checkpoint = {}
            if checkpoint.get("description") == description and checkpoint.get("blueprint"):
                blueprint = checkpoint["blueprint"]
                self.validate_blueprint(blueprint)
                written = [
                    item for item in checkpoint.get("written", [])
                    if (root / item).is_file()
                ]
                resumed = True
            else:
                checkpoint = {}
        else:
            checkpoint = {}

        if not resumed:
            if root.exists() and any(root.iterdir()):
                if not overwrite:
                    raise FileExistsError(
                        f"Папка уже существует: {root}. Включите осознанную перезапись."
                    )
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
            context.set_total(8)
            simple_markers = re.compile(
                r"\b(микросервис|миграц|кластер|kubernetes|production|продакш|oauth|оплат|"
                r"распредел[её]н|микрофронт|highload|высоконагруз|сложн)\w*",
                re.IGNORECASE,
            )
            fast_build = len(description) <= 1400 and not simple_markers.search(description)
            if fast_build:
                context.update("Собираю компактную рабочую версию", completed_steps=0, progress=0.02)
                # A 4B instruct model is considerably faster on mixed CPU/GPU laptops.
                # The generated project is still compiled and tested afterwards; repair uses the coder model.
                installed = {item.lower(): item for item in self.gateway.installed_models()}
                # The same integrated Qwen3.5 family handles tools, chat and code, avoiding
                # costly model swaps while a project is being generated.
                fast_model = installed.get((self.settings.fast_model or "").lower(), self.settings.fast_model or self.settings.model)
                try:
                    plan = self._with_heartbeat(
                        context,
                        "Создаю файлы проекта",
                        lambda: self.generate_plan(name, description, fast_model, num_predict=560),
                    )
                    created = self.create(name, plan, overwrite=True)
                    written = list(created["files"])
                    blueprint = {
                        "summary": plan.get("summary", ""),
                        "architecture": plan.get("architecture", ""),
                        "run_command": plan.get("run_command", ""),
                        "test_command": plan.get("test_command", ""),
                        "files": [
                            {"path": item["path"], "purpose": "Создано быстрым генератором"}
                            for item in plan.get("files", [])
                        ],
                    }
                except (LLMError, ValueError) as exc:
                    context.update(
                        f"Быстрый проход не завершился ({str(exc)[:160]}). Перехожу к пофайловой сборке",
                        progress=0.03,
                        level="warning",
                    )
                    blueprint = self._with_heartbeat(
                        context,
                        "Проектирую архитектуру",
                        lambda: self.generate_blueprint(name, description, model),
                    )
                    written = []
            else:
                context.update("Проектирую архитектуру", completed_steps=0, progress=0.02)
                blueprint = self._with_heartbeat(
                    context, "Проектирую архитектуру", lambda: self.generate_blueprint(name, description, model)
                )
                written = []
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "description": description,
                        "model": model,
                        "blueprint": blueprint,
                        "written": written,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            context.update(
                f"Продолжаю незавершённую сборку: готово {len(written)} файлов",
                progress=0.01,
            )

        files = blueprint["files"]
        # Files + environment + dependencies + syntax + tests + optional repair + git + archive.
        total_steps = len(files) + 7
        context.set_total(total_steps)
        if not resumed:
            context.update(
                f"Архитектура готова: {len(files)} файлов",
                completed_steps=0,
                data={"files": len(files), "model": model},
            )

        written_set = set(written)
        for index, item in enumerate(files, start=1):
            context.check_cancelled()
            relative = Path(item["path"])
            relative_name = relative.as_posix()
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"Выход из папки проекта: {relative}")
            if relative_name in written_set and target.is_file():
                context.update(
                    f"Файл уже готов: {relative_name}",
                    completed_steps=index,
                    data={"file": relative_name, "resumed": True},
                )
                continue
            context.update(
                f"Пишу {relative_name}",
                completed_steps=index - 1,
                data={"file": relative_name},
            )
            live_now = context.manager.live_instructions(context.task_id) if hasattr(context, "manager") and hasattr(context, "task_id") else []
            if len(live_now) > live_seen:
                additions = live_now[live_seen:]
                description += "\n\nНовые правки владельца во время сборки:\n- " + "\n- ".join(additions)
                live_seen = len(live_now)
                context.update("Учитываю новые голосовые правки", completed_steps=index - 1, data={"live_updates": len(additions)})
            content = self._with_heartbeat(
                context,
                f"Пишу {relative_name}",
                lambda item=item: self.generate_file(name, description, blueprint, item, model),
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            written.append(relative_name)
            written_set.add(relative_name)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "description": description,
                        "model": model,
                        "blueprint": blueprint,
                        "written": written,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            context.update(f"Готов: {relative_name}", completed_steps=index)

        # A correction may request new files or changes to files already generated. Apply
        # one focused in-place patch before verification while the project task is still running.
        live_now = context.manager.live_instructions(context.task_id) if hasattr(context, "manager") and hasattr(context, "task_id") else []
        if live_now:
            context.update("Вношу правки, полученные во время сборки", progress=0.72)
            agent.run(
                f"Проект находится в папке {name}. Не пересоздавай его. Внеси все эти правки владельца прямо сейчас: "
                + " | ".join(live_now)
                + ". Прочитай существующие файлы, внеси точечные изменения и сохрани результат.",
                model=model, max_steps=16, external_stop_event=context.stop_event,
            )

        manifest = {
            "summary": blueprint["summary"],
            "architecture": blueprint["architecture"],
            "run_command": blueprint["run_command"],
            "test_command": blueprint["test_command"],
            "files": written,
            "description": description,
            "model": model,
        }
        (root / ".eirven_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        completed = len(files)
        context.check_cancelled()

        # Do not spend minutes creating a venv and installing pytest for a tiny stdlib
        # utility. Prepare an isolated environment only when the generated project
        # actually declares external dependencies.
        requirements = root / "requirements.txt"
        req_text = requirements.read_text(encoding="utf-8", errors="replace").strip() if requirements.is_file() else ""
        pyproject = root / "pyproject.toml"
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
        has_external_dependencies = bool(req_text) or bool(re.search(r"(?im)^\s*dependencies\s*=\s*\[[^]]*\S", pyproject_text))
        venv_python = ".venv\\Scripts\\python.exe" if os.name == "nt" else ".venv/bin/python"
        python_cmd = "python"
        install_results: list[dict[str, Any]] = []

        if has_external_dependencies:
            context.update("Готовлю зависимости", completed_steps=completed)
            venv_result = tools.execute(
                "run_command",
                {"command": "python -m venv .venv", "cwd": name, "timeout": 600},
            )
            if not venv_result.get("ok") or int(venv_result.get("result", {}).get("returncode", 1)) != 0:
                raise RuntimeError(f"Не удалось создать окружение: {venv_result}")
            python_cmd = venv_python
            if pyproject.is_file():
                install_commands = [f"{venv_python} -m pip install -e ."]
            else:
                install_commands = [f"{venv_python} -m pip install -r requirements.txt"]
            for command in install_commands:
                context.check_cancelled()
                result = tools.execute("run_command", {"command": command, "cwd": name, "timeout": 1200})
                install_results.append(result)
                if not result.get("ok") or int(result.get("result", {}).get("returncode", 1)) != 0:
                    raise RuntimeError("Не удалось установить зависимости проекта: " + json.dumps(result, ensure_ascii=False))
        else:
            context.update("Внешних зависимостей нет — запускаю без лишней установки", completed_steps=completed)
        completed += 1

        context.update("Проверяю синтаксис", completed_steps=completed)
        compile_result = tools.execute(
            "run_command", {"command": f"{python_cmd} -m compileall -q .", "cwd": name, "timeout": 180}
        )
        completed += 1

        # Respect the generated test command. Do not force-install pytest into every
        # throw-away utility. If no tests were requested, successful compile is enough.
        declared_test = str(blueprint.get("test_command") or "").strip()
        if declared_test and declared_test.lower() not in {"none", "нет", "-"}:
            test_command = declared_test.replace("python ", f"{python_cmd} ", 1) if declared_test.startswith("python ") else declared_test
            context.update("Проверяю запуск/тесты", completed_steps=completed)
            test_result = tools.execute("run_command", {"command": test_command, "cwd": name, "timeout": 600})
        else:
            test_command = f"{python_cmd} -m compileall -q ."
            test_result = compile_result
        completed += 1

        test_ok = bool(test_result.get("ok") and int(test_result.get("result", {}).get("returncode", 1)) == 0)
        if not test_ok:
            context.update("Исправляю фактическую ошибку", completed_steps=completed)
            report = agent.run(
                f"Исправь проект в папке {name}. Требование: {description}. Фактическая ошибка проверки: {json.dumps(test_result, ensure_ascii=False)}. Внеси минимальное исправление и повтори команду {test_command}.",
                model=model,
                max_steps=10,
                external_stop_event=context.stop_event,
            )
            test_result = tools.execute("run_command", {"command": test_command, "cwd": name, "timeout": 600})
            test_ok = bool(test_result.get("ok") and int(test_result.get("result", {}).get("returncode", 1)) == 0)
        else:
            report = "Проверка прошла с первого раза."
        completed += 1

        # Keep generated projects clean even though Git itself is no longer initialised
        # automatically for every tiny utility.
        gitignore = root / ".gitignore"
        current_ignore = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else ""
        required_ignores = [".venv/", "__pycache__/", "*.py[cod]", ".pytest_cache/", ".eirven_build_state.json"]
        lines = current_ignore.splitlines()
        missing_ignores = [item for item in required_ignores if item not in lines]
        if missing_ignores:
            prefix = "" if not current_ignore or current_ignore.endswith("\n") else "\n"
            gitignore.write_text(current_ignore + prefix + "\n".join(missing_ignores) + "\n", encoding="utf-8")

        # Git is a user action, not mandatory project scaffolding. Initialising and
        # committing every tiny project adds noticeable latency and surprising history.
        git_results: list[dict[str, Any]] = []
        context.update("Рабочая версия проверена", completed_steps=completed)
        completed += 1

        context.update("Создаю компактный архив", completed_steps=completed)
        archive_path = self.settings.workspace_dir / f"{name}-release.zip"
        if archive_path.exists():
            archive_path.unlink()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in root.rglob("*"):
                if not file.is_file():
                    continue
                relative = file.relative_to(root)
                if relative.parts and relative.parts[0] in {".venv", ".git", "__pycache__"}:
                    continue
                if relative.name == ".eirven_build_state.json" or "__pycache__" in relative.parts:
                    continue
                archive.write(file, relative.as_posix())
        completed += 1

        if state_path.exists():
            state_path.unlink()
        context.update("Проект готов", completed_steps=total_steps, progress=0.99)
        return {
            "project_name": name,
            "project_path": str(root),
            "archive_path": str(archive_path),
            "run_command": blueprint.get("run_command", ""),
            "test_command": test_command,
            "files": written,
            "environment": python_cmd,
            "dependencies": install_results,
            "compile": compile_result,
            "tests": test_result,
            "repair_report": report,
            "git": git_results,
            "verified": test_ok,
            "resumed": resumed,
        }

    def modify_production(
        self,
        context: "TaskContext",
        payload: dict[str, Any],
        tools: "ToolExecutor",
        agent: "LocalAgent",
        model: str,
    ) -> dict[str, Any]:
        name = self.clean_name(str(payload.get("name") or ""))
        request = str(payload.get("request") or "").strip()
        root = self.project_root(name)
        if not root.is_dir():
            raise FileNotFoundError(f"Проект ещё не создан: {root}")
        if not request:
            raise ValueError("Не указано, что изменить")

        context.set_total(4)
        context.update("Изучаю существующий проект", completed_steps=0, progress=0.02)
        venv_python = ".venv\\Scripts\\python.exe" if os.name == "nt" else ".venv/bin/python"
        report = agent.run(
            (
                f"Доработай существующий проект в папке {name}. Требование владельца: {request}. "
                "Сначала прочитай .eirven_manifest.json и нужные исходники. Вноси точечные изменения, "
                f"не переписывай всё без причины. Используй окружение {venv_python}. "
                f"После изменений запусти {venv_python} -m pytest -q и исправляй ошибки."
            ),
            model=model,
            max_steps=24,
            external_stop_event=context.stop_event,
        )
        context.update("Проверяю проект после изменений", completed_steps=1)
        test_command = f"{venv_python} -m pytest -q"
        test_result = tools.execute(
            "run_command", {"command": test_command, "cwd": name, "timeout": 1800}
        )
        verified = bool(
            test_result.get("ok")
            and int(test_result.get("result", {}).get("returncode", 1)) == 0
        )

        context.update("Сохраняю версию в Git", completed_steps=2)
        git_results = []
        for command in (
            "git add .",
            "git commit -m eirven-project-update",
        ):
            context.check_cancelled()
            git_results.append(
                tools.execute("run_command", {"command": command, "cwd": name, "timeout": 300})
            )

        context.update("Обновляю архив", completed_steps=3)
        archive_path = self.settings.workspace_dir / f"{name}-release.zip"
        if archive_path.exists():
            archive_path.unlink()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in root.rglob("*"):
                if not file.is_file():
                    continue
                relative = file.relative_to(root)
                if relative.parts and relative.parts[0] in {".venv", ".git", "__pycache__"}:
                    continue
                if "__pycache__" in relative.parts:
                    continue
                archive.write(file, relative.as_posix())
        context.update("Изменения готовы", completed_steps=4, progress=0.99)
        return {
            "project_name": name,
            "project_path": str(root),
            "archive_path": str(archive_path),
            "request": request,
            "tests": test_result,
            "verified": verified,
            "report": report,
            "git": git_results,
        }

