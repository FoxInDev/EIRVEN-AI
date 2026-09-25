# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import re
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

    @staticmethod
    def desktop_root() -> Path:
        candidates = [
            Path(os.environ.get("OneDrive", "")) / "Desktop" if os.environ.get("OneDrive") else None,
            Path(os.environ.get("USERPROFILE", "")) / "Desktop" if os.environ.get("USERPROFILE") else None,
            Path.home() / "Desktop",
            Path.home() / "Рабочий стол",
        ]
        for candidate in candidates:
            if candidate and candidate.is_dir():
                return candidate.resolve()
        return (Path.home() / "Desktop").resolve()

    def project_root(self, name: str, requested_path: str | Path | None = None) -> Path:
        """Resolve a project under the configured workspace or the user's Desktop.

        A path is accepted only when it is explicitly supplied by the owner/model and
        remains below one of those two local roots.  This lets a project be placed in
        ``Рабочий стол\1`` without opening an unrestricted filesystem escape hatch.
        """
        requested = str(requested_path or "").strip()
        if requested:
            normalized = requested.replace("/", os.sep).strip().strip('"')
            if re.fullmatch(r"(?:рабочий\s+стол|рабоч\w*\s+стол|desktop)(?:[\\/].*)?", normalized, re.I):
                suffix = re.sub(r"^(?:рабочий\s+стол|рабоч\w*\s+стол|desktop)[\\/]?", "", normalized, flags=re.I)
                root = (self.desktop_root() / suffix) if suffix else (self.desktop_root() / self.clean_name(name))
            else:
                candidate = Path(normalized).expanduser()
                root = candidate if candidate.is_absolute() else (self.settings.workspace_dir / candidate)
            root = root.resolve()
            allowed = [self.settings.workspace_dir.resolve(), self.desktop_root()]
            if not any(root == base or base in root.parents for base in allowed):
                raise ValueError("Путь проекта должен находиться в workspace или на Рабочем столе")
            if root == self.desktop_root():
                root = (root / self.clean_name(name)).resolve()
            return root
        root = (self.settings.workspace_dir / self.clean_name(name)).resolve()
        if self.settings.workspace_dir.resolve() not in root.parents:
            raise ValueError("Некорректный путь проекта")
        return root

    @staticmethod
    def _safe_check_command(command: Any) -> str:
        """Return a single, non-mutating project check command or an empty string."""
        value = str(command or "").strip()
        if not value or value.lower() in {"none", "нет", "-"}:
            return ""
        # Project metadata is data, not an unrestricted shell script. Native package
        # managers may still dispatch their named test/check script, but Eirven never
        # accepts shell chaining or repository-changing Git commands as verification.
        if any(token in value for token in ("\r", "\n", "&&", "||", ";", "|", ">", "<", "`", "$(")):
            return ""
        if re.search(r"(?i)(?:^|\s)git(?:\.exe)?\b", value):
            return ""
        if re.search(r"(?i)(?:^|\s)(?:rm|rmdir|del|erase|remove-item)\b", value):
            return ""
        if re.search(r"(?i)(?:powershell|pwsh|cmd)(?:\.exe)?\s+(?:-[a-z]+\s+)*[-/]?(?:c|command)\b", value):
            return ""
        if re.search(r"(?i)(?:^|\s)(?:bash|sh|python|node)\s+(?:-[a-z]+\s+)*-(?:c|e)\b", value):
            return ""
        return value

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _python_command(root: Path) -> str:
        relative = Path(".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        return str(relative).replace("/", "\\") if (root / relative).is_file() and os.name == "nt" else (
            relative.as_posix() if (root / relative).is_file() else "python"
        )

    @classmethod
    def inspect_project(cls, root: Path, declared_test: Any = None) -> dict[str, Any]:
        """Infer the existing stack and its native, bounded verification command.

        Explicit project metadata wins, then ecosystem metadata. This deliberately
        avoids a language whitelist: an unfamiliar stack remains supported through a
        safe test command stored in ``.eirven_manifest.json``.
        """
        root = root.resolve()
        manifest = cls._read_json_file(root / ".eirven_manifest.json")
        explicit = cls._safe_check_command(declared_test) or cls._safe_check_command(manifest.get("test_command"))
        if explicit:
            if explicit.startswith("python "):
                explicit = explicit.replace("python ", f"{cls._python_command(root)} ", 1)
            stack = str(manifest.get("stack") or "").strip().lower()
            if not stack:
                if (root / "package.json").is_file():
                    stack = "javascript"
                elif any((root / item).is_file() for item in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")) or any(root.glob("*.py")):
                    stack = "python"
                elif (root / "Cargo.toml").is_file():
                    stack = "rust"
                elif (root / "go.mod").is_file():
                    stack = "go"
                else:
                    stack = "declared"
            return {"stack": stack, "commands": [explicit], "source": "manifest"}

        package = cls._read_json_file(root / "package.json")
        if package:
            scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
            manager = (
                "pnpm" if (root / "pnpm-lock.yaml").is_file()
                else "yarn" if (root / "yarn.lock").is_file()
                else "bun" if any((root / item).is_file() for item in ("bun.lock", "bun.lockb"))
                else "npm"
            )
            for script_name in ("test", "check", "typecheck", "lint", "build"):
                body = cls._safe_check_command(scripts.get(script_name))
                if not body or re.search(r"(?i)no tests? specified|exit\s+1", body):
                    continue
                command = f"{manager} {script_name}" if script_name == "test" else f"{manager} run {script_name}"
                return {"stack": "javascript", "commands": [command], "source": "package.json"}

        python_files = list(root.glob("*.py")) or list(root.glob("src/**/*.py"))
        python_metadata = any((root / item).is_file() for item in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"))
        python_tests = (root / "tests").is_dir() or any(root.glob("test_*.py"))
        if python_metadata or python_files:
            python_cmd = cls._python_command(root)
            command = f"{python_cmd} -m pytest -q" if python_tests else f"{python_cmd} -m compileall -q ."
            return {"stack": "python", "commands": [command], "source": "python metadata"}

        metadata_commands: tuple[tuple[str, tuple[str, ...], str], ...] = (
            ("rust", ("Cargo.toml",), "cargo test"),
            ("go", ("go.mod",), "go test ./..."),
            ("dotnet", ("*.sln", "*.csproj", "*.fsproj"), "dotnet test"),
            ("maven", ("pom.xml",), "mvn test"),
            ("gradle", ("gradlew", "gradlew.bat"), ".\\gradlew.bat test" if os.name == "nt" else "./gradlew test"),
            ("php", ("composer.json",), "composer test"),
        )
        for stack, patterns, command in metadata_commands:
            if any(any(root.glob(pattern)) for pattern in patterns):
                return {"stack": stack, "commands": [command], "source": "project metadata"}

        makefile = root / "Makefile"
        if makefile.is_file():
            try:
                make_text = makefile.read_text(encoding="utf-8", errors="replace")
            except OSError:
                make_text = ""
            if re.search(r"(?m)^test\s*:", make_text):
                return {"stack": "make", "commands": ["make test"], "source": "Makefile"}
        return {"stack": "unknown", "commands": [], "source": "none"}

    @classmethod
    def setup_commands(cls, root: Path) -> list[str]:
        """Choose dependency setup from files created for a new project."""
        root = root.resolve()
        package = cls._read_json_file(root / "package.json")
        if package and not (root / "node_modules").is_dir():
            has_packages = any(isinstance(package.get(key), dict) and package[key] for key in ("dependencies", "devDependencies", "optionalDependencies"))
            if has_packages:
                if (root / "pnpm-lock.yaml").is_file():
                    return ["pnpm install --frozen-lockfile"]
                if (root / "yarn.lock").is_file():
                    return ["yarn install --frozen-lockfile"]
                if (root / "package-lock.json").is_file():
                    return ["npm ci"]
                if any((root / item).is_file() for item in ("bun.lock", "bun.lockb")):
                    return ["bun install --frozen-lockfile"]
                return ["npm install"]

        requirements = root / "requirements.txt"
        requirements_text = requirements.read_text(encoding="utf-8", errors="replace").strip() if requirements.is_file() else ""
        pyproject = root / "pyproject.toml"
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
        has_python_packages = bool(requirements_text) or bool(re.search(r"(?im)^\s*dependencies\s*=\s*\[[^]]*\S", pyproject_text))
        if has_python_packages:
            venv_python = ".venv\\Scripts\\python.exe" if os.name == "nt" else ".venv/bin/python"
            install = f"{venv_python} -m pip install -e ." if pyproject.is_file() else f"{venv_python} -m pip install -r requirements.txt"
            commands = [] if (root / Path(venv_python.replace("\\", "/"))).is_file() else ["python -m venv .venv"]
            return [*commands, install]
        return []

    @staticmethod
    def _unused_archive_path(root: Path, name: str) -> Path:
        """Never overwrite a prior release archive."""
        preferred = root.parent / f"{name}-release.zip"
        if not preferred.exists():
            return preferred
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = root.parent / f"{name}-release-{stamp}.zip"
        suffix = 2
        while candidate.exists():
            candidate = root.parent / f"{name}-release-{stamp}-{suffix}.zip"
            suffix += 1
        return candidate

    def generate_plan(
        self, name: str, description: str, model: str | None = None, *, num_predict: int | None = None
    ) -> dict[str, Any]:
        clean_name = self.clean_name(name)
        prompt = f"""
Создай полностью запускаемый небольшой программный проект с нуля.
Имя: {clean_name}
Требования пользователя:
{description}

Условия:
- Выбери стек по задаче. Если пользователь назвал язык, фреймворк или синтаксис — строго следуй этому выбору.
- Не ограничивай решение Python: допустим любой локально проверяемый стек, который подходит задаче.
- Для небольшой утилиты делай минимальную структуру выбранного стека: точка входа + README.md + .gitignore; тест только если он реально полезен.
- Не добавляй зависимости и инструменты только ради объёма; используй нативные metadata-файлы выбранного стека.
- Не пиши длинную архитектурную документацию: summary и architecture по 1 короткому предложению.
- Пользователю важна скорость до первого рабочего запуска, а не количество файлов.
- Не вставляй секреты и реальные токены.
- Пути относительные, без ../ и абсолютных путей.
- test_command и run_command — по одной безопасной команде без конвейеров и Git-операций.
- Содержимое каждого текстового файла верни полностью.
- Не используй Markdown-ограждения внутри JSON.

Верни только объект по JSON-схеме.
""".strip()
        result = self.gateway.json(
            [
                {"role": "system", "content": "Ты сильный универсальный архитектор ПО и пишешь рабочий код на выбранном для задачи стеке."},
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
Спроектируй production-ready, но локально запускаемый программный проект.
Имя проекта: {clean_name}
Техническое задание пользователя:
{description}

Составь точную архитектуру и список файлов. Не пиши содержимое файлов на этом шаге.
Требования:
- Выбери стек по задаче; явно названные пользователем язык, фреймворк и синтаксис обязательны.
- Не ограничивай архитектуру одним языком. Проверка должна использовать нативные metadata и команды выбранного стека.
- Полная обработка ошибок, конфигурация через env, README, .gitignore, тесты.
- Минимум зависимостей; никаких секретов.
- Пути только относительные и безопасные.
- Не больше 35 файлов; каждый файл должен быть реально нужен.
- test_command и run_command должны быть одной командой без shell-конвейеров и Git-операций.
Верни только JSON по схеме.
""".strip()
        blueprint = self.gateway.json(
            [
                {"role": "system", "content": "Ты проектируешь компактные, проверяемые системы на подходящем задаче стеке."},
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
                {"role": "system", "content": "Ты пишешь один законченный файл production-проекта, строго соблюдая выбранный стек и архитектуру."},
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

    def create(self, name: str, plan: dict[str, Any], overwrite: bool = False, *, target_root: Path | None = None) -> dict[str, Any]:
        self.validate_plan(plan)
        root = (target_root or self.project_root(name)).resolve()
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
        root = self.project_root(name, payload.get("project_path") or payload.get("target_dir"))
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
                # Explicit overwrite may replace generated paths, but it must not erase
                # unrelated owner files that happen to share the target directory.
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
                # The same DeepSeek-Coder-V2 checkpoint handles tools, chat and code, avoiding
                # costly model swaps while a project is being generated.
                fast_model = installed.get((self.settings.fast_model or "").lower(), self.settings.fast_model or self.settings.model)
                try:
                    plan = self._with_heartbeat(
                        context,
                        "Создаю файлы проекта",
                        lambda: self.generate_plan(name, description, fast_model, num_predict=560),
                    )
                    created = self.create(name, plan, overwrite=True, target_root=root)
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
        # Files + dependencies + native checks + optional repair + archive.
        total_steps = len(files) + 5
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
                f"Проект находится в папке {root}. Не пересоздавай его. Внеси все эти правки владельца прямо сейчас: "
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

        # Dependency setup is driven by the files the project actually declares. A
        # Python venv is not imposed on Node/Rust/Go/future stacks.
        install_results: list[dict[str, Any]] = []
        install_commands = self.setup_commands(root)
        if install_commands:
            context.update("Готовлю зависимости", completed_steps=completed)
            for command in install_commands:
                context.check_cancelled()
                result = tools.execute("run_command", {"command": command, "cwd": str(root), "timeout": 1200})
                install_results.append(result)
                if not result.get("ok") or int(result.get("result", {}).get("returncode", 1)) != 0:
                    raise RuntimeError("Не удалось установить зависимости проекта: " + json.dumps(result, ensure_ascii=False))
        else:
            context.update("Внешних зависимостей нет — запускаю без лишней установки", completed_steps=completed)
        completed += 1

        profile = self.inspect_project(root, blueprint.get("test_command"))
        check_commands = list(profile["commands"])
        context.update(
            f"Запускаю нативную проверку ({profile['stack']})" if check_commands else "У проекта нет проверяемой команды",
            completed_steps=completed,
        )
        check_results: list[dict[str, Any]] = []
        for command in check_commands:
            context.check_cancelled()
            check_results.append(
                tools.execute("run_command", {"command": command, "cwd": str(root), "timeout": 900})
            )
        no_check_result = {
            "ok": False,
            "result": {"returncode": 2, "stderr": "Проект не объявляет безопасную нативную проверку"},
        }
        compile_result = check_results[0] if check_results else no_check_result
        test_result = check_results[-1] if check_results else no_check_result
        test_command = check_commands[-1] if check_commands else ""
        completed += 1

        test_ok = bool(
            check_results
            and all(item.get("ok") and int(item.get("result", {}).get("returncode", 1)) == 0 for item in check_results)
        )
        if not test_ok:
            context.update("Исправляю фактическую ошибку", completed_steps=completed)
            report = agent.run(
                f"Исправь проект в папке {root}. Требование: {description}. "
                f"Фактическая ошибка проверки: {json.dumps(test_result, ensure_ascii=False)}. "
                f"Внеси минимальное исправление. Используй metadata существующего стека; "
                f"не создавай Python-окружение для другого стека и не запускай git add/commit/push. "
                f"Команда проверки: {test_command or 'добавь безопасную нативную test/check-команду в metadata проекта'}.",
                model=model,
                max_steps=10,
                external_stop_event=context.stop_event,
            )
            profile = self.inspect_project(root, blueprint.get("test_command"))
            check_commands = list(profile["commands"])
            check_results = [
                tools.execute("run_command", {"command": command, "cwd": str(root), "timeout": 900})
                for command in check_commands
            ]
            compile_result = check_results[0] if check_results else no_check_result
            test_result = check_results[-1] if check_results else no_check_result
            test_command = check_commands[-1] if check_commands else ""
            test_ok = bool(
                check_results
                and all(item.get("ok") and int(item.get("result", {}).get("returncode", 1)) == 0 for item in check_results)
            )
        else:
            report = "Проверка прошла с первого раза."
        completed += 1

        # Keep generated projects clean even though Git itself is no longer initialised
        # automatically for every tiny utility.
        gitignore = root / ".gitignore"
        current_ignore = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else ""
        required_ignores = [".eirven_build_state.json", ".env"]
        if profile["stack"] == "python":
            required_ignores.extend([".venv/", "__pycache__/", "*.py[cod]", ".pytest_cache/"])
        elif profile["stack"] == "javascript":
            required_ignores.extend(["node_modules/", ".npm/"])
        lines = current_ignore.splitlines()
        missing_ignores = [item for item in required_ignores if item not in lines]
        if missing_ignores:
            prefix = "" if not current_ignore or current_ignore.endswith("\n") else "\n"
            gitignore.write_text(current_ignore + prefix + "\n".join(missing_ignores) + "\n", encoding="utf-8")

        # Git history is an irreversible owner-facing action and is never changed here.
        git_results: list[dict[str, Any]] = []
        context.update("Рабочая версия проверена", completed_steps=completed)
        completed += 1

        context.update("Создаю компактный архив", completed_steps=completed)
        archive_path = self._unused_archive_path(root, name)
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
            "environment": profile["stack"],
            "dependencies": install_results,
            "compile": compile_result,
            "checks": check_results,
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
        root = self.project_root(name, payload.get("project_path") or payload.get("target_dir"))
        if not root.is_dir():
            raise FileNotFoundError(f"Проект ещё не создан: {root}")
        if not request:
            raise ValueError("Не указано, что изменить")

        context.set_total(3)
        context.update("Изучаю существующий проект", completed_steps=0, progress=0.02)
        initial_profile = self.inspect_project(root)
        native_checks = ", ".join(initial_profile["commands"]) or "не объявлена — определи её по metadata проекта"
        report = agent.run(
            (
                f"Доработай существующий проект в папке {root}. Требование владельца: {request}. "
                "Сначала прочитай metadata стека, .eirven_manifest.json (если есть) и нужные исходники. "
                "Вноси только точечные изменения и сохраняй все не относящиеся к запросу файлы. "
                f"Определённый стек: {initial_profile['stack']}; нативная проверка: {native_checks}. "
                "Используй существующее окружение проекта, а не навязывай Python/venv/pytest другому стеку. "
                "После изменений запусти нативную проверку и исправь ошибки. "
                "Не запускай git add, commit, push, reset, clean или checkout."
            ),
            model=model,
            max_steps=24,
            external_stop_event=context.stop_event,
        )
        context.update("Проверяю проект после изменений", completed_steps=1)
        profile = self.inspect_project(root)
        check_commands = list(profile["commands"])
        check_results: list[dict[str, Any]] = []
        for command in check_commands:
            context.check_cancelled()
            check_results.append(
                tools.execute("run_command", {"command": command, "cwd": str(root), "timeout": 1800})
            )
        test_result = check_results[-1] if check_results else {
            "ok": False,
            "result": {"returncode": 2, "stderr": "Проект не объявляет безопасную нативную проверку"},
        }
        verified = bool(
            check_results
            and all(item.get("ok") and int(item.get("result", {}).get("returncode", 1)) == 0 for item in check_results)
        )

        # Do not alter Git history or stage owner files. A release archive is additive
        # and collision-safe, so a previous archive is preserved as well.
        git_results: list[dict[str, Any]] = []
        context.update("Создаю архив проверенной версии", completed_steps=2)
        archive_path = self._unused_archive_path(root, name)
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
        context.update("Изменения готовы", completed_steps=3, progress=0.99)
        return {
            "project_name": name,
            "project_path": str(root),
            "archive_path": str(archive_path),
            "request": request,
            "test_command": check_commands[-1] if check_commands else "",
            "tests": test_result,
            "checks": check_results,
            "stack": profile["stack"],
            "verified": verified,
            "report": report,
            "git": git_results,
        }
