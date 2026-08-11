from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".mpg",
    ".mpeg", ".mts", ".m2ts", ".3gp", ".flv", ".ts", ".vob", ".ogv",
    ".asf", ".rm", ".rmvb", ".divx", ".f4v", ".hevc", ".h264", ".av1",
}
AUDIO_OUTPUTS = {"mp3", "wav", "flac", "m4a", "aac", "ogg"}
VIDEO_OUTPUTS = {"mp4", "mkv", "mov", "webm", "gif"}
INBOX_STABLE_SECONDS = 0.9


class VideoError(RuntimeError):
    pass


class VideoEditor:
    """Safe, stateful FFmpeg workspace behind EIRVEN's natural-language video lane.

    The public ``video`` directory is deliberately only an inbox. Active source files
    are recorded in a manifest, so a file copied there later can never silently become
    part of an already-running project. Accepted results and their sources are moved to
    separate durable directories instead of being deleted.
    """

    HELP_TEXT = (
        "Я умею монтировать и обрабатывать видео: склеивать, обрезать, менять формат и "
        "скорость, убирать или усиливать звук, поворачивать, кадрировать, стабилизировать, "
        "улучшать резкость и разрешение, добавлять текст и плавные переходы. Положи все "
        "нужные исходники прямо в открытую папку video, затем скажи обычными словами, что "
        "сделать. Например: «Эрви, склей все видео по порядку», «работай только с первым "
        "видео и убери первые 5 секунд», «у второго видео оставь от 00:12 до 00:40, потом "
        "склей 2, 1 и 3» или «сделай ролик 1080p, немного очисти шум и увеличь громкость». "
        "Как только копирование завершится, я сама сразу переименую исходники по порядку: 1.mp4, "
        "2.mov, 3.mkv и так далее, сохранив их настоящий формат. Перед монтажом я дополнительно "
        "проверю, что каждый файл читается. Если формулировка допускает "
        "несколько вариантов, я сначала задам короткий уточняющий вопрос."
    )

    _EDIT_RE = re.compile(
        r"\b(?:с?монт\w*|монтаж\w*|скле\w*|соедин\w*|объедин\w*|обре[зж]\w*|отре[зж]\w*|"
        r"выреж\w*|кадрир\w*|ролик\w*|видеофайл\w*|улучш\w*\s+качество|увелич\w*\s+"
        r"качество|разрешени\w*|1080p|720p|4k|2k|ускор\w*|замедл\w*|стабилиз\w*|"
        r"цветокор\w*|яркост\w*|насыщенн\w*|конверт\w*|формат\w*\s+видео|убер\w*\s+"
        r"звук|без\s+звука|извлек\w*\s+(?:звук|аудио)|налож\w*\s+текст|добав\w*\s+"
        r"текст|субтитр\w*|плавн\w*\s+(?:появ|затух)|переход\w*|поверн\w*\s+видео|вертикальн\w*\s+"
        r"видео|сожм\w*\s+видео|обработ\w*\s+видео|работай\w*.{0,25}\b(?:видео|ролик)|"
        r"(?:видео|ролик)\w*.{0,15}\b(?:в|формат\w*)\s+(?:mp4|mkv|mov|webm|gif|mp3|wav))\b",
        re.I,
    )
    _HELP_RE = re.compile(
        r"\b(?:умеешь|можешь\s+ли|ка(?:к)?|что\s+нужно|куда|где)\b.{0,80}"
        r"\b(?:видео|ролик|монтаж|монтировать|обработать|склеить)\w*",
        re.I | re.S,
    )
    _READY_RE = re.compile(
        r"\b(?:(?:я\s+)?(?:положил\w*|закинул\w*|скопировал\w*|перенес\w*|перенёс\w*|"
        r"загрузил\w*|добавил\w*)\b.{0,55}\b(?:видео|ролик|видеофайл)\w*|"
        r"(?:все|всё)\s+(?:видео|ролик)\w*\s+(?:уже\s+)?(?:в\s+папке|загрузил\w*|готов\w*))",
        re.I | re.S,
    )
    _FRAGMENT_RE = re.compile(
        r"^\s*(?:(?:у|для|из)\s+)?(?:всех|кажд\w*)\s+(?:видео|ролик)\w*|"
        r"^\s*только\s+(?:у\s+)?(?:перв\w*|втор\w*|трет\w*|\d+\w*)\s+(?:видео|ролик)\w*",
        re.I,
    )
    _PLAYBACK_RE = re.compile(
        r"\b(?:youtube|ютуб|vk\s*video|вк\s*видео|включи\s+видео|открой\s+видео|"
        r"поставь\s+(?:видео\s+)?на\s+паузу|продолжи\s+видео|следующее\s+видео)\b",
        re.I,
    )
    _NUMBER_TOKEN = (
        r"(?:\d+(?:[.,]\d+)?|полтор[аы]|один|одна|одно|одну|два|две|три|четыре|пять|"
        r"шесть|семь|восемь|девять|десять|одиннадцать|двенадцать|тринадцать|"
        r"четырнадцать|пятнадцать|шестнадцать|семнадцать|восемнадцать|девятнадцать|"
        r"двадцать|тридцать|сорок|пятьдесят|шестьдесят)"
    )

    def __init__(self, settings: Any, gateway: Any | None = None, *, start_watcher: bool = True):
        self.settings = settings
        self.gateway = gateway
        self.root = Path(settings.root_dir).resolve()
        self.inbox = self.root / "video"
        self.archive_root = self.root / "video_archive"
        self.results_root = self.root / "video_results"
        self.work_root = Path(settings.data_dir).resolve() / "video_work"
        self.state_path = Path(settings.data_dir).resolve() / "video_editor_state.json"
        self._lock = threading.RLock()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.results_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._last_original_names: dict[str, str] = {}
        self._watch_observations: dict[str, tuple[tuple[int, int], float]] = {}
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watcher_enabled = bool(start_watcher)
        self._conversation_context: dict[str, float] = {}
        self._last_watch_error = ""
        self._state = self._load_state()
        self._recover_interrupted_state()
        if self._watcher_enabled:
            self._watch_thread = threading.Thread(
                target=self._watch_inbox,
                daemon=True,
                name="eirven-video-inbox",
            )
            self._watch_thread.start()

    # ------------------------------------------------------------------ lifecycle
    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("version", 1)
                raw.setdefault("active", None)
                raw.setdefault("pending", {})
                raw.setdefault("inbox_originals", {})
                raw.setdefault("inbox_blocked", {})
                return raw
        except Exception:
            pass
        return {
            "version": 1,
            "active": None,
            "pending": {},
            "inbox_originals": {},
            "inbox_blocked": {},
        }

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _recover_interrupted_state(self) -> None:
        with self._lock:
            active = self._state.get("active")
            if not isinstance(active, dict):
                return
            if active.get("status") in {"queued", "rendering"}:
                active["status"] = "interrupted"
                active["error"] = (
                    "Предыдущая обработка прервалась при закрытии EIRVEN. Исходники сохранены; "
                    "команду можно повторить."
                )
                self._save_state()

    @staticmethod
    def _natural_key(path: Path) -> list[Any]:
        return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)

    def stop(self) -> None:
        """Stop the lightweight inbox watcher during a normal EIRVEN shutdown."""
        self._watch_stop.set()
        thread = self._watch_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _watch_inbox(self) -> None:
        # Polling avoids another Windows dependency and is reliable for ordinary local,
        # OneDrive and removable-drive copies. A file is only touched after its size and
        # modification time have stayed unchanged for a short grace period.
        while not self._watch_stop.wait(0.35):
            try:
                self._number_stable_inbox()
                self._last_watch_error = ""
            except Exception as exc:
                self._last_watch_error = str(exc)[:500]

    def _staged_original(self, path: Path) -> str:
        entries = self._state.get("inbox_originals") or {}
        entry = entries.get(path.name) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return ""
        try:
            size, mtime_ns = self._file_signature(path)
        except OSError:
            return ""
        if int(entry.get("size") or -1) != size or int(entry.get("mtime_ns") or -1) != mtime_ns:
            return ""
        return Path(str(entry.get("original_name") or "")).name

    def _is_blocked_copy(self, path: Path) -> bool:
        entries = self._state.get("inbox_blocked") or {}
        entry = entries.get(path.name) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return False
        try:
            size, mtime_ns = self._file_signature(path)
        except OSError:
            return False
        return int(entry.get("size") or -1) == size and int(entry.get("mtime_ns") or -1) == mtime_ns

    def _original_name_for(self, path: Path, existing_by_path: dict[str, dict[str, Any]] | None = None) -> str:
        resolved = str(path.resolve())
        previous = (existing_by_path or {}).get(resolved) or {}
        original = Path(str(previous.get("original_name") or "")).name
        return original or self._staged_original(path) or path.name

    def _prune_inbox_originals(self) -> None:
        entries = self._state.get("inbox_originals") or {}
        if not isinstance(entries, dict):
            self._state["inbox_originals"] = {}
            return
        current = {path.name: path for path in self._scan_inbox()}
        kept: dict[str, Any] = {}
        for name, entry in entries.items():
            path = current.get(str(name))
            if path is not None and isinstance(entry, dict) and self._staged_original(path):
                kept[str(name)] = entry
        self._state["inbox_originals"] = kept
        blocked = self._state.get("inbox_blocked") or {}
        kept_blocked: dict[str, Any] = {}
        if isinstance(blocked, dict):
            for name, entry in blocked.items():
                path = current.get(str(name))
                if path is not None and isinstance(entry, dict) and self._is_blocked_copy(path):
                    kept_blocked[str(name)] = entry
        self._state["inbox_blocked"] = kept_blocked

    def _number_stable_inbox(self, *, now: float | None = None) -> dict[str, Any]:
        """Rename completed inbox copies immediately while leaving live copies alone."""
        with self._lock:
            moment = time.monotonic() if now is None else float(now)
            files = self._scan_inbox()
            current_keys = {str(path.resolve()) for path in files}
            self._watch_observations = {
                key: value for key, value in self._watch_observations.items() if key in current_keys
            }
            active = self._state.get("active") if isinstance(self._state.get("active"), dict) else None
            active_sources = list(active.get("sources") or []) if active else []
            active_by_path = {
                str(Path(str(item.get("path") or "")).resolve()): item for item in active_sources
            }
            active_paths = set(active_by_path)

            candidates: list[Path] = []
            stable: list[Path] = []
            for path in files:
                key = str(path.resolve())
                if key in active_paths or self._staged_original(path) or self._is_blocked_copy(path):
                    continue
                candidates.append(path)
                try:
                    signature = self._file_signature(path)
                except OSError:
                    continue
                previous = self._watch_observations.get(key)
                if previous is None or previous[0] != signature:
                    self._watch_observations[key] = (signature, moment)
                elif moment - previous[1] >= INBOX_STABLE_SECONDS:
                    stable.append(path)

            if not candidates:
                self._prune_inbox_originals()
                return {
                    "renamed": False,
                    "copying": False,
                    "files": [path.name for path in files],
                }
            if len(stable) != len(candidates):
                return {
                    "renamed": False,
                    "copying": True,
                    "files": [path.name for path in files],
                    "pending": [path.name for path in candidates if path not in stable],
                }

            active_is_live = bool(
                active_sources
                and active
                and active.get("status") not in {"accepted", "archived"}
                and active_paths.issubset(current_keys)
            )
            if active_is_live:
                # Never rename files already referenced by a prepared/running render.
                # New arrivals still get clear numbers, continuing after the active set.
                scope = [path for path in files if str(path.resolve()) not in active_paths]
                start_index = max((int(item.get("index") or 0) for item in active_sources), default=0) + 1
            else:
                scope = files
                start_index = 1
                active_by_path = {}
            scope = sorted(
                scope,
                key=lambda path: self._natural_key(Path(self._original_name_for(path, active_by_path))),
            )
            normalized = self._normalize_files(scope, active_sources if active_is_live else [], start_index=start_index)
            self._watch_observations.clear()
            self._prune_inbox_originals()
            self._save_state()
            all_files = self._scan_inbox()
            return {
                "renamed": True,
                "copying": False,
                "files": [path.name for path in all_files],
                "renamed_files": [path.name for path in normalized],
            }

    def _scan_inbox(self) -> list[Path]:
        try:
            files = [
                path for path in self.inbox.iterdir()
                if path.is_file()
                and path.suffix.casefold() in VIDEO_EXTENSIONS
                and not path.name.casefold().startswith(("result", ".eirven-"))
            ]
        except OSError:
            return []
        return sorted(files, key=self._natural_key)

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._state.get("active")
            return {
                "inbox": str(self.inbox),
                "archive": str(self.archive_root),
                "results": str(self.results_root),
                "files": [path.name for path in self._scan_inbox()],
                "ffmpeg_ready": bool(self._ffmpeg_executable()),
                "auto_numbering": self._watcher_enabled,
                "auto_numbering_error": self._last_watch_error,
                "active": dict(active) if isinstance(active, dict) else None,
            }

    def open_inbox(self) -> dict[str, Any]:
        self.inbox.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(self.inbox))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":  # pragma: no cover - Windows product
                subprocess.Popen(["open", str(self.inbox)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):  # pragma: no cover
                subprocess.Popen(["xdg-open", str(self.inbox)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                return {"opened": False, "path": str(self.inbox), "reason": "no_desktop_session"}
            return {"opened": True, "path": str(self.inbox)}
        except Exception as exc:
            return {"opened": False, "path": str(self.inbox), "error": str(exc)}

    @staticmethod
    def _clean_query(query: str) -> str:
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        return re.sub(r"^(?:эрви|эйрвен|еирвен|eirven)[,;:!\-\s]*", "", clean, count=1, flags=re.I).strip()

    def _remember_context(self, conversation_id: str) -> None:
        if conversation_id:
            self._conversation_context[conversation_id] = time.monotonic()

    def _has_recent_context(self, conversation_id: str) -> bool:
        seen = self._conversation_context.get(conversation_id, 0.0)
        return bool(seen and time.monotonic() - seen <= 20 * 60)

    def is_relevant(self, query: str, conversation_id: str = "") -> bool:
        clean = self._clean_query(query)
        with self._lock:
            pending = self._state.get("pending") or {}
            active = self._state.get("active") or {}
            if conversation_id and conversation_id in pending:
                return True
            if self._is_acceptance(clean, active if isinstance(active, dict) else None, conversation_id):
                return True
            if (
                conversation_id
                and active.get("conversation_id") == conversation_id
                and active.get("status") == "rendered"
                and re.fullmatch(r"(?:да[,. ]*)?(?:принима\w*(?:\s+(?:монтаж|видео|ролик))?|подходит|оставляем|все\s+(?:хорошо|устраивает)|готово|нормально)[.! ]*", clean)
            ):
                return True
            if (
                conversation_id
                and active.get("conversation_id") == conversation_id
                and active.get("status") == "rendered"
                and re.search(r"\b(?:громче|тише|светлее|темнее|насыщеннее|быстрее|медленнее|"
                              r"еще\s+раз|ещё\s+раз|передел\w*|исправ\w*|добав\w*|уб\w*|"
                              r"остав\w*|поверн\w*|стабилиз\w*|сожм\w*)\b", clean, re.I)
            ):
                return True
        if self._PLAYBACK_RE.search(clean) and not self._EDIT_RE.search(clean):
            return False
        recent_fragment = bool(
            self._has_recent_context(conversation_id)
            and re.fullmatch(
                r"(?:они|видео|ролики|файлы)\s+(?:уже\s+)?(?:наход\w*\s+)?(?:там|тут|здесь|в\s+папке)|"
                r"(?:у|для|из)\s+(?:всех|кажд\w*)\s+(?:видео|ролик)\w*",
                clean.strip(" .,!-э"),
                re.I,
            )
        )
        return bool(
            self._EDIT_RE.search(clean)
            or self._HELP_RE.search(clean)
            or self._READY_RE.search(clean)
            or self._FRAGMENT_RE.search(clean)
            or recent_fragment
        )

    def handle_query(self, query: str, conversation_id: str) -> dict[str, Any]:
        if not self.is_relevant(query, conversation_id):
            return {"handled": False}
        opened = self.open_inbox()
        clean = self._clean_query(query)
        self._remember_context(conversation_id)
        with self._lock:
            active = self._state.get("active") if isinstance(self._state.get("active"), dict) else None

            if self._is_acceptance(clean, active, conversation_id):
                answer, details = self._accept_active()
                return self._result("video_accept", answer, opened, **details)

            if re.search(r"\b(?:как\s+там|статус|что\s+с|готов\w*\s+ли|монтаж\w*\s+готов|обработк\w*\s+готов)\b", clean, re.I):
                if not active:
                    return self._result("video_status", "Активного видеопроекта сейчас нет. Папку video открыла — положи исходники и скажи, что сделать.", opened)
                status = str(active.get("status") or "")
                if status in {"queued", "rendering"}:
                    return self._result("video_status", "Монтаж ещё выполняется. Точный этап и прогресс видны в разделе «Задачи».", opened, task_id=active.get("task_id"))
                if status == "rendered":
                    names = ", ".join(str(item.get("name") or "") for item in active.get("outputs") or [])
                    return self._result("video_status", f"Монтаж готов: {names}. Посмотри результат в папке video; если всё устраивает, скажи «Эрви, принимаю монтаж».", opened)
                if status in {"failed", "interrupted"}:
                    return self._result("video_status", f"Обработка остановилась: {active.get('error') or 'причина не записана'}. Исходники сохранены; команду можно уточнить и повторить.", opened)
                return self._result("video_status", "Исходники подготовлены, но рендер ещё не запущен. Повтори команду монтажа.", opened)

            if self._READY_RE.search(clean) or (
                self._has_recent_context(conversation_id)
                and re.fullmatch(
                    r"(?:они|видео|ролики|файлы)\s+(?:уже\s+)?(?:наход\w*\s+)?(?:там|тут|здесь|в\s+папке)[.! ]*",
                    clean,
                    re.I,
                )
            ):
                numbered = self._number_stable_inbox()
                names = [str(name) for name in numbered.get("files") or []]
                if not names:
                    return self._result(
                        "video_needs_files",
                        "Папку video открыла, но видеофайлов в ней пока не вижу. Положи их прямо сюда и дождись окончания копирования — я сама сразу пронумерую файлы.",
                        opened,
                    )
                if numbered.get("copying"):
                    return self._result(
                        "video_copying",
                        f"Вижу видеофайлов: {len(names)}. Копирование ещё завершается; как только размеры файлов перестанут меняться, я сама переименую их в 1, 2, 3 и так далее. Ничего дополнительно говорить не нужно.",
                        opened,
                        files=names,
                    )
                active_now = self._state.get("active") if isinstance(self._state.get("active"), dict) else None
                suffix = ""
                if active_now and active_now.get("sources") and active_now.get("status") not in {"accepted", "archived"}:
                    suffix = " Новые файлы не подмешиваю в активный проект без разрешения."
                return self._result(
                    "video_files_ready",
                    f"Нашла видеофайлов: {len(names)}. Они уже пронумерованы: {', '.join(names)}.{suffix} Теперь скажи обычными словами, что сделать.",
                    opened,
                    files=names,
                )

            if re.search(r"\b(?:новый|новую|следующий|другой)\b.{0,35}\b(?:видеопроект|видео\s*проект|монтаж|ролик)\w*", clean):
                if active and active.get("status") not in {"accepted", "archived"}:
                    self._state.setdefault("pending", {})[conversation_id] = {
                        "issue": "new_project_confirm",
                        "original": query,
                        "created_at": time.time(),
                    }
                    self._save_state()
                    return self._result(
                        "video_clarification",
                        "Текущий видеопроект ещё не принят. Архивировать его без удаления файлов и начать новый? Ответь «да, новый проект» или «нет, продолжаем текущий».",
                        opened,
                    )
                self._state["active"] = None
                self._state.get("pending", {}).pop(conversation_id, None)
                self._save_state()
                return self._result(
                    "video_new_project",
                    "Новый видеопроект готов. Папку video открыла — положи сюда только новые исходники, затем скажи, что с ними сделать.",
                    opened,
                )

            pending = (self._state.get("pending") or {}).get(conversation_id)
            overrides: dict[str, Any] = {}
            request_text = query
            if isinstance(pending, dict):
                resolved = self._resolve_pending(pending, query)
                if not resolved.get("ok"):
                    return self._result("video_clarification", str(resolved.get("answer") or "Уточни, пожалуйста."), opened)
                if resolved.get("cancel"):
                    self._state.get("pending", {}).pop(conversation_id, None)
                    self._save_state()
                    return self._result("video_clarification_cancelled", "Хорошо, продолжаем текущий видеопроект.", opened)
                request_text = str(pending.get("original") or query)
                overrides = dict(pending.get("overrides") or {})
                overrides.update(dict(resolved.get("overrides") or {}))
                self._state.get("pending", {}).pop(conversation_id, None)
                if resolved.get("new_project"):
                    self._archive_active(accepted=False)
                    active = None

            active = self._state.get("active") if isinstance(self._state.get("active"), dict) else None
            if active and active.get("status") in {"queued", "rendering"}:
                return self._result(
                    "video_busy",
                    "Этот монтаж уже выполняется. Дождись результата или скажи «останови задачу», прежде чем менять команду.",
                    opened,
                    task_id=active.get("task_id"),
                )

            if self._HELP_RE.search(clean) and (
                not self._looks_like_edit_command(clean)
                or re.match(r"^(?:как|умеешь|можешь\s+ли|что\s+нужно|куда|где)\b", clean, re.I)
            ):
                return self._result("video_help", self.HELP_TEXT, opened)

            if (
                not isinstance(pending, dict)
                and self._FRAGMENT_RE.search(clean)
                and not self._looks_like_edit_command(clean)
            ):
                self._state.setdefault("pending", {})[conversation_id] = {
                    "issue": "request",
                    "original": query,
                    "created_at": time.time(),
                }
                self._save_state()
                return self._result(
                    "video_clarification",
                    "Поняла: применить действие ко всем указанным видео. Теперь скажи, что именно сделать — например: «убери первые три секунды и склей по порядку».",
                    opened,
                )

            prepared = self._prepare_sources(conversation_id, allow_extra=bool(overrides.get("add_files")))
            if not prepared.get("ok"):
                issue = str(prepared.get("issue") or "")
                if issue == "new_files":
                    self._state.setdefault("pending", {})[conversation_id] = {
                        "issue": "new_files",
                        "original": request_text,
                        "created_at": time.time(),
                    }
                    self._save_state()
                return self._result(
                    "video_needs_files" if issue == "no_files" else "video_clarification",
                    str(prepared.get("answer") or "Не удалось подготовить видео."),
                    opened,
                )

            sources = list(prepared.get("sources") or [])
            plan_result = self._build_plan(request_text, sources, overrides)
            if not plan_result.get("ok"):
                issue = str(plan_result.get("issue") or "request")
                self._state.setdefault("pending", {})[conversation_id] = {
                    "issue": issue,
                    "original": request_text,
                    "created_at": time.time(),
                    "details": plan_result.get("details") or {},
                    "overrides": overrides,
                }
                self._save_state()
                names = ", ".join(str(item.get("name") or "") for item in sources)
                prefix = f"Файлы проверила и упорядочила: {names}. " if names else ""
                return self._result(
                    "video_clarification",
                    prefix + str(plan_result.get("answer") or "Уточни задачу, пожалуйста."),
                    opened,
                )

            plan = dict(plan_result["plan"])
            active = self._state.get("active") or {}
            if active.get("status") == "rendered" and isinstance(active.get("plan"), dict):
                plan = self._merge_revision_plan(dict(active["plan"]), plan, request_text)
            active.update({
                "conversation_id": conversation_id,
                "request": request_text,
                "plan": plan,
                "status": "prepared",
                "error": "",
                "updated_at": time.time(),
            })
            self._state["active"] = active
            self._save_state()
            names = ", ".join(str(item.get("name") or "") for item in sources)
            return self._result(
                "video_enqueue",
                f"Проверила файлы и закрепила понятный порядок: {names}. Начинаю обработку; прогресс будет в разделе «Задачи».",
                opened,
                enqueue={"project_id": active["id"], "plan": plan},
                project_id=active["id"],
                files=names,
            )

    @staticmethod
    def _result(action: str, answer: str, opened: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "handled": True,
            "answer": answer,
            "route": {"action": action, "model": "video-ffmpeg", "control_plane": True, "folder": opened, **extra},
            **({"enqueue": extra["enqueue"]} if "enqueue" in extra else {}),
        }

    @staticmethod
    def _looks_like_edit_command(clean: str) -> bool:
        return bool(re.search(
            r"\b(?:сделай|смонтируй|склей|соедини|объедини|обре[зж]\w*|отре[зж]\w*|уб\w*|остав\w*|"
            r"увеличь|улучши|ускорь|замедли|поверни|добавь|наложи|конвертируй|сожми|"
            r"стабилизируй|работай)\w*\b",
            clean, re.I,
        ))

    @staticmethod
    def _is_acceptance(clean: str, active: dict[str, Any] | None, conversation_id: str) -> bool:
        if not active or active.get("status") != "rendered" or active.get("conversation_id") != conversation_id:
            return False
        return bool(re.fullmatch(
            r"(?:ну\s+)?(?:да[,. ]*)?(?:(?:монтаж|видео|ролик|результат)\s+)?(?:"
            r"(?:принимаю|принимай|принимаем|прими)(?:\s+(?:монтаж|видео|ролик|результат))?|"
            r"подходит|оставляем|оставляй|сохраняй|готово|нормально|"
            r"(?:мне\s+)?все\s+(?:нравится|хорошо|устраивает|отлично)|"
            r"(?:результат|монтаж|видео|ролик)\s+(?:нравится|устраивает|подходит))"
            r"[.! ]*",
            clean,
        ))

    def _resolve_pending(self, pending: dict[str, Any], answer: str) -> dict[str, Any]:
        clean = self._clean_query(answer)
        issue = str(pending.get("issue") or "")
        if issue == "new_project_confirm":
            if re.search(r"\b(?:да|архивируй|начинай|новый)\b", clean):
                return {"ok": True, "new_project": True}
            if re.search(r"\b(?:нет|не надо|продолжаем|текущ)\b", clean):
                return {"ok": True, "cancel": True}
            return {"ok": False, "answer": "Архивировать текущий проект и начать новый — да или нет?"}
        if issue == "new_files":
            if re.search(r"\b(?:добавь|добавить|в текущий|вместе)\b", clean):
                return {"ok": True, "overrides": {"add_files": True}}
            if re.search(r"\b(?:новый|отдельн|не смешивай|только новые)\b", clean):
                return {"ok": True, "new_project": True}
            return {"ok": False, "answer": "Добавить новые файлы к текущему проекту или архивировать текущий и начать отдельный?"}
        if issue == "trim_meaning":
            if re.search(r"\b(?:убрать|удалить|отрезать|начать с)\b", clean):
                return {"ok": True, "overrides": {"trim_mode": "remove_start"}}
            if re.search(r"\b(?:оставить|сохранить|только первые)\b", clean):
                return {"ok": True, "overrides": {"trim_mode": "keep_first"}}
            return {"ok": False, "answer": "Первые секунды убрать или, наоборот, оставить только их?"}
        if issue == "trim_scope":
            if re.search(r"\b(?:после|готовой|у склейки|результат)\b", clean):
                return {"ok": True, "overrides": {"trim_scope": "after_join"}}
            indices = self._indices_from_text(clean + " видео", 999)
            if indices:
                return {"ok": True, "overrides": {"trim_scope": "selected", "selected_indices": indices}}
            if re.search(r"\b(?:все\w*|кажд\w*|у\s+всех)\b", clean):
                return {"ok": True, "overrides": {"trim_scope": "each"}}
            return {"ok": False, "answer": "Обрезать каждое видео, конкретный номер или уже готовую склейку?"}
        if issue == "quality_target":
            resolution = self._resolution_from_text(clean)
            if resolution:
                return {"ok": True, "overrides": {"resolution": resolution, "enhance": True}}
            if re.search(r"\b(?:перв\w*(?:\s+вариант)?|вариант\s*(?:1|один))\b", clean):
                return {"ok": True, "overrides": {"resolution": [1920, 1080], "enhance": True}}
            if re.search(r"\b(?:втор\w*(?:\s+вариант)?|вариант\s*(?:2|два))\b", clean):
                return {"ok": True, "overrides": {"resolution": [3840, 2160], "enhance": True}}
            if re.search(r"\b(?:трет\w*(?:\s+вариант)?|вариант\s*(?:3|три))\b", clean):
                return {"ok": True, "overrides": {"enhance": True, "resolution": None}}
            if re.search(
                r"\b(?:без\s+(?:увеличения|изменени\w*(?:\s+размера)?)|исходн\w*|"
                r"только\s+очист\w*|резкост\w*|шум\w*)\b",
                clean,
            ):
                return {"ok": True, "overrides": {"enhance": True, "resolution": None}}
            return {
                "ok": False,
                "answer": (
                    "Выбери один вариант: «первый — 1080p», «второй — 4K» или "
                    "«третий — очистить шум и повысить резкость без изменения размера»."
                ),
            }
        if issue == "aspect_method":
            if re.search(r"\b(?:обреж|кадрир|без полей)\b", clean):
                return {"ok": True, "overrides": {"aspect_method": "crop", "aspect": "9:16"}}
            if re.search(r"\b(?:пол|вместить|не обрез)\b", clean):
                return {"ok": True, "overrides": {"aspect_method": "pad", "aspect": "9:16"}}
            return {"ok": False, "answer": "Для вертикального кадра обрезать края или сохранить весь кадр с полями?"}
        if issue == "request":
            return {"ok": True, "overrides": {"clarification": answer}}
        return {"ok": True}

    def _prepare_sources(self, conversation_id: str, *, allow_extra: bool = False) -> dict[str, Any]:
        if self._watcher_enabled:
            numbered = self._number_stable_inbox()
            if numbered.get("copying"):
                return {
                    "ok": False,
                    "issue": "copying_files",
                    "answer": (
                        "Копирование видео ещё не закончилось. Дождись, пока файлы полностью перенесутся: "
                        "после этого я сама сразу переименую их и команда будет готова к запуску."
                    ),
                }
        files = self._scan_inbox()
        active = self._state.get("active") if isinstance(self._state.get("active"), dict) else None
        active_sources = list(active.get("sources") or []) if active else []
        active_paths = {str(Path(item.get("path") or "").resolve()) for item in active_sources}
        current_paths = {str(path.resolve()) for path in files}
        existing_active = active_sources and active_paths.issubset(current_paths)

        if existing_active:
            extras = [path for path in files if str(path.resolve()) not in active_paths]
            if extras and not allow_extra:
                return {
                    "ok": False,
                    "issue": "new_files",
                    "answer": (
                        "В папке video появились новые файлы, но я не смешиваю их со старым проектом без разрешения. "
                        "Добавить их к текущему монтажу или начать отдельный проект?"
                    ),
                }
            if not extras:
                return {"ok": True, "sources": active_sources}
            files = [Path(item["path"]) for item in active_sources] + sorted(extras, key=self._natural_key)
        elif active and active.get("status") not in {"accepted", "archived"} and active_sources:
            # Missing active files are never silently replaced by a different source set.
            return {
                "ok": False,
                "issue": "new_files",
                "answer": (
                    "Состав папки video изменился: часть исходников текущего проекта отсутствует. "
                    "Начать отдельный проект с тем, что сейчас в папке, или вернуть старые файлы?"
                ),
            }

        if not files:
            return {
                "ok": False,
                "issue": "no_files",
                "answer": (
                    "Папку video открыла. Положи прямо сюда все нужные видеофайлы, дождись окончания копирования "
                    "и повтори команду. Форматы могут быть разными."
                ),
            }

        normalized = self._normalize_files(files, active_sources if existing_active else [])
        sources: list[dict[str, Any]] = []
        failures: list[str] = []
        old_by_path = {str(Path(item.get("path") or "").resolve()): item for item in active_sources}
        for index, path in enumerate(normalized, 1):
            previous = old_by_path.get(str(path.resolve()), {})
            try:
                metadata = self._probe(path, verify_frame=True)
                sources.append({
                    "index": index,
                    "name": path.name,
                    "path": str(path.resolve()),
                    "original_name": previous.get("original_name") or self._last_original_names.get(str(path.resolve()), path.name),
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                    **metadata,
                })
            except Exception as exc:
                original = self._last_original_names.get(str(path.resolve()), path.name)
                failures.append(f"{original}: {exc}")
        if failures:
            restore_files = (
                [path for path in normalized if str(path.resolve()) not in active_paths]
                if existing_active else normalized
            )
            self._restore_original_names(restore_files)
            return {
                "ok": False,
                "issue": "invalid_files",
                "answer": "Не могу безопасно начать: " + "; ".join(failures) + ". Замени повреждённые файлы и повтори команду.",
            }

        project_id = str(active.get("id") or "") if existing_active and active else ""
        if not project_id:
            project_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self._state["active"] = {
            **(active or {}),
            "id": project_id,
            "conversation_id": conversation_id,
            "created_at": (active or {}).get("created_at") or time.time(),
            "updated_at": time.time(),
            "status": "prepared",
            "sources": sources,
            "outputs": list((active or {}).get("outputs") or []),
        }
        self._save_state()
        return {"ok": True, "sources": sources}

    def _normalize_files(
        self,
        files: list[Path],
        existing: list[dict[str, Any]],
        *,
        start_index: int = 1,
    ) -> list[Path]:
        existing_by_path = {
            str(Path(str(item.get("path") or "")).resolve()): item
            for item in existing
        }
        staged: list[dict[str, Any]] = []
        try:
            for path in files:
                if not path.is_file():
                    continue
                original_name = self._original_name_for(path, existing_by_path)
                temp = self.inbox / f".eirven-{uuid.uuid4().hex}{path.suffix.casefold()}"
                item = {"old": path, "temp": temp, "current": path, "original": original_name}
                staged.append(item)
                path.replace(temp)
                item["current"] = temp

            normalized: list[Path] = []
            for offset, item in enumerate(staged):
                temp = Path(item["temp"])
                target = self.inbox / f"{start_index + offset}{temp.suffix.casefold()}"
                if target.exists():
                    raise VideoError(f"не удалось освободить имя {target.name}")
                temp.replace(target)
                item["current"] = target
                item["target"] = target
                normalized.append(target)
        except Exception:
            # Every source first receives another unique temporary name, then returns to
            # its exact pre-operation path. This also rolls back a partially completed
            # numbering pass without overwriting another source.
            rollback: list[tuple[Path, Path]] = []
            for item in staged:
                current = Path(item.get("current") or item["old"])
                if not current.exists():
                    continue
                temporary = self.inbox / f".eirven-rollback-{uuid.uuid4().hex}{current.suffix.casefold()}"
                try:
                    current.replace(temporary)
                    rollback.append((temporary, Path(item["old"])))
                except OSError:
                    pass
            for temporary, old in rollback:
                try:
                    temporary.replace(old)
                except OSError:
                    recovered = self.inbox / f"recovered-{uuid.uuid4().hex[:6]}-{old.name}"
                    try:
                        temporary.replace(recovered)
                    except OSError:
                        pass
            raise

        entries = self._state.get("inbox_originals")
        if not isinstance(entries, dict):
            entries = {}
        blocked = self._state.get("inbox_blocked")
        if not isinstance(blocked, dict):
            blocked = {}
        touched_names = {
            Path(item["old"]).name for item in staged
        } | {
            Path(item["target"]).name for item in staged if item.get("target")
        }
        entries = {name: value for name, value in entries.items() if name not in touched_names}
        blocked = {name: value for name, value in blocked.items() if name not in touched_names}
        self._last_original_names = {}
        for item in staged:
            target = Path(item["target"])
            original_name = Path(str(item["original"] or target.name)).name
            size, mtime_ns = self._file_signature(target)
            entries[target.name] = {
                "original_name": original_name,
                "size": size,
                "mtime_ns": mtime_ns,
                "numbered_at": time.time(),
            }
            self._last_original_names[str(target.resolve())] = original_name
        self._state["inbox_originals"] = entries
        self._state["inbox_blocked"] = blocked
        self._save_state()
        return normalized

    def _restore_original_names(self, files: list[Path]) -> None:
        staged: list[tuple[Path, str]] = []
        old_names: set[str] = set()
        for path in files:
            if not path.is_file():
                continue
            original = self._last_original_names.get(str(path.resolve())) or self._staged_original(path) or path.name
            temp = self.inbox / f".eirven-restore-{uuid.uuid4().hex}{path.suffix.casefold()}"
            try:
                old_names.add(path.name)
                path.replace(temp)
                staged.append((temp, original))
            except OSError:
                continue
        if not staged:
            return
        existing_blocked = self._state.get("inbox_blocked") or {}
        blocked: dict[str, Any] = dict(existing_blocked) if isinstance(existing_blocked, dict) else {}
        restored_names: set[str] = set()
        for temp, original in staged:
            target = self.inbox / Path(original).name
            if target.exists():
                target = self.inbox / f"recovered-{uuid.uuid4().hex[:6]}-{Path(original).name}"
            try:
                temp.replace(target)
                size, mtime_ns = self._file_signature(target)
                blocked[target.name] = {
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "reason": "invalid_video",
                }
                restored_names.add(target.name)
            except OSError:
                pass
        entries = self._state.get("inbox_originals") or {}
        if isinstance(entries, dict):
            self._state["inbox_originals"] = {
                name: value for name, value in entries.items() if name not in old_names | restored_names
            }
        self._state["inbox_blocked"] = blocked
        self._last_original_names = {}
        self._save_state()

    # ---------------------------------------------------------------- interpretation
    @staticmethod
    def _indices_from_text(text: str, count: int) -> list[int]:
        values: set[int] = set()
        words = {
            "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5,
            "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
        }
        for stem, value in words.items():
            if re.search(rf"\b{stem}\w*\s+(?:видео|ролик|файл)", text, re.I):
                values.add(value)
        for match in re.finditer(r"\b(\d{1,3})(?:-?(?:е|й|ое|го|му))?\s*(?:видео|ролик|файл)\w*\b", text, re.I):
            value = int(match.group(1))
            nearby = text[max(0, match.start()-20):min(len(text), match.end()+20)]
            if re.search(r"сек|мин|p\b|k\b|fps|кадр", nearby, re.I):
                continue
            if 1 <= value <= count:
                values.add(value)
        for match in re.finditer(r"\b(?:видео|ролик|файл)\w*\s*(?:номер\s*)?(\d{1,3})\b", text, re.I):
            value = int(match.group(1))
            if 1 <= value <= count:
                values.add(value)
        for match in re.finditer(r"\b(\d{1,3})\.(?:mp4|mkv|mov|avi|webm|m4v|wmv|mpg|mpeg|mts|m2ts|3gp|flv|ts|vob|ogv|asf|rmvb?|divx|f4v|hevc|h264|av1)\b", text, re.I):
            value = int(match.group(1))
            if 1 <= value <= count:
                values.add(value)
        return sorted(values)

    @staticmethod
    def _seconds(value: str, unit: str = "") -> float:
        normalized = value.casefold().replace("ё", "е").strip()
        words = {
            "полтора": 1.5, "полторы": 1.5,
            "один": 1, "одна": 1, "одно": 1, "одну": 1,
            "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
            "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
            "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
            "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
            "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
            "двадцать": 20, "тридцать": 30, "сорок": 40,
            "пятьдесят": 50, "шестьдесят": 60,
        }
        number = float(words.get(normalized, normalized.replace(",", ".")))
        return number * 60.0 if unit.casefold().startswith("мин") else number

    @staticmethod
    def _timecode(value: str) -> float:
        parts = [float(part.replace(",", ".")) for part in value.strip().split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0]

    @staticmethod
    def _resolution_from_text(text: str) -> list[int] | None:
        if re.search(r"\b(?:4k|4к|4\s*[кk]|2160p?|2\s*160\s*(?:p|р|пи)?)\b", text, re.I):
            return [3840, 2160]
        if re.search(r"\b(?:2k|2к|2\s*[кk]|1440p?|1\s*440\s*(?:p|р|пи)?)\b", text, re.I):
            return [2560, 1440]
        if re.search(
            r"\b(?:1080p?|1\s*080\s*(?:p|р|пи)?|1\s+0\s*8\s*0\s*(?:p|р|пи)?|"
            r"full\s*hd|фулл?\s*хд|(?:одна\s+)?тысяч[ау]?\s+восемьдесят)\b",
            text,
            re.I,
        ):
            return [1920, 1080]
        if re.search(r"\b(?:720p?|7\s*20\s*(?:p|р|пи)?)\b", text, re.I):
            return [1280, 720]
        match = re.search(r"\b(\d{3,4})\s*[xх×]\s*(\d{3,4})\b", text, re.I)
        if match:
            return [int(match.group(1)), int(match.group(2))]
        return None

    def _build_plan(self, query: str, sources: list[dict[str, Any]], overrides: dict[str, Any]) -> dict[str, Any]:
        clarification = str(overrides.get("clarification") or "")
        text = " ".join(f"{query} {clarification}".casefold().replace("ё", "е").split())
        count = len(sources)
        all_indices = list(range(1, count + 1))
        selected = list(overrides.get("selected_indices") or self._indices_from_text(text, count))
        selected = [value for value in selected if 1 <= int(value) <= count]
        join = bool(re.search(r"\b(?:скле\w*|соедин\w*|объедин\w*|одним\s+роликом|в\s+один\s+файл)\b", text, re.I))
        if join:
            explicit_order = self._join_order(text, count)
            selected_sources = selected if (len(selected) >= 2 or (selected and re.search(r"\bтолько\b", text, re.I))) else []
            render_indices = explicit_order or selected_sources or all_indices
        else:
            render_indices = selected or all_indices
        targets = selected or all_indices
        clips: dict[str, dict[str, float]] = {str(index): {} for index in render_indices}
        post: dict[str, float] = {}
        recognized = join

        range_match = re.search(r"(?:остав\w*|выреж\w*|возьми\w*)?.{0,20}\bс\s+(\d{1,2}:\d{2}(?::\d{2})?|\d+(?:[.,]\d+)?)\s+(?:до|по)\s+(\d{1,2}:\d{2}(?::\d{2})?|\d+(?:[.,]\d+)?)", text, re.I)
        trim_start: float | None = None
        trim_end: float | None = None
        trim_mode = str(overrides.get("trim_mode") or "")
        if range_match:
            trim_start = self._timecode(range_match.group(1))
            trim_end = self._timecode(range_match.group(2))
            if trim_end <= trim_start:
                return {"ok": False, "issue": "request", "answer": "Конец нужного фрагмента должен быть позже начала. Назови диапазон ещё раз."}
            recognized = True
        else:
            seconds_match = re.search(
                rf"\b(?:перв\w*|с\s+перв\w*|с\s+начала|в\s+начале|начни\s+с|начиная\s+с)\s*({self._NUMBER_TOKEN})\s*(сек\w*|мин\w*)",
                text, re.I,
            ) or re.search(
                rf"\b(?:уб\w*|удал\w*|отре[зж]\w*|обре[зж]\w*)\b.{{0,28}}?({self._NUMBER_TOKEN})\s*(сек\w*|мин\w*)\s*(?:с\s+начала)?",
                text, re.I,
            )
            if seconds_match:
                seconds = self._seconds(seconds_match.group(1), seconds_match.group(2))
                ambiguous = bool(re.search(r"\bобре[зж]\w*\b.{0,30}\bперв\w*\b", text, re.I)) and not re.search(r"\b(?:с\s+перв\w*|уб\w*|удал\w*|отре[зж]\w*|начни\s+с|остав\w*)\b", text, re.I)
                if ambiguous and not trim_mode:
                    return {
                        "ok": False,
                        "issue": "trim_meaning",
                        "answer": f"Под «обрезать первые {seconds:g} секунд» ты имеешь в виду убрать их или оставить только их?",
                    }
                if not trim_mode:
                    trim_mode = "keep_first" if re.search(r"\bостав\w*\b.{0,25}\bпервые\b", text, re.I) else "remove_start"
                if trim_mode == "keep_first":
                    trim_start, trim_end = 0.0, seconds
                else:
                    trim_start = seconds
                recognized = True

        last_match = re.search(
            rf"\b(?:уб\w*|удал\w*|отре[зж]\w*|обре[зж]\w*)\b.{{0,25}}\bпоследн\w*\s+({self._NUMBER_TOKEN})\s*(сек\w*|мин\w*)",
            text,
            re.I,
        )
        trim_last = self._seconds(last_match.group(1), last_match.group(2)) if last_match else None
        if last_match:
            recognized = True

        has_trim = trim_start is not None or trim_end is not None or trim_last is not None
        trim_scope = str(overrides.get("trim_scope") or "")
        if has_trim and join and count > 1 and not selected and not trim_scope:
            if re.search(r"\b(?:(?:у|из)\s+кажд\w*|каждое\s+(?:видео|ролик)|у\s+всех)\b", text, re.I):
                trim_scope = "each"
            elif re.search(r"\b(?:после\s+склейки|у\s+(?:готовой\s+)?склейки|готовый\s+ролик|результат)\b", text, re.I):
                trim_scope = "after_join"
            else:
                return {
                    "ok": False,
                    "issue": "trim_scope",
                    "answer": "Что именно обрезать: каждое исходное видео, только конкретный номер или уже готовую склейку?",
                }
        if has_trim:
            destination = post if trim_scope == "after_join" else None
            if destination is not None:
                if trim_start is not None: destination["start"] = trim_start
                if trim_end is not None: destination["end"] = trim_end
                if trim_last is not None: destination["remove_last"] = trim_last
            else:
                for index in (targets if trim_scope != "each" else all_indices):
                    if str(index) not in clips:
                        continue
                    if trim_start is not None: clips[str(index)]["start"] = trim_start
                    if trim_end is not None: clips[str(index)]["end"] = trim_end
                    if trim_last is not None: clips[str(index)]["remove_last"] = trim_last

        resolution = overrides.get("resolution") if "resolution" in overrides else self._resolution_from_text(text)
        quality_requested = bool(
            resolution is not None
            or re.search(
                r"\b(?:улучш\w*|увелич\w*|повыс\w*|сдела\w*)\b.{0,28}\b(?:качеств|разрешен|резкост)\w*|"
                r"\b(?:1080p?|1\s*080|720p?|4k|4к|2k|2к|1440p?|2160p?)\b",
                text,
                re.I,
            )
        )
        enhance = bool(overrides.get("enhance") or quality_requested)
        if quality_requested and resolution is None and "resolution" not in overrides:
            return {
                "ok": False,
                "issue": "quality_target",
                "answer": "Как улучшить качество: сделать 1080p, 4K или только мягко убрать шум и повысить резкость без увеличения кадра?",
            }
        recognized = recognized or quality_requested

        speed = 1.0
        speed_match = re.search(rf"\b(?:ускор\w*|быстрее)\b.{{0,20}}?(?:в\s*)?({self._NUMBER_TOKEN})\s*(?:раза?)?", text, re.I)
        slow_match = re.search(rf"\b(?:замедл\w*|медленнее)\b.{{0,20}}?(?:в\s*)?({self._NUMBER_TOKEN})\s*(?:раза?)?", text, re.I)
        if speed_match:
            speed = max(0.25, min(self._seconds(speed_match.group(1)), 4.0)); recognized = True
        elif slow_match:
            speed = 1.0 / max(1.0, min(self._seconds(slow_match.group(1)), 4.0)); recognized = True

        mute = bool(re.search(r"\b(?:без\s+звука|убер\w*\s+(?:весь\s+)?звук|отключ\w*\s+звук)\b", text, re.I))
        volume = 1.0
        volume_match = re.search(r"\b(?:громкост\w*|звук\w*)\b.{0,18}?(\d{1,3})\s*%", text, re.I)
        if volume_match:
            volume = max(0.0, min(int(volume_match.group(1)) / 100.0, 4.0)); recognized = True
        elif re.search(r"\b(?:сделай\b.{0,18}\bгромче|увелич\w*\s+громкост)\b", text, re.I):
            volume = 1.2; recognized = True
        elif re.search(r"\b(?:сделай\b.{0,18}\bтише|уменьш\w*\s+громкост)\b", text, re.I):
            volume = .8; recognized = True
        if mute: recognized = True

        rotate = 0
        rotate_match = re.search(r"\bповерн\w*\b.{0,20}?\b(90|180|270)\b", text, re.I)
        if rotate_match:
            rotate = int(rotate_match.group(1)); recognized = True

        aspect = str(overrides.get("aspect") or "")
        aspect_match = re.search(r"\b(9\s*[:xх]\s*16|16\s*[:xх]\s*9|1\s*[:xх]\s*1)\b", text, re.I)
        if aspect_match:
            aspect = re.sub(r"[xх]", ":", re.sub(r"\s+", "", aspect_match.group(1)))
            recognized = True
        vertical = bool(re.search(r"\b(?:вертикальн\w*|для\s+(?:reels|рилс|shorts|шортс|tiktok|тикток))\b", text, re.I))
        aspect_method = str(overrides.get("aspect_method") or "")
        if vertical and not aspect:
            aspect = "9:16"
        if vertical and not aspect_method and not re.search(r"\b(?:обреж|кадрир|с\s+полями|не\s+обрез)\w*", text, re.I):
            return {"ok": False, "issue": "aspect_method", "answer": "Для вертикального видео обрезать боковые края или сохранить весь кадр с полями?"}
        if aspect:
            aspect_method = aspect_method or ("crop" if re.search(r"\b(?:обреж|кадрир|без\s+полей)\w*", text, re.I) else "pad")
            recognized = True

        fade_in = self._effect_seconds(text, "появ")
        fade_out = self._effect_seconds(text, "затух")
        if fade_in or fade_out: recognized = True
        deshake = bool(re.search(r"\b(?:стабилиз\w*|убер\w*\s+тряск|дрожан)\b", text, re.I)); recognized = recognized or deshake
        compress = bool(re.search(r"\b(?:сожм\w*|уменьш\w*\s+(?:размер|вес))\b", text, re.I)); recognized = recognized or compress
        brightness = 0.0
        if re.search(r"\b(?:светлее|осветл\w*|увелич\w*\s+яркост)\b", text, re.I): brightness = 0.08; recognized = True
        if re.search(r"\b(?:темнее|затемн\w*|уменьш\w*\s+яркост)\b", text, re.I): brightness = -0.08; recognized = True
        saturation = 1.0
        if re.search(r"\b(?:насыщеннее|увелич\w*\s+насыщ)\b", text, re.I): saturation = 1.15; recognized = True
        if re.search(r"\b(?:черно-бел|чёрно-бел|обесцвет)\w*", text, re.I): saturation = 0.0; recognized = True

        overlay_text = ""
        text_match = re.search(r"\b(?:добав\w*|налож\w*)\s+текст\w*\s*[«\"'](.+?)[»\"']", query, re.I)
        if text_match:
            overlay_text = text_match.group(1).strip()[:180]; recognized = True

        output_format = "mp4"
        format_match = re.search(
            r"\b(?:в|формат\w*|расширени\w*|сохрани\w*(?:\s+в)?)\s+"
            r"(mp4|mkv|mov|webm|gif|mp3|wav|flac|m4a|aac|ogg)\b",
            text,
            re.I,
        )
        if format_match:
            output_format = format_match.group(1).casefold(); recognized = True
        elif re.search(
            r"\b(?:формат\w*|расширени\w*)\b.{0,16}\b(?:мп\s*4|эм\s*пи\s*4|по\s*4|пи\s*4)\b",
            text,
            re.I,
        ):
            output_format = "mp4"; recognized = True
        if re.search(r"\b(?:извлек\w*|достань|сохрани)\b.{0,25}\b(?:звук|аудио|дорожк)\w*", text, re.I):
            output_format = output_format if output_format in AUDIO_OUTPUTS else "mp3"; recognized = True

        if not recognized:
            model_plan = self._model_fallback_plan(query, count)
            if model_plan:
                return model_plan
            return {
                "ok": False,
                "issue": "request",
                "answer": (
                    "Я вижу исходники, но пока не понимаю желаемый результат. Скажи одним предложением: какие номера видео взять, "
                    "в каком порядке, что обрезать и нужен ли один итоговый файл."
                ),
            }

        plan = {
            "request": query,
            "render_indices": render_indices,
            "join": join,
            "clips": clips,
            "post": post,
            "format": output_format,
            "resolution": resolution,
            "enhance": enhance,
            "speed": speed,
            "mute": mute,
            "volume": volume,
            "rotate": rotate,
            "aspect": aspect,
            "aspect_method": aspect_method,
            "fade_in": fade_in,
            "fade_out": fade_out,
            "deshake": deshake,
            "compress": compress,
            "brightness": brightness,
            "saturation": saturation,
            "text": overlay_text,
        }
        return {"ok": True, "plan": plan}

    @staticmethod
    def _merge_revision_plan(previous: dict[str, Any], current: dict[str, Any], query: str) -> dict[str, Any]:
        """Keep the accepted structure when the owner asks for one more adjustment."""
        merged = json.loads(json.dumps(previous, ensure_ascii=False))
        text = query.casefold().replace("ё", "е")
        merged["request"] = query
        structural = bool(re.search(r"\b(?:скле\w*|соедин\w*|объедин\w*|отдельн\w*|не\s+скле\w*|только\s+.*(?:видео|ролик))\b", text, re.I))
        if structural:
            for key in ("join", "render_indices"):
                merged[key] = current.get(key)
        if re.search(r"\b(?:обре[зж]\w*|отре[зж]\w*|выреж\w*|уб\w*\s+(?:перв|последн)|остав\w*.{0,20}\b(?:с|до|по)\b)\b", text, re.I):
            merged["clips"] = current.get("clips", {})
            merged["post"] = current.get("post", {})
        categories = {
            "resolution": r"\b(?:качеств|разрешен|1080p|720p|4k|4к|2k|2к|1440p|2160p|\d{3,4}\s*[xх×]\s*\d{3,4})",
            "enhance": r"\b(?:качеств|резкост|шум|улучш|увелич.*разрешен)",
            "speed": r"\b(?:ускор|замедл|быстрее|медленнее)",
            "mute": r"\b(?:без\s+звука|уб.*звук|отключ.*звук|верни.*звук)",
            "volume": r"\b(?:громкост|громче|тише|звук.*%)",
            "rotate": r"\b(?:поверн|разверн).*(?:90|180|270)",
            "aspect": r"\b(?:9\s*[:xх]\s*16|16\s*[:xх]\s*9|1\s*[:xх]\s*1|вертикальн|квадрат)",
            "fade_in": r"\b(?:появ|fade\s*in)",
            "fade_out": r"\b(?:затух|fade\s*out)",
            "deshake": r"\b(?:стабилиз|тряск|дрожан)",
            "compress": r"\b(?:сожм|размер|вес\s+файл)",
            "brightness": r"\b(?:яркост|светлее|темнее|осветл|затемн)",
            "saturation": r"\b(?:насыщ|черно-бел|чёрно-бел|обесцвет)",
            "text": r"\b(?:текст|надпис)",
            "format": r"\b(?:mp4|mkv|mov|webm|gif|mp3|wav|flac|m4a|aac|ogg|формат|извлек.*(?:звук|аудио))",
        }
        for key, pattern in categories.items():
            if re.search(pattern, text, re.I):
                merged[key] = current.get(key)
        if re.search(categories["aspect"], text, re.I):
            merged["aspect_method"] = current.get("aspect_method")
        return merged

    @staticmethod
    def _join_order(text: str, count: int) -> list[int]:
        match = re.search(r"\b(?:порядок|по\s+порядку|склей|соедини|объедини)\w*[^\d]{0,18}((?:\d+\s*[,→>\-и]\s*)+\d+)", text, re.I)
        if not match:
            return []
        values = [int(value) for value in re.findall(r"\d+", match.group(1))]
        return values if values and len(values) == len(set(values)) and all(1 <= value <= count for value in values) else []

    def _effect_seconds(self, text: str, stem: str) -> float:
        match = re.search(rf"\b(?:плавн\w*\s+)?{stem}\w*\b.{{0,18}}?({self._NUMBER_TOKEN})\s*сек", text, re.I)
        if match:
            return max(0.0, min(self._seconds(match.group(1)), 10.0))
        return 1.0 if re.search(rf"\bплавн\w*\s+{stem}\w*", text, re.I) else 0.0

    def _model_fallback_plan(self, query: str, count: int) -> dict[str, Any] | None:
        if self.gateway is None:
            return None
        try:
            schema = {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "join": {"type": "boolean"},
                    "indices": {"type": "array", "items": {"type": "integer"}},
                    "format": {"type": "string"},
                },
                "required": ["question", "join", "indices", "format"],
            }
            result = self.gateway.json(
                [
                    {"role": "system", "content": (
                        "Ты планировщик видеомонтажа. Не выдумывай параметры. Если запрос нельзя безопасно свести к выбору "
                        "исходников, склейке и формату, задай один понятный уточняющий вопрос новичку. Верни только JSON."
                    )},
                    {"role": "user", "content": f"Исходников: {count}. Запрос: {query}"},
                ],
                model=getattr(self.settings, "fast_model", None), temperature=0.0,
                schema=schema, num_ctx=900, num_predict=180, timeout_seconds=6,
            )
            question = str(result.get("question") or "").strip()
            if question:
                return {"ok": False, "issue": "request", "answer": question}
            indices = [int(v) for v in result.get("indices") or [] if 1 <= int(v) <= count]
            output_format = str(result.get("format") or "mp4").casefold()
            if output_format not in VIDEO_OUTPUTS | AUDIO_OUTPUTS:
                output_format = "mp4"
            if not indices:
                indices = list(range(1, count + 1))
            return {"ok": True, "plan": {
                "request": query, "render_indices": indices, "join": bool(result.get("join")),
                "clips": {str(i): {} for i in indices}, "post": {}, "format": output_format,
                "resolution": None, "enhance": False, "speed": 1.0, "mute": False,
                "volume": 1.0, "rotate": 0, "aspect": "", "aspect_method": "",
                "fade_in": 0.0, "fade_out": 0.0, "deshake": False, "compress": False,
                "brightness": 0.0, "saturation": 1.0, "text": "",
            }}
        except Exception:
            return None

    # --------------------------------------------------------------------- ffmpeg
    def _ffmpeg_executable(self) -> str:
        configured = str(os.getenv("EIRVEN_FFMPEG", "")).strip()
        candidates = [
            configured,
            str(self.root / "tools" / "ffmpeg" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")),
            str(self.root / "ffmpeg" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")),
            shutil.which("ffmpeg") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        try:
            import imageio_ffmpeg  # type: ignore
            candidate = imageio_ffmpeg.get_ffmpeg_exe()
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        except Exception:
            pass
        return ""

    def _ffprobe_executable(self, ffmpeg: str) -> str:
        sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            return str(sibling)
        return shutil.which("ffprobe") or ""

    def _probe(self, path: Path, *, verify_frame: bool = False) -> dict[str, Any]:
        ffmpeg = self._ffmpeg_executable()
        if not ffmpeg:
            raise VideoError("FFmpeg не найден; запусти INSTALL EIRVEN AI.cmd ещё раз")
        ffprobe = self._ffprobe_executable(ffmpeg)
        metadata: dict[str, Any] = {}
        if ffprobe:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate", "-of", "json", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if result.returncode != 0:
                raise VideoError(self._friendly_ffmpeg_error(result.stderr))
            data = json.loads(result.stdout or "{}")
            streams = list(data.get("streams") or [])
            video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
            if not video:
                raise VideoError("в файле нет видеодорожки")
            duration = float((data.get("format") or {}).get("duration") or 0.0)
            metadata = {
                "duration": round(duration, 4),
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "codec": str(video.get("codec_name") or ""),
                "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
            }
        else:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            report = result.stderr or result.stdout
            video_match = re.search(r"Video:\s*([^,]+).*?(\d{2,5})x(\d{2,5})", report, re.I | re.S)
            duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", report, re.I)
            if not video_match:
                raise VideoError("в файле нет читаемой видеодорожки")
            duration = 0.0
            if duration_match:
                duration = int(duration_match.group(1)) * 3600 + int(duration_match.group(2)) * 60 + float(duration_match.group(3))
            metadata = {
                "duration": round(duration, 4), "width": int(video_match.group(2)),
                "height": int(video_match.group(3)), "codec": video_match.group(1).strip(),
                "has_audio": bool(re.search(r"Audio:\s*", report, re.I)),
            }
        if verify_frame:
            check = subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
            )
            if check.returncode != 0:
                raise VideoError("первый кадр не декодируется: " + self._friendly_ffmpeg_error(check.stderr))
        return metadata

    def mark_queued(self, project_id: str, task_id: str) -> None:
        with self._lock:
            active = self._state.get("active") or {}
            if active.get("id") == project_id:
                active.update({"status": "queued", "task_id": task_id, "updated_at": time.time()})
                self._save_state()

    def render(self, context: Any, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        if not re.fullmatch(r"[0-9A-Za-z-]{8,48}", project_id):
            raise VideoError("Некорректный идентификатор видеопроекта")
        with self._lock:
            active = self._state.get("active") if isinstance(self._state.get("active"), dict) else None
            if not active or active.get("id") != project_id:
                raise VideoError("Активный видеопроект изменился; старую задачу не запускаю")
            plan = dict(payload.get("plan") or active.get("plan") or {})
            sources = list(active.get("sources") or [])
            active.update({"status": "rendering", "updated_at": time.time(), "error": ""})
            self._save_state()

        ffmpeg = self._ffmpeg_executable()
        if not ffmpeg:
            self._mark_failed(project_id, "FFmpeg не найден")
            raise VideoError("FFmpeg не найден. Повтори установку EIRVEN, чтобы установить видеодвижок.")
        source_by_index = {int(item["index"]): item for item in sources}
        indices = [int(value) for value in plan.get("render_indices") or []]
        if not indices or any(index not in source_by_index for index in indices):
            self._mark_failed(project_id, "Исходники плана не совпадают с текущим проектом")
            raise VideoError("Не нахожу все исходники этого плана. Файлы не смешиваю; повтори команду.")
        context.set_total(len(indices) + 2)
        context.update("Проверяю исходники перед обработкой", completed_steps=0, progress=0.03)
        try:
            for index in indices:
                path = Path(source_by_index[index]["path"])
                if not path.is_file():
                    raise VideoError(f"исходник {index} исчез из папки video")
                current = path.stat()
                if current.st_size != int(source_by_index[index].get("size") or -1):
                    raise VideoError(f"исходник {index} изменился после проверки; повтори команду")
                self._probe(path, verify_frame=True)
            work = self.work_root / project_id
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(parents=True, exist_ok=True)
            revisions = self.archive_root / f"{project_id}-revisions"
            for old_output in sorted(self.inbox.glob("result*")):
                if not old_output.is_file():
                    continue
                revisions.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                shutil.move(str(old_output), str(revisions / f"{stamp}-{old_output.name}"))
            output_format = str(plan.get("format") or "mp4").casefold()
            outputs: list[Path] = []
            if bool(plan.get("join")):
                segments: list[Path] = []
                width, height = self._target_size(plan, [source_by_index[i] for i in indices])
                for number, index in enumerate(indices, 1):
                    context.check_cancelled()
                    context.update(f"Обрабатываю видео {index}", completed_steps=number - 1, progress=(number - .5) / (len(indices) + 2))
                    segment = work / f"segment-{number:03d}.mp4"
                    self._transcode(
                        ffmpeg, Path(source_by_index[index]["path"]), segment, source_by_index[index],
                        plan, dict((plan.get("clips") or {}).get(str(index)) or {}), context,
                        target_size=(width, height), force_mp4=True,
                    )
                    segments.append(segment)
                context.update("Склеиваю подготовленные фрагменты", completed_steps=len(indices), progress=(len(indices) + .2) / (len(indices) + 2))
                concat_file = work / "concat.txt"
                concat_file.write_text(
                    "".join(
                        f"file '{path.resolve().as_posix().replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n"
                        for path in segments
                    ),
                    encoding="utf-8",
                )
                joined = work / "joined.mp4"
                self._run_process(
                    [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)],
                    context,
                )
                destination = self.inbox / f"result.{output_format}"
                self._finish_joined(ffmpeg, joined, destination, plan, context)
                outputs.append(destination)
            else:
                multiple = len(indices) > 1
                for number, index in enumerate(indices, 1):
                    context.check_cancelled()
                    context.update(f"Обрабатываю видео {index}", completed_steps=number - 1, progress=(number - .5) / (len(indices) + 2))
                    name = f"result-{index}.{output_format}" if multiple else f"result.{output_format}"
                    destination = self.inbox / name
                    temporary = work / name
                    requested_size = tuple(plan["resolution"]) if plan.get("resolution") else None
                    if requested_size and str(plan.get("aspect") or "") == "9:16" and requested_size[0] > requested_size[1]:
                        requested_size = (requested_size[1], requested_size[0])
                    self._transcode(
                        ffmpeg, Path(source_by_index[index]["path"]), temporary, source_by_index[index],
                        plan, dict((plan.get("clips") or {}).get(str(index)) or {}), context,
                        target_size=requested_size,
                        force_mp4=False,
                    )
                    self._publish_output(temporary, destination)
                    outputs.append(destination)

            context.update("Проверяю готовый результат", completed_steps=len(indices) + 1, progress=.94)
            verified: list[dict[str, Any]] = []
            for output in outputs:
                if not output.is_file() or output.stat().st_size < 64:
                    raise VideoError(f"итоговый файл {output.name} не создан")
                verified.append({"path": str(output.resolve()), "name": output.name, "size": output.stat().st_size, **self._probe_output(output)})
            context.update("Видео готово", completed_steps=len(indices) + 2, progress=.99)
            with self._lock:
                active = self._state.get("active") or {}
                if active.get("id") == project_id:
                    active.update({"status": "rendered", "outputs": verified, "updated_at": time.time(), "error": ""})
                    self._save_state()
            self.open_inbox()
            names = ", ".join(item["name"] for item in verified)
            return {
                "project_id": project_id,
                "outputs": verified,
                "answer": (
                    f"Монтаж готов и проверен: {names}. Папку video открыла — посмотри результат. "
                    "Если всё устраивает, скажи «Эрви, принимаю монтаж». Тогда я очищу папку video, "
                    "сохраню исходники в video_archive, а результат — в video_results. Если нужна правка, просто опиши её."
                ),
            }
        except Exception as exc:
            self._mark_failed(project_id, str(exc))
            raise

    def _target_size(self, plan: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[int, int]:
        if plan.get("resolution"):
            width, height = [int(value) for value in plan["resolution"]]
        else:
            first = sources[0]
            width, height = int(first.get("width") or 1920), int(first.get("height") or 1080)
        if str(plan.get("aspect") or "") == "9:16" and width > height:
            width, height = height, width
        width = max(2, min(width, 4096)); height = max(2, min(height, 4096))
        return width - width % 2, height - height % 2

    def _transcode(
        self, ffmpeg: str, source: Path, output: Path, metadata: dict[str, Any],
        plan: dict[str, Any], clip: dict[str, Any], context: Any,
        *, target_size: tuple[int, int] | None, force_mp4: bool,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
        add_silence = bool(force_mp4 and not metadata.get("has_audio") and not plan.get("mute"))
        if add_silence:
            args.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
        start = max(0.0, float(clip.get("start") or 0.0))
        end = float(clip.get("end") or 0.0)
        duration = max(0.0, float(metadata.get("duration") or 0.0))
        remove_last = max(0.0, float(clip.get("remove_last") or 0.0))
        if remove_last and duration:
            end = max(start, duration - remove_last)
        if start:
            args.extend(["-ss", f"{start:.4f}"])
        if end > start:
            args.extend(["-t", f"{end - start:.4f}"])
        fmt = "mp4" if force_mp4 else str(plan.get("format") or output.suffix.lstrip(".") or "mp4").casefold()
        if fmt in AUDIO_OUTPUTS:
            args.extend(["-vn"])
            audio_filters = self._audio_filters(plan)
            if audio_filters:
                args.extend(["-af", ",".join(audio_filters)])
            if fmt == "wav": args.extend(["-c:a", "pcm_s16le"])
            elif fmt == "flac": args.extend(["-c:a", "flac"])
            elif fmt in {"m4a", "aac"}: args.extend(["-c:a", "aac", "-b:a", "256k"])
            elif fmt == "ogg": args.extend(["-c:a", "libopus", "-b:a", "192k"])
            else: args.extend(["-c:a", "libmp3lame", "-b:a", "256k"])
            args.append(str(output))
            self._run_process(args, context)
            return
        if fmt == "gif":
            filters = self._video_filters(plan, metadata, target_size)
            filters.extend(["fps=15", "scale='min(960,iw)':-2:flags=lanczos"])
            args.extend(["-an", "-vf", ",".join(filters), "-loop", "0", str(output)])
            self._run_process(args, context)
            return

        filters = self._video_filters(plan, metadata, target_size)
        if force_mp4:
            filters.append("fps=30")
        if filters:
            args.extend(["-vf", ",".join(filters)])
        args.extend(["-map", "0:v:0"])
        if add_silence:
            args.extend(["-map", "1:a:0", "-shortest"])
        else:
            args.extend(["-map", "0:a:0?"])
        if bool(plan.get("mute")):
            args.append("-an")
        else:
            audio_filters = self._audio_filters(plan)
            if audio_filters:
                args.extend(["-af", ",".join(audio_filters)])
        crf = "28" if plan.get("compress") else ("18" if plan.get("enhance") else "21")
        if fmt == "webm":
            args.extend(["-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0", "-c:a", "libopus", "-b:a", "160k"])
        else:
            args.extend(["-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
            if fmt in {"mp4", "mov"}: args.extend(["-movflags", "+faststart"])
        args.append(str(output))
        self._run_process(args, context)

    def _video_filters(self, plan: dict[str, Any], metadata: dict[str, Any], target_size: tuple[int, int] | None) -> list[str]:
        filters: list[str] = []
        aspect = str(plan.get("aspect") or "")
        if aspect and ":" in aspect:
            left, right = [max(1.0, float(value)) for value in aspect.split(":", 1)]
            ratio = left / right
            if plan.get("aspect_method") == "crop":
                filters.append(f"crop='min(iw,ih*{ratio:.8f})':'min(ih,iw/{ratio:.8f})'")
            elif target_size is None:
                height = int(metadata.get("height") or 1080)
                width = int(round(height * ratio))
                target_size = (max(2, width - width % 2), max(2, height - height % 2))
        rotate = int(plan.get("rotate") or 0)
        if rotate == 90: filters.append("transpose=1")
        elif rotate == 180: filters.extend(["hflip", "vflip"])
        elif rotate == 270: filters.append("transpose=2")
        if plan.get("deshake"): filters.append("deshake")
        if plan.get("enhance"): filters.extend(["hqdn3d=1.2:1.2:4.5:4.5", "unsharp=5:5:0.55:5:5:0.0"])
        brightness = float(plan.get("brightness") or 0.0)
        saturation = float(plan.get("saturation") if plan.get("saturation") is not None else 1.0)
        if brightness or abs(saturation - 1.0) > .001:
            filters.append(f"eq=brightness={brightness:.3f}:saturation={saturation:.3f}")
        if target_size:
            width, height = int(target_size[0]), int(target_size[1])
            filters.extend([
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
                "setsar=1",
            ])
        else:
            filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        speed = max(.25, min(float(plan.get("speed") or 1.0), 4.0))
        if abs(speed - 1.0) > .001: filters.append(f"setpts=PTS/{speed:.6f}")
        fade_in = float(plan.get("fade_in") or 0.0)
        fade_out = float(plan.get("fade_out") or 0.0)
        if fade_in: filters.append(f"fade=t=in:st=0:d={fade_in:.3f}")
        if fade_out and float(metadata.get("duration") or 0.0) > fade_out:
            adjusted = float(metadata.get("duration") or 0.0) / speed
            filters.append(f"fade=t=out:st={max(0.0, adjusted-fade_out):.3f}:d={fade_out:.3f}")
        overlay = str(plan.get("text") or "").strip()
        if overlay:
            escaped = overlay.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'").replace("%", r"\%")
            font = self._font_file()
            font_part = f"fontfile='{font}':" if font else ""
            filters.append(f"drawtext={font_part}text='{escaped}':fontcolor=white:fontsize=h/18:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-h/12")
        return filters

    @staticmethod
    def _audio_filters(plan: dict[str, Any]) -> list[str]:
        filters: list[str] = []
        speed = max(.25, min(float(plan.get("speed") or 1.0), 4.0))
        remaining = speed
        while remaining > 2.0:
            filters.append("atempo=2.0"); remaining /= 2.0
        while remaining < .5:
            filters.append("atempo=0.5"); remaining /= .5
        if abs(remaining - 1.0) > .001: filters.append(f"atempo={remaining:.6f}")
        volume = max(0.0, min(float(plan.get("volume") or 1.0), 4.0))
        if abs(volume - 1.0) > .001: filters.append(f"volume={volume:.4f}")
        fade_in = float(plan.get("fade_in") or 0.0)
        if fade_in: filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        return filters

    def _finish_joined(self, ffmpeg: str, joined: Path, destination: Path, plan: dict[str, Any], context: Any) -> None:
        fmt = str(plan.get("format") or "mp4").casefold()
        post = dict(plan.get("post") or {})
        no_post = not any(float(post.get(key) or 0.0) for key in ("start", "end", "remove_last"))
        if fmt == "mp4" and no_post:
            self._publish_output(joined, destination)
            return
        metadata = self._probe(joined)
        final_plan = dict(plan)
        # Visual effects were already applied to normalized segments; this pass only
        # performs a whole-join trim and/or container conversion.
        for key in ("enhance", "deshake", "text"):
            final_plan[key] = False if key != "text" else ""
        final_plan.update({"speed": 1.0, "rotate": 0, "brightness": 0.0, "saturation": 1.0, "fade_in": 0.0, "fade_out": 0.0, "resolution": None, "aspect": ""})
        temporary = destination.parent / f".eirven-result-{uuid.uuid4().hex}.{fmt}"
        self._transcode(ffmpeg, joined, temporary, metadata, final_plan, post, context, target_size=None, force_mp4=False)
        self._publish_output(temporary, destination)

    @staticmethod
    def _publish_output(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        shutil.move(str(source), str(destination))

    def _run_process(self, args: list[str], context: Any) -> None:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        while True:
            try:
                stdout, stderr = process.communicate(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                try:
                    context.check_cancelled()
                except Exception:
                    process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired: process.kill()
                    raise
        if process.returncode != 0:
            raise VideoError(self._friendly_ffmpeg_error(stderr or stdout))

    @staticmethod
    def _friendly_ffmpeg_error(text: str) -> str:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        message = lines[-1] if lines else "FFmpeg не смог обработать файл"
        replacements = {
            "Invalid data found when processing input": "файл повреждён или имеет неподдерживаемый контейнер",
            "No space left on device": "на диске закончилось место",
            "Permission denied": "нет доступа к файлу — возможно, он открыт другой программой",
            "Unknown encoder": "в установленной сборке FFmpeg нет нужного кодека",
        }
        for source, friendly in replacements.items():
            if source.casefold() in message.casefold() or source.casefold() in str(text).casefold():
                return friendly
        return message[:500]

    def _probe_output(self, path: Path) -> dict[str, Any]:
        if path.suffix.casefold().lstrip(".") in AUDIO_OUTPUTS:
            ffmpeg = self._ffmpeg_executable()
            if not ffmpeg:
                raise VideoError("FFmpeg недоступен для проверки аудиорезультата")
            duration = 0.0
            ffprobe = self._ffprobe_executable(ffmpeg)
            if ffprobe:
                probe = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                )
                if probe.returncode != 0:
                    raise VideoError(self._friendly_ffmpeg_error(probe.stderr))
                data = json.loads(probe.stdout or "{}")
                if not any(item.get("codec_type") == "audio" for item in data.get("streams") or []):
                    raise VideoError("в результате нет аудиодорожки")
                duration = float((data.get("format") or {}).get("duration") or 0.0)
            check = subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(path), "-t", "0.5", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
            )
            if check.returncode != 0:
                raise VideoError("аудиорезультат не декодируется: " + self._friendly_ffmpeg_error(check.stderr))
            return {"duration": round(duration, 4), "audio_only": True, "has_audio": True}
        return self._probe(path, verify_frame=True)

    @staticmethod
    def _font_file() -> str:
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate).replace("\\", "/").replace(":", r"\:")
        return ""

    def _mark_failed(self, project_id: str, error: str) -> None:
        with self._lock:
            active = self._state.get("active") or {}
            if active.get("id") == project_id:
                active.update({"status": "failed", "error": str(error)[:1200], "updated_at": time.time()})
                self._save_state()

    # -------------------------------------------------------------- accept/archive
    def _accept_active(self) -> tuple[str, dict[str, Any]]:
        active = self._state.get("active") or {}
        if active.get("status") != "rendered":
            return "Готового видеорезультата для принятия сейчас нет.", {}
        if not any(Path(str(item.get("path") or "")).is_file() for item in active.get("outputs") or []):
            return "Не нахожу готовый result.* в папке video. Исходники сохранены — повтори последнюю команду, чтобы создать результат заново.", {}
        archived = self._archive_active(accepted=True)
        outputs = archived.get("outputs") or []
        sources_path = str(archived.get("archive") or self.archive_root)
        result_names = ", ".join(Path(path).name for path in outputs)
        archive_label = f"video_archive/{Path(sources_path).name}" if sources_path else "video_archive"
        return (
            f"Приняла монтаж. Папка video очищена: исходники не удалены, а сохранены в {archive_label}. "
            f"Готовый результат {result_names} лежит в папке video_results.",
            {"outputs": outputs, "archive": sources_path},
        )

    def _archive_active(self, *, accepted: bool) -> dict[str, Any]:
        active = self._state.get("active") if isinstance(self._state.get("active"), dict) else None
        if not active:
            self._state["active"] = None
            self._save_state()
            return {"archive": "", "outputs": []}
        project_id = str(active.get("id") or datetime.now().strftime("%Y%m%d-%H%M%S"))
        destination = self.archive_root / project_id
        if destination.exists():
            destination = self.archive_root / f"{project_id}-{uuid.uuid4().hex[:4]}"
        source_destination = destination / "sources"
        output_destination = destination / "results"
        source_destination.mkdir(parents=True, exist_ok=True)
        saved_sources: list[str] = []
        for item in list(active.get("sources") or []):
            path = Path(str(item.get("path") or ""))
            if path.is_file() and path.parent.resolve() == self.inbox.resolve():
                target = source_destination / path.name
                shutil.move(str(path), str(target))
                saved_sources.append(str(target.resolve()))
        saved_outputs: list[str] = []
        for item in list(active.get("outputs") or []):
            path = Path(str(item.get("path") or ""))
            if not path.is_file() or path.parent.resolve() != self.inbox.resolve():
                continue
            target_dir = self.results_root if accepted else output_destination
            target_dir.mkdir(parents=True, exist_ok=True)
            prefix = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = target_dir / f"montazh-{prefix}-{path.name}"
            counter = 2
            while target.exists():
                target = target_dir / f"montazh-{prefix}-{counter}-{path.name}"
                counter += 1
            shutil.move(str(path), str(target))
            saved_outputs.append(str(target.resolve()))
        manifest = {**active, "archived_at": time.time(), "accepted": accepted, "archived_sources": saved_sources, "final_outputs": saved_outputs}
        (destination / "project.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        work = self.work_root / project_id
        if work.is_dir():
            shutil.rmtree(work)
        self._state["pending"] = {}
        self._state["active"] = {
            "id": project_id, "status": "accepted" if accepted else "archived",
            "archive": str(destination.resolve()), "final_outputs": saved_outputs,
            "updated_at": time.time(), "conversation_id": active.get("conversation_id"),
        } if accepted else None
        self._save_state()
        return {"archive": str(destination.resolve()), "outputs": saved_outputs}
