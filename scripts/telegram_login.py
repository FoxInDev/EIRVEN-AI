from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from eirven_ai.config import Settings  # noqa: E402


async def main() -> None:
    try:
        from telethon import TelegramClient
    except ImportError:
        print("Telethon не установлен. Запустите scripts/repair_windows.ps1")
        raise SystemExit(1)

    settings = Settings.load()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print("Укажите EIRVEN_TELEGRAM_API_ID и EIRVEN_TELEGRAM_API_HASH в .env")
        raise SystemExit(1)
    client = TelegramClient(
        str(settings.data_dir / "telegram"),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start(phone=settings.telegram_phone or None)
    me = await client.get_me()
    print(f"Готово. Авторизован аккаунт: {getattr(me, 'first_name', '')}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
