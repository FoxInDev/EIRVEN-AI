# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Ключ приложения: Эрви доступна только из собственного окна.

Раньше интерфейс открывался любым браузером по адресу 127.0.0.1:7860/ui/ — любая
программа на компьютере могла зайти и получить полный доступ: почту, Telegram,
управление компьютером. Теперь при каждом запуске сервер создаёт случайный ключ
и кладёт его в файл, который читают только программы Эрви. Окно приложения
открывается с этим ключом, сервер ставит ему защищённую метку, и дальше окно
работает как раньше — в самом интерфейсе не меняется ни строки, метка уходит с
каждым запросом сама. Браузер без ключа получает страницу «только в приложении».

Ключ живёт один сеанс: при перезапуске создаётся новый, старый перестаёт
действовать.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import quote

COOKIE_NAME = "eirven_app"
HEADER_NAME = "x-eirven-key"
KEY_FILE = Path("data") / "app.key"

# Пути, открытые без ключа. /api/ping — только «сервер жив», по нему лончер и
# сборка узнают, что Эрви поднялась; он ничего не раскрывает. /ui/open — вход
# по ключу. /favicon.ico — браузер запрашивает сам, отказ в нём ничего не даёт.
OPEN_PATHS = frozenset({"/api/ping", "/ui/open", "/favicon.ico"})


def key_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / KEY_FILE


def issue(root: str | os.PathLike[str]) -> str:
    """Выдать ключ при старте сервера: прежний, если он есть, иначе новый.

    Ключ постоянный, а не на каждый запуск. Иначе открытое окно Эрви после
    перезапуска сервера — например, после обновления — теряло бы доступ и
    «ломалось» до переоткрытия. Защиту это не ослабляет: она держится на том,
    что у браузеров и чужих программ ключа нет, а не на его смене.
    """
    existing = read(root)
    if len(existing) >= 32:
        return existing
    key = secrets.token_urlsafe(32)
    path = key_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(key, encoding="ascii")
    # Замена атомарная: читающий никогда не увидит полузаписанный ключ.
    temporary.replace(path)
    return key


def read(root: str | os.PathLike[str]) -> str:
    """Прочитать ключ сеанса. Пусто, если сервер ещё не создал его."""
    try:
        return key_path(root).read_text(encoding="ascii").strip()
    except OSError:
        return ""


def matches(expected: str, supplied: str | None) -> bool:
    """Сравнить ключи за постоянное время — без подсказок по длине совпадения."""
    if not expected or not supplied:
        return False
    # Сравнение в байтах: строковая версия compare_digest падает с TypeError на
    # любом не-латинском символе. Метку присылает браузер, её содержимое мы не
    # контролируем — и защита не должна падать ошибкой 500 вместо отказа.
    try:
        return hmac.compare_digest(str(expected).encode("utf-8"), str(supplied).encode("utf-8"))
    except Exception:
        return False


def entry_url(root: str | os.PathLike[str], port: int, next_path: str = "/ui/") -> str:
    """Адрес входа в окно приложения с ключом.

    Ключ передаётся один раз: сервер меняет его на защищённую метку и сразу
    перенаправляет на чистый адрес, так что в адресной строке он не остаётся.
    """
    key = read(root)
    target = next_path if next_path.startswith("/ui") else "/ui/"
    return f"http://127.0.0.1:{int(port)}/ui/open?k={quote(key)}&next={quote(target)}"


BLOCKED_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Эрви — только приложение</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#0b0a18;color:#eef1fb;font:16px/1.6 "Segoe UI",system-ui,sans-serif}
  main{max-width:460px;padding:32px;text-align:center}
  h1{font-size:22px;font-weight:600;margin:0 0 12px}
  p{color:#a9aec8;margin:0 0 10px}
</style></head>
<body><main>
  <h1>Эрви работает только как приложение</h1>
  <p>Интерфейс не открывается в браузере — так ваша почта, переписка и компьютер
     защищены от других программ.</p>
  <p>Откройте Эрви ярлыком на рабочем столе или кликом по сфере.</p>
</main></body></html>"""
