# Целостность публичного репозитория

Перед установкой зависимостей CI и Release workflow запускают:

```bash
python scripts/repo_preflight.py
```

Проверка выполняется только стандартной библиотекой Python, поэтому работает ещё до `pip install`.

Она блокирует публикацию, если:

- `pyproject.toml` не разбирается стандартным `tomllib`;
- версия проекта не равна текущей публичной версии;
- в README, документации, исходниках или workflow остались Git merge-маркеры `<<<<<<<`, `=======`, `>>>>>>>`;
- в репозиторий случайно попали runtime DB, SQLite, `.log`, `.pyc`, `.env`, `loggg2.txt`, кеши или локальные профили.

Для локальной проверки перед commit:

```powershell
python scripts/repo_preflight.py
pytest -q
node --check src/eirven_ai/web/app.js
```

Ожидаемый первый результат:

```text
REPO_PREFLIGHT=PASS version=1.2.2
```
