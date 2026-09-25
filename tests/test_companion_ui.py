# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from eirven_ai.companion import DesktopCompanion


def test_companion_anchor_stays_inside_work_area() -> None:
    anchor = {"x": 9999, "y": 9999}
    DesktopCompanion._clamp_anchor(anchor, 430, 214, 1920, 1080)
    assert anchor == {"x": 1490, "y": 818}

    anchor = {"x": -50, "y": -20}
    DesktopCompanion._clamp_anchor(anchor, 430, 214, 1920, 1080)
    assert anchor == {"x": 0, "y": 0}
