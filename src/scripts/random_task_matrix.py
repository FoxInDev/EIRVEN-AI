# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from eirven_ai.services import build_services


@dataclass(slots=True)
class Scenario:
    name: str
    query: str
    verify: Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    seed = int(os.getenv("EIRVEN_MATRIX_SEED", "60060"))
    rng = random.Random(seed)
    services = build_services()
    services.settings.enable_desktop_control = True
    services.db.set_setting("desktop_control_enabled", True)
    workspace = services.settings.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    token = f"{rng.randrange(100000, 999999)}"

    exact_name = f"exact_{token}.txt"
    exact_value = f"Эрви ✅ ёжик — EXACT_{token}"
    nested_dir = f"nested_{token}"
    replace_name = f"replace_{token}.txt"
    replace_value = f"NEW_{token}\nвторая строка ё"
    read_name = f"read_{token}.txt"
    read_secret = f"SECRET_{token}"
    list_dir = workspace / f"list_{token}"
    rename_dir = workspace / f"rename_{token}"

    (workspace / replace_name).write_text("OLD", encoding="utf-8")
    (workspace / read_name).write_text(f"Кодовое слово: {read_secret}", encoding="utf-8")
    list_dir.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (list_dir / f"item_{index}.txt").write_text(str(index), encoding="utf-8")
    rename_dir.mkdir(parents=True, exist_ok=True)
    for name in ("red.txt", "green.txt", "blue.txt"):
        (rename_dir / name).write_text(name, encoding="utf-8")

    def exact_file(path: Path, expected: str) -> Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]]:
        def check(_out: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            actual = path.read_text("utf-8") if path.is_file() else None
            return actual == expected, {"path": str(path), "actual": actual, "expected": expected}
        return check

    scenarios = [
        Scenario(
            "unicode_exact_file",
            f"Создай в рабочей папке файл {exact_name} с точным текстом: {exact_value}",
            exact_file(workspace / exact_name, exact_value),
        ),
        Scenario(
            "nested_directory_and_file",
            f"Создай в рабочей папке каталог {nested_dir}, а внутри файл note.txt с точным текстом: NESTED_{token}",
            exact_file(workspace / nested_dir / "note.txt", f"NESTED_{token}"),
        ),
        Scenario(
            "replace_existing_content",
            f"Замени всё содержимое файла {replace_name} в рабочей папке на точный текст: {replace_value}",
            exact_file(workspace / replace_name, replace_value),
        ),
        Scenario(
            "read_and_answer",
            f"Прочитай файл {read_name} в рабочей папке и ответь, какое кодовое слово в нём записано.",
            lambda out: (
                read_secret in str(out.get("answer") or ""),
                {"expected_in_answer": read_secret, "answer": str(out.get("answer") or "")},
            ),
        ),
        Scenario(
            "count_directory_files",
            f"Посчитай, сколько файлов лежит в папке {list_dir.name} внутри рабочей папки, и сообщи только фактическое количество.",
            lambda out: (
                bool(re.search(r"(?<!\d)3(?!\d)", str(out.get("answer") or "")))
                and "не найден" not in str(out.get("answer") or "").casefold(),
                {"expected_count": 3, "answer": str(out.get("answer") or "")},
            ),
        ),
        Scenario(
            "verified_batch_rename",
            f"В папке {rename_dir} переименуй все файлы *.txt по шаблону doc_{{n}} и проверь, что старых имён не осталось.",
            lambda _out: (
                sorted(p.name for p in rename_dir.glob("*.txt")) == ["doc_1.txt", "doc_2.txt", "doc_3.txt"],
                {"actual_names": sorted(p.name for p in rename_dir.glob("*.txt"))},
            ),
        ),
        Scenario(
            "compound_two_files",
            f"Создай в рабочей папке файл first_{token}.txt с текстом FIRST_{token}, затем файл second_{token}.txt с текстом SECOND_{token}.",
            lambda _out: (
                (workspace / f"first_{token}.txt").read_text("utf-8") == f"FIRST_{token}"
                and (workspace / f"second_{token}.txt").read_text("utf-8") == f"SECOND_{token}",
                {
                    "first_exists": (workspace / f"first_{token}.txt").is_file(),
                    "second_exists": (workspace / f"second_{token}.txt").is_file(),
                },
            ),
        ),
        Scenario(
            "installed_command_probe",
            "Проверь на этом компьютере, установлен ли Git, и сообщи подтверждённый результат без установки.",
            lambda out: (
                bool(str(out.get("answer") or "").strip()) and "не удалось" not in str(out.get("answer") or "").casefold(),
                {"answer": str(out.get("answer") or "")},
            ),
        ),
    ]
    rng.shuffle(scenarios)
    results: list[dict[str, Any]] = []
    started_all = time.monotonic()
    for index, scenario in enumerate(scenarios, start=1):
        started = time.monotonic()
        try:
            output = services.chat.complete(
                scenario.query,
                conversation_id=f"random-matrix-{seed}-{index}",
                mode="Друг",
            )
            passed, evidence = scenario.verify(output)
            error = "" if passed else "external postcondition failed"
        except Exception as exc:
            output = {}
            passed = False
            evidence = {}
            error = f"{type(exc).__name__}: {exc}"
        row = {
            "index": index,
            "name": scenario.name,
            "query": scenario.query,
            "passed": passed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "answer": str(output.get("answer") or ""),
            "route": output.get("route") or {},
            "evidence": evidence,
            "error": error,
        }
        results.append(row)
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)

    report = {
        "seed": seed,
        "model": services.settings.model,
        "workspace": str(workspace),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.monotonic() - started_all, 3),
        "passed": sum(bool(row["passed"]) for row in results),
        "total": len(results),
        "results": results,
    }
    report_path = services.settings.data_dir / "random-task-matrix-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "passed": report["passed"], "total": report["total"]}, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
