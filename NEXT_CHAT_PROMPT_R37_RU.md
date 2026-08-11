# Промпт для следующего чата: закончить EIRVEN r37

Продолжи работу над приложением из приложенного архива. Это незавершённый релиз `EIRVEN 1.7.3 · r37-mobile-photo-studio`. Работай строго поверх текущего состояния, ничего уже исправленного не откатывай и не ломай остальные функции. Пользователь просил тестировать от лица обычного пользователя через Playwright CLI, но текущий чат был остановлен до финальной проверки и упаковки.

## Исходные жалобы пользователя

1. Телефон и ПК находятся в одной Wi‑Fi сети, но QR/адрес с телефона не открывался.
2. При попытке открыть локальный адрес на телефоне вместо страницы установки показывалось `Р­С‚РѕС‚ СЂР°Р·РґРµР» РґРѕСЃС‚СѓРїРµРЅ...`.
3. APK не устанавливался даже при самой первой установке: Android показывал только «Приложение не установлено».
4. Нужен скрытый раздел «Фото 18+»: в самом низу общих настроек выключенный по умолчанию переключатель; после включения появляется отдельная вкладка. Только генерация из текста, режимы Realistic и Anime, 4K, максимально нормальные руки/лицо.
5. Запрещено делать nudify/дипфейк реального человека. Реализован только вымышленный персонаж 21+, без загрузки фото и переноса лица.
6. Всё должно ставиться на чистый Windows-компьютер без заранее установленного Python; ярлык на рабочем столе обязателен. Нельзя ломать прежние функции.

## Что уже изменено

### LAN, QR и кодировка

- `launcher.py`: `CURRENT_BUILD = r37-mobile-photo-studio`; при запуске сервера принудительно задаётся `EIRVEN_HOST=0.0.0.0`, чтобы старое системное значение `127.0.0.1` не делало сервер localhost-only; маркер обновлён до `.installed-v1.7.3-r37`.
- `src/eirven_ai/api.py`: удалённая частная LAN при GET/HEAD на `/`, `/ui`, `/ui/` получает redirect 307 на `/mobile/install`, а не ошибку ПК-раздела.
- Все текстовые запреты для LAN теперь возвращаются через `PlainTextResponse`, то есть с явным `text/plain; charset=utf-8`; это исправляет кракозябры `Р...`.
- QR по-прежнему должен вести на `http://<wifi-ip>:<port>/mobile/install`.
- Не ослабляй guard: полный ПК-интерфейс всё ещё должен быть недоступен телефону; мобильный API требует pairing token, кроме страницы установки и APK.

### APK

- Диагностика нашла точную проблему старого r35 APK: v2-подпись была валидна, но JAR/v1-подпись была повреждена. Это могло давать «Приложение не установлено» на части Android.
- Добавлен `scripts/build_mobile_apk.py`, который создаёт APK с валидными v1 и v2 подписями, выравнивает stored ZIP entries, добавляет `X-Android-APK-Signed: 2` и подписывает всё одним сертификатом.
- Текущий `mobile_client/EIRVEN-Mobile.apk` пересобран как:
  - package ID: `ai.eirven.client`
  - versionName: `1.9.4`
  - versionCode: `10904`
  - min SDK: 21
  - target SDK: 34
  - main activity: `com.nicron.webview.MainActivity`
  - `usesCleartextTraffic=true`
  - разрешения INTERNET, ACCESS_NETWORK_STATE, RECORD_AUDIO, VIBRATE.
- До остановки чата этот APK был проверен pure-Python verifier: `v1 True`, `v2 True`, ошибок нет; binary manifest разбирался успешно; все uncompressed entries были выровнены на 4 байта.
- API и мобильная страница уже называют файл `EIRVEN-Mobile-1.9.4.apk`.
- Важно: пользователь всё равно должен проверить реальную установку на физическом Android. В текущей среде ADB/эмулятора не было.

### Скрытый раздел «Фото 18+»

- `src/eirven_ai/web/index.html`:
  - скрытая вкладка `#adult-photo-tab`;
  - в самом низу общих настроек переключатель `#adult-photo-enabled`;
  - отдельная панель с режимами `realistic`/`anime`, portrait/square/landscape, textarea промпта, кнопкой генерации, установкой движка и результатом.
- `src/eirven_ai/web/app.js`: переключатель сохраняется в preferences, вкладка появляется/исчезает, статус движка опрашивается, setup показывает прогресс, генерация показывает 4K-результат и download.
- `src/eirven_ai/web/styles.css`: оформление панели и результата.
- `src/eirven_ai/api.py`:
  - preference `adult_photo_enabled`, default false;
  - GET `/api/adult-photo/status`;
  - POST `/api/adult-photo/setup`;
  - POST `/api/adult-photo/generate`;
  - GET `/api/adult-photo/result/{filename}`.
- `src/eirven_ai/creative.py`:
  - только text-to-image;
  - блокируются слова про детей/несовершеннолетних/loli/shota, nudify/deepfake, сходство с реальным человеком/знаменитостью;
  - положительный prompt принудительно задаёт fictional adult woman age 25/no resemblance to real person/correct hands;
  - negative prompt включает bad hands, extra/missing fingers, limbs, face, blur, watermark и т. п.;
  - Realistic и Anime выбирают checkpoint по имени;
  - генерация идёт в SDXL-размере, затем ComfyUI `ImageScale` Lanczos даёт 4K: portrait 2160×3840, square 3840×3840, landscape 3840×2160.
- Честное ограничение уже показано в UI: ни одна diffusion-модель не гарантирует идеальные пальцы в каждом кадре; пользователь может перегенерировать.

### Автоустановка фото-движка

- Добавлен `scripts/install_photo_engine.py`.
- Он должен по одной кнопке поставить отдельный локальный ComfyUI в `data/photo_engine`, отдельный venv, зависимости и две модели:
  - официальный SDXL Base 1.0 (`sd_xl_base_1.0.safetensors`, ожидаемый SHA-256 `31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b`);
  - Animagine XL 4.0 Opt (`animagine-xl-4.0-opt.safetensors`).
- Требует минимум 22 ГБ свободного места, поддерживает `.part` resume, пишет `data/photo_engine/install_status.json`.
- `CreativeService.ensure_local_engine()` запускает установленный ComfyUI только на `127.0.0.1:8188` с `--lowvram`.
- Это новая и пока НЕ ПРОВЕРЕННАЯ часть. Обязательно проверить на чистой Windows/NVIDIA и CPU-only. Не обещай её готовность без проверки.
- Проверь, что актуальный ComfyUI master совместим с Python 3.12, что CUDA wheel URL `cu128` доступен, что Hugging Face direct URLs реально отдают полные файлы без авторизации, а Animagine filename существует. Лучше зафиксировать конкретный ComfyUI commit/release вместо master и добавить SHA-256 Animagine.
- Проверь поведение при паузе сети, нехватке диска, повторном setup, повреждённом `.part`, перезапуске приложения и уже занятом порте 8188.
- Учти лицензионные файлы моделей: Open RAIL++ notices, если модели скачиваются пользователю.

### Windows release identity

- Обновлены `scripts/build_windows.ps1`, `scripts/create_shortcut.ps1`, `scripts/start_windows.bat`, `scripts/bootstrap_r27.py`, `scripts/package_release.py`, `assets/eirven-version.txt`, `BUILD_INFO.json`, `CHANGELOG.md`, `README.md` на r37 / `EIRVEN-AI-r37.exe` / `.installed-v1.7.3-r37`.
- Не возвращай legacy overwrite: `scripts/build_windows.ps1` не должен удалять/перезаписывать запущенный `EIRVEN-AI.exe`.
- `scripts/create_shortcut.ps1` должен оставаться ASCII-only для Windows PowerShell 5 и после создания повторно проверять TargetPath.

## Что уже было проверено до команды «тормози»

- `python -m pytest -q`: 92 passed, 1 warning. Это было ДО добавления последних setup/install-photo-engine изменений, поэтому полный pytest нужно повторить.
- `python -m py_compile` проходил для изменённых Python/JS файлов по мере работы.
- `node --check src/eirven_ai/web/app.js` проходил после добавления setup UI.
- APK v1/v2 и manifest проверены, как описано выше.
- Playwright Test CLI был запущен, но Chromium отсутствовал. Попытка `.venv/bin/playwright install chromium` получила нулевой/обрезанный ZIP от CDN. Это инфраструктурный блокер текущей среды, не успешный UI-тест.

## Что обязательно доделать

1. Сначала распакуй архив в отдельную чистую папку. Не используй `.venv`, data, logs из старой копии.
2. Запусти полный `pytest`, `compileall`, `node --check` и `scripts/repo_preflight.py`. Исправь любые новые падения.
3. Проверь, что в `tests/test_user_experience_r34.py` новые r37-контракты корректны. Текущие тесты ожидали 92 passed до последних изменений.
4. Запусти настоящий Playwright CLI с реальным Chromium:
   - тесты `tests/playwright_mobile_lan.spec.cjs`;
   - скрытая вкладка изначально не видна;
   - переключатель находится в самом низу общих настроек;
   - после включения вкладка появляется;
   - Realistic/Anime и размеры переключаются;
   - пользователь видит честное предупреждение и отсутствие upload photo;
   - setup/progress/error/retry понятны;
   - QR содержит именно `/mobile/install`;
   - мобильная страница скачивает `EIRVEN-Mobile-1.9.4.apk`;
   - телефонный `/ui/` перенаправляется на `/mobile/install`;
   - UTF-8 текст больше не искажён.
5. Исправь имена временных Playwright screenshots/results: в config/spec ещё могут быть `r35` в путях. Это косметика, но приведи к r37.
6. Проверь LAN на реальном Windows:
   - Uvicorn реально слушает `0.0.0.0:<port>` через `netstat`/`Get-NetTCPConnection`;
   - firewall rule совпадает с реальным python.exe владельца listener;
   - Wi‑Fi IP выбран, VPN/WSL не выбран первым;
   - с физического телефона открывается URL;
   - учти AP/client isolation и гостевую сеть, но не списывай всё на роутер без проверки bind/firewall.
7. Проверь APK через официальный Android `apksigner verify --verbose --print-certs` и затем `adb install` на чистом эмуляторе/API 21+ и на современном API 34/35. Убедись, что именно впервые устанавливается, запускается WebView, LAN HTTP разрешён, микрофон запрашивается.
8. Проверь загрузку APK через реальный endpoint: полный размер, SHA-256, Content-Disposition, MIME и отсутствие кэша.
9. Проверь установку Windows с нуля без Python/Node/Ollama:
   - `INSTALL EIRVEN AI.cmd`;
   - bootstrap сам ставит всё;
   - `EIRVEN-AI-r37.exe` собирается;
   - Desktop shortcut создаётся и реально запускает r37;
   - маркер появляется только после успешной проверки ярлыка;
   - повторный запуск идемпотентен;
   - существующие `.venv`, models, user data не удаляются.
10. Отдельно проверь one-click photo setup на чистой Windows. Если он ненадёжен, не выдавай его как готовый: исправь до надёжного resume/status/restart или явно пометь функцию preview.
11. Проверь ComfyUI workflow на обеих реальных моделях. `ImageScale` должен существовать в текущем ComfyUI. Убедись, что checkpoint loader принимает SDXL/Animagine и что 4K output не вызывает OOM. При необходимости делай upscale CPU/Pillow после 1024/1216 генерации.
12. Добавь автоматическую проверку результата: минимум размеры/битый файл/однотонность. Vision-проверка пальцев может давать ложные результаты; не обещай абсолютную гарантию. Если реализуешь retries, ограничь их и показывай прогресс.
13. Проверь безопасность prompt filter вариациями на русском/английском и обходами регистра/пробелов. Не блокируй нормальные adult prompts, но блокируй несовершеннолетних и real-person likeness/deepfake.
14. Обнови `docs/USER_PERSPECTIVE_AUDIT_R37_RU.md`, checksum-файл и итоговые имена.
15. Собери новый чистый Windows ZIP и отдельный APK. Проверь, что ZIP не содержит `.venv`, `.env`, models, logs, user DB, generated images, photo-engine downloads, private signing key или временные тестовые артефакты.
16. Если работаешь с ChatGPT Library, замени прежние стабильные Library identities, а не создавай бесконечные дубликаты; сохрани version history.

## Особо важные риски

- Не утверждай, что Playwright пройден: в остановленном чате он не прошёл из-за отсутствия browser binary.
- Не утверждай, что чистая Windows-установка r37 проверена: пока нет.
- Не утверждай, что физическая установка APK проверена: пока только криптография/manifest/ZIP.
- Не утверждай, что автоматическая установка ComfyUI и две модели проверена: пока нет.
- Не добавляй загрузку фото или face swap в 18+ раздел.
- Не ослабляй LAN allowlist/token guard и не открывай порт в интернет.
- Не удаляй пользовательские данные и не перезаписывай работающий legacy EXE.

Итог следующего чата должен содержать: исправленный и полностью проверенный ZIP, APK 1.9.4 или выше, SHA-256 обоих файлов, пользовательский аудит с честным списком реальных проверок и короткую инструкцию установки/проверки с телефона.
