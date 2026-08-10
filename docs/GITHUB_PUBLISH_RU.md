# Как красиво опубликовать EIRVEN на GitHub

Репозиторий: `FoxInDev/EIRVEN-AI`.

## 1. Перед первым push

Открой PowerShell в корне проекта и задай имя Git, если ещё не задано:

```powershell
git config --global user.name "Даниил"
git config --global user.email "YOUR_EMAIL@example.com"
```

## 2. Инициализация репозитория

```powershell
git init
git branch -M main
git remote add origin https://github.com/FoxInDev/EIRVEN-AI.git
```

Если `origin` уже существует:

```powershell
git remote set-url origin https://github.com/FoxInDev/EIRVEN-AI.git
```

## 3. Проверка целостности перед commit

Сначала запусти встроенный preflight — он проверяет TOML, merge-маркеры и случайные runtime/private файлы ещё до CI:

```powershell
python scripts/repo_preflight.py
```

Ожидается `REPO_PREFLIGHT=PASS version=1.2.2`. Затем:

```powershell
git status --short
git check-ignore -v .env data\eirven.db logs\example.log
```

В публичный commit не должны попасть `.env`, `data/`, `workspace/`, `models/`, `logs/`, browser profiles и пользовательские БД.

## 4. Первый красивый commit

```powershell
git add .
git status --short
git commit -m "feat: publish EIRVEN 1.2.2 public release"
git push -u origin main
```

## 5. Тег релиза

```powershell
git tag -a v1.2.2 -m "EIRVEN 1.2.2 — public release"
git push origin v1.2.2
```

Tag запускает `.github/workflows/release.yml`: тесты → wheel → Windows ZIP → SHA256 → GitHub Release.

## 6. Если используешь GitHub CLI вручную

После авторизации `gh auth login` можно создать релиз самому:

```powershell
gh release create v1.2.2 `
  .\release\EIRVEN-Windows-v1.2.2.zip `
  .\release\SHA256SUMS.txt `
  --title "EIRVEN 1.2.2 — Public Release" `
  --generate-notes
```

## 7. Оформление страницы репозитория

В `About` укажи короткое описание:

```text
Living voice-first AI companion for Windows. Say “Эрви” — then just talk.
```

Topics:

```text
ai-assistant voice-assistant windows local-ai desktop-agent automation python russian
```

Рекомендуется включить:

- Issues;
- Discussions после первых пользователей;
- Private Vulnerability Reporting;
- branch protection для `main` после стабилизации CI.

## 8. Текст первого GitHub Release

```markdown
# EIRVEN 1.2.2 — Public Release

Первый публичный GitHub-релиз Эйрвен — живого voice-first ИИ-компаньона для Windows.

### Главное
- wake phrase «Эрви»;
- liquid-glass living sphere;
- фирменный голос Бая;
- Telegram / музыка / браузер / Windows actions;
- long-horizon missions;
- GitHub Releases updates;
- новый onboarding и минимальные настройки.

Создатель: Даниил.
```
