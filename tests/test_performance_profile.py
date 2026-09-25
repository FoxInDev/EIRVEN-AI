# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

from eirven_ai import hardware


def test_eight_gigabyte_gpu_uses_one_resident_multimodal_model(monkeypatch) -> None:
    monkeypatch.setattr(hardware, "_memory_gb", lambda: 32.0)
    monkeypatch.setattr(
        hardware,
        "_gpu_info",
        lambda: ("NVIDIA GeForce RTX 3070 Ti", 8.0, True),
    )
    monkeypatch.setattr(hardware, "_cpu_name", lambda: "test cpu")

    profile = hardware.detect_hardware()

    assert profile.runtime_mode == "gpu_resident"
    assert profile.tier == "responsive"
    assert {
        profile.recommended_fast_model,
        profile.recommended_main_model,
        profile.recommended_code_model,
        profile.recommended_vision_model,
    } == {"qwen3.5:4b"}
