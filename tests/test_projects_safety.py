# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from eirven_ai.projects import ProjectBuilder


class _Context:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.updates: list[tuple[str, dict]] = []

    def set_total(self, _total: int) -> None:
        pass

    def update(self, label: str, **kwargs) -> None:
        self.updates.append((label, kwargs))

    def check_cancelled(self) -> None:
        assert not self.stop_event.is_set()


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        return {"ok": True, "result": {"returncode": 0, "stdout": "ok", "stderr": ""}}


class _Agent:
    def __init__(self) -> None:
        self.goals: list[str] = []

    def run(self, goal: str, **_kwargs) -> str:
        self.goals.append(goal)
        return "changed"


def _builder(workspace: Path, gateway=None) -> ProjectBuilder:
    settings = SimpleNamespace(
        workspace_dir=workspace,
        fast_model="fast",
        model="main",
        code_model="code",
        task_num_ctx=8192,
        task_num_predict=1200,
    )
    return ProjectBuilder(settings, gateway or SimpleNamespace())


def test_project_check_detection_uses_metadata_and_rejects_git(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    assert ProjectBuilder.inspect_project(node) == {
        "stack": "javascript",
        "commands": ["npm test"],
        "source": "package.json",
    }

    future = tmp_path / "future"
    future.mkdir()
    (future / ".eirven_manifest.json").write_text(
        json.dumps({"stack": "zig", "test_command": "zig build test"}), encoding="utf-8"
    )
    assert ProjectBuilder.inspect_project(future)["commands"] == ["zig build test"]

    (future / ".eirven_manifest.json").write_text(
        json.dumps({"stack": "zig", "test_command": "git add . && git commit -m hidden"}),
        encoding="utf-8",
    )
    assert ProjectBuilder.inspect_project(future)["commands"] == []


def test_python_check_uses_existing_environment_only(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    if __import__("os").name == "nt":
        python_path = tmp_path / ".venv" / "Scripts" / "python.exe"
        expected = ".venv\\Scripts\\python.exe -m pytest -q"
    else:
        python_path = tmp_path / ".venv" / "bin" / "python"
        expected = ".venv/bin/python -m pytest -q"
    python_path.parent.mkdir(parents=True)
    python_path.touch()
    assert ProjectBuilder.inspect_project(tmp_path)["commands"] == [expected]
    # Modifying an existing project never creates a new environment implicitly.
    assert ProjectBuilder.setup_commands(tmp_path) == []


def test_modify_uses_absolute_desktop_root_native_check_and_never_git(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    desktop = tmp_path / "Desktop"
    root = desktop / "2"
    workspace.mkdir()
    root.mkdir(parents=True)
    monkeypatch.setattr(ProjectBuilder, "desktop_root", staticmethod(lambda: desktop.resolve()))
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    owner_file = root / "owner-notes.txt"
    owner_file.write_text("keep", encoding="utf-8")
    previous_archive = desktop / "2-release.zip"
    previous_archive.write_bytes(b"owner archive")

    builder = _builder(workspace)
    tools = _Tools()
    agent = _Agent()
    result = builder.modify_production(
        _Context(),
        {"name": "2", "project_path": str(root), "request": "Исправь ошибку"},
        tools,
        agent,
        "code",
    )

    assert [call[1]["command"] for call in tools.calls] == ["npm test"]
    assert all(call[1]["cwd"] == str(root.resolve()) for call in tools.calls)
    assert not any("git " in call[1]["command"].lower() for call in tools.calls)
    assert "не запускай git add" in agent.goals[0].lower()
    assert result["git"] == [] and result["verified"] is True
    assert Path(result["archive_path"]) != previous_archive
    assert previous_archive.read_bytes() == b"owner archive"
    assert owner_file.read_text(encoding="utf-8") == "keep"


class _BuildGateway:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    def installed_models(self) -> list[str]:
        return ["fast"]

    def json(self, messages: list[dict], **_kwargs) -> dict:
        self.messages.append(messages)
        return {
            "summary": "Node utility",
            "architecture": "Minimal Node project",
            "run_command": "node index.js",
            "test_command": "npm test",
            "files": [
                {
                    "path": "package.json",
                    "content": json.dumps({"scripts": {"test": "node --test"}}),
                },
                {"path": "index.js", "content": "export const value = 1;\n"},
            ],
        }


def test_build_respects_requested_stack_and_preserves_unrelated_files(tmp_path: Path) -> None:
    gateway = _BuildGateway()
    builder = _builder(tmp_path, gateway)
    root = tmp_path / "app"
    root.mkdir()
    owner_file = root / "owner.txt"
    owner_file.write_text("keep", encoding="utf-8")
    tools = _Tools()

    result = builder.build_production(
        _Context(),
        {
            "name": "app",
            "description": "Создай проект на Node.js",
            "project_path": str(root),
            "overwrite": True,
        },
        tools,
        _Agent(),
        "code",
    )

    assert owner_file.read_text(encoding="utf-8") == "keep"
    assert result["environment"] == "javascript"
    assert [call[1]["command"] for call in tools.calls] == ["npm test"]
    assert all(call[1]["cwd"] == str(root.resolve()) for call in tools.calls)
    assert "не ограничивай решение python" in gateway.messages[0][1]["content"].lower()
    assert "не используй php, node.js" not in gateway.messages[0][1]["content"].lower()
