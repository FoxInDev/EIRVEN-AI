# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import argparse
import os
from pathlib import Path


TEXT_MODEL = "qwen3.5:4b"
VISION_MODEL = "qwen3.5:2b"

MODEL_ENV: dict[str, str] = {
    "EIRVEN_LLM_BACKEND": "ollama",
    "EIRVEN_FAST_MODEL": TEXT_MODEL,
    "EIRVEN_MODEL": TEXT_MODEL,
    "EIRVEN_RELEASE_MODEL": TEXT_MODEL,
    "EIRVEN_STRICT_RELEASE_MODEL": "true",
    "EIRVEN_CODE_MODEL": TEXT_MODEL,
    "EIRVEN_DEEP_MODEL": TEXT_MODEL,
    "EIRVEN_VISION_MODEL": VISION_MODEL,
    "EIRVEN_RELEASE_VISION_MODEL": VISION_MODEL,
    "EIRVEN_EMBEDDING_MODEL": "",
}


def enforce_process_environment() -> None:
    """Provide safe defaults while preserving the hardware-calibrated profile."""
    for key, value in MODEL_ENV.items():
        os.environ.setdefault(key, value)


def rewrite_env_file(path: Path) -> bool:
    """Repair model keys without touching Telegram, access, voice or user settings."""
    source = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else ""
    output: list[str] = []
    seen: set[str] = set()
    changed = False
    for raw in source.splitlines():
        line = raw.lstrip("\ufeff")
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in MODEL_ENV:
            output.append(line)
            continue
        if key in seen:
            changed = True
            continue
        expected = line
        output.append(line)
        seen.add(key)
        changed = changed or line != expected
    for key, value in MODEL_ENV.items():
        if key not in seen:
            output.append(f"{key}={value}")
            changed = True
    rendered = "\n".join(output).rstrip() + "\n"
    if not changed and source == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(rendered, encoding="utf-8", newline="\n")
    pending.replace(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    args = parser.parse_args()
    changed = rewrite_env_file(args.env.resolve())
    print("adaptive-policy-updated" if changed else "adaptive-policy-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
