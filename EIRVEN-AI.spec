# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/Admin/AppData/Local/EIRVEN AI/build/stage_src', 'src'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/scripts', 'scripts'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/assets', 'assets'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/.env.example', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/launcher.py', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/pyproject.toml', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/requirements.txt', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/requirements-desktop.txt', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/requirements-integrations.txt', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/requirements-voice.txt', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/requirements-build.txt', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/UNINSTALL.cmd', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/BUILD_INFO.json', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/EIRVEN_VERSION.txt', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/LICENSE', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/README.md', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/SECURITY.md', '.'), ('C:/Users/Admin/AppData/Local/EIRVEN AI/THIRD_PARTY_NOTICES.md', '.')],
    hiddenimports=['numpy', 'psutil'],
    hookspath=['C:/Users/Admin/AppData/Local/EIRVEN AI/pyinstaller-hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'cv2', 'playwright', 'av', 'ctranslate2', 'onnxruntime', 'sympy', 'mpmath', 'networkx', 'transformers', 'tokenizers', 'huggingface_hub', 'hf_xet', 'faster_whisper', 'telethon', 'primp', 'lxml', 'fastapi', 'uvicorn', 'starlette', 'pydantic', 'pydantic_core', 'aiohttp', 'pptx', 'pypdf', 'openpyxl', 'sounddevice', 'soundfile', 'imageio_ffmpeg', 'eirven_ai', 'scipy', 'pandas', 'matplotlib', 'winrt', 'PIL._avif', 'PIL.AvifImagePlugin'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='EIRVEN-AI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='C:/Users/Admin/AppData/Local/EIRVEN AI/assets/eirven-version.txt',
    icon=['C:/Users/Admin/AppData/Local/EIRVEN AI/assets/eirven.ico'],
)
