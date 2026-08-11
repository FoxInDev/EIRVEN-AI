# Пользовательский аудит EIRVEN 1.7.3 r37

Дата аудита: **11 августа 2026 года**. Сборка: `r37-mobile-photo-studio`.

## Итоговый статус

r37 приведена в консистентное состояние и проходит весь доступный в текущей Linux-среде автоматический набор. LAN/mobile-контракты, APK на уровне архива/manifest/v1/v2-криптографии, скрытая фотостудия и защитные проверки покрыты регрессиями. При этом три вида проверки нельзя честно считать завершёнными без внешней среды: реальный чистый Windows, физический/эмулированный Android через `adb`, и полный запуск двух многогигабайтных моделей ComfyUI на Windows/NVIDIA. Поэтому фотостудия в интерфейсе помечена `PREVIEW`, а ниже отдельно перечислено, что именно не проверено.

## Что реально проверено в этой сессии

### Репозиторий и регрессии

- Исходный HANDOFF ZIP распакован в отдельную чистую рабочую папку; старые `.venv`, `data` и `logs` не использовались.
- `python -m pytest -q`: **98 passed**.
- `python -m compileall -q launcher.py run.py src scripts tests`: PASS.
- `node --check src/eirven_ai/web/app.js`: PASS.
- `python scripts/repo_preflight.py`: `REPO_PREFLIGHT=PASS version=1.7.3`.
- Временные Playwright-пути `r35` в config/spec переименованы в `r37`.
- Исправлен Windows version resource: `1.7.3.37`, а не оставшийся от r36 `1.7.3.36`.

### LAN, QR и мобильная страница

Автотестами FastAPI/TestClient подтверждено:

- удалённый частный LAN-клиент на `/ui/` получает `307 -> /mobile/install`;
- полный desktop API остаётся закрыт телефону;
- запрещённый LAN-ответ имеет `text/plain; charset=utf-8`, поэтому русская строка не превращается в `Р...`;
- интернет-клиент не получает APK/mobile API;
- LAN-кандидаты предпочитают активный Wi-Fi/физический Ethernet и понижают VPN/WSL/виртуальные адаптеры;
- mobile config формирует `install_url` именно с `/mobile/install`;
- APK endpoint отдаёт `application/vnd.android.package-archive`, `Cache-Control: no-store, max-age=0`, правильный `Content-Length` и `Content-Disposition` с именем `EIRVEN-Mobile-1.9.4.apk`; GET и HEAD проверены.

Launcher по коду и тестам принудительно передаёт серверу `EIRVEN_HOST=0.0.0.0`, даже если в системном окружении остался `127.0.0.1`. Firewall-правило остаётся ограниченным фактическим Python-процессом, TCP-портом и `LocalSubnet`.

### APK 1.9.4

Проверенный файл: `EIRVEN-Mobile-1.9.4-r37.apk`.

- Standalone APK и `mobile_client/EIRVEN-Mobile.apk` внутри проекта совпадают байт-в-байт.
- SHA-256: `db0e8dc04cabedf0f1330c84505c21158d785ec6c4502a43d276ead719eec0fa`.
- `jarsigner -verify -verbose -certs`: `jar verified` — v1/JAR-подпись валидна.
- Независимый Python-разбор APK Signing Block проверил RSA/SHA-256 подпись v2 и пересчитанный content digest: PASS.
- Сертификат подписанта: `O=EIRVEN, CN=EIRVEN Mobile r37`; сертификат self-signed, что допустимо для Android app signing, но он должен оставаться тем же для будущего обновления поверх установленного APK.
- Автотестом проверены package ID `ai.eirven.client`, versionName `1.9.4`, versionCode `10904` и наличие v2 block.

**Не выполнялось:** официальный `apksigner verify --verbose --print-certs` и `adb install`, потому что Android SDK/ADB/эмулятор в текущей среде отсутствуют. Поэтому первая установка на API 21 и API 34/35, запуск WebView и системный запрос микрофона всё ещё требуют acceptance-проверки на Android.


## Что не удалось подтвердить в этой среде

1. **Playwright Test CLI spec не объявляется пройденным.** В системе есть Chromium 144 и Python Playwright, но Node-пакет `playwright/test`, который импортирует `tests/playwright_mobile_lan.spec.cjs`, отсутствует; `npx` не смог нормально получить пакет. Дополнительно политика окружения блокирует browser navigation к локальным HTTP-адресам (`ERR_BLOCKED_BY_ADMINISTRATOR`). Поэтому screenshot/spec result не выдаётся за успешный UI acceptance.
2. **Чистая Windows 10/11 установка не запускалась.** Здесь нет Windows/PowerShell 5/UAC/Desktop shell, поэтому нельзя реально подтвердить `INSTALL EIRVEN AI.cmd`, сборку `EIRVEN-AI-r37.exe`, TargetPath ярлыка и `Get-NetTCPConnection`/Firewall на настоящем Windows.
3. **Физический Android/эмулятор не запускался.** Нет `adb`, `apksigner` и Android system PackageManager для реального install/launch сценария.
4. **Полный photo-engine runtime не запускался.** Две большие модели намеренно не скачивались в release workspace.

Это не скрытые «успешные» пункты: до проведения этих acceptance-тестов их статус — **не проверено на целевой платформе**.

## Защита пользовательских данных и release ZIP

Финальная упаковка должна исключать `.venv`, `.env`, `data` (кроме `.gitkeep`), `models`, `logs`, пользовательскую БД, generated images, `photo_engine`, `.part`, тестовые screenshots/results и приватные signing keys. `scripts/package_release.py` применяет этот allow/exclude-контур; финальный ZIP после сборки дополнительно сканируется по именам.

Существующий `EIRVEN-AI.exe` не удаляется и не перезаписывается: r37 собирается/запускается как `EIRVEN-AI-r37.exe`. Маркер `.installed-v1.7.3-r37` в bootstrap находится после обязательного шага создания и проверки Desktop shortcut.

## Короткая установка и проверка телефона

1. На Windows распаковать ZIP в обычную локальную папку и запустить `INSTALL EIRVEN AI.cmd`.
2. После успешного bootstrap запускать EIRVEN созданным ярлыком рабочего стола.
3. Открыть `Настройки -> Телефон`. Убедиться, что выбран Wi-Fi-адрес вида `192.168.x.x`, а статус Firewall зелёный.
4. Телефон и ПК должны быть в одной обычной Wi-Fi сети; гостевая сеть/client isolation могут запрещать связь между устройствами.
5. Отсканировать QR. Он должен открыть `http://<wifi-ip>:<port>/mobile/install`, где заголовок — `Связь с компьютером есть`.
6. Скачать `EIRVEN-Mobile-1.9.4.apk`, установить и открыть. Если Android впервые спросит разрешение браузеру устанавливать APK — разрешить для этого браузера.
7. В мобильном приложении ввести адрес и pairing code из `Настройки -> Телефон`, затем разрешить микрофон.
8. Если QR не открывается: сначала проверить фактический bind `0.0.0.0:<port>` и Windows Firewall для процесса-владельца listener, затем правильность Wi-Fi IP; только после этого проверять guest/client isolation роутера.

## Acceptance checklist для целевого Windows/Android перед снятием PREVIEW

- `Get-NetTCPConnection`/`netstat`: listener = `0.0.0.0:<port>` и PID совпадает с разрешённым firewall program.
- Физический телефон открывает `/mobile/install` и скачивает APK полностью.
- `apksigner verify --verbose --print-certs` = success.
- `adb install` на чистом API 21 и API 34/35 = success; WebView запускается, cleartext LAN работает, microphone permission запрашивается.
- `INSTALL EIRVEN AI.cmd` на Windows без Python/Node/Ollama создаёт `EIRVEN-AI-r37.exe`, проверенный Desktop shortcut и только затем release marker.
- Фото setup с чистого состояния скачивает обе модели, переживает сетевой обрыв/restart, стартует ComfyUI на `127.0.0.1:8188`; Realistic и Anime делают валидный результат; повторный setup идемпотентен.
- Прогнать `tests/playwright_mobile_lan.spec.cjs` через настоящий Playwright Test CLI + Chromium и сохранить r37 artifacts.
