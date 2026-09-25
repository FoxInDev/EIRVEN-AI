# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .distribution import DistributionState
from .training_guard import owner_training_active


DEFAULT_UPDATE_API = "https://eirven.foxyhosty.ru/api/eirven/v1"


class UpdateError(RuntimeError):
    pass


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    parts = [int(x) for x in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple((parts + [0, 0, 0, 0])[:4])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class UpdateManager:
    """Server-first updater with an Express entitlement decided only server-side.

    The client contains no premium secret. The public repository only knows the HTTPS API
    contract. The server validates a bearer entitlement and returns a standard or Express
    URL. During owner training the updater will check metadata but will never download or
    apply an update, preserving GPU/disk headroom for QLoRA.
    """

    def __init__(self, root_dir: Path, db: Any, *, version: str, build: str):
        self.root_dir = Path(root_dir).resolve()
        self.db = db
        self.version = str(version)
        self.build = str(build)
        self.api_base = str(os.getenv("EIRVEN_UPDATE_API", DEFAULT_UPDATE_API) or DEFAULT_UPDATE_API).rstrip("/")
        self.distribution = DistributionState(self.root_dir)
        self.distribution.capture_environment()
        self.update_dir = self.root_dir / "data" / "updates"
        self.update_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._status: dict[str, Any] = {
            "state": "idle", "progress": 0.0, "message": "", "error": "", "latest": "",
        }
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    def _headers(self) -> dict[str, str]:
        return {
            **self.distribution.auth_headers(),
            "Accept": "application/json",
            "User-Agent": f"EIRVEN-AI/{self.version} ({self.build})",
        }

    def _get_json(self, path: str, *, params: dict[str, str] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.api_base}/{path.lstrip('/')}", params=params or {}, headers=self._headers(),
                timeout=timeout, follow_redirects=False, trust_env=False,
            )
            response.raise_for_status()
            body = response.json() if response.content else {}
            if not isinstance(body, dict):
                raise UpdateError("Сервер обновлений вернул некорректный ответ")
            return body
        except (httpx.HTTPError, ValueError) as exc:
            raise UpdateError(f"Сервер обновлений недоступен: {exc}") from exc

    def _post_json(self, path: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.api_base}/{path.lstrip('/')}", json=payload, headers=self._headers(),
                timeout=timeout, follow_redirects=False, trust_env=False,
            )
            response.raise_for_status()
            body = response.json() if response.content else {}
            if not isinstance(body, dict):
                raise UpdateError("Сервер обновлений вернул некорректный ответ")
            return body
        except (httpx.HTTPError, ValueError) as exc:
            raise UpdateError(f"Не удалось связаться с сервером обновлений: {exc}") from exc

    def claim_pending_ticket(self) -> dict[str, Any]:
        state = self.distribution.load()
        ticket = str(state.get("claim_ticket") or "")
        if not ticket:
            return {"ok": True, "claimed": False, **self.distribution.public()}
        body = self._post_json("claim", {
            "ticket": ticket,
            "install_id": state["install_id"],
            "version": self.version,
        })
        token = str(body.get("entitlement_token") or "").strip()
        if not bool(body.get("ok")) or not token:
            return {"ok": False, "claimed": False, "error": str(body.get("error") or "Express-код не подтверждён")[:240]}
        public = self.distribution.save_entitlement(token)
        return {"ok": True, "claimed": True, **{
            "source": public.get("source"), "claimed_channel": public.get("claimed_channel"),
            "has_entitlement": True,
        }}

    def check(self) -> dict[str, Any]:
        # A one-time Express ticket in a personalized installer is exchanged only once.
        try:
            if self.distribution.public().get("claim_pending"):
                self.claim_pending_ticket()
        except Exception:
            # Update discovery still works through the standard channel if claim is down.
            pass
        channel = str(self.db.get_setting("update_channel", "stable") or "stable")
        primary_error = ""
        try:
            payload = self._get_json("update", params={
                "version": self.version,
                "build": self.build,
                "channel": channel,
            })
            payload.setdefault("update_source", "eirven-server")
        except UpdateError as exc:
            primary_error = str(exc)
            return {
                "ok": False,
                "current": self.version,
                "build": self.build,
                "latest": "",
                "update_available": False,
                "delivery_channel": "offline",
                "delivery_degraded": bool(self.distribution.public().get("has_entitlement")),
                "error": primary_error[:300],
                "distribution": self.distribution.public(),
            }
        latest = str(payload.get("latest") or payload.get("version") or "").strip()
        if "update_available" not in payload:
            newer = bool(latest) and _version_tuple(latest) > _version_tuple(self.version)
            # Та же версия, но другая сборка — тоже обновление. Раньше сравнивался
            # только номер: при исправлениях без смены версии (2.4.0 → 2.4.0 с
            # новой сборкой) обновление честно отвечало «у вас актуальная» и
            # ничего не делало. Сайт — источник правды: если там другая сборка
            # той же версии, её и надо поставить.
            same_version = bool(latest) and _version_tuple(latest) == _version_tuple(self.version)
            remote_build = str(payload.get("build") or "").strip()
            rebuilt = same_version and bool(remote_build) and remote_build != str(self.build or "")
            payload["update_available"] = newer or rebuilt
        payload.update({
            "ok": bool(payload.get("ok", True)),
            "current": self.version,
            "build": self.build,
            "channel": channel,
            "latest": latest,
            "training_deferred": bool(owner_training_active()),
            "distribution": self.distribution.public(),
        })
        # Never trust a server to make the browser execute javascript/file URLs.
        url = str(payload.get("download_url") or "")
        if url and not url.lower().startswith("https://"):
            payload["download_url"] = ""
            payload["ok"] = False
            payload["error"] = "Сервер обновлений вернул небезопасный URL"
        return payload

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "distribution": self.distribution.public(), "training_deferred": bool(owner_training_active())}

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _safe_extract(self, archive: Path, target: Path) -> None:
        target = target.resolve()
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                resolved = (target / info.filename).resolve()
                if resolved != target and target not in resolved.parents:
                    raise UpdateError("Некорректный путь внутри update ZIP")
            bundle.extractall(target)

    def _download(self, meta: dict[str, Any]) -> Path:
        url = str(meta.get("download_url") or "").strip()
        if not url.startswith("https://"):
            raise UpdateError("Нет защищённой ссылки на обновление")
        latest = re.sub(r"[^0-9A-Za-z._-]", "_", str(meta.get("latest") or "update"))[:80]
        suffix = ".zip" if str(meta.get("asset_type") or "zip").casefold() == "zip" else ".exe"
        target = self.update_dir / f"EIRVEN-{latest}{suffix}.partial"
        final = self.update_dir / f"EIRVEN-{latest}{suffix}"
        expected = str(meta.get("sha256") or "").casefold().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise UpdateError("Обновление отклонено: сервер не передал корректный SHA-256")
        expected_size = int(meta.get("size") or 0)
        headers = self._headers()
        total = expected_size
        done = 0
        self._set_status(state="downloading", progress=0.0, message="Скачиваю обновление", error="", latest=latest)
        with httpx.stream("GET", url, headers=headers, timeout=httpx.Timeout(15.0, read=60.0), follow_redirects=True, trust_env=False) as response:
            response.raise_for_status()
            if not total:
                try: total = int(response.headers.get("content-length") or 0)
                except ValueError: total = 0
            with target.open("wb") as handle:
                for block in response.iter_bytes(1024 * 1024):
                    if self._stop.is_set():
                        raise UpdateError("Обновление остановлено")
                    if not block:
                        continue
                    handle.write(block); done += len(block)
                    self._set_status(progress=(done / total if total else 0.0), message=f"Скачано {done // (1024*1024)} МБ")
        if expected_size and done != expected_size:
            target.unlink(missing_ok=True)
            raise UpdateError("Размер обновления не совпал с манифестом")
        actual = _sha256(target)
        if expected and actual != expected:
            target.unlink(missing_ok=True)
            raise UpdateError("SHA-256 обновления не совпал")
        target.replace(final)
        return final

    def _stage_zip(self, archive: Path, latest: str) -> Path:
        stage = self.update_dir / f"stage-{re.sub(r'[^0-9A-Za-z._-]', '_', latest)}"
        shutil.rmtree(stage, ignore_errors=True); stage.mkdir(parents=True, exist_ok=True)
        self._safe_extract(archive, stage)
        roots = [item for item in stage.iterdir()]
        payload_root = roots[0] if len(roots) == 1 and roots[0].is_dir() else stage
        manifest = payload_root / "EIRVEN_UPDATE.json"
        if manifest.is_file():
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
                if str(info.get("version") or "") and _version_tuple(str(info["version"])) < _version_tuple(self.version):
                    raise UpdateError("Update ZIP содержит более старую версию")
            except json.JSONDecodeError as exc:
                raise UpdateError("Повреждён EIRVEN_UPDATE.json") from exc
        return payload_root

    def _write_apply_script(self, payload_root: Path, latest: str) -> Path:
        script = self.update_dir / "apply-eirven-update.ps1"
        root = str(self.root_dir).replace("'", "''")
        stage = str(payload_root).replace("'", "''")
        exe = str((self.root_dir / "EIRVEN-AI.exe")).replace("'", "''")
        content = f"""$ErrorActionPreference='Stop'\n$Root='{root}'\n$Stage='{stage}'\n$ServerPidFile=Join-Path $Root 'logs\\server.pid'\n$Deadline=(Get-Date).AddMinutes(3)\nwhile((Get-Date)-lt $Deadline){{\n  $running=$false\n  if(Test-Path $ServerPidFile){{ try{{$p=[int](Get-Content $ServerPidFile -Raw); if(Get-Process -Id $p -ErrorAction SilentlyContinue){{$running=$true}}}}catch{{}} }}\n  if(-not $running){{break}}\n  Start-Sleep -Milliseconds 350\n}}\n$RoboArgs=@($Stage,$Root,'/E','/R:2','/W:1','/NFL','/NDL','/NJH','/NJS','/NP','/XD','data','workspace','logs','/XF','.env')
& robocopy @RoboArgs | Out-Null
$RoboCode=$LASTEXITCODE
if($RoboCode -ge 8){{ throw "robocopy failed with exit code $RoboCode" }}
$marker=Join-Path $Root 'data\\updates\\last-applied.json'\n@{{version='{latest}';applied_at=(Get-Date).ToString('o')}} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8\nif(Test-Path '{exe}'){{ Start-Process -FilePath '{exe}' -WorkingDirectory $Root }}\n"""
        script.write_text(content, encoding="utf-8-sig")
        return script

    def _run_update(self, meta: dict[str, Any]) -> None:
        try:
            if owner_training_active():
                raise UpdateError("Дообучение владельца активно — обновление отложено, чтобы не мешать QLoRA")
            if not meta.get("update_available"):
                self._set_status(state="idle", progress=1.0, message="Установлена актуальная версия")
                return
            archive = self._download(meta)
            kind = str(meta.get("asset_type") or "zip").casefold()
            latest = str(meta.get("latest") or "update")
            self._set_status(state="staging", progress=1.0, message="Проверяю и подготавливаю обновление")
            if kind == "installer":
                if os.name != "nt":
                    raise UpdateError("Автоустановка EXE поддерживается только в Windows")
                # The one-file installer has a dedicated quiet update lane.  Ask the
                # supervised core to stop before it replaces source files, then let the
                # installer repair the editable package/launchers and restart EIRVEN.
                stop = self.root_dir / "logs" / "stop.request"
                stop.parent.mkdir(parents=True, exist_ok=True)
                stop.touch(exist_ok=True)
                # Без DETACHED_PROCESS: с ним CREATE_NO_WINDOW игнорируется, у процесса нет консоли вовсе,
                # и КАЖДАЯ консольная программа, которую он запускает (у Ollama — проверка видеокарт и
                # процессы моделей), открывала своё видимое окно: 20–30 окон cmd при запуске.
                # С одним CREATE_NO_WINDOW консоль скрытая, и потомки наследуют её — окон нет.
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen(
                    [str(archive), "/EIRVEN-UPDATE"],
                    cwd=str(self.root_dir),
                    creationflags=flags,
                )
            else:
                payload = self._stage_zip(archive, latest)
                if os.name == "nt":
                    script = self._write_apply_script(payload, latest)
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=str(self.root_dir), creationflags=flags)
                    stop = self.root_dir / "logs" / "stop.request"; stop.parent.mkdir(exist_ok=True); stop.touch(exist_ok=True)
                else:
                    # Development/test platforms stage only; never overwrite a live source tree.
                    self._set_status(state="ready", progress=1.0, message=f"Обновление {latest} подготовлено")
                    return
            self._set_status(state="applying", progress=1.0, message="EIRVEN перезапустится после обновления")
        except Exception as exc:
            self._set_status(state="error", error=str(exc)[:500], message="Не удалось обновить EIRVEN")

    def install(self, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if owner_training_active():
            return {"ok": False, "deferred": True, "error": "Дообучение активно — обновление не запускаю"}
        with self._lock:
            if self._worker and self._worker.is_alive():
                return {"ok": True, "started": False, "status": self.status()}
            meta = metadata or self.check()
            if not bool(meta.get("ok")):
                return {"ok": False, "error": str(meta.get("error") or "Не удалось проверить обновление")}
            self._worker = threading.Thread(target=self._run_update, args=(meta,), daemon=True, name="eirven-updater")
            self._worker.start()
        return {"ok": True, "started": True, "latest": meta.get("latest"), "delivery_channel": meta.get("delivery_channel", "standard")}

    def maybe_auto_update(self) -> None:
        """Non-intrusive startup task. A disabled toggle means check-only, never nag."""
        def worker() -> None:
            # Startup has higher-priority voice/model work. Metadata check is delayed and
            # disabled from installation while owner training is active.
            if self._stop.wait(18.0):
                return
            try:
                meta = self.check()
                self.db.set_setting("last_update_check", {
                    "checked_at": time.time(), "latest": meta.get("latest"),
                    "update_available": bool(meta.get("update_available")),
                    "delivery_channel": meta.get("delivery_channel", "standard"),
                })
                enabled = bool(self.db.get_setting("auto_update_enabled", False))
                if enabled and meta.get("update_available") and not owner_training_active():
                    self.install(metadata=meta)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True, name="eirven-update-check").start()

    def stop(self) -> None:
        self._stop.set()
