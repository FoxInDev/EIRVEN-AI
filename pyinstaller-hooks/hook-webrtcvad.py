# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
# Overrides _pyinstaller_hooks_contrib's stdhooks/hook-webrtcvad.py, which does:
#     datas = copy_metadata('webrtcvad')
# The installed distribution here is `webrtcvad-wheels` (prebuilt Windows wheels;
# the plain `webrtcvad` package has none and needs a C compiler), so no distribution
# is registered under the literal name "webrtcvad" and that call raises
# importlib.metadata.PackageNotFoundError, aborting the whole PyInstaller build.
#
# This project never calls importlib.metadata.version('webrtcvad') or similar at
# runtime, so the copied metadata was never actually needed -- only the module
# import itself, which PyInstaller still discovers and bundles normally regardless
# of this file. Loaded via --additional-hooks-dir, which PyInstaller checks before
# falling back to the bundled contrib hook of the same name.
datas = []
