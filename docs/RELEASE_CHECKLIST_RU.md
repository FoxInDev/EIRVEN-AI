# Release checklist EIRVEN

Перед публичным тегом `vX.Y.Z`:

1. Обновить версию в `pyproject.toml`, `src/eirven_ai/__init__.py`, API/build metadata и `CHANGELOG.md`.
2. Проверить, что `.env`, runtime DB, browser profile, логи, модели и пользовательские файлы не отслеживаются Git.
3. Запустить `pytest -q` и `node --check src/eirven_ai/web/app.js`.
4. Собрать wheel: `python -m build --wheel`.
5. Собрать Windows archive: `python scripts/package_release.py --version vX.Y.Z`.
6. Проверить ZIP и SHA256.
7. На чистой/тестовой Windows пройти: установка → onboarding → wake phrase → Telegram → музыка → браузер → shutdown.
8. Проверить `Настройки → Обновления` против опубликованного GitHub Release.
9. Commit в `main`, затем annotated tag и push tag.
10. После GitHub Actions скачать release asset и проверить его ещё раз как обычный пользователь.
