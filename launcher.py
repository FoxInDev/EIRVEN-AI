from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FROZEN = bool(getattr(sys, "frozen", False))
EXECUTABLE_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT
_install_override = str(os.environ.get("EIRVEN_INSTALL_ROOT") or "").strip()
if _install_override:
    APP_ROOT = Path(_install_override).expanduser().resolve()
elif FROZEN and (EXECUTABLE_DIR / "src" / "eirven_ai").is_dir():
    APP_ROOT = EXECUTABLE_DIR
elif FROZEN:
    APP_ROOT = Path(os.environ.get("LOCALAPPDATA") or EXECUTABLE_DIR) / "EIRVEN AI"
else:
    APP_ROOT = ROOT
DEFAULT_PORT = 7860
CURRENT_BUILD = "r72-k5"
INSTALL_MARKER = ".installed-v2.4.0-r72-k5"
# Отметки установки прошлых сборок, у которых тот же набор зависимостей. С ними
# полная переустановка при обновлении не нужна: раньше каждая новая сборка при
# первом запуске заново гоняла весь установщик — это минуты ожидания впустую.
COMPATIBLE_INSTALL_MARKERS = (".installed-v2.4.0-r72-k4",)
RELEASE_MANIFEST = "release_manifest.json"
PAYLOAD_MARKER = ".payload"
RELEASE_TEXT_MODEL = "qwen3.5:4b"
RELEASE_VISION_MODEL = "qwen3.5:2b"
RELEASE_MODEL_ENV = {
    "EIRVEN_LLM_BACKEND": "ollama",
    "EIRVEN_FAST_MODEL": RELEASE_TEXT_MODEL,
    "EIRVEN_MODEL": RELEASE_TEXT_MODEL,
    "EIRVEN_RELEASE_MODEL": RELEASE_TEXT_MODEL,
    "EIRVEN_STRICT_RELEASE_MODEL": "true",
    "EIRVEN_CODE_MODEL": RELEASE_TEXT_MODEL,
    "EIRVEN_DEEP_MODEL": RELEASE_TEXT_MODEL,
    "EIRVEN_VISION_MODEL": RELEASE_VISION_MODEL,
    "EIRVEN_RELEASE_VISION_MODEL": RELEASE_VISION_MODEL,
    "EIRVEN_EMBEDDING_MODEL": "",
}

EMBEDDED_DIRECTORIES = ("src", "scripts", "assets")


def _enable_windows_glass(root, tint: int = 0x000503) -> None:
    """Keep the Windows title bar dark while Tk owns every visible pixel.

    SetWindowCompositionAttribute is intentionally not used here. Even its nominally
    opaque accent modes can hand child compositing to DWM and produce an empty
    transparent rectangle on some Windows/GPU combinations. The launcher is a normal
    opaque Tk window; only documented DWM title-bar attributes are requested.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        root.update_idletasks()
        child = int(root.winfo_id())
        hwnd = int(ctypes.windll.user32.GetParent(child) or child)
        dark = ctypes.c_int(1)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)
            )
            if result == 0:
                break
        try:
            caption = ctypes.c_uint(int(tint) & 0x00FFFFFF)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
            )
        except Exception:
            pass
    except Exception:
        return
EMBEDDED_FILES = (
    ".env.example", "launcher.py", "pyproject.toml", "requirements.txt",
    "requirements-desktop.txt", "requirements-integrations.txt", "requirements-voice.txt",
    "requirements-build.txt",
    "BUILD_INFO.json", "EIRVEN_VERSION.txt", "LICENSE", "NOTICE.md", "README.md", "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    # Видимый файл удаления рядом с установщиком: сборка идёт через PyInstaller,
    # поэтому привычного unins000.exe (его создаёт только Inno Setup) здесь нет.
    "UNINSTALL.cmd",
)




_TRACE_LOCK = threading.Lock()


def _trace(event: str, **fields: object) -> None:
    """Записать строку в loggg2.txt — тот же файл и формат, что у сервера.

    Раньше лончер в журнал не писал вовсе. А «Не получилось запустить» возникает
    именно здесь: при замене файлов, установке или ожидании сервера — когда сам
    сервер ещё не поднялся и писать ему нечем. Журнал о сбое оставался пустым,
    и человеку нечего было показать. Теперь лончер пишет каждый шаг и полный
    текст любой ошибки. Запись никогда не роняет запуск: любой сбой здесь глушится.
    """
    try:
        import json as _json
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mono": round(time.monotonic(), 3),
               "event": str(event), "source": "launcher", **fields}
        text = _json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        path = APP_ROOT / "loggg2.txt"
        with _TRACE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 12_000_000:
                backup = path.with_suffix(".prev.txt")
                try:
                    backup.unlink(missing_ok=True)
                    path.replace(backup)
                except Exception:
                    pass
            with path.open("a", encoding="utf-8") as out:
                out.write(text)
    except Exception:
        pass


def _trace_environment() -> None:
    """Всё об окружении — чтобы по одной строке понять, на чём запускали."""
    info: dict[str, object] = {
        "build": CURRENT_BUILD, "frozen": FROZEN, "argv": sys.argv[1:],
        "app_root": str(APP_ROOT), "exe": sys.executable, "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    try:
        import platform as _pf
        info["os"] = _pf.platform()
    except Exception:
        pass
    try:
        info["marker_installed"] = (APP_ROOT / INSTALL_MARKER).exists()
        info["venv_exists"] = (APP_ROOT / ".venv").exists()
        info["src_exists"] = (APP_ROOT / "src" / "eirven_ai").exists()
        usage = shutil.disk_usage(str(APP_ROOT if APP_ROOT.exists() else APP_ROOT.parent))
        info["disk_free_gb"] = round(usage.free / 1024**3, 1)
    except Exception as exc:
        info["env_probe_error"] = str(exc)[:160]
    if os.name == "nt":
        try:
            import ctypes
            info["screen_px"] = [ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)]
        except Exception:
            pass
    _trace("LAUNCHER_START", **info)

def _replace_program_tree(source: Path, target: Path) -> None:
    """Заменить папку программы целиком, а не наложить поверх.

    Раньше новые файлы копировались поверх старых без удаления. У тех, кто
    ставил Эрви не впервые, в папке оставались модули и скомпилированный код
    от прошлых версий — и новая версия запускалась поверх этой смеси. Отсюда
    «у меня на чистой машине работает, у людей со старыми версиями — нет».

    В src, scripts и assets нет данных человека: переписка, настройки, ключи и
    модели лежат в других местах. Поэтому эти папки безопасно заменять целиком.

    Замена идёт через промежуточную папку: сначала новое копируется рядом,
    потом старое убирается, потом новое встаёт на место. Если что-то прервётся
    посреди, рабочая копия не останется наполовину пустой.
    """
    staging = target.with_name(target.name + ".new")
    previous = target.with_name(target.name + ".old")
    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(source, staging)
    try:
        if target.exists():
            target.rename(previous)
        staging.rename(target)
    except OSError as rename_error:
        _trace("MATERIALIZE_FALLBACK", folder=str(target.name), reason=str(rename_error)[:200])
        # Файл занят другой программой — переименование невозможно. Тогда
        # накладываем поверх, как раньше, но сначала вычищаем кэш байткода:
        # именно он чаще всего и подсовывает код старой версии.
        _purge_bytecode(target)
        try:
            shutil.copytree(staging, target, dirs_exist_ok=True)
        except (shutil.Error, OSError) as copy_error:
            # Часть файлов занята запущенной программой — обновим их при следующем
            # запуске. Остальное уже на месте, запуск продолжается.
            _trace("MATERIALIZE_PARTIAL", folder=str(target.name), reason=str(copy_error)[:1500])
        shutil.rmtree(staging, ignore_errors=True)
        return
    shutil.rmtree(previous, ignore_errors=True)


def _purge_bytecode(root: Path) -> None:
    """Удалить скомпилированный кэш Python во всей папке.

    Файлы .pyc от старой версии могут подхватиться вместо свежего исходника,
    если совпадут отметки времени. Кэш пересоздаётся сам при первом запуске,
    так что его удаление ничего не ломает.
    """
    if not root.exists():
        return
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for stray in root.rglob("*.pyc"):
        try:
            stray.unlink()
        except OSError:
            pass



def _focus_existing_app_window(title: str = "Эрви") -> bool:
    """Если окно Эрви уже открыто — вывести его вперёд и вернуть True.

    Раньше каждый клик по сфере или повторный запуск ярлыка открывали НОВОЕ окно,
    и копии множились. Окно Эрви узнаём точно: класс окон Edge и заголовок ровно
    «Эрви». У обычной вкладки браузера к заголовку дописано название браузера,
    так что её с окном Эрви не спутать.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found: list[int] = []
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            text = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, text, length + 1)
            if text.value.strip() == title:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(proc_type(visit), 0)
        if not found:
            return False
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE — развернуть из панели задач
        user32.ShowWindow(hwnd, 5)              # SW_SHOW
        if not user32.SetForegroundWindow(hwnd):
            # Windows иногда не даёт фоновому процессу перехватить фокус —
            # тогда просим вывести окно вперёд штатным запасным способом.
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
        return True
    except Exception:
        return False

def _open_app_window(url: str) -> None:
    """Открыть интерфейс Эрви окном-приложением (режим приложения Edge).

    Лончер — отдельная программа и не подключает пакет Эрви, поэтому здесь своя
    короткая копия. Если Edge не найден — открываем в браузере по умолчанию.
    """
    # Повторный запуск ярлыком при открытом окне — вывести его вперёд, без копии.
    if _focus_existing_app_window():
        _trace("APP_WINDOW_FOCUSED")
        return
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            edge = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe") if base else ""
            if edge and os.path.isfile(edge):
                try:
                    profile = str(APP_ROOT / "app-window")
                    os.makedirs(profile, exist_ok=True)
                    subprocess.Popen(
                        [edge, f"--app={url}", f"--user-data-dir={profile}",
                         "--no-first-run", "--no-default-browser-check", "--window-size=1280,860"],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True,
                    )
                    return
                except Exception:
                    break
    webbrowser.open(url)


def _app_key_headers() -> dict[str, str]:
    """Заголовок с ключом сеанса для обращений лончера к своему серверу.

    Сервер пускает запросы с компьютера только с ключом. Лончер — своя
    программа, поэтому читает ключ из того же файла, что пишет сервер.
    Старая версия сервера лишний заголовок просто проигнорирует.
    """
    try:
        key = (APP_ROOT / "data" / "app.key").read_text(encoding="ascii").strip()
    except OSError:
        key = ""
    return {"X-Eirven-Key": key} if key else {}


def _app_entry_url(port: int, next_path: str = "/ui/") -> str:
    """Адрес входа в окно приложения с ключом. Ключ появляется у сервера чуть
    позже ответа на ping — поэтому немного подождём, если его ещё нет."""
    from urllib.parse import quote
    key = ""
    for _ in range(40):
        headers = _app_key_headers()
        key = headers.get("X-Eirven-Key", "")
        if key:
            break
        time.sleep(0.25)
    if not key:
        _trace("APP_KEY_MISSING", note="окно откроется без ключа и покажет страницу-заглушку")
    target = next_path if next_path.startswith("/ui") else "/ui/"
    return f"http://127.0.0.1:{int(port)}/ui/open?k={quote(key)}&next={quote(target)}"

def _load_release_manifest(base: Path) -> tuple[dict | None, str]:
    """Манифест версии: список файлов программы с размерами и SHA-256."""
    import hashlib
    try:
        raw = (base / RELEASE_MANIFEST).read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, ""
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None, ""
    return data, hashlib.sha256(raw).hexdigest()[:20]


def _manifest_program_files(manifest: dict) -> dict[str, dict]:
    """Только то, что лончер раскладывает: папки src/scripts/assets и корневые файлы."""
    files = manifest.get("files") or {}
    return {
        rel: meta for rel, meta in files.items()
        if rel.split("/", 1)[0] in EMBEDDED_DIRECTORIES or rel in EMBEDDED_FILES
    }


def _payload_is_current(manifest: dict, payload_id: str) -> bool:
    """Установленная копия уже именно этой версии — раскладывать заново не нужно.

    Сверка по отметке и по размерам файлов: это доли секунды. Если файл удалён
    или подменён, размеры не сойдутся — и копия будет разложена заново.
    """
    try:
        recorded = (APP_ROOT / PAYLOAD_MARKER).read_text(encoding="ascii").split()
    except OSError:
        return False
    if not recorded or recorded[-1] != payload_id:
        return False
    for rel, meta in _manifest_program_files(manifest).items():
        try:
            if (APP_ROOT / rel).stat().st_size != int(meta.get("size", -1)):
                return False
        except (OSError, ValueError, TypeError):
            return False
    return True


def _prune_foreign_files(manifest: dict) -> int:
    """Удалить из папок программы файлы, которых нет в этой версии.

    Нужны, когда папку не удалось заменить целиком (её держал антивирус или
    другая программа) и новые файлы легли поверх старых. Раньше в этом случае
    модули прошлых версий оставались рядом с новыми. Трогаем только
    src/eirven_ai, scripts и assets: данные, настройки и модели лежат в других местах.
    """
    allowed = set(manifest.get("files") or {})
    removed = 0
    for base in (APP_ROOT / "src" / "eirven_ai", APP_ROOT / "scripts", APP_ROOT / "assets"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*"), reverse=True):
            try:
                if "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(APP_ROOT).as_posix()
                if path.is_file() and rel not in allowed:
                    path.unlink()
                    removed += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue
    return removed


def _materialize_embedded_application() -> None:
    """Install the one-file download's application payload per user.

    Earlier public EXEs were launchers for a neighbouring source ZIP.  The direct
    website download now embeds that payload in PyInstaller and refreshes only
    program files here; owner data, credentials, models and ``.env`` stay intact.

    r72-k5: раскладка идёт только когда версия действительно сменилась. Раньше
    при КАЖДОМ запуске, ещё до окна, копировались все папки программы и сам exe,
    а кэш байткода стирался — и сервер потом заново компилировал все модули.
    Отсюда долгий запуск. Теперь при совпадении версии это доли секунды.
    """
    if not FROZEN:
        return
    bundled_source = ROOT / "src" / "eirven_ai"
    if not bundled_source.is_dir():
        # Распаковка пуста. Файл при этом цел — её могла вычистить соседняя копия
        # Эрви. Если программа уже установлена, запускаем установленную: совет
        # «скачай заново» тут неверен и только сбивает с толку.
        installed = APP_ROOT / "src" / "eirven_ai"
        _trace("BUNDLE_MISSING", meipass=str(ROOT), installed_ok=installed.is_dir())
        if installed.is_dir():
            return
        raise RuntimeError("В EXE отсутствует встроенный пакет EIRVEN. Скачай файл заново с официального сайта.")
    manifest, payload_id = _load_release_manifest(ROOT)
    installed_exe = APP_ROOT / "EIRVEN.exe"
    running_exe = Path(sys.executable).resolve()
    if manifest is not None and _payload_is_current(manifest, payload_id):
        exe_ok = installed_exe.is_file()
        if exe_ok and installed_exe.resolve() != running_exe:
            try:
                exe_ok = installed_exe.stat().st_size == running_exe.stat().st_size
            except OSError:
                exe_ok = False
        if exe_ok:
            _trace("MATERIALIZE_SKIPPED", build=CURRENT_BUILD, payload=payload_id)
            return
    started = time.monotonic()
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    for name in EMBEDDED_DIRECTORIES:
        source = ROOT / name
        if source.is_dir():
            _replace_program_tree(source, APP_ROOT / name)
    # Кэш байткода и в корне установки — там тоже могли остаться следы прошлых версий.
    _purge_bytecode(APP_ROOT / "src")
    removed = _prune_foreign_files(manifest) if manifest is not None else 0
    for name in EMBEDDED_FILES:
        source = ROOT / name
        if source.is_file():
            target = APP_ROOT / name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError as file_error:
                _trace("MATERIALIZE_FILE_LOCKED", file=name, reason=str(file_error)[:300])
    if manifest is not None:
        try:
            shutil.copy2(ROOT / RELEASE_MANIFEST, APP_ROOT / RELEASE_MANIFEST)
        except OSError:
            pass
    if installed_exe.resolve() != running_exe:
        pending = installed_exe.with_suffix(".exe.new")
        try:
            shutil.copy2(running_exe, pending)
            pending.replace(installed_exe)
        except OSError as exe_error:
            # EIRVEN.exe занят: он и есть запущенная сейчас Эрви. Windows не даёт
            # заменить работающую программу. Раньше это роняло запуск с кодом 1 —
            # в том числе смок-тест сборки, если твоя Эрви была открыта. Программные
            # файлы уже обновлены; сам exe обновится при следующем запуске.
            _trace("MATERIALIZE_EXE_LOCKED", reason=str(exe_error)[:300])
            try:
                pending.unlink()
            except OSError:
                pass
    # Отметки прошлых версий убираем: одна отметка .payload вместо файла на каждую сборку.
    for stale in APP_ROOT.glob(".payload-*"):
        try:
            stale.unlink()
        except OSError:
            pass
    try:
        (APP_ROOT / PAYLOAD_MARKER).write_text(f"2.4.0 {CURRENT_BUILD} {payload_id or 'no-manifest'}\n", encoding="ascii")
    except OSError:
        pass
    _trace("MATERIALIZE_DONE", build=CURRENT_BUILD, payload=payload_id, removed_foreign=removed,
           seconds=round(time.monotonic() - started, 2))


def _adopt_compatible_install_marker(marker: Path) -> bool:
    """Перенести отметку установки прошлой сборки с тем же набором зависимостей."""
    for name in COMPATIBLE_INSTALL_MARKERS:
        previous = APP_ROOT / name
        if not previous.is_file():
            continue
        try:
            marker.write_text(f"перенесено из {name}: набор зависимостей тот же\n", encoding="utf-8")
        except OSError:
            return False
        try:
            previous.unlink()
        except OSError:
            pass
        _trace("INSTALL_MARKER_ADOPTED", previous=name, current=marker.name)
        return True
    return False


def _env_wants_full_access() -> bool:
    env_path = APP_ROOT / ".env"
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if raw.strip().upper().startswith("EIRVEN_FULL_ACCESS="):
                return raw.split("=", 1)[1].strip().lower() in {"1", "true", "yes", "on", "да"}
    except Exception:
        pass
    return False


def _is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _internet_available(timeout: float = 2.0) -> bool:
    """Есть ли выход в интернет — нужен только установщику пакетов."""
    import socket
    try:
        with socket.create_connection(("pypi.org", 443), timeout=timeout):
            return True
    except OSError:
        return False


def _autostart_requested(argv: list[str] | None = None) -> bool:
    return "--autostart" in (sys.argv[1:] if argv is None else argv)


def _windows_platform() -> bool:
    return os.name == "nt"


def _request_elevation(*, autostart: bool = False) -> bool:
    """r42: normal EIRVEN startup never elevates the whole application.

    Privileged Windows operations must request UAC only for that individual action.
    This prevents installer/runtime files from being created under a different token.
    """
    return False


def _repair_existing_autostart() -> bool:
    """Migrate an already-enabled r29 Startup shortcut to the quiet launcher."""
    if not _windows_platform():
        return False
    try:
        appdata = os.environ.get("APPDATA", "").strip()
        if not appdata:
            return False
        shortcut = (
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "EIRVEN AI.lnk"
        )
        marker = APP_ROOT / "data" / ".autostart-quiet-launcher"
        if marker.is_file():
            return True
        script = APP_ROOT / "scripts" / "install_autostart.ps1"
        if not shortcut.is_file() or not script.is_file():
            return False
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(APP_ROOT), capture_output=True, text=True, timeout=20,
            creationflags=flags,
        )
        if result.returncode != 0:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("quiet-launcher-v1", encoding="ascii")
        return True
    except Exception:
        return False


def _installed_python() -> Path | None:
    candidates = [APP_ROOT / ".venv" / "Scripts" / "pythonw.exe", APP_ROOT / ".venv" / "Scripts" / "python.exe"]
    return next((item for item in candidates if item.exists()), None)


def _runtime_imports_ready(python: Path) -> bool:
    """Cheap sanity check before repairing a missing release marker.

    A partial venv from a failed first install must not be mistaken for a complete
    installation merely because python.exe exists.
    """
    try:
        exe = python
        if exe.name.casefold() == "pythonw.exe":
            console = exe.with_name("python.exe")
            if console.is_file():
                exe = console
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        result = subprocess.run(
            [str(exe), "-c", "import eirven_ai,fastapi,uvicorn,httpx,pydantic"],
            cwd=str(APP_ROOT), capture_output=True, timeout=20, creationflags=flags,
        )
        return result.returncode == 0
    except Exception:
        return False


def _write_mobile_network_status(port: int, ready: bool, detail: str) -> None:
    """Expose the launch-time firewall result to the desktop phone panel."""
    try:
        logs = APP_ROOT / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / "mobile_network.json"
        pending = path.with_suffix(".tmp")
        pending.write_text(
            json.dumps(
                {
                    "port": int(port),
                    "firewall_ready": bool(ready),
                    "detail": str(detail or "").strip(),
                    "updated_at": int(time.time()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pending.replace(path)
    except Exception:
        pass


def _ensure_mobile_firewall(python: Path, port: int) -> bool:
    """Create the narrow LAN firewall rule, elevating only this one operation.

    The whole launcher remains per-user/non-admin. Windows UAC is requested only when
    the inbound LocalSubnet rule is missing, which fixes same-Wi-Fi phone access without
    giving the assistant elevated runtime privileges.
    """
    if os.name != "nt":
        _write_mobile_network_status(port, True, "Локальная сеть готова.")
        return True
    script = APP_ROOT / "scripts" / "ensure_mobile_firewall.ps1"
    if not script.is_file():
        _write_mobile_network_status(port, False, "Не найден scripts/ensure_mobile_firewall.ps1")
        return False
    status_file = APP_ROOT / "logs" / "mobile_firewall.status"
    status_file.parent.mkdir(exist_ok=True)
    status_file.unlink(missing_ok=True)
    base_args = [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Program", str(Path(python).resolve()), "-Port", str(int(port)),
        "-StatusPath", str(status_file),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(["powershell", *base_args], cwd=str(APP_ROOT),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20, creationflags=flags)
        if result.returncode == 5:
            # Elevate only the firewall helper. The EIRVEN server itself stays under
            # the current standard-user token.
            elevated_rc = None
            try:
                import ctypes
                from ctypes import wintypes

                params = " ".join(
                    ('"' + str(x).replace('"', r'\"') + '"') if " " in str(x) else str(x)
                    for x in base_args
                )
                SEE_MASK_NOCLOSEPROCESS = 0x00000040
                SEE_MASK_NOASYNC = 0x00000100

                class SHELLEXECUTEINFOW(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                        ("hwnd", wintypes.HANDLE), ("lpVerb", wintypes.LPCWSTR),
                        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                        ("hProcess", wintypes.HANDLE),
                    ]

                info = SHELLEXECUTEINFOW()
                info.cbSize = ctypes.sizeof(info)
                info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
                info.lpVerb = "runas"
                info.lpFile = "powershell.exe"
                info.lpParameters = params
                info.lpDirectory = str(APP_ROOT)
                info.nShow = 0
                if ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)) and info.hProcess:
                    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 120000)
                    code = wintypes.DWORD()
                    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
                    ctypes.windll.kernel32.CloseHandle(info.hProcess)
                    elevated_rc = int(code.value)
                else:
                    # 1223 is ERROR_CANCELLED: the person declined the prompt.
                    elevated_rc = int(ctypes.get_last_error() or 1223)
            except Exception:
                elevated_rc = None
            if elevated_rc is None:
                quoted = ",".join("'" + str(x).replace("'", "''") + "'" for x in base_args)
                elevate = (
                    "$ErrorActionPreference='Stop';"
                    f"$a=@({quoted});"
                    "$p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a -Wait -PassThru;"
                    "exit $p.ExitCode"
                )
                result = subprocess.run([\
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", elevate],
                    cwd=str(APP_ROOT), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=140,
                    creationflags=flags)
            else:
                result = subprocess.CompletedProcess(base_args, elevated_rc, "", "")
        ready = result.returncode == 0 and status_file.is_file() and status_file.read_text("utf-8", errors="ignore").strip() == "READY"
        if ready:
            detail = f"Windows Firewall: TCP {port} открыт только для LocalSubnet и текущего EIRVEN Python."
        elif result.returncode == 1223:
            detail = "Настройка сети отменена в UAC; EIRVEN продолжит работать на этом ПК, но телефон может быть недоступен."
        else:
            reason = " ".join((result.stderr or result.stdout or "").split())[:200]
            detail = "Не удалось настроить Windows Firewall. " + (reason or "Разреши запрос UAC для доступа с телефона.")
    except Exception as exc:
        ready = False
        detail = f"Не удалось настроить Windows Firewall: {exc}"
    _write_mobile_network_status(port, ready, detail)
    return ready

def _ollama_ping(timeout: float = 1.2) -> bool:
    """Direct localhost health probe; never goes through VPN/proxy settings."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout)
        conn.request("GET", "/api/version", headers={"Connection": "close"})
        response = conn.getresponse()
        response.read(2048)
        conn.close()
        return response.status == 200
    except Exception:
        return False


def _owner_training_active() -> bool:
    """Read-only check for the v2.6.6 owner QLoRA wrapper in WSL."""
    # При входе в Windows вызов wsl.exe может запустить саму виртуальную машину WSL —
    # тяжело и по времени, и по памяти. Обучение в этот момент идти не может.
    if _autostart_requested():
        return False
    if os.name != "nt":
        return str(os.environ.get("EIRVEN_TRAINING_COEXIST", "0")).strip().casefold() in {"1", "true", "yes", "on"}
    command = (
        'p=/var/lib/eirven-full/training_wrapper.pid; test -s "$p" || exit 1; '
        'pid=$(cat "$p" 2>/dev/null); test -n "$pid" && kill -0 "$pid" 2>/dev/null'
    )
    try:
        completed = subprocess.run(
            ["wsl.exe", "-e", "bash", "-lc", command],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2.5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except Exception:
        return False


def _ollama_models() -> list[str]:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2.0)
        conn.request("GET", "/api/tags", headers={"Connection": "close"})
        response = conn.getresponse()
        raw = response.read(2_000_000)
        conn.close()
        if response.status != 200:
            return []
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return [str(item.get("name") or "") for item in (data.get("models") or []) if item.get("name")]
    except Exception:
        return []


_HARDWARE_CACHE: list = []


def _hardware_once():
    """Определить железо один раз за запуск: раньше это делалось дважды подряд —
    для текстовой модели и для модели зрения, — каждый раз с вызовом nvidia-smi."""
    if not _HARDWARE_CACHE:
        from eirven_ai.hardware import detect_hardware
        _HARDWARE_CACHE.append(detect_hardware())
    return _HARDWARE_CACHE[0]


def _configured_model() -> str:
    try:
        source = APP_ROOT / "src"
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        return str(_hardware_once().recommended_main_model or RELEASE_TEXT_MODEL)
    except Exception:
        return RELEASE_TEXT_MODEL


def _configured_vision_model() -> str:
    try:
        source = APP_ROOT / "src"
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        return str(_hardware_once().recommended_vision_model or RELEASE_VISION_MODEL)
    except Exception:
        return RELEASE_VISION_MODEL


def _enforce_release_env() -> None:
    """Persist the device-calibrated route and preserve owner secrets/preferences."""
    selected = _configured_model()
    vision = _configured_vision_model()
    adaptive_env = dict(RELEASE_MODEL_ENV)
    for key in ("EIRVEN_FAST_MODEL", "EIRVEN_MODEL", "EIRVEN_RELEASE_MODEL", "EIRVEN_CODE_MODEL", "EIRVEN_DEEP_MODEL"):
        adaptive_env[key] = selected
    adaptive_env["EIRVEN_VISION_MODEL"] = vision
    adaptive_env["EIRVEN_RELEASE_VISION_MODEL"] = vision
    path = APP_ROOT / ".env"
    source = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else ""
    output: list[str] = []
    seen: set[str] = set()
    for raw in source.splitlines():
        line = raw.lstrip("\ufeff")
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in adaptive_env:
            output.append(line)
            continue
        if key in seen:
            continue
        output.append(f"{key}={adaptive_env[key]}")
        seen.add(key)
    for key, value in adaptive_env.items():
        if key not in seen:
            output.append(f"{key}={value}")
    rendered = "\n".join(output).rstrip() + "\n"
    if source != rendered:
        pending = path.with_name(".env.tmp")
        pending.write_text(rendered, encoding="utf-8", newline="\n")
        pending.replace(path)
    os.environ.update(adaptive_env)


def _ollama_executable() -> str:
    found = shutil.which("ollama") or shutil.which("ollama.exe")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "").strip()
    candidates = []
    if local:
        candidates.extend([
            Path(local) / "Programs" / "Ollama" / "ollama.exe",
            Path(local) / "Ollama" / "ollama.exe",
        ])
    return next((str(path) for path in candidates if path.is_file()), "")


def _ensure_ollama_running(timeout: float = 35.0) -> tuple[bool, str]:
    """Install/start local Ollama idempotently without asking the user for CLI steps."""
    if _ollama_ping():
        return True, "Рабочий контур готов"
    executable = _ollama_executable()
    if not executable and os.name == "nt":
        script = APP_ROOT / "scripts" / "ensure_ollama.ps1"
        if script.is_file():
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                     "-InstallIfMissing", "-StartServer"],
                    cwd=APP_ROOT,
                    timeout=1200,
                    check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                return False, f"Автоматическая подготовка не завершилась: {exc}"
            executable = _ollama_executable()
            if executable and _ollama_ping():
                return True, "Рабочий контур подготовлен автоматически"
    if not executable:
        return False, "Не удалось автоматически подготовить рабочий контур"
    logs = APP_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / "ollama-launch.log"
    try:
        flags = 0
        if os.name == "nt":
            # Без DETACHED_PROCESS: с ним CREATE_NO_WINDOW игнорируется, у процесса нет консоли вовсе,
            # и КАЖДАЯ консольная программа, которую он запускает (у Ollama — проверка видеокарт и
            # процессы моделей), открывала своё видимое окно: 20–30 окон cmd при запуске.
            # С одним CREATE_NO_WINDOW консоль скрытая, и потомки наследуют её — окон нет.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        log = log_path.open("a", encoding="utf-8")
        subprocess.Popen(
            [executable, "serve"], cwd=str(APP_ROOT), stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )
    except OSError as exc:
        # Понятная причина вместо сырого кода Windows: чаще всего это повреждённая
        # установка Ollama или блокировка антивирусом.
        hints = {
            1392: "файл Ollama повреждён или не читается — переустанови Ollama или проверь антивирус",
            225: "антивирус блокирует ollama.exe",
            5: "Windows отказала в доступе к ollama.exe (часто это антивирус)",
            193: "ollama.exe повреждён",
            2: "ollama.exe не найден",
        }
        code = int(getattr(exc, "winerror", 0) or 0)
        return False, f"Не удалось запустить Ollama: {hints.get(code) or exc}"
    except Exception as exc:
        return False, f"Не удалось запустить рабочий контур: {exc}"
    deadline = time.monotonic() + max(5.0, timeout)
    while time.monotonic() < deadline:
        if _ollama_ping(.7):
            return True, "Рабочий контур запущен автоматически"
        time.sleep(.35)
    return False, "Рабочий контур не ответил после автоматического запуска"


def _direct_ping(port: int, timeout: float = 1.0) -> bool:
    """Direct localhost probe that never goes through VPN/system proxies."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/api/ping", headers={"Connection": "close"})
        response = conn.getresponse()
        body = response.read(700).decode("utf-8", errors="replace").lower()
        conn.close()
        return response.status == 200 and "eirven" in body and '"ok"' in body
    except Exception:
        return False


def _running_build(port: int) -> str:
    """Read the active build directly, bypassing browser/system proxies."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        conn.request("GET", "/api/preferences", headers={"Connection": "close", **_app_key_headers()})
        response = conn.getresponse()
        value = json.loads(response.read(16_000).decode("utf-8", errors="replace"))
        conn.close()
        return str(value.get("build") or "").strip() if response.status == 200 else ""
    except Exception:
        return ""


def _running_model(port: int) -> str:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        conn.request("GET", "/api/preferences", headers={"Connection": "close", **_app_key_headers()})
        response = conn.getresponse()
        value = json.loads(response.read(16_000).decode("utf-8", errors="replace"))
        conn.close()
        return str(value.get("model") or "").strip() if response.status == 200 else ""
    except Exception:
        return ""


def _eirven_runtime_counts() -> tuple[int, int]:
    """Count global EIRVEN supervisors/servers across venv and system Python."""
    if os.name != "nt":
        return 0, 0
    script = (
        "$p=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -match 'eirven_ai\\.(supervisor|app)' });"
        "$s=@($p | Where-Object { $_.CommandLine -match 'eirven_ai\\.supervisor' }).Count;"
        "$a=@($p | Where-Object { $_.CommandLine -match 'eirven_ai\\.app' }).Count;"
        "[Console]::Out.Write(\"$s|$a\")"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        supervisor, app = str(result.stdout or "0|0").strip().split("|", 1)
        return int(supervisor), int(app)
    except Exception:
        return 0, 0


def _stop_all_eirven_instances() -> bool:
    script = APP_ROOT / "scripts" / "stop_eirven.ps1"
    if not script.is_file():
        return False
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-AllInstances"],
            cwd=str(APP_ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def _ollama_running_models() -> list[str]:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2.0)
        conn.request("GET", "/api/ps", headers={"Connection": "close"})
        response = conn.getresponse()
        raw = response.read(1_000_000)
        conn.close()
        if response.status != 200:
            return []
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return [str(item.get("name") or item.get("model") or "") for item in data.get("models", [])]
    except Exception:
        return []


def _unload_non_release_models() -> list[str]:
    """Free legacy-model VRAM without deleting any locally downloaded files."""
    unloaded: list[str] = []
    for model in _ollama_running_models():
        if not model or model.casefold() == _configured_model().casefold():
            continue
        try:
            body = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=15.0)
            conn.request("POST", "/api/generate", body=body, headers={"Content-Type": "application/json", "Connection": "close"})
            response = conn.getresponse(); response.read(4096); conn.close()
            if response.status == 200:
                unloaded.append(model)
        except Exception:
            pass
    return unloaded


def _warm_release_model(timeout: float = 600.0) -> tuple[bool, str]:
    """Load the exact release model before the UI accepts its first real request."""
    started = time.monotonic()
    try:
        body = json.dumps({
            "model": _configured_model(),
            "prompt": "Ответь одним словом: готов",
            "stream": False,
            "keep_alive": "2h",
            "options": {"num_ctx": 4096, "num_predict": 2, "temperature": 0},
        }, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=max(30.0, timeout))
        conn.request(
            "POST", "/api/generate", body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        response = conn.getresponse()
        raw = response.read(1_000_000)
        status = response.status
        conn.close()
        if status != 200:
            detail = raw.decode("utf-8", errors="replace")[-500:]
            return False, f"Рабочий контур вернул ошибку {status}: {detail}"
        active = {name.casefold() for name in _ollama_running_models()}
        if _configured_model().casefold() not in active:
            return False, "Локальный контур завершил тест, но не остался готовым"
        elapsed = max(0.0, time.monotonic() - started)
        return True, f"Рабочий контур готов за {elapsed:.1f} с"
    except Exception as exc:
        return False, str(exc)


def _stop_outdated_runtime(port: int) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        conn.request("POST", "/api/system/shutdown", body=b"", headers={"Connection": "close", **_app_key_headers()})
        response = conn.getresponse()
        response.read(2_000)
        conn.close()
        if response.status not in {200, 202, 204}:
            return False
    except Exception:
        return False
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if not _direct_ping(port, timeout=.35):
            return True
        time.sleep(.2)
    return False


def _port_used(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _runtime_port() -> int | None:
    path = APP_ROOT / "logs" / "runtime_port"
    try:
        value = int(path.read_text(encoding="ascii").strip())
        return value if 1024 <= value <= 65535 else None
    except Exception:
        return None


def _find_existing_eirven() -> int | None:
    preferred = []
    runtime = _runtime_port()
    if runtime:
        preferred.append(runtime)
    preferred.extend(port for port in range(DEFAULT_PORT, DEFAULT_PORT + 12) if port not in preferred)
    # Раньше порты опрашивались по очереди с ожиданием в секунду. На Windows подключение
    # к ЗАКРЫТОМУ локальному порту не отказывает сразу — система повторяет попытку, — и
    # когда Эрви не запущена (при автозапуске — всегда), 13 портов стоили ~12,6 секунды,
    # а функция вызывается дважды за запуск. Теперь сначала параллельно и коротко
    # выясняем, какие порты вообще открыты: открытый отвечает за миллисекунды, закрытый
    # отсекается за четверть секунды. Полный пинг — только открытым, в прежнем порядке.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(preferred)) as pool:
        listening = [port for port, is_open in zip(preferred, pool.map(_port_open, preferred)) if is_open]
    for port in listening:
        if _direct_ping(port):
            return port
    return None


def _port_open(port: int, timeout: float = 0.25) -> bool:
    """Принимает ли кто-то соединения на этом локальном порту — быстро и без HTTP."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _sweep_abandoned_instances() -> None:
    """Terminate EIRVEN processes left over from previous runs.

    A crashed or force-closed run leaves its server and worker processes alive.
    They keep the frozen bundle's temp folder locked -- which is what produces
    "Failed to remove temporary directory" -- and compete for the listening port.
    Identification is by command line, so nothing unrelated is affected, and the
    current process tree is always excluded.
    """
    if os.name != "nt":
        return
    try:
        import psutil  # type: ignore
    except Exception:
        return
    keep = {os.getpid()}
    try:
        keep.add(psutil.Process(os.getpid()).ppid())
    except Exception:
        pass
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid in keep:
                continue
            name = str(proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if "eirven_ai.supervisor" not in cmdline and "eirven_ai.app" not in cmdline \
                    and "eirven_ai.tts_worker" not in cmdline and "eirven_ai.voice_worker" not in cmdline:
                continue
            proc.terminate()
        except Exception:
            continue
    # Папки распаковки здесь больше не удаляем. Раньше удалялись все чужие _MEI
    # без проверки, и у соседней, только что запущенной копии Эрви пропадал
    # встроенный пакет. Этим занимается _clean_orphaned_bundles: удаляет только
    # действительно брошенные папки.


def _clean_orphaned_bundles() -> None:
    """Удалить распаковки PyInstaller, брошенные прошлыми запусками.

    Раньше удалялись ВСЕ папки _MEI, кроме своей, с ignore_errors — в расчёте, что
    папку работающего процесса Windows удалить не даст. Это неверно: работающий
    процесс держит заблокированными только свои .dll, а данные (src, scripts,
    assets) не держит вовсе. Удаление сносило их у соседней копии Эрви — и та
    падала с «В EXE отсутствует встроенный пакет», хотя пакет был на месте.

    Теперь папка удаляется, только если она действительно брошена:
      — ей больше получаса: только что распакованная копия ещё стартует;
      — её удаётся переименовать: Windows не даёт переименовать папку, в которой
        работающий процесс держит файлы. Переименование — проверка «никто не
        пользуется», и только после неё — удаление.
    """
    try:
        import shutil
        import tempfile
        current = str(getattr(sys, "_MEIPASS", "") or "")
        now = time.time()
        removed = 0
        kept_in_use = 0
        for folder in Path(tempfile.gettempdir()).glob("_MEI*"):
            if not folder.is_dir() or str(folder) == current:
                continue
            try:
                if now - folder.stat().st_mtime < 1800:
                    continue            # свежая распаковка: её копия ещё запускается
            except OSError:
                continue
            probe = folder.with_name(folder.name + "-eirven-cleanup")
            try:
                folder.rename(probe)    # не вышло — значит, папкой кто-то пользуется
            except OSError:
                kept_in_use += 1
                continue
            shutil.rmtree(probe, ignore_errors=True)
            removed += 1
        if removed or kept_in_use:
            _trace("BUNDLE_CLEANUP", removed=removed, kept_in_use=kept_in_use)
    except Exception:
        pass


def _reclaim_default_port() -> None:
    """Free port 7860 if a stale EIRVEN process is still holding it.

    The port walk below drifts upward whenever the canonical port is taken, so one
    survivor from a previous run permanently changes the address the phone must use
    and invalidates the firewall rule, which is created per port. Only processes
    whose command line identifies them as EIRVEN are touched.
    """
    if os.name != "nt" or not _port_used(DEFAULT_PORT):
        return
    try:
        import psutil  # type: ignore
    except Exception:
        return
    for conn in psutil.net_connections(kind="inet"):
        laddr = getattr(conn, "laddr", None)
        if not laddr or getattr(laddr, "port", None) != DEFAULT_PORT or not conn.pid:
            continue
        try:
            proc = psutil.Process(conn.pid)
            cmdline = " ".join(proc.cmdline()).lower()
            if "eirven" not in cmdline:
                continue
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            continue


def _choose_free_port() -> int:
    _reclaim_default_port()
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 30):
        if not _port_used(port):
            return port
    raise RuntimeError("Не найден свободный локальный порт для EIRVEN")


def _run_installer() -> int:
    script = APP_ROOT / "scripts" / "ensure_runtime.ps1"
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=APP_ROOT)


def _start_server(python: Path, port: int) -> subprocess.Popen:
    logs = APP_ROOT / "logs"; logs.mkdir(exist_ok=True)
    training_coexist = _owner_training_active()
    (logs / "runtime_port").write_text(str(port), encoding="ascii")
    log = (logs / "supervisor.log").open("a", encoding="utf-8")
    env = {
        **os.environ,
        **RELEASE_MODEL_ENV,
        "EIRVEN_ROOT_DIR": str(APP_ROOT),
        # A stale user/system environment variable must never silently turn the
        # phone server back into localhost-only mode.  The HTTP guard still exposes
        # only the token-scoped mobile surface to private-LAN clients.
        "EIRVEN_HOST": "0.0.0.0",
        "EIRVEN_OPEN_BROWSER": "false",
        "EIRVEN_PORT": str(port),
        "EIRVEN_TRAINING_COEXIST": "1" if training_coexist else "0",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    # The frozen launcher unpacks itself into a temp _MEI folder and prepends that
    # folder to PATH. A child inheriting it may load DLLs from there, which keeps the
    # directory locked and makes Windows report "Failed to remove temporary directory"
    # when the launcher exits. The server runs entirely from the installed tree, so
    # remove the bundle's entries instead of passing them down.
    meipass = str(getattr(sys, "_MEIPASS", "") or "")
    if meipass:
        env.pop("_MEIPASS2", None)
        parts = [p for p in str(env.get("PATH", "")).split(os.pathsep) if p and os.path.normcase(os.path.normpath(p)) != os.path.normcase(os.path.normpath(meipass))]
        env["PATH"] = os.pathsep.join(parts)
    return subprocess.Popen([str(python), "-m", "eirven_ai.supervisor"], cwd=APP_ROOT, env=env, stdout=log, stderr=log, creationflags=flags)


def _orb_radial_gradient(size: int, stops: list, blur: float):
    """Pure-PIL (no numpy) radial gradient: concentric rings, colour/alpha
    interpolated per stop, then softened with one blur pass. Runs once per
    orb build, not per animation frame, so plain nested drawing is fine.
    ``stops`` is ``[(radius_fraction_0..1, (r, g, b, a)), ...]`` from the
    centre outward.
    """
    from PIL import Image, ImageDraw, ImageFilter
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    max_r = size / 2
    steps = 120
    for i in range(steps, -1, -1):
        t = i / steps
        r = t * max_r
        color = stops[-1][1]
        for (r0, c0), (r1, c1) in zip(stops, stops[1:]):
            if r0 <= t <= r1 or (r1, c1) == stops[-1]:
                seg = 0.0 if r1 == r0 else max(0.0, min(1.0, (t - r0) / (r1 - r0)))
                color = tuple(int(c0[k] + (c1[k] - c0[k]) * seg) for k in range(4))
                break
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    return img.filter(ImageFilter.GaussianBlur(blur)) if blur else img


def _orb_comet_ring(size: int, *, radius: float, width: float, start_deg: float,
                     sweep_deg: float, rgb: tuple, max_alpha: int):
    """A thin ring arc that fades in/out along its sweep instead of a flat
    outline -- the same 'gradient arc via a mask' idea as the web orb's
    conic-gradient rings, done with a chain of soft dots since Canvas/PIL
    has no conic-gradient primitive."""
    import math
    from PIL import Image, ImageDraw, ImageFilter
    # Сглаживание через отрисовку в двойном размере: тонкая дуга, нарисованная
    # сразу в 190 пикселей, даёт заметные ступеньки на краях. Рисуем вдвое
    # крупнее и уменьшаем — края становятся плавными, стоимость приемлемая.
    SS = 2
    work = size * SS
    layer = Image.new("RGBA", (work, work), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx = cy = work / 2
    radius = radius * SS
    width = width * SS
    steps = 260
    peak = 0.35
    for i in range(steps):
        t = i / (steps - 1)
        ang = math.radians(start_deg + sweep_deg * t)
        fall = abs(t - peak) / max(peak, 1 - peak)
        alpha = max(0.0, 1 - fall ** 1.6) * max_alpha
        if alpha < 1:
            continue
        x = cx + radius * math.cos(ang)
        y = cy + radius * math.sin(ang)
        r = width / 2
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*rgb, int(alpha)))
    # Размываем в рабочем разрешении, затем уменьшаем: LANCZOS усредняет
    # соседние пиксели и убирает лестницу на краях дуги.
    layer = layer.filter(ImageFilter.GaussianBlur(0.6 * SS))
    return layer.resize((size, size), Image.LANCZOS)



def _with_eyes(image):
    """Нарисовать глаза на картинке сферы через модуль orb_eyes, если он доступен."""
    try:
        import importlib.util
        for base in (ROOT, APP_ROOT):
            module_path = base / "src" / "eirven_ai" / "orb_eyes.py"
            if module_path.is_file():
                spec = importlib.util.spec_from_file_location("eirven_orb_eyes", module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.add_eyes(image)
    except Exception as exc:
        _trace("LAUNCHER_EYES_FAILED", error=str(exc)[:200])
    return image

def _build_orb_base(image_path: Path):
    """Build the static part of the mini orb once: ambient glow, the actual
    eirven-orb.png texture (soft-edged circular crop), a crisp glass rim and
    a fixed specular highlight. The only thing animated per frame afterwards
    is the pair of comet rings composited on top of a copy of this image --
    see LauncherWindow._animate_orb."""
    from PIL import Image, ImageDraw, ImageFilter
    size = 190
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    glow = _orb_radial_gradient(size, [
        (0.00, (101, 232, 255, 235)),
        (0.34, (92, 143, 255, 150)),
        (0.55, (136, 116, 255, 90)),
        (0.78, (197, 101, 173, 34)),
        (1.00, (10, 15, 30, 0)),
    ], blur=9)
    base.alpha_composite(glow)

    off = int((size - 158) / 2)
    if image_path.is_file():
        tex = Image.open(image_path).convert("RGBA")
        # Глаза — те же, что в окне Эрви и на сфере рабочего стола. Лончер пакет
        # не подключает, поэтому модуль глаз загружаем прямо из файла. Не вышло —
        # сфера останется без глаз, но запуск от этого не пострадает.
        tex = _with_eyes(tex)
        pad = int(min(tex.size) * .075)
        tex = tex.crop((pad, pad, tex.width - pad, tex.height - pad)).resize((158, 158), Image.LANCZOS)
        alpha = Image.new("L", tex.size, 0)
        ImageDraw.Draw(alpha).ellipse((2, 2, 156, 156), fill=255)
        tex.putalpha(alpha.filter(ImageFilter.GaussianBlur(1.2)))
        tex_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        tex_layer.paste(tex, (off, off), tex)
        base.alpha_composite(tex_layer)

    rim = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse((off + 1, off + 1, off + 156, off + 156),
                                 outline=(200, 232, 255, 130), width=1)
    base.alpha_composite(rim)

    spec = _orb_radial_gradient(size, [
        (0.00, (255, 255, 255, 165)),
        (0.10, (255, 255, 255, 60)),
        (0.22, (255, 255, 255, 0)),
        (1.00, (255, 255, 255, 0)),
    ], blur=3)
    spec = spec.transform(spec.size, Image.AFFINE, (1, 0, 18, 0, 1, 14))
    base.alpha_composite(spec)
    return base


def _show_orb(port: int) -> None:
    # Раньше любой сбой здесь глотался молча: сфера не появлялась, а в журнале —
    # ни слова. Теперь причина остаётся в loggg2.txt.
    try:
        import urllib.request
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/companion/show?respect_setting=true", data=b"", method="POST", headers=_app_key_headers())
        urllib.request.urlopen(request, timeout=4.0).read()
        _trace("ORB_SHOW_OK", port=port)
    except Exception as exc:
        status = getattr(exc, "code", "")
        _trace("ORB_SHOW_FAIL", port=port, status=str(status), error=str(exc)[:300],
               key_present=bool(_app_key_headers()))


def _present(port: int, *, first_install: bool = False) -> None:
    """Показать Эрви человеку после запуска.

    Раньше после запуска показывалась только сфера, а окно интерфейса — лишь при
    самой первой установке. Пока был открыт путь через браузер, это не мешало.
    Теперь Эрви — только приложение, и если сфера не появилась, войти было
    некуда. Поэтому при ручном запуске окно открывается сразу, как у любой
    программы. При автозапуске вместе с Windows — только сфера, без окна:
    человек только включил компьютер и не просил ничего открывать.
    """
    _show_orb(port)
    if _autostart_requested():
        _trace("PRESENT", mode="autostart", window=False)
        return
    try:
        next_path = "/ui/?welcome=1" if first_install else "/ui/"
        _open_app_window(_app_entry_url(port, next_path))
        _trace("PRESENT", mode="manual", window=True, first_install=first_install)
    except Exception as exc:
        _trace("PRESENT_WINDOW_FAIL", error=str(exc)[:300])


class LauncherWindow:
    def __init__(self, *, quiet: bool = False, preview: bool = False) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.quiet = bool(quiet)
        self.preview = bool(preview)
        self._shown_at = time.monotonic()
        self.root = tk.Tk()
        self.root.title("Эрви")
        self.root.geometry("640x360")
        self.root.minsize(600, 340)
        self.root.resizable(False, False)
        self.root.configure(bg="#03050d")
        try:
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass
        _enable_windows_glass(self.root)
        if self.quiet:
            self.root.withdraw()
        try:
            self.root.iconbitmap(str(APP_ROOT / "assets" / "eirven.ico"))
        except Exception:
            pass
        tk.Label(
            self.root, text="Э Р В И", font=("Segoe UI", 10, "bold"),
            fg="#dce8ff", bg="#03050d",
        ).place(x=24, y=18)
        tk.Label(
            self.root, text="Личный помощник", font=("Segoe UI", 9),
            fg="#71809f", bg="#03050d",
        ).place(x=96, y=19)
        card = tk.Frame(
            self.root, bg="#0b1124", highlightbackground="#263759",
            highlightthickness=1,
        )
        card.place(x=20, y=48, width=600, height=286)
        accent = tk.Frame(card, bg="#65e8ff")
        accent.place(x=0, y=0, width=4, height=286)
        self.orb = tk.Canvas(card, width=190, height=190, bg="#0b1124", highlightthickness=0)
        self.orb.place(x=16, y=38)
        self._orb_angle_a = 200.0
        self._orb_angle_b = 30.0
        self._orb_base = None
        self._orb_photo = None
        self._orb_image_id = None
        try:
            image_path = APP_ROOT / "src" / "eirven_ai" / "web" / "eirven-orb.png"
            if not image_path.is_file():
                image_path = APP_ROOT / "assets" / "eirven.png"
            self._orb_base = _build_orb_base(image_path)
        except Exception:
            pass
        self._animate_orb()
        self.status = tk.Label(
            card, text="Запускаю Эрви…", font=("Segoe UI", 20, "bold"),
            fg="#f5f8ff", bg="#0b1124", anchor="w",
        )
        self.status.place(x=218, y=52, width=350, height=36)
        self.details = tk.Label(
            card, text="Проверяю локальный сервис и голос", font=("Segoe UI", 10),
            fg="#9aa9c8", bg="#0b1124", anchor="nw", justify="left",
            wraplength=342,
        )
        self.details.place(x=218, y=98, width=350, height=52)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Launcher.Horizontal.TProgressbar", troughcolor="#151c34",
            background="#65e8ff", bordercolor="#151c34",
            lightcolor="#65e8ff", darkcolor="#8874ff", thickness=12,
        )
        self.progress = ttk.Progressbar(
            card, mode="indeterminate", length=342,
            style="Launcher.Horizontal.TProgressbar",
        )
        self.progress.place(x=218, y=166)
        self.progress.start(10)
        self.footer = tk.Label(
            card, text="Ничего нажимать не нужно — откроюсь сама",
            font=("Segoe UI", 9), fg="#71809f", bg="#0b1124", anchor="w",
        )
        self.footer.place(x=218, y=193, width=342)
        self.log_button = tk.Button(
            card, text="Открыть журнал", command=self._open_logs,
            font=("Segoe UI", 9), fg="#e8f4ff", bg="#1b2745",
            activeforeground="#ffffff", activebackground="#26365f",
            relief="flat", cursor="hand2", padx=12, pady=5,
        )
        self.retry_button = tk.Button(
            card, text="Повторить", command=self._retry,
            font=("Segoe UI", 9, "bold"), fg="#06111b", bg="#77eaff",
            activeforeground="#06111b", activebackground="#aaf3ff",
            relief="flat", cursor="hand2", padx=14, pady=5,
        )

    def _animate_orb(self) -> None:
        # Two slow gradient comet-rings orbiting a cached glow+texture+specular
        # base, matching the same 12s/16s timing as the living orb in the main
        # window. Only the cheap ring layer is rebuilt each tick (~2-3ms); the
        # numpy-free radial gradients and the orb texture are composited once
        # in _build_orb_base and reused as a plain image copy every frame.
        try:
            from PIL import Image, ImageTk
            self._orb_angle_a = (self._orb_angle_a + 0.66) % 360.0
            self._orb_angle_b = (self._orb_angle_b - 0.50) % 360.0
            if self._orb_base is not None:
                frame = self._orb_base.copy()
                frame.alpha_composite(_orb_comet_ring(
                    190, radius=88, width=3.2, start_deg=self._orb_angle_a, sweep_deg=190,
                    rgb=(139, 214, 255), max_alpha=235,
                ))
                frame.alpha_composite(_orb_comet_ring(
                    190, radius=80, width=2.2, start_deg=self._orb_angle_b, sweep_deg=165,
                    rgb=(214, 150, 255), max_alpha=190,
                ))
                card_bg = Image.new("RGBA", (190, 190), (11, 17, 36, 255))
                card_bg.alpha_composite(frame)
                rgb_frame = card_bg.convert("RGB")
                if self._orb_photo is None:
                    # Allocate the Tk image once and repaint its pixels afterwards.
                    # Creating a new PhotoImage every tick churned ~29 images a second
                    # and left the old ones for Tk to reclaim under load.
                    self._orb_photo = ImageTk.PhotoImage(rgb_frame)
                    self._orb_image_id = self.orb.create_image(95, 95, image=self._orb_photo)
                else:
                    self._orb_photo.paste(rgb_frame)
        except Exception:
            # One bad frame must not end the animation. Without rescheduling below,
            # any single failure froze the sphere for the rest of the session.
            pass
        finally:
            # Сфера в окне запуска и установки живая: кольца вращаются, пока идёт
            # работа. В r72 этот цикл был убран по ошибке — просьба «мини-сфера не
            # должна двигаться» относилась к плавающей сфере на рабочем столе, а
            # остановлена была эта. С тех пор здесь стояла неподвижная картинка.
            # Плавность — от сглаживания колец в двойном размере и 45 кадров в секунду.
            try:
                self.root.after(22, self._animate_orb)
            except Exception:
                pass

    def set(self, status: str, details: str = "") -> None:
        _trace("LAUNCHER_STEP", status=status, details=details)
        self.root.after(0, lambda: self.status.config(text=status)); self.root.after(0, lambda: self.details.config(text=details))

    def done(self) -> None:
        if self.quiet:
            self.root.after(0, self.root.destroy)
            return
        elapsed = time.monotonic() - self._shown_at
        delay_ms = max(0, int((.95 - elapsed) * 1000))
        self.root.after(delay_ms, self.root.destroy)

    def _open_logs(self) -> None:
        try:
            logs = APP_ROOT / "logs"
            logs.mkdir(exist_ok=True)
            if os.name == "nt":
                os.startfile(str(logs))
            else:
                webbrowser.open(logs.as_uri())
        except Exception as exc:
            self.details.config(text=f"Журнал: {APP_ROOT / 'logs'}\n{exc}", fg="#ff9eaf")

    def _retry(self) -> None:
        self.log_button.place_forget()
        self.retry_button.place_forget()
        self.status.config(text="Пробую ещё раз…", fg="#f4f8ff")
        self.details.config(text="Продолжаю с уже подготовленного шага", fg="#aebee2")
        self.progress.place(x=218, y=166)
        self.progress.start(10)
        self.footer.place(x=218, y=193, width=342)
        threading.Thread(target=self.worker, daemon=True).start()

    def fail(self, message: str) -> None:
        def show():
            if self.quiet:
                self.root.deiconify(); self.root.lift()
            self.progress.stop(); self.progress.place_forget(); self.footer.place_forget()
            self.status.config(text="Не получилось запустить", fg="#ffd7e2")
            self.details.config(text=message[:220], fg="#ff9eaf", height=3)
            self.log_button.place(x=218, y=174)
            self.retry_button.place(x=360, y=174)
        self.root.after(0, show)

    def worker(self) -> None:
        first_install = False
        try:
            _enforce_release_env()
            port = _find_existing_eirven()
            active_build = _running_build(port) if port else ""
            active_model = _running_model(port) if port else ""
            # При входе в Windows старых копий Эрви быть не может: это первое, что
            # запускается после входа, а процессы прошлого сеанса Windows закрывает
            # сама. Список процессов через PowerShell (WMI) при загрузке системы
            # занимал до 8 секунд — а искал то, чего нет. Если на порту Эрви никто
            # не отвечает, при автозапуске его пропускаем.
            _phase_started = time.monotonic()
            # Поиск запущенных копий через WMI занимает до 8 секунд. Нужен он, только
            # если на порту Эрви кто-то отвечает: иначе брошенные процессы и так
            # уберёт быстрая проверка перед выбором порта.
            _scan_processes = bool(port)
            if _scan_processes:
                supervisors, servers = _eirven_runtime_counts()
            else:
                supervisors, servers = 0, 0
            _trace("LAUNCHER_PHASE", phase="поиск запущенных копий",
                   seconds=round(time.monotonic() - _phase_started, 2), skipped=not _scan_processes)
            duplicate_runtime = supervisors > 1 or servers > 1
            wrong_runtime = bool(port and (
                active_build != CURRENT_BUILD
                or active_model.casefold() != _configured_model().casefold()
            ))
            if duplicate_runtime or wrong_runtime:
                reason = "убираю дублирующие процессы" if duplicate_runtime else f"заменяю {active_build or 'старую сборку'}"
                self.set("Обновляю EIRVEN", reason)
                stopped = _stop_all_eirven_instances()
                if not stopped and port:
                    stopped = _stop_outdated_runtime(port)
                if not stopped:
                    raise RuntimeError(
                        "Windows не дала завершить старую копию EIRVEN. "
                        "Перезагрузи компьютер и запусти этот файл ещё раз."
                    )
                deadline = time.monotonic() + 12
                while port and _direct_ping(port, timeout=.3) and time.monotonic() < deadline:
                    time.sleep(.2)
                port = None
            if port:
                python = _installed_python()
                if python is not None:
                    self.set("Проверяю доступ с телефона", f"Локальная сеть · порт {port}")
                    _ensure_mobile_firewall(python, port)
                self.set("EIRVEN уже работает", "Показываю сферу. Интерфейс откроется по клику.")
                _present(port); time.sleep(.25); self.done(); return

            python = _installed_python()
            # Only the installer may create the release marker after models, voice,
            # core checks and shortcut setup have completed. The launcher must never
            # fabricate completion from a working Python import alone.
            marker = APP_ROOT / INSTALL_MARKER
            if python is not None and not marker.exists():
                _adopt_compatible_install_marker(marker)
            # При автозапуске установку не запускаем, если Эрви уже стоит (Python на
            # месте). Маркер меняется с каждой сборкой, и первый запуск после
            # обновления шёл в полную установку — а при входе в Windows сеть и
            # Ollama могут ещё не подняться: запуск тянулся минутами или падал с
            # «Установка не завершилась». Обновление маркера случится при следующем
            # обычном запуске, когда человек рядом и сеть есть.
            skip_install_at_boot = _autostart_requested() and python is not None
            if skip_install_at_boot and not marker.exists():
                _trace("AUTOSTART_SKIP_INSTALL", reason="маркер сборки устарел, окружение на месте")
            # Без интернета установщик всё равно не сможет поставить пакеты — а Эрви уже
            # установлена и работает без сети. Раньше первый запуск после обновления без
            # интернета шёл в установку и падал: «без интернета Эрви не работает». Сеть
            # проверяем только здесь, когда обновление пакетов действительно ожидается.
            if python is not None and not marker.exists() and not skip_install_at_boot and not _internet_available():
                skip_install_at_boot = True
                _trace("OFFLINE_SKIP_INSTALL", reason="нет интернета, окружение на месте; обновлю пакеты, когда сеть появится")
            if (python is None or not marker.exists()) and not skip_install_at_boot:
                first_install = True
                self.set("Первый запуск", "Сейчас откроется окно с прогрессом установки.")
                code = _run_installer()
                if code != 0:
                    raise RuntimeError("Установка не завершилась. Открой logs/install.log — уже установленные компоненты сохранены.")
                python = _installed_python()
                if python is None:
                    raise RuntimeError("Не найдено окружение Python после установки")

            self.set("Подготавливаю Эрви", "Проверяю готовность · без повторных загрузок")
            if _autostart_requested() and not _ollama_ping():
                # При входе в Windows не ждём Ollama до 35 секунд: серверу для старта
                # она не нужна, только для ответа на сообщение. Поднимаем её в фоне.
                threading.Thread(target=_ensure_ollama_running, daemon=True,
                                 name="eirven-ollama-boot").start()
                ollama_ready, ollama_detail = False, "Ollama поднимается в фоне"
                _trace("AUTOSTART_OLLAMA_BACKGROUND")
            else:
                ollama_ready, ollama_detail = _ensure_ollama_running()
            if not ollama_ready:
                # Keep the web/UI runtime available so diagnostics and the bundled
                # Education fallback can still explain what is wrong.
                try:
                    logs = APP_ROOT / "logs"
                    logs.mkdir(exist_ok=True)
                    (logs / "ollama_startup_status.txt").write_text(ollama_detail, encoding="utf-8")
                except Exception:
                    pass

            installed_models = _ollama_models() if ollama_ready else []
            expected_model = _configured_model()
            installed_keys = {name.casefold() for name in installed_models}
            model_ready = expected_model.casefold() in installed_keys
            expected_vision = _configured_vision_model()
            vision_ready = expected_vision.casefold() in installed_keys
            if ollama_ready and not (model_ready and vision_ready) and _autostart_requested():
                # При входе в Windows Ollama может ещё не отдать полный список моделей,
                # и они покажутся «отсутствующими». Докачку гигабайт при загрузке системы
                # не начинаем — она случится при обычном запуске, если модели и правда нет.
                _trace("AUTOSTART_SKIP_MODEL_INSTALL", model_ready=model_ready, vision_ready=vision_ready)
            elif ollama_ready and not (model_ready and vision_ready):
                missing = [name for name, ready in ((expected_model, model_ready), (expected_vision, vision_ready)) if not ready]
                self.set("Завершаю подготовку", "Скорость и прогресс появятся в окне установки.")
                code = _run_installer()
                if code != 0:
                    raise RuntimeError("Подготовка не завершилась. Повтори запуск — уже загруженные данные сохранены.")
                _enforce_release_env()
                ollama_ready, ollama_detail = _ensure_ollama_running()
                installed_models = _ollama_models() if ollama_ready else []
                installed_keys = {name.casefold() for name in installed_models}
                model_ready = expected_model.casefold() in installed_keys
                vision_ready = expected_vision.casefold() in installed_keys
            if not ollama_ready or not model_ready or not vision_ready:
                if _autostart_requested():
                    # При входе в Windows Ollama поднимается в фоне — мы сами решили её не
                    # ждать. Раньше здесь запуск падал через долю секунды после этого
                    # решения, и человек при каждом автозапуске жал «Повторить». Серверу
                    # для старта Ollama не нужна: шлюз к модели дождётся её или сам запустит.
                    _trace("AUTOSTART_CONTINUE_WITHOUT_OLLAMA", ollama_ready=ollama_ready,
                           model_ready=model_ready, vision_ready=vision_ready)
                else:
                    raise RuntimeError("Рабочий контур не подготовлен. Повтори запуск — прогресс сохранён.")
            unloaded_models = _unload_non_release_models()
            # Прогрев модели — в фоне. Раньше лончер ждал его до 10 минут ДО запуска
            # сервера, а при сбое ронял весь запуск: «Не удалось завершить подготовку».
            # При входе в Windows Ollama ещё поднимается, на слабой машине модель
            # грузится долго — отсюда медленный или сорванный автозапуск. Прогрев
            # нужен лишь для быстрого первого ответа, и сервер при старте делает его
            # сам. Поэтому сервер запускается сразу, а неудача прогрева ни на что не
            # влияет: модель догрузится при первом сообщении.
            def _background_warm() -> None:
                try:
                    ok, detail = _warm_release_model()
                    _trace("WARMUP_DONE", ok=ok, detail=str(detail)[:200])
                except Exception as exc:
                    _trace("WARMUP_FAILED", error=str(exc)[:200])
            threading.Thread(target=_background_warm, daemon=True, name="eirven-warm").start()
            warm_ready, warm_detail = True, "прогрев идёт в фоне"
            _trace("WARMUP_BACKGROUND")
            try:
                logs = APP_ROOT / "logs"; logs.mkdir(exist_ok=True)
                (logs / "ollama_model_status.json").write_text(json.dumps({
                    "ollama_ready": ollama_ready, "configured_model": expected_model,
                    "model_ready": model_ready, "vision_ready": vision_ready,
                    "installed": installed_models, "unloaded_legacy_models": unloaded_models,
                    "warm_ready": warm_ready, "warm_detail": warm_detail,
                    "training_coexist": _owner_training_active(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            _phase_started = time.monotonic()
            # Удаление старых распаковок — сотни мегабайт файлов. Запуск его не ждёт:
            # чистка всегда идёт в фоне.
            threading.Thread(target=_clean_orphaned_bundles, daemon=True, name="eirven-cleanup").start()
            port = _find_existing_eirven()
            if port is None:
                # Nothing is answering, so anything still running is abandoned.
                # Clear it out before choosing a port: survivors hold both the
                # port and their unpacked bundle folder. При входе в Windows таких
                # копий нет — обход всех процессов тогда пропускаем.
                if not _autostart_requested():
                    _sweep_abandoned_instances()
                port = _choose_free_port()
            _trace("LAUNCHER_PHASE", phase="подготовка порта", seconds=round(time.monotonic() - _phase_started, 2))
            self.set("Настраиваю доступ с телефона", f"Локальная сеть · порт {port}")
            # Правило брандмауэра нужно, когда подключится телефон, а не для старта
            # сервера. Запуск на нём не держим — ни при входе в Windows, ни при обычном.
            threading.Thread(target=_ensure_mobile_firewall, args=(python, port), daemon=True,
                             name="eirven-firewall").start()
            firewall_ready = True
            detail = f"127.0.0.1:{port}"
            if not firewall_ready:
                detail += " · телефону может мешать Windows Firewall"
            self.set("Запускаю локальный сервер", detail)
            process = _start_server(python, port)
            started = time.monotonic()
            hard_deadline = started + 600
            slow_notice = False
            while time.monotonic() < hard_deadline:
                if _direct_ping(port, timeout=.8):
                    self.set("Готово", "Эрви запускается.")
                    _present(port, first_install=first_install)
                    time.sleep(.3); self.done(); return
                if process.poll() is not None:
                    # Supervisor may have intentionally exited because another instance won the race.
                    existing = _find_existing_eirven()
                    if existing:
                        _present(existing); self.done(); return
                    raise RuntimeError("Сервер завершился. EIRVEN сохранил причину в logs/server.log")
                elapsed = time.monotonic() - started
                if elapsed > 90 and not slow_notice:
                    slow_notice = True
                    self.set("Первый запуск занимает дольше обычного", "Сервер жив. Жду и не переустанавливаю компоненты.")
                time.sleep(.45)
            raise RuntimeError("Сервер не поднялся за 10 минут. Проверь logs/server.log; повторная установка не требуется.")
        except Exception as exc:
            import traceback as _tb
            tail = ""
            try:
                server_log = APP_ROOT / "logs" / "server.log"
                if server_log.exists():
                    tail = server_log.read_text(encoding="utf-8", errors="replace")[-4000:]
            except Exception:
                pass
            # Всё, что нужно для разбора: сообщение, тип, полная трассировка и хвост
            # журнала сервера — если сервер поднялся и упал, причина будет в нём.
            _trace("LAUNCHER_FAIL", error=str(exc), error_type=type(exc).__name__,
                   traceback=_tb.format_exc()[-6000:], server_log_tail=tail)
            self.fail(str(exc))

    def run(self) -> None:
        if self.preview:
            self.status.config(text="Запускаю Эрви…")
            self.details.config(text="Проверяю голос, память и связь с телефоном")
        else:
            self.root.after(
                140,
                lambda: threading.Thread(target=self.worker, daemon=True).start(),
            )
        self.root.mainloop()


def _wait_for_previous_to_exit(timeout: float = 25.0) -> None:
    """Дождаться, пока прежняя копия Эрви закроется.

    Фоновое обновление запускает новый файл с флагом /EIRVEN-UPDATE и тут же
    просит старую копию остановиться. Раньше лончер этот флаг не понимал и сразу
    начинал заменять файлы — пока старая копия ещё работала и держала их.
    Теперь он сначала ждёт её выхода, а если за отведённое время она не ушла —
    закрывает её сам, штатным способом, и только потом обновляет файлы.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            supervisors, servers = _eirven_runtime_counts()
        except Exception:
            return
        if supervisors == 0 and servers == 0:
            return
        time.sleep(0.5)
    try:
        _stop_all_eirven_instances()
    except Exception:
        pass


def _install_crash_logger() -> None:
    """Любая необработанная ошибка — в журнал, с полной трассировкой.

    Без этого сбой вне окна лончера закрывал программу молча: ни окна, ни записи.
    """
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            import traceback as _tb
            _trace("LAUNCHER_CRASH", error=str(exc), error_type=getattr(exc_type, "__name__", str(exc_type)),
                   traceback="".join(_tb.format_exception(exc_type, exc, tb))[-6000:])
        except Exception:
            pass
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    # Ошибки в фоновых потоках (установка, ожидание сервера) — тоже в журнал.
    def thread_hook(args):
        try:
            import traceback as _tb
            _trace("LAUNCHER_THREAD_CRASH", thread=getattr(args.thread, "name", ""),
                   error=str(args.exc_value), error_type=getattr(args.exc_type, "__name__", ""),
                   traceback="".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))[-6000:])
        except Exception:
            pass
    threading.excepthook = thread_hook


if __name__ == "__main__":
    _install_crash_logger()
    preview = "--ui-preview" in sys.argv[1:]
    updating = any(arg.upper() == "/EIRVEN-UPDATE" for arg in sys.argv[1:])
    _trace_environment()
    if updating:
        _trace("LAUNCHER_UPDATE_MODE", note="жду выхода прежней копии")
        _wait_for_previous_to_exit()
    if not preview:
        try:
            _materialize_embedded_application()
            _trace("MATERIALIZE_OK")
        except Exception as _mat_exc:
            import traceback as _tb
            # Замена файлов идёт ДО окна лончера. Если она падала, программа
            # закрывалась молча. Теперь причина остаётся в журнале.
            _trace("MATERIALIZE_FAIL", error=str(_mat_exc), error_type=type(_mat_exc).__name__,
                   traceback=_tb.format_exc()[-6000:])
            raise
    if "--materialize-only" in sys.argv[1:]:
        raise SystemExit(0)
    if preview:
        LauncherWindow(preview=True).run()
    else:
        autostart = _autostart_requested()
        _repair_existing_autostart()
        if not _request_elevation(autostart=autostart):
            LauncherWindow(quiet=autostart).run()
