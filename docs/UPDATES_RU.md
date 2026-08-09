# Обновления через GitHub Releases

## Репозиторий

По умолчанию EIRVEN проверяет:

```text
FoxInDev/EIRVEN-AI
```

Переопределение для fork:

```env
EIRVEN_UPDATE_REPO=OWNER/REPO
```

## Stable

Канал `stable` запрашивает последний опубликованный стабильный GitHub Release и сравнивает его tag с текущей версией.

## Preview

Канал `preview` просматривает последние опубликованные Releases и может показывать prerelease.

## Как выбирается файл

Если в release assets присутствует ZIP, название которого содержит `EIRVEN` и желательно `Windows`, он показывается как основной файл обновления. `Source code.zip` не считается предпочтительным пользовательским пакетом.

Рекомендуемая схема имени:

```text
EIRVEN-Windows-v1.2.2.zip
```

## API GitHub

EIRVEN использует GitHub REST Releases API. В приложение не вшивается токен. Для публичного репозитория проверка может выполняться без авторизации. Опционально можно задать `GITHUB_TOKEN` в окружении; не помещай его в исходники или release archive.

## Публикация новой версии

1. Обновить `pyproject.toml` и `eirven_ai.__version__`.
2. Обновить `CHANGELOG.md`.
3. Запустить `pytest -q`.
4. Создать tag `vX.Y.Z`.
5. Push tag в GitHub.
6. Workflow `.github/workflows/release.yml` соберёт wheel и Windows ZIP и создаст GitHub Release.
