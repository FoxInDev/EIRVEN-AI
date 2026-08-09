# Changelog

## 1.2.2 · r22.4 hotfix

- Telegram Web подтверждает ввод по реальному переходу кнопки композера из `record` в `send`, даже когда Chromium скрывает значение `contenteditable` от UIA.
- Сообщение отправляется одним семантическим кликом по `btn-send`; после успешного клика Enter больше не нажимается.
- Windows-сборка удаляет старый EXE перед PyInstaller, использует абсолютный путь к новому ICO, добавляет обновлённый version resource и проверяет наличие встроенной иконки.
- Favicon заменён отдельным PNG с прозрачным фоном из нового логотипа EIRVEN.

## 1.2.2 · r22.3 hotfix

- Исправлен ввод сообщений в браузерных `contenteditable`-полях Telegram: фокус ставится в реальную зону каретки, а вставка имеет безопасный идемпотентный резервный путь.
- Голосовая активация и все подсказки теперь используют только имя ассистента из настроек; старое имя не остаётся скрытым wake-словом.
- Расширена последняя проверка женского рода для ответов чата и фоновых задач.
- Все PNG/ICO-ресурсы, окно запуска, EXE, ярлык рабочего стола и ярлык автозапуска используют новый логотип.

## 1.2.2 — Living Desktop & Resilient Installer

- Установщик автоматически повторяет упавшие команды и до трёх раз перезапускает bootstrap без ручного закрытия окна. Уже скачанные модели и окружение сохраняются.
- На экране установки используется живая высокодетализированная EIRVEN-сфера и официальный логотип.
- Официальный логотип EIRVEN используется в web UI, desktop companion, Windows icon, ярлыке и installer.
- Desktop companion получил выразительные глаза, которые реагируют на listening / thinking / speaking / success.
- Комментарии desktop-сферы переработаны в короткие человеческие фразы и liquid-glass карточку.
- В `Настройки → Внешний вид` добавлен переключатель живых глаз.
- В `Настройки → Общее` добавлена безопасная кнопка `Отключить EIRVEN`.
- Версия обновлена до 1.2.2; GitHub Release pipeline и update checker публикуют/ищут новый Windows ZIP.

## 1.2.1 — Public Release

- Финальный r22 reliability router и единый ownership команд.
- Живая liquid-glass сфера с премиальным `E I R V E N` wordmark.
- Единственная публичная identity: женская Эйрвен, фирменный голос Бая.
- Мужские voice-профили и их загрузка удалены из публичного продукта.
- Настройки: Общее / Голос / Внешний вид / Приватность / Обновления.
- GitHub Releases update checker с stable/preview каналами и release asset link.
- Упрощён чистый Windows installer и автозапуск.
- Репозиторий очищен от runtime DB, логов, старых patch/migration artifacts и caches.
- Полный набор публичной документации и demo-script.

## 1.2.0

Внутренняя серия r16–r22: autonomous workflow, grounding, browser recovery, long-horizon missions, hardening, living presence и final reliability.

### 1.2.2 final reliability refresh

- restored the r22 action core as the behavioral baseline;
- fixed Yandex Music Play recognition from visible UIA;
- fixed repeated wake-word stripping and tolerant Telegram Web search verification;
- fixed real process shutdown through the supervisor;
- replaced desktop eyes with two soft line-only expressions;
- uses the transparent EIRVEN orb artwork throughout the public UI.
